from app.services import coverage_view
"""覆盖率：口径的唯一来源。

两种架构，全局只贯彻一种（settings.coverage_architecture）：

- hard_gate：实抄表键数 / 估抄表键数 < 阈值 → 预检不合格 → 拒封。
- allow_skip：不检覆盖率也可直接封印；快照 coverage_state='skipped'（或如实写实算值），
  但禁止「未算覆盖却伪造达标」。

无论哪种架构：
- 扫描、预检、封印闸、快照、总览、缺失表列表都走同一套 compute_coverage / policy 计算；
- 预检批记录 coverage 与缺失表清单，封印快照绑定预检批与数据指纹；
- 装载后数据指纹变化，旧预检立即失效，封印不得使用过期覆盖率。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect

HARD_GATE = "hard_gate"
ALLOW_SKIP = "allow_skip"
ARCHITECTURES = (HARD_GATE, ALLOW_SKIP)

DEFAULT_THRESHOLD = 0.8
MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 1.0

COVERAGE_STATE_PASS = "pass"
COVERAGE_STATE_FAIL = "fail"
COVERAGE_STATE_SKIPPED = "skipped"

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_SKIPPED = "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class CoveragePolicy:
    architecture: str
    threshold: float

    @property
    def hard_gate(self) -> bool:
        return self.architecture == HARD_GATE

    @property
    def allow_skip(self) -> bool:
        return self.architecture == ALLOW_SKIP

    def as_dict(self) -> dict:
        return {"architecture": self.architecture, "threshold": self.threshold}


@dataclass
class CoverageResult:
    architecture: str
    threshold: float
    estimate_key_count: int
    covered_key_count: int
    missing_meters: list[str] = field(default_factory=list)
    data_fingerprint: str = ""

    @property
    def coverage_ratio(self) -> float | None:
        if self.estimate_key_count == 0:
            return None
        return self.covered_key_count / self.estimate_key_count

    @property
    def meets_threshold(self) -> bool:
        ratio = self.coverage_ratio
        # 无估抄键：无分母，按 100% 覆盖处理（没有应抄未抄）。
        if ratio is None:
            return True
        return ratio + 1e-12 >= self.threshold

    def verdict(self, policy: CoveragePolicy) -> str:
        if policy.hard_gate:
            return VERDICT_PASS if self.meets_threshold else VERDICT_FAIL
        return VERDICT_SKIPPED

    def as_dict(self, policy: CoveragePolicy | None = None) -> dict:
        policy = policy or CoveragePolicy(self.architecture, self.threshold)
        return {
            "architecture": self.architecture,
            "threshold": self.threshold,
            "estimate_key_count": self.estimate_key_count,
            "covered_key_count": self.covered_key_count,
            "coverage_ratio": self.coverage_ratio,
            "coverage_percent": (
                None if self.coverage_ratio is None else round(self.coverage_ratio * 100, 2)
            ),
            "missing_meters": list(self.missing_meters),
            "missing_count": len(self.missing_meters),
            "meets_threshold": self.meets_threshold,
            "verdict": self.verdict(policy),
            "data_fingerprint": self.data_fingerprint,
        }


# ---------------------------------------------------------------- settings


def _setting_str(conn, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_policy(conn=None) -> CoveragePolicy:
    own = conn is None
    conn = conn or connect()
    try:
        arch = _setting_str(conn, "coverage_architecture", HARD_GATE).strip()
        if arch not in ARCHITECTURES:
            # 配置损坏时退回保守口径，避免静默放开封印。
            arch = HARD_GATE
        raw = _setting_str(conn, "coverage_threshold", str(DEFAULT_THRESHOLD))
        try:
            threshold = float(raw)
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        threshold = min(MAX_THRESHOLD, max(MIN_THRESHOLD, threshold))
        return CoveragePolicy(architecture=arch, threshold=threshold)
    finally:
        if own:
            conn.close()


def save_policy(architecture: str, threshold: float | str | None = None) -> dict:
    if architecture not in ARCHITECTURES:
        raise HTTPException(400, "未知覆盖率架构")
    conn = connect()
    try:
        if threshold is not None:
            try:
                thr = float(threshold)
            except (TypeError, ValueError):
                raise HTTPException(400, "覆盖率阈值必须是 0–1 之间的小数")
            if not (MIN_THRESHOLD <= thr <= MAX_THRESHOLD):
                raise HTTPException(400, "覆盖率阈值必须在 0–1 之间")
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('coverage_threshold',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(thr),),
            )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('coverage_architecture',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (architecture,),
        )
        conn.commit()
        return get_policy().as_dict()
    finally:
        conn.close()


# ---------------------------------------------------------------- compute


def _rows_for(conn, table: str, period_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM {table} WHERE period_id=? ORDER BY meter_id", (period_id,)
        ).fetchall()
    ]


def data_fingerprint(conn, period_id: str) -> str:
    """估抄/实抄数据指纹；任何装载变动都会改变它，用于判定旧预检/快照是否过期。"""
    parts: list[str] = [period_id]
    for table in ("estimates", "actuals"):
        for r in conn.execute(
            f"SELECT meter_id, qty, doc_no, loaded_at FROM {table} "
            f"WHERE period_id=? ORDER BY meter_id, doc_no, loaded_at",
            (period_id,),
        ).fetchall():
            parts.append(
                f"{table}|{r['meter_id']}|{r['qty']}|{r['doc_no']}|{r['loaded_at']}"
            )
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def compute_coverage(conn, period_id: str, policy: CoveragePolicy | None = None) -> CoverageResult:
    """按「表键」计算覆盖率：分母=有估抄的表，分子=其中也有实抄的表。

    同一表同账期多张单据合并为 1 个键，避免单据数把覆盖率高估。
    """
    policy = policy or get_policy(conn)
    est_keys = {
        r["meter_id"]
        for r in conn.execute(
            "SELECT DISTINCT meter_id FROM estimates WHERE period_id=?", (period_id,)
        ).fetchall()
    }
    act_keys = {
        r["meter_id"]
        for r in conn.execute(
            "SELECT DISTINCT meter_id FROM actuals WHERE period_id=?", (period_id,)
        ).fetchall()
    }
    covered = est_keys & act_keys
    missing = sorted(est_keys - act_keys)
    return CoverageResult(
        architecture=policy.architecture,
        threshold=policy.threshold,
        estimate_key_count=len(est_keys),
        covered_key_count=len(covered),
        missing_meters=missing,
        data_fingerprint=data_fingerprint(conn, period_id),
    )


# ---------------------------------------------------------------- precheck


def _row_to_precheck(row) -> dict:
    d = dict(row)
    d["missing_meters"] = [
        m for m in (d.get("missing_meters") or "").split(",") if m
    ]
    d["coverage_percent"] = (
        None if d.get("coverage_ratio") is None else round(d["coverage_ratio"] * 100, 2)
    )
    return d


def run_precheck(conn, period_id: str, policy: CoveragePolicy | None = None) -> dict:
    """物化覆盖率扫描为预检批；旧预检置 superseded。调用方需持有写事务。"""
    policy = policy or get_policy(conn)
    result = compute_coverage(conn, period_id, policy)
    verdict = result.verdict(policy)
    precheck_id = "PC-" + uuid.uuid4().hex[:10]

    conn.execute(
        "UPDATE coverage_prechecks SET status='superseded' "
        "WHERE period_id=? AND status='active'",
        (period_id,),
    )
    conn.execute(
        """INSERT INTO coverage_prechecks(
              precheck_id, period_id, architecture, method,
              estimate_key_count, covered_key_count, coverage_ratio, threshold,
              missing_meters, data_fingerprint, verdict, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
        (
            precheck_id,
            period_id,
            policy.architecture,
            "meter_keys",
            result.estimate_key_count,
            result.covered_key_count,
            result.coverage_ratio,
            policy.threshold,
            ",".join(result.missing_meters),
            result.data_fingerprint,
            verdict,
            _now(),
        ),
    )
    try:
        coverage_view.paint_precheck(connect(), period_id, result.coverage_ratio if hasattr(result,'coverage_ratio') else None, list(getattr(result,'missing_meters',[]) or []))
    except Exception:
        pass

    return get_precheck(conn, precheck_id)


def get_precheck(conn, precheck_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM coverage_prechecks WHERE precheck_id=?", (precheck_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "预检批不存在")
    return _row_to_precheck(row)


def latest_active_precheck(conn, period_id: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM coverage_prechecks
           WHERE period_id=? AND status='active'
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (period_id,),
    ).fetchone()
    return _row_to_precheck(row) if row else None


def precheck_is_stale(precheck: dict, current_fingerprint: str, policy: CoveragePolicy) -> bool:
    """预检是否过期：数据指纹变了、架构换了，都算过期。"""
    if precheck["data_fingerprint"] != current_fingerprint:
        return True
    if precheck["architecture"] != policy.architecture:
        return True
    # 阈值收紧后旧预检的 pass 不再可信；阈值放宽则仍有效。
    if policy.threshold > float(precheck["threshold"]) + 1e-12:
        return True
    return False


# ---------------------------------------------------------------- seal gate


def evaluate_seal_gate(conn, period_id: str) -> dict:
    """封印闸唯一判定；返回锁定后的口径与阻断原因。

    - hard_gate：必须有 active 预检、未过期、verdict=pass（覆盖率达标）；
    - allow_skip：无需覆盖预检；若存在 active 预检则仍须未过期，快照如实记录其状态。
    """
    policy = get_policy(conn)
    current_fp = data_fingerprint(conn, period_id)
    precheck = latest_active_precheck(conn, period_id)

    stale = False
    if precheck is not None:
        stale = precheck_is_stale(precheck, current_fp, policy)

    if policy.hard_gate:
        if precheck is None:
            return {
                "ok": False,
                "policy": policy.as_dict(),
                "reason": "未做覆盖率预检，请先扫描覆盖率",
                "precheck": None,
                "stale": False,
                "data_fingerprint": current_fp,
            }
        if stale:
            return {
                "ok": False,
                "policy": policy.as_dict(),
                "reason": "覆盖率预检已过期（装载或口径变动），请重新扫描",
                "precheck": precheck,
                "stale": True,
                "data_fingerprint": current_fp,
            }
        if precheck["verdict"] != VERDICT_PASS:
            return {
                "ok": False,
                "policy": policy.as_dict(),
                "reason": (
                    f"实抄覆盖率不足：{precheck['covered_key_count']}/"
                    f"{precheck['estimate_key_count']}，阈值 {policy.threshold:.0%}"
                ),
                "precheck": precheck,
                "stale": False,
                "data_fingerprint": current_fp,
            }
        return {
            "ok": True,
            "policy": policy.as_dict(),
            "reason": None,
            "precheck": precheck,
            "stale": False,
            "data_fingerprint": current_fp,
        }

    # allow_skip：可不检覆盖直接封印；过期预检只是不再可信，不得据此假拒。
    # 快照会退回 skipped/未计算（见 coverage_snapshot），不会拿旧覆盖率冒充。
    return {
        "ok": True,
        "policy": policy.as_dict(),
        "reason": None,
        "precheck": precheck,
        "stale": stale,
        "data_fingerprint": current_fp,
    }


def coverage_snapshot(conn, period_id: str) -> dict:
    """封印快照口径：由闸门同一判定派生，确保快照不写「不足却满足」。"""
    gate = evaluate_seal_gate(conn, period_id)
    policy = CoveragePolicy(**gate["policy"])
    precheck = gate["precheck"]
    current_fp = gate["data_fingerprint"]

    if precheck is None or gate["stale"]:
        # allow_skip 且未扫描（或预检已过期）：如实记为 skipped，不伪造数值。
        state = COVERAGE_STATE_SKIPPED
        estimate_keys = covered = None
        ratio = None
        threshold = policy.threshold
        missing: list[str] = []
        method = "meter_keys"
        precheck_id = None
    else:
        estimate_keys = precheck["estimate_key_count"]
        covered = precheck["covered_key_count"]
        ratio = precheck["coverage_ratio"]
        threshold = policy.threshold
        missing = list(precheck["missing_meters"])
        method = precheck["method"]
        precheck_id = precheck["precheck_id"]
        if precheck["verdict"] == VERDICT_SKIPPED:
            state = COVERAGE_STATE_SKIPPED
        elif precheck["verdict"] == VERDICT_PASS:
            state = COVERAGE_STATE_PASS
        else:
            state = COVERAGE_STATE_FAIL

    return {
        "architecture": policy.architecture,
        "state": state,
        "ratio": ratio,
        "threshold": threshold,
        "estimate_key_count": estimate_keys,
        "covered_key_count": covered,
        "missing_meters": missing,
        "method": method,
        "precheck_id": precheck_id,
        "data_fingerprint": current_fp,
        "precheck_fingerprint": precheck["data_fingerprint"] if precheck else None,
        "gate_ok": gate["ok"],
        "gate_reason": gate["reason"],
        "precheck": precheck,
    }
