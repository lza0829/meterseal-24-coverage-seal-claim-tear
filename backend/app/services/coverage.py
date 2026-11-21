"""覆盖率：口径与证据的唯一来源。

证据架构（全局只贯彻这一种）——**正式键重放**：

- 唯一权威是正式实抄键集合 ∪ 应对齐（估抄）键集合，直接由库内
  ``estimates`` / ``actuals`` 的 ``meter_id`` 重放得到；
- 扫描、预检批、封印闸、封印批头、账期快照、未抄列表、总览全部走
  ``replay_coverage`` / ``compute_coverage`` 同一计算，禁止任何旁路缓存或粉饰；
- 预检批与封印批头的覆盖数字必须能由正式键重放；重放不一致即视为
  伪造/过期，拒封并回滚，账期与未抄列表保持原样；
- 覆盖率为 100% 当且仅当未抄列表为空；未抄列表非空则覆盖率必须 < 100%。

门槛架构（``settings.coverage_architecture``）只决定是否允许低覆盖封印：

- hard_gate：``covered / estimate < threshold`` → 预检不合格 → 拒封；
- allow_skip：可低覆盖直封，但快照如实记录 ``skipped`` 与真实覆盖数字/未抄列表，
  不得伪造 ``pass`` 或清空未抄列表。

并发与交错：预检、装载、封印均走 ``BEGIN IMMEDIATE`` 写事务，封印在写锁内
重放正式键；预检批带数据指纹，装载后旧预检立即过期；同一账期最多一条
active 预检（库级唯一部分索引），并发双预检不会留下两套覆盖声明。
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

CODE_GATE_BLOCKED = "coverage_gate_blocked"
CODE_CLAIM_TEAR = "coverage_claim_tear"

METHOD_METER_KEYS = "meter_keys"

_RATIO_EPS = 1e-12


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CoverageTear(Exception):
    """覆盖宣告与正式键集合证据撕裂。任何落库前发现都应回滚。"""


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
    """由正式键集合重放出的覆盖证据。

    分母 = 有估抄的表键；分子 = 其中也有实抄的表键。同一表同账期多张
    单据合并为 1 个键。``actual_only_meters``（有实抄无估抄）不抬高覆盖率，
    但保留在键证据里，避免「键集合」被悄悄丢掉。
    """

    architecture: str
    threshold: float
    estimate_key_count: int
    covered_key_count: int
    missing_meters: list[str] = field(default_factory=list)
    estimate_keys: list[str] = field(default_factory=list)
    actual_keys: list[str] = field(default_factory=list)
    covered_meters: list[str] = field(default_factory=list)
    actual_only_meters: list[str] = field(default_factory=list)
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
        return ratio + _RATIO_EPS >= self.threshold

    def verdict(self, policy: CoveragePolicy) -> str:
        if policy.hard_gate:
            return VERDICT_PASS if self.meets_threshold else VERDICT_FAIL
        return VERDICT_SKIPPED

    def state(self, policy: CoveragePolicy) -> str:
        """把判词映射为快照状态，供扫描/缺失/总览与封印快照直接对齐。"""
        return {
            VERDICT_PASS: COVERAGE_STATE_PASS,
            VERDICT_FAIL: COVERAGE_STATE_FAIL,
            VERDICT_SKIPPED: COVERAGE_STATE_SKIPPED,
        }[self.verdict(policy)]

    def key_evidence(self) -> dict:
        return {
            "estimate_keys": list(self.estimate_keys),
            "actual_keys": list(self.actual_keys),
            "covered_keys": list(self.covered_meters),
            "missing_keys": list(self.missing_meters),
            "actual_only_keys": list(self.actual_only_meters),
        }

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
            "state": self.state(policy),
            "data_fingerprint": self.data_fingerprint,
            "key_evidence": self.key_evidence(),
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
        thr: float | None = None
        if threshold is not None:
            try:
                thr = float(threshold)
            except (TypeError, ValueError):
                raise HTTPException(400, "覆盖率阈值必须是 0–1 之间的小数")
            if not (MIN_THRESHOLD <= thr <= MAX_THRESHOLD):
                raise HTTPException(400, "覆盖率阈值必须在 0–1 之间")
        conn.execute("BEGIN IMMEDIATE")
        if thr is not None:
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
        conn.execute("COMMIT")
        return get_policy().as_dict()
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- compute / replay


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
    这是正式键集合的唯一重放入口。
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
    missing = est_keys - act_keys
    return CoverageResult(
        architecture=policy.architecture,
        threshold=policy.threshold,
        estimate_key_count=len(est_keys),
        covered_key_count=len(covered),
        missing_meters=sorted(missing),
        estimate_keys=sorted(est_keys),
        actual_keys=sorted(act_keys),
        covered_meters=sorted(covered),
        actual_only_meters=sorted(act_keys - est_keys),
        data_fingerprint=data_fingerprint(conn, period_id),
    )


def verify_claim(result: CoverageResult) -> None:
    """覆盖数字必须与键集合自洽；不绿也要报撕裂，而不是把数字抹平。"""
    est = set(result.estimate_keys)
    act = set(result.actual_keys)
    covered = set(result.covered_meters)
    missing = set(result.missing_meters)

    if len(est) != result.estimate_key_count or len(act) != len(result.actual_keys):
        raise CoverageTear("估抄/实抄键集合计数不一致")
    if len(covered) != result.covered_key_count:
        raise CoverageTear("已覆盖键计数与键集合不一致")
    if covered != est & act:
        raise CoverageTear("已覆盖键集合 ≠ 估抄键 ∩ 实抄键")
    if missing != est - act:
        raise CoverageTear("未抄列表 ≠ 估抄键 - 实抄键")
    if set(result.actual_only_meters) != act - est:
        raise CoverageTear("实抄独有键集合不一致")
    if result.covered_key_count + len(missing) != result.estimate_key_count:
        raise CoverageTear("已覆盖 + 未抄 ≠ 估抄键数")

    ratio = result.coverage_ratio
    if result.estimate_key_count == 0:
        if ratio is not None or missing:
            raise CoverageTear("无估抄键时不得有覆盖率或未抄行")
        return
    if ratio is None or abs(ratio - result.covered_key_count / result.estimate_key_count) > _RATIO_EPS:
        raise CoverageTear("覆盖率 ≠ 已覆盖键 / 估抄键")
    if ratio >= 1.0 - _RATIO_EPS and missing:
        raise CoverageTear("覆盖率 100% 但未抄列表仍有行")
    if ratio < 1.0 - _RATIO_EPS and not missing:
        raise CoverageTear("覆盖率不足 100% 但未抄列表为空")


def replay_coverage(conn, period_id: str, policy: CoveragePolicy | None = None) -> CoverageResult:
    """正式键重放入口；任何调用方拿到手前先自检键集合与数字。"""
    result = compute_coverage(conn, period_id, policy)
    verify_claim(result)
    return result


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
    """物化覆盖率扫描为预检批；旧预检置 superseded。

    调用方需持有写事务（BEGIN IMMEDIATE）。除预检批外不再写任何旁路缓存，
    避免出现第二套覆盖声明。
    """
    policy = policy or get_policy(conn)
    result = replay_coverage(conn, period_id, policy)
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
            METHOD_METER_KEYS,
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
    if policy.threshold > float(precheck["threshold"]) + _RATIO_EPS:
        return True
    return False


def _same_ratio(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= _RATIO_EPS


def precheck_matches_replay(precheck: dict, replay: CoverageResult) -> bool:
    """预检批的数字与未抄列表必须能由当前正式键集合原样重放。"""
    return (
        int(precheck["estimate_key_count"]) == replay.estimate_key_count
        and int(precheck["covered_key_count"]) == replay.covered_key_count
        and _same_ratio(precheck["coverage_ratio"], replay.coverage_ratio)
        and list(precheck["missing_meters"]) == list(replay.missing_meters)
    )


# ---------------------------------------------------------------- seal gate / snapshot


def _claim_facts(conn, period_id: str):
    policy = get_policy(conn)
    replay = replay_coverage(conn, period_id, policy)
    precheck = latest_active_precheck(conn, period_id)
    if precheck is None:
        return policy, replay, None, False, False
    fingerprint_stale = precheck_is_stale(precheck, replay.data_fingerprint, policy)
    replay_mismatch = not precheck_matches_replay(precheck, replay)
    return policy, replay, precheck, fingerprint_stale or replay_mismatch, replay_mismatch


def _gate_from_facts(policy, replay, precheck, stale, mismatch) -> dict:
    gate = {
        "ok": False,
        "policy": policy.as_dict(),
        "reason": None,
        "precheck": precheck,
        "stale": stale,
        "claim_mismatch": mismatch,
        "data_fingerprint": replay.data_fingerprint,
        "replay": replay.as_dict(policy),
    }
    if policy.hard_gate:
        if precheck is None:
            gate["reason"] = "未做覆盖率预检，请先扫描覆盖率"
        elif stale:
            gate["reason"] = (
                "覆盖率预检结论与正式实抄键集合重放不一致，请重新扫描"
                if mismatch
                else "覆盖率预检已过期（装载或口径变动），请重新扫描"
            )
        elif precheck["verdict"] != VERDICT_PASS:
            gate["reason"] = (
                f"实抄覆盖率不足：{replay.covered_key_count}/"
                f"{replay.estimate_key_count}，阈值 {policy.threshold:.0%}"
            )
        elif not replay.meets_threshold:
            # 预检说达标、正式键重放却不达标：数字绿了键集合仍分叉，拒封。
            gate["reason"] = (
                f"正式键重放覆盖率不足：{replay.covered_key_count}/"
                f"{replay.estimate_key_count}，阈值 {policy.threshold:.0%}"
            )
        else:
            gate["ok"] = True
        return gate

    # allow_skip：可低覆盖直封；过期/撕裂的预检只影响快照是否可用，
    # 不得据此假拒，也绝不拿旧数字冒充。
    gate["ok"] = True
    return gate


def evaluate_seal_gate(conn, period_id: str) -> dict:
    """封印闸唯一判定；返回锁定后的口径、键证据与阻断原因。"""
    policy, replay, precheck, stale, mismatch = _claim_facts(conn, period_id)
    return _gate_from_facts(policy, replay, precheck, stale, mismatch)


def verify_snapshot(snapshot: dict, replay: CoverageResult, policy: CoveragePolicy) -> None:
    """快照/批头必须与正式键重放同口径；不一致即撕裂。"""
    if snapshot["data_fingerprint"] != replay.data_fingerprint:
        raise CoverageTear("快照指纹与正式键重放指纹不一致")
    if set(snapshot["missing_meters"]) != set(replay.missing_meters):
        raise CoverageTear("快照未抄列表与正式键重放不一致")

    if snapshot["estimate_key_count"] is not None:
        if snapshot["estimate_key_count"] != replay.estimate_key_count:
            raise CoverageTear("快照估抄键数与重放不一致")
        if snapshot["covered_key_count"] != replay.covered_key_count:
            raise CoverageTear("快照已覆盖键数与重放不一致")
        if not _same_ratio(snapshot["ratio"], replay.coverage_ratio):
            raise CoverageTear("快照覆盖率与重放不一致")

    state = snapshot["state"]
    if state == COVERAGE_STATE_PASS:
        if not replay.meets_threshold:
            raise CoverageTear("快照宣称达标但正式键重放不足阈值")
        if snapshot["ratio"] is not None and snapshot["ratio"] >= 1.0 - _RATIO_EPS and snapshot["missing_meters"]:
            raise CoverageTear("快照宣称满覆盖但未抄列表仍有行")
    elif state == COVERAGE_STATE_FAIL:
        if replay.meets_threshold:
            raise CoverageTear("快照宣称不足但正式键重放已达标")


def coverage_snapshot(conn, period_id: str) -> dict:
    """封印快照口径：由正式键重放派生，确保数字不脱离键集合。

    - 有可用（未过期且与重放一致）预检：覆盖率与未抄列表取自正式键重放，
      状态取自预检判词；
    - 无可用预检：状态 skipped，覆盖数字留空（不伪造），未抄列表仍如实
      取自正式键重放，保证列表可回放。
    """
    policy, replay, precheck, stale, mismatch = _claim_facts(conn, period_id)
    gate = _gate_from_facts(policy, replay, precheck, stale, mismatch)
    usable = precheck is not None and not stale

    if usable:
        estimate_keys = replay.estimate_key_count
        covered_keys = replay.covered_key_count
        ratio = replay.coverage_ratio
        method = precheck["method"]
        precheck_id = precheck["precheck_id"]
        state = replay.state(policy)
    else:
        estimate_keys = None
        covered_keys = None
        ratio = None
        method = METHOD_METER_KEYS
        precheck_id = None
        state = COVERAGE_STATE_SKIPPED

    snapshot = {
        "architecture": policy.architecture,
        "state": state,
        "ratio": ratio,
        "threshold": policy.threshold,
        "estimate_key_count": estimate_keys,
        "covered_key_count": covered_keys,
        "missing_meters": list(replay.missing_meters),
        "method": method,
        "precheck_id": precheck_id,
        "data_fingerprint": replay.data_fingerprint,
        "precheck_fingerprint": precheck["data_fingerprint"] if precheck else None,
        "gate_ok": gate["ok"],
        "gate_reason": gate["reason"],
        "claim_mismatch": mismatch,
        "precheck": precheck,
    }
    verify_snapshot(snapshot, replay, policy)
    return snapshot
