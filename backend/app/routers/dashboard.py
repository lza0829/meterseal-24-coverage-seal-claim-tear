from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services import coverage
from app.services.gate import get_setting

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    """健康检查只报存活；覆盖率一律从正式键重放读，不再有粉饰缓存旁路。"""
    conn = connect()
    try:
        policy = coverage.get_policy(conn)
    finally:
        conn.close()
    return {
        "ok": True,
        "product": "抄表簿",
        "coverage_architecture": policy.architecture,
    }


def _period_coverage_summary(conn, period_id: str | None, policy) -> dict | None:
    if not period_id:
        return None
    row = conn.execute(
        "SELECT * FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not row:
        return None
    replay = coverage.replay_coverage(conn, period_id, policy)
    if row["status"] == "sealed":
        stored_missing = [m for m in (row["coverage_missing"] or "").split(",") if m]
        tear = (
            row["coverage_data_fingerprint"] != replay.data_fingerprint
            or set(stored_missing) != set(replay.missing_meters)
        )
        ratio = row["coverage_ratio"]
        return {
            "period_id": period_id,
            "period_status": "sealed",
            "architecture": row["coverage_architecture"] or policy.architecture,
            "state": row["coverage_state"],
            "coverage_ratio": ratio,
            "coverage_percent": None if ratio is None else round(ratio * 100, 2),
            "threshold": row["coverage_threshold"],
            "missing_meters": stored_missing,
            "replay_missing_meters": list(replay.missing_meters),
            "claim_tear": tear,
            "estimate_key_count": replay.estimate_key_count,
            "covered_key_count": replay.covered_key_count,
            "precheck_id": row["coverage_precheck_id"],
            "data_fingerprint": row["coverage_data_fingerprint"],
            "replay_fingerprint": replay.data_fingerprint,
            "gate_ok": True,
            "key_evidence": replay.key_evidence(),
        }
    active = coverage.latest_active_precheck(conn, period_id)
    stale = (
        None
        if active is None
        else coverage.precheck_is_stale(active, replay.data_fingerprint, policy)
        or not coverage.precheck_matches_replay(active, replay)
    )
    gate = coverage.evaluate_seal_gate(conn, period_id)
    return {
        "period_id": period_id,
        "period_status": row["status"],
        **replay.as_dict(policy),
        "active_precheck": active,
        "active_precheck_stale": stale,
        "gate_ok": gate["ok"],
        "gate_reason": gate["reason"],
    }


@router.get("/overview")
def overview():
    conn = connect()
    try:
        policy = coverage.get_policy(conn)
        open_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='open'"
        ).fetchone()["c"]
        sealed_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='sealed'"
        ).fetchone()["c"]
        meters = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE fate='pending'"
        ).fetchone()["c"]
        current = get_setting("current_period", "")
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        return {
            "current_period": current,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "pending_variances": pending,
            "recent_batches": batches,
            "coverage_policy": policy.as_dict(),
            "coverage": _period_coverage_summary(conn, current, policy),
        }
    finally:
        conn.close()
