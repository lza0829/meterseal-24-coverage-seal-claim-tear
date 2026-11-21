from __future__ import annotations
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 只导入一次：db_path() 每次连接时读环境变量，测试只需切库路径，无需重载模块。
from app.main import app as fastapi_app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立 SQLite 文件，避免相互污染。"""
    db = tmp_path / "test.db"
    monkeypatch.setenv("METERS_DB_PATH", str(db))
    seed_if_empty()  # 新库在切换到本用例路径后建表 + 种子数据
    with TestClient(fastapi_app) as c:
        c.db_path = db
        yield c


PERIOD = "2026-10"
ALL_METERS = ["M-W-101", "M-W-102", "M-E-101", "M-W-201", "M-E-201", "M-E-202"]


# ---------------------------------------------------------------- helpers


def set_policy(c, architecture, threshold=0.8):
    r = c.put("/api/coverage/policy", json={"architecture": architecture, "threshold": threshold})
    assert r.status_code == 200, r.text
    return r.json()


def scan(c, period=PERIOD, persist=False):
    r = c.get("/api/coverage/scan", params={"period_id": period, "persist": persist})
    assert r.status_code == 200, r.text
    return r.json()


def precheck(c, period=PERIOD):
    r = c.post("/api/coverage/precheck", params={"period_id": period})
    return r


def seal(c, period=PERIOD):
    return c.post("/api/seal", json={"period_id": period})


def delete_actuals(c, meter_ids, period=PERIOD):
    """直接改库模拟实抄缺失（API 无删除接口）。"""
    import sqlite3

    conn = sqlite3.connect(c.db_path)
    try:
        for mid in meter_ids:
            conn.execute(
                "DELETE FROM actuals WHERE meter_id=? AND period_id=?", (mid, period)
            )
        conn.commit()
    finally:
        conn.close()


def add_actual(c, meter_id, qty=1.0, doc_no=None, period=PERIOD):
    return c.post(
        "/api/actuals",
        json={
            "meter_id": meter_id,
            "period_id": period,
            "qty": qty,
            "occurred_at": "2026-10-30T09:00:00",
            "reader_code": "T01",
            "doc_no": doc_no or f"T-{meter_id}",
        },
    )


# ---------------------------------------------------------------- scan basics


def test_scan_reports_ratio_and_missing_list(client):
    data = scan(client)
    assert data["estimate_key_count"] == 6
    assert data["covered_key_count"] == 6
    assert data["coverage_ratio"] == 1.0
    assert data["missing_meters"] == []
    assert data["verdict"] == "pass"


def test_scan_counts_meter_keys_not_documents(client):
    # 同一表加第二张实抄单，不应把覆盖率抬过 1 或分母变化。
    r = add_actual(client, "M-W-101", qty=99.0, doc_no="DUP-1")
    assert r.status_code == 200, r.text
    data = scan(client)
    assert data["estimate_key_count"] == 6
    assert data["covered_key_count"] == 6


def test_missing_endpoint_matches_scan(client):
    delete_actuals(client, ["M-E-202", "M-W-201"])
    data = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
    assert data["covered_key_count"] == 4
    assert data["estimate_key_count"] == 6
    assert data["missing_meters"] == ["M-E-202", "M-W-201"]
    assert [r["meter_id"] for r in data["rows"]] == ["M-E-202", "M-W-201"]
    assert data["rows"][0]["building"] == "B栋"


# ---------------------------------------------------------------- hard gate


def test_hard_gate_low_coverage_blocks_seal(client):
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-E-202", "M-W-201"])  # 4/6 = 0.667
    pc = precheck(client).json()
    assert pc["verdict"] == "fail"
    assert pc["coverage_ratio"] == pytest.approx(4 / 6, abs=1e-9)

    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "coverage_gate_blocked"
    assert "覆盖率不足" in detail["message"]

    periods = client.get("/api/periods").json()
    period = next(p for p in periods if p["period_id"] == PERIOD)
    assert period["status"] == "open"
    assert period["coverage_state"] is None  # 不足绝不能落封、更不能写满覆盖快照


def test_hard_gate_without_precheck_refuses_seal(client):
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "coverage_gate_blocked"
    assert "预检" in r.json()["detail"]["message"]


def test_hard_gate_pass_then_seal_and_snapshot(client):
    set_policy(client, "hard_gate", 0.8)
    pc = precheck(client).json()
    assert pc["verdict"] == "pass"
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "sealed"
    assert body["coverage"]["state"] == "pass"
    assert body["coverage"]["coverage_ratio"] == 1.0
    assert body["coverage"]["precheck_id"] == pc["precheck_id"]

    snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert snap["sealed"] is True
    assert snap["state"] == "pass"
    assert snap["coverage_ratio"] == 1.0
    assert snap["precheck_id"] == pc["precheck_id"]

    period = next(
        p for p in client.get("/api/periods").json() if p["period_id"] == PERIOD
    )
    assert period["coverage_state"] == "pass"
    assert period["coverage_precheck_id"] == pc["precheck_id"]


def test_hard_gate_boundary_exactly_at_threshold_seals(client):
    # 4/6 覆盖，阈值设 2/3 恰好达标。
    set_policy(client, "hard_gate", 4 / 6)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    pc = precheck(client).json()
    assert pc["verdict"] == "pass"
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200


def test_hard_gate_empty_estimates_is_not_low_coverage(client):
    # 无估抄键时无分母，按 100% 处理，不应假拒。
    delete_actuals(client, ALL_METERS)
    import sqlite3

    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute("DELETE FROM estimates WHERE period_id=?", (PERIOD,))
        conn.commit()
    finally:
        conn.close()
    add_actual(client, "M-W-101")
    set_policy(client, "hard_gate", 0.9)
    pc = precheck(client).json()
    assert pc["estimate_key_count"] == 0
    assert pc["coverage_ratio"] is None
    assert pc["verdict"] == "pass"
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200


# ---------------------------------------------------------------- allow skip


def test_allow_skip_seals_without_any_precheck(client):
    set_policy(client, "allow_skip", 0.99)
    delete_actuals(client, ["M-E-202", "M-W-201"])  # 低覆盖
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 200, r.text
    cov = r.json()["coverage"]
    assert cov["state"] == "skipped"
    assert cov["coverage_ratio"] is None  # 未扫就封：如实记 skipped，不伪造数值
    assert cov["architecture"] == "allow_skip"

    snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert snap["state"] == "skipped"
    assert snap["coverage_ratio"] is None


def test_allow_skip_with_scan_records_real_value(client):
    set_policy(client, "allow_skip", 0.99)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    pc = precheck(client).json()
    assert pc["verdict"] == "skipped"
    assert pc["coverage_ratio"] == pytest.approx(4 / 6, abs=1e-9)
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 200, r.text
    cov = r.json()["coverage"]
    assert cov["state"] == "skipped"
    assert cov["coverage_ratio"] == pytest.approx(4 / 6, abs=1e-9)
    assert cov["missing_meters"] == ["M-E-202", "M-W-201"]


def test_switching_architecture_changes_low_coverage_day(client):
    """同一低覆盖账期：硬闸日拒封；切到可跳过即可封。"""
    delete_actuals(client, ["M-E-202", "M-W-201"])

    set_policy(client, "hard_gate", 0.9)
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 409

    # 切到「允许低覆盖直封」：旧预检因架构不符而失效，但该架构承诺可直封，
    # 不得因过期预检假拒；快照如实退回 skipped / 未计算。
    set_policy(client, "allow_skip", 0.9)
    r = seal(client)
    assert r.status_code == 200, r.text
    cov = r.json()["coverage"]
    assert cov["architecture"] == "allow_skip"
    assert cov["state"] == "skipped"
    assert cov["coverage_ratio"] is None


def test_switch_to_allow_skip_with_fresh_precheck_records_real_ratio(client):
    delete_actuals(client, ["M-E-202", "M-W-201"])
    set_policy(client, "hard_gate", 0.9)
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 409

    # 切架构后不重扫：可直封，快照为 skipped（不得沿用旧硬闸的 fail 数值冒充达标）。
    set_policy(client, "allow_skip", 0.9)
    r = seal(client)
    assert r.status_code == 200
    assert r.json()["coverage"]["state"] == "skipped"
    assert r.json()["coverage"]["coverage_ratio"] is None


# ---------------------------------------------------------------- staleness


def test_precheck_expires_after_loading(client):
    set_policy(client, "hard_gate", 0.8)
    pc = precheck(client).json()
    old_fp = pc["data_fingerprint"]

    # 装载新实抄后，旧预检与当前数据指纹不符。
    assert add_actual(client, "M-W-101", qty=50.0, doc_no="LATE-1").status_code == 200
    data = scan(client)
    assert data["data_fingerprint"] != old_fp
    assert data["active_precheck_stale"] is True

    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["stale"] is True
    assert "过期" in detail["message"]

    # 重新预检后恢复可封。
    precheck(client)
    assert seal(client).status_code == 200


def test_precheck_expires_when_threshold_tightened(client):
    set_policy(client, "hard_gate", 0.5)
    precheck(client)
    set_policy(client, "hard_gate", 0.99)  # 收紧
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["stale"] is True


def test_threshold_loosened_keeps_precheck_valid(client):
    set_policy(client, "hard_gate", 0.9)
    precheck(client)
    set_policy(client, "hard_gate", 0.5)  # 放宽：旧 pass 仍可信
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200


def test_failed_precheck_not_promoted_by_loosening_threshold(client):
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    pc = precheck(client).json()
    assert pc["verdict"] == "fail"
    # 放宽阈值不会把已落库的 fail 预检改写成 pass；必须在同一口径下重扫。
    set_policy(client, "hard_gate", 0.5)
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "coverage_gate_blocked"

    fresh = precheck(client).json()
    assert fresh["verdict"] == "pass"
    assert fresh["threshold"] == 0.5
    assert seal(client).status_code == 200
    snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert snap["state"] == "pass"
    assert snap["threshold"] == 0.5


# ---------------------------------------------------------------- forged coverage


def test_forged_coverage_setting_rejected(client):
    r = client.put(
        "/api/coverage/policy", json={"architecture": "hard_gate", "threshold": 5}
    )
    assert r.status_code == 400
    r = client.put(
        "/api/coverage/policy", json={"architecture": "sneaky_bypass", "threshold": 0.8}
    )
    assert r.status_code == 400
    # 直接改库伪造合法架构但设置非法阈值：读取时夹紧，不产生假达标。
    r = client.put(
        "/api/coverage/policy", json={"architecture": "hard_gate", "threshold": "-3"}
    )
    assert r.status_code == 400


def test_corrupt_architecture_setting_falls_back_to_hard_gate(client):
    import sqlite3

    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('coverage_architecture','bogus') "
            "ON CONFLICT(key) DO UPDATE SET value='bogus'"
        )
        conn.commit()
    finally:
        conn.close()
    low = client.get("/api/coverage/policy").json()
    assert low["architecture"] == "hard_gate"  # 损坏配置不放开封印


def test_forged_precheck_row_cannot_fake_pass(client):
    """直接往预检表塞伪造 pass 行：指纹不符 → 视为过期 → 拒封。"""
    import sqlite3
    from datetime import datetime, timezone

    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            """INSERT INTO coverage_prechecks(
                 precheck_id, period_id, architecture, method,
                 estimate_key_count, covered_key_count, coverage_ratio, threshold,
                 missing_meters, data_fingerprint, verdict, status, created_at)
               VALUES ('PC-forged', ?, 'hard_gate', 'meter_keys',
                       6, 6, 1.0, 0.8, '', 'deadbeefdeadbeef', 'pass', 'active', ?)""",
            (PERIOD, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    set_policy(client, "hard_gate", 0.8)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["stale"] is True


def test_snapshot_endpoint_never_previews_pass_when_gate_blocks(client):
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    data = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert data["sealed"] is False
    assert data["gate_ok"] is False
    assert data["state"] == "skipped"  # 无预检：不伪造成 pass
    assert data["coverage_ratio"] is None


# ---------------------------------------------------------------- overview


def test_overview_uses_same_coverage_numbers(client):
    delete_actuals(client, ["M-E-202", "M-W-201"])
    set_policy(client, "hard_gate", 0.9)
    precheck(client)
    ov = client.get("/api/overview").json()
    assert ov["coverage_policy"] == {"architecture": "hard_gate", "threshold": 0.9}
    cov = ov["coverage"]
    assert cov["estimate_key_count"] == 6
    assert cov["covered_key_count"] == 4
    assert cov["missing_meters"] == ["M-E-202", "M-W-201"]
    assert cov["gate_ok"] is False
    scan_data = scan(client)
    assert cov["coverage_ratio"] == scan_data["coverage_ratio"]
    missing = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
    assert cov["missing_meters"] == missing["missing_meters"]


def test_overview_after_seal_reads_stored_snapshot(client):
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200
    cov = client.get("/api/overview").json()["coverage"]
    assert cov["period_status"] == "sealed"
    assert cov["state"] == "pass"
    assert cov["coverage_ratio"] == 1.0


# ---------------------------------------------------------------- sealing guards


def test_cannot_load_into_sealed_period(client):
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200
    r = add_actual(client, "M-W-101", doc_no="POST-SEAL")
    assert r.status_code == 409


def test_sealed_snapshot_is_immutable_under_recompute(client):
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 409

    # 补齐实抄 → 重扫 → 达标可封。
    for mid in ("M-E-202", "M-W-201"):
        assert add_actual(client, mid, doc_no=f"FIX-{mid}").status_code == 200
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 200, r.text
    assert r.json()["coverage"]["coverage_ratio"] == 1.0


# ---------------------------------------------------------------- concurrency


def test_concurrent_load_and_seal_never_seals_on_stale_coverage(client):
    """装载与封印并发：要么装载先落库使预检过期→拒封，要么封印先拿锁→装载被拒。"""
    import sqlite3
    import threading

    set_policy(client, "hard_gate", 0.9)
    # 新表只有估抄、没有实抄：一旦装载，覆盖率 6/7 ≈ 0.857 < 0.9。
    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) "
            "VALUES ('M-W-999','C栋','999','water','active')"
        )
        conn.commit()
    finally:
        conn.close()
    assert precheck(client).json()["verdict"] == "pass"
    client.post("/api/align", json={"period_id": PERIOD})

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def load():
        barrier.wait()
        results["load"] = client.post(
            "/api/estimates",
            json={
                "meter_id": "M-W-999",
                "period_id": PERIOD,
                "qty": 7.0,
                "doc_no": "RACE-EST-999",
            },
        )

    def do_seal():
        barrier.wait()
        results["seal"] = seal(client)

    t1 = threading.Thread(target=load)
    t2 = threading.Thread(target=do_seal)
    t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)

    load_res = results["load"]
    seal_res = results["seal"]
    if seal_res.status_code == 200:
        # 封印成功：快照必须是达标口径，且装载（若在后）必须被拒。
        snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
        assert snap["state"] == "pass"
        assert snap["coverage_ratio"] is not None and snap["coverage_ratio"] >= 0.9
        assert load_res.status_code == 409
    else:
        # 装载先落库：预检过期，拒封且不得留下封印快照。
        assert seal_res.status_code == 409
        assert load_res.status_code == 200
        period = next(
            p for p in client.get("/api/periods").json() if p["period_id"] == PERIOD
        )
        assert period["status"] == "open"
        assert period["coverage_state"] is None


def test_loading_after_seal_rejected_and_snapshot_unchanged(client):
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200
    before = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    r = client.post(
        "/api/estimates",
        json={"meter_id": "M-W-101", "period_id": PERIOD, "qty": 1, "doc_no": "X"},
    )
    assert r.status_code == 409
    after = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert before == after


def test_missing_page_after_seal_reads_sealed_snapshot(client):
    """封印后缺失表清单须与封印快照同口径，不得因封印后数据变化而分叉。"""
    import sqlite3

    delete_actuals(client, ["M-E-202", "M-W-201"])
    set_policy(client, "allow_skip", 0.8)
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 200
    snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()

    # 直接改库绕过封印校验，模拟历史数据漂移。
    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            "DELETE FROM actuals WHERE meter_id='M-W-102' AND period_id=?", (PERIOD,)
        )
        conn.commit()
    finally:
        conn.close()

    data = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
    assert data["period_status"] == "sealed"
    assert data["missing_meters"] == snap["missing_meters"]
    assert data["coverage_ratio"] == snap["coverage_ratio"]
    assert [r["meter_id"] for r in data["rows"]] == snap["missing_meters"]


def test_double_seal_race_only_one_wins(client):
    import threading

    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def do_seal():
        barrier.wait()
        codes.append(seal(client).status_code)

    threads = [threading.Thread(target=do_seal) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)

    assert sorted(codes) == [200, 409, 409, 409], codes
    snap = client.get("/api/coverage/snapshot", params={"period_id": PERIOD}).json()
    assert snap["sealed"] is True


# ---------------------------------------------------------------- legacy migration


def test_legacy_db_without_coverage_columns_migrates(tmp_path, monkeypatch):
    """旧库（periods 无快照列、settings 无口径）启动后自动补列并默认硬闸。"""
    import sqlite3

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    try:
        conn.executescript(
            """
            CREATE TABLE meters(meter_id TEXT PRIMARY KEY, building TEXT, unit_label TEXT,
                                meter_type TEXT, status TEXT);
            CREATE TABLE periods(period_id TEXT PRIMARY KEY, status TEXT DEFAULT 'open',
                                 sealed_at TEXT, batch_no TEXT);
            INSERT INTO periods(period_id,status) VALUES ('2026-10','open');
            INSERT INTO meters VALUES ('M-W-101','A栋','101','water','active');
            CREATE TABLE estimates(estimate_id TEXT PRIMARY KEY, meter_id TEXT, period_id TEXT,
                                   qty REAL, doc_no TEXT, loaded_at TEXT);
            CREATE TABLE actuals(actual_id TEXT PRIMARY KEY, meter_id TEXT, period_id TEXT,
                                 qty REAL, occurred_at TEXT, reader_code TEXT, doc_no TEXT,
                                 loaded_at TEXT);
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("METERS_DB_PATH", str(legacy))
    from app.seed import seed_if_empty

    seed_if_empty()
    conn = sqlite3.connect(legacy)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(periods)")}
        assert {"coverage_architecture", "coverage_state", "coverage_ratio"} <= cols
        settings = dict(conn.execute("SELECT key,value FROM settings").fetchall())
        assert settings["coverage_architecture"] == "hard_gate"
    finally:
        conn.close()

    with TestClient(fastapi_app) as c:
        policy = c.get("/api/coverage/policy").json()
        assert policy == {"architecture": "hard_gate", "threshold": 0.8}


# ---------------------------------------------------------------- claim tear fixtures
#
# 四条链必须同源于「正式实抄键 ∪ 应对齐键集合」的重放：
#   预检 → 批头 → 总览 → 未抄列表。
# 下面四个夹具分别钉住用户列出的撕裂场景。


def _chain_snapshot(c, period=PERIOD):
    """一次取齐四条链的覆盖声明，便于断言它们不分叉。"""
    return {
        "scan": c.get("/api/coverage/scan", params={"period_id": period}).json(),
        "missing": c.get("/api/coverage/missing", params={"period_id": period}).json(),
        "overview": c.get("/api/overview").json()["coverage"],
        "gate": c.get("/api/coverage/gate", params={"period_id": period}).json(),
    }


def _sealed_chain(c, period=PERIOD):
    snap = c.get("/api/coverage/snapshot", params={"period_id": period}).json()
    period_row = next(p for p in c.get("/api/periods").json() if p["period_id"] == period)
    batch = c.get("/api/batches", params={"period_id": period}).json()[0]
    missing = c.get("/api/coverage/missing", params={"period_id": period}).json()
    overview = c.get("/api/overview").json()["coverage"]
    return {"snapshot": snap, "period": period_row, "batch": batch,
            "missing": missing, "overview": overview}


def test_chain_all_four_links_agree_before_seal(client):
    delete_actuals(client, ["M-E-202", "M-W-201"])
    set_policy(client, "hard_gate", 0.9)
    pc = precheck(client).json()
    ch = _chain_snapshot(client)
    for link in ("scan", "missing"):
        assert ch[link]["covered_key_count"] == pc["covered_key_count"] == 4
        assert ch[link]["estimate_key_count"] == 6
        assert ch[link]["missing_meters"] == pc["missing_meters"] == ["M-E-202", "M-W-201"]
        assert ch[link]["coverage_ratio"] == pc["coverage_ratio"] == pytest.approx(4 / 6)
    assert ch["overview"]["coverage_ratio"] == pytest.approx(4 / 6)
    assert ch["overview"]["missing_meters"] == ["M-E-202", "M-W-201"]
    assert ch["gate"]["ok"] is False
    # 覆盖率不是 100% 且未抄列表非空：数字与键集合一致。
    assert ch["scan"]["key_evidence"]["missing_keys"] == ["M-E-202", "M-W-201"]


def test_fixture_high_precheck_full_header_missing_list_not_empty(client):
    """夹具 1：预检 95% / 批头 100% / 未抄仍有行 —— 必须收口，禁止数字绿键集合分叉。"""
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-W-201"])  # 5/6 = 83.3% < 0.9
    pc = precheck(client).json()
    assert pc["verdict"] == "fail"
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "coverage_gate_blocked"
    # 未封印：未抄列表仍有行，且 100% 从未出现。
    ch = _chain_snapshot(client)
    assert ch["missing"]["missing_meters"] == ["M-W-201"]
    assert ch["scan"]["coverage_ratio"] == pytest.approx(5 / 6)
    assert ch["overview"]["coverage_ratio"] == pytest.approx(5 / 6)
    assert ch["overview"]["verdict"] == "fail"


def test_fixture_seal_clears_list_without_actuals_is_refused(client):
    """夹具 2：封印试图清空未抄列表但没有正式实抄 —— 正式键重放不足，回滚假封印。"""
    set_policy(client, "hard_gate", 0.8)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    assert seal(client).status_code == 409
    period = next(p for p in client.get("/api/periods").json() if p["period_id"] == PERIOD)
    assert period["status"] == "open"
    assert period["coverage_state"] is None
    # 未抄列表未被清空：仍能由正式键重放。
    data = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
    assert data["missing_meters"] == ["M-E-202", "M-W-201"]


def test_fixture_sealed_missing_list_replayable_from_formal_keys(client):
    """正式键架构：封印后未抄列表必须能由正式键原样重放。"""
    delete_actuals(client, ["M-E-202"])
    set_policy(client, "allow_skip", 0.8)
    precheck(client)
    client.post("/api/align", json={"period_id": PERIOD})
    r = seal(client)
    assert r.status_code == 200, r.text
    ch = _sealed_chain(client)
    snap = ch["snapshot"]
    assert snap["missing_meters"] == ["M-E-202"]
    assert ch["missing"]["missing_meters"] == snap["missing_meters"]
    assert ch["missing"]["claim_tear"] is False
    assert ch["overview"]["missing_meters"] == snap["missing_meters"]
    assert ch["overview"]["claim_tear"] is False
    # 批头与快照同事务写，字段一致。
    assert ch["batch"]["coverage_state"] == snap["state"]
    assert ch["batch"]["coverage_ratio"] == snap["coverage_ratio"]
    assert ch["batch"]["coverage_missing"] == "M-E-202"
    assert ch["batch"]["coverage_precheck_id"] == snap["precheck_id"]
    assert ch["batch"]["coverage_data_fingerprint"] == snap["data_fingerprint"]


def test_fixture_interleaved_old_snapshot_never_becomes_current(client):
    """夹具 3：预检与封印交错 —— 装载先落库则旧预检过期，封印不得按旧快照落批。"""
    set_policy(client, "hard_gate", 0.9)
    assert precheck(client).json()["verdict"] == "pass"
    client.post("/api/align", json={"period_id": PERIOD})

    # 交错：封印前又装载了一条新估抄（新表无实抄），覆盖率降到 6/7 < 0.9。
    import sqlite3

    conn = sqlite3.connect(client.db_path)
    try:
        conn.execute(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) "
            "VALUES ('M-W-998','C栋','998','water','active')"
        )
        conn.execute(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) "
            "VALUES ('E-LATE','M-W-998',?,3.0,'EST-LATE','2026-10-31T08:00:00')",
            (PERIOD,),
        )
        conn.commit()
    finally:
        conn.close()

    # 闸门看的必须是新装载后的正式键，而不是封印前那套 100% 旧快照。
    gate = client.get("/api/coverage/gate", params={"period_id": PERIOD}).json()
    assert gate["ok"] is False
    assert gate["stale"] is True
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["stale"] is True
    period = next(p for p in client.get("/api/periods").json() if p["period_id"] == PERIOD)
    assert period["status"] == "open"
    assert period["coverage_ratio"] is None


def test_fixture_concurrent_double_precheck_leaves_single_claim(client):
    """夹具 4：并发双预检只能留下一条 active 覆盖声明。"""
    import sqlite3
    import threading

    barrier = threading.Barrier(6)
    ids: list[str] = []

    def do_precheck():
        barrier.wait()
        r = precheck(client)
        assert r.status_code == 200, r.text
        ids.append(r.json()["precheck_id"])

    threads = [threading.Thread(target=do_precheck) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(ids) == 6
    conn = sqlite3.connect(client.db_path)
    try:
        active = conn.execute(
            "SELECT precheck_id FROM coverage_prechecks WHERE period_id=? AND status='active'",
            (PERIOD,),
        ).fetchall()
    finally:
        conn.close()
    assert len(active) == 1, active
    # 接口读到的 active 预检就是那唯一一条。
    latest = client.get("/api/coverage/scan", params={"period_id": PERIOD}).json()["active_precheck"]
    assert latest["precheck_id"] == active[0][0]


def test_fixture_forged_full_coverage_header_is_torn_not_green(client):
    """伪造高覆盖预检：数字绿、键集合不足 → 撕裂拒封，不许只改缓存糊过去。"""
    import sqlite3
    from datetime import datetime, timezone

    set_policy(client, "hard_gate", 0.8)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    client.post("/api/align", json={"period_id": PERIOD})

    conn = sqlite3.connect(client.db_path)
    try:
        # 伪造一条与数据无关的 pass 预检：6/6、满覆盖、未抄为空。
        conn.execute(
            """INSERT INTO coverage_prechecks(
                 precheck_id, period_id, architecture, method,
                 estimate_key_count, covered_key_count, coverage_ratio, threshold,
                 missing_meters, data_fingerprint, verdict, status, created_at)
               VALUES ('PC-painted', ?, 'hard_gate', 'meter_keys',
                       6, 6, 1.0, 0.8, '', 'deadbeefdeadbeef', 'pass', 'active', ?)""",
            (PERIOD, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    # 未抄列表仍由正式键重放给出，绝不被伪造批头清空。
    data = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
    assert data["missing_meters"] == ["M-E-202", "M-W-201"]
    gate = client.get("/api/coverage/gate", params={"period_id": PERIOD}).json()
    assert gate["ok"] is False
    assert gate["claim_mismatch"] is True
    r = seal(client)
    assert r.status_code == 409
    assert r.json()["detail"]["claim_mismatch"] is True
    period = next(p for p in client.get("/api/periods").json() if p["period_id"] == PERIOD)
    assert period["status"] == "open"
    assert period["coverage_ratio"] is None


def test_health_has_no_paint_bypass(client):
    """旧的粉饰缓存口径已移除：health 不再回报任何覆盖率数字。"""
    set_policy(client, "hard_gate", 0.9)
    delete_actuals(client, ["M-E-202", "M-W-201"])
    precheck(client)
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert "coverage_ratio" not in health
    assert "coverage_missing" not in health
    # 覆盖率只以正式键重放为准。
    ov = client.get("/api/overview").json()["coverage"]
    assert ov["coverage_ratio"] == pytest.approx(4 / 6)


def test_paint_setting_write_is_rejected(client):
    r = client.put("/api/settings/coverage_paint", json={"value": '{"ratio": 1.0}'})
    assert r.status_code == 400


def test_ratio_100_iff_missing_empty_invariant(client):
    """覆盖率 100% 当且仅当未抄列表为空 —— 对每个中间状态都成立。"""
    delete_actuals(client, ALL_METERS)
    for i, mid in enumerate(ALL_METERS):
        add_actual(client, mid, doc_no=f"INV-{mid}")
        data = client.get("/api/coverage/missing", params={"period_id": PERIOD}).json()
        if data["missing_count"] == 0:
            assert data["coverage_ratio"] == 1.0
        else:
            assert data["coverage_ratio"] < 1.0


def test_legacy_paint_cache_is_purged_and_duplicate_active_collapsed(tmp_path, monkeypatch):
    """旧库遗留的粉饰缓存与多套 active 预检：启动迁移后只留一套正式键声明。"""
    import sqlite3

    from app.seed import SCHEMA, _migrate

    db = tmp_path / "legacy_paint.db"
    monkeypatch.setenv("METERS_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('coverage_paint',"
            "'{\"ratio\": 1.0, \"missing\": []}')"
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-10','open')"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('current_period','2026-10')"
        )
        for pid, created in (("PC-a", "2026-11-01T00:00:00"), ("PC-b", "2026-11-02T00:00:00")):
            conn.execute(
                """INSERT INTO coverage_prechecks(
                     precheck_id, period_id, architecture, method,
                     estimate_key_count, covered_key_count, coverage_ratio, threshold,
                     missing_meters, data_fingerprint, verdict, status, created_at)
                   VALUES (?, '2026-10', 'hard_gate', 'meter_keys', 6, 6, 1.0, 0.8,
                           '', 'fp', 'pass', 'active', ?)""",
                (pid, created),
            )
        conn.commit()
        _migrate(conn)
        conn.commit()

        assert not conn.execute(
            "SELECT 1 FROM settings WHERE key='coverage_paint'"
        ).fetchone()
        active = conn.execute(
            "SELECT precheck_id FROM coverage_prechecks WHERE status='active'"
        ).fetchall()
        assert [r["precheck_id"] for r in active] == ["PC-b"], active
        batch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reconcile_batches)")}
        assert "coverage_state" in batch_cols and "coverage_missing" in batch_cols
    finally:
        conn.close()

    from app.main import app as _app

    with TestClient(_app) as c:
        # 旧粉饰缓存的 100% 不会出现在任何覆盖链路上。
        ov = c.get("/api/overview").json()["coverage"]
        assert ov["coverage_ratio"] is None  # 无估抄/实抄：无分母
        assert ov["missing_meters"] == []
