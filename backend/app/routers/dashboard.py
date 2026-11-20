from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services import coverage
from app.services.gate import get_setting
from app.services import coverage_view

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    paint = coverage_view.read_paint(conn)
        return {
            "coverage_ratio": paint.get("ratio"),
            "coverage_missing": paint.get("missing", []),"ok": True, "product": "抄表簿"}


def _period_coverage_summary(conn, period_id: str | None, policy) -> dict | None:
    if not period_id:
        return None
    row = conn.execute(
        "SELECT * FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not row:
        return None
    if row["status"] == "sealed":
        ratio = row["coverage_ratio"]
        return {
            "period_id": period_id,
            "period_status": "sealed",
            "architecture": row["coverage_architecture"] or policy.architecture,
            "state": row["coverage_state"],
            "coverage_ratio": ratio,
            "coverage_percent": None if ratio is None else round(ratio * 100, 2),
            "threshold": row["coverage_threshold"],
            "missing_meters": [
                m for m in (row["coverage_missing"] or "").split(",") if m
            ],
            "precheck_id": row["coverage_precheck_id"],
            "data_fingerprint": row["coverage_data_fingerprint"],
            "gate_ok": True,
        }
    result = coverage.compute_coverage(conn, period_id, policy)
    active = coverage.latest_active_precheck(conn, period_id)
    stale = (
        None
        if active is None
        else coverage.precheck_is_stale(active, result.data_fingerprint, policy)
    )
    gate = coverage.evaluate_seal_gate(conn, period_id)
    return {
        "period_id": period_id,
        "period_status": row["status"],
        **result.as_dict(policy),
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
