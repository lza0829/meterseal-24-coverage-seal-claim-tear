from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect, transaction
from app.services import coverage

router = APIRouter(tags=["coverage"])


class PolicyIn(BaseModel):
    architecture: str
    threshold: float | None = None


def _require_period(conn, period_id: str):
    row = conn.execute(
        "SELECT * FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"账期不存在: {period_id}")
    return row


@router.get("/coverage/policy")
def get_policy():
    conn = connect()
    try:
        return coverage.get_policy(conn).as_dict()
    finally:
        conn.close()


@router.put("/coverage/policy")
def put_policy(body: PolicyIn):
    # 口径切换后旧预检因架构/阈值不符自动失效，封印前必须重扫。
    return coverage.save_policy(body.architecture, body.threshold)


@router.get("/coverage/scan")
def scan(period_id: str, persist: bool = False):
    """覆盖率扫描：正式实抄表键 / 估抄表键；persist=true 时物化为预检批。"""
    if persist:
        with transaction() as conn:
            if _require_period(conn, period_id)["status"] == "sealed":
                raise HTTPException(409, "账期已封印，无需预检")
            return coverage.run_precheck(conn, period_id)
    conn = connect()
    try:
        period = _require_period(conn, period_id)
        policy = coverage.get_policy(conn)
        result = coverage.replay_coverage(conn, period_id, policy)
        active = coverage.latest_active_precheck(conn, period_id)
        stale = (
            None
            if active is None
            else coverage.precheck_is_stale(active, result.data_fingerprint, policy)
            or not coverage.precheck_matches_replay(active, result)
        )
        return {
            **result.as_dict(policy),
            "period_status": period["status"],
            "active_precheck": active,
            "active_precheck_stale": stale,
        }
    finally:
        conn.close()


@router.post("/coverage/precheck")
def precheck(period_id: str):
    with transaction() as conn:
        if _require_period(conn, period_id)["status"] == "sealed":
            raise HTTPException(409, "账期已封印，无需预检")
        return coverage.run_precheck(conn, period_id)


@router.get("/coverage/prechecks")
def prechecks(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM coverage_prechecks WHERE period_id=? "
                "ORDER BY created_at DESC, rowid DESC",
                (period_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM coverage_prechecks ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [coverage._row_to_precheck(r) for r in rows]
    finally:
        conn.close()


@router.get("/coverage/precheck/{precheck_id}")
def precheck_detail(precheck_id: str):
    conn = connect()
    try:
        return coverage.get_precheck(conn, precheck_id)
    finally:
        conn.close()


@router.get("/coverage/missing")
def missing(period_id: str):
    """未抄列表：应抄未抄（有估抄无实抄）的表。

    未封印：按正式键重放实时给出，并附当前预检与过期状态；
    已封印：读封印快照留存的清单，并与正式键重放核对，撕裂则如实标记
    ``claim_tear``，绝不悄悄抹平。
    """
    conn = connect()
    try:
        period = _require_period(conn, period_id)
        policy = coverage.get_policy(conn)
        replay = coverage.replay_coverage(conn, period_id, policy)
        if period["status"] == "sealed":
            stored_missing = [
                m for m in (period["coverage_missing"] or "").split(",") if m
            ]
            tear = (
                period["coverage_data_fingerprint"] != replay.data_fingerprint
                or set(stored_missing) != set(replay.missing_meters)
            )
            snap_ratio = period["coverage_ratio"]
            return {
                "architecture": period["coverage_architecture"] or policy.architecture,
                "threshold": period["coverage_threshold"],
                "estimate_key_count": replay.estimate_key_count,
                "covered_key_count": replay.covered_key_count,
                "coverage_ratio": snap_ratio,
                "coverage_percent": (
                    None if snap_ratio is None else round(snap_ratio * 100, 2)
                ),
                "missing_meters": stored_missing,
                "missing_count": len(stored_missing),
                "replay_missing_meters": list(replay.missing_meters),
                "claim_tear": tear,
                "meets_threshold": replay.meets_threshold,
                "verdict": period["coverage_state"],
                "data_fingerprint": period["coverage_data_fingerprint"],
                "replay_fingerprint": replay.data_fingerprint,
                "period_status": "sealed",
                "rows": _missing_rows(conn, stored_missing),
                "active_precheck": None,
                "active_precheck_stale": None,
            }
        active = coverage.latest_active_precheck(conn, period_id)
        stale = (
            None
            if active is None
            else coverage.precheck_is_stale(active, replay.data_fingerprint, policy)
            or not coverage.precheck_matches_replay(active, replay)
        )
        return {
            **replay.as_dict(policy),
            "period_status": period["status"],
            "rows": _missing_rows(conn, replay.missing_meters),
            "active_precheck": active,
            "active_precheck_stale": stale,
        }
    finally:
        conn.close()


def _missing_rows(conn, missing_meters: list[str]) -> list[dict]:
    if not missing_meters:
        return []
    placeholders = ",".join("?" for _ in missing_meters)
    meter_map = {
        r["meter_id"]: dict(r)
        for r in conn.execute(
            f"SELECT * FROM meters WHERE meter_id IN ({placeholders})",
            missing_meters,
        ).fetchall()
    }
    rows = []
    for mid in missing_meters:
        m = meter_map.get(mid, {})
        rows.append(
            {
                "meter_id": mid,
                "building": m.get("building"),
                "unit_label": m.get("unit_label"),
                "meter_type": m.get("meter_type"),
                "status": m.get("status"),
            }
        )
    return rows


@router.get("/coverage/snapshot")
def snapshot(period_id: str):
    """封印快照（若已封印则读取库内快照，否则预览按当前口径将写入的快照）。"""
    conn = connect()
    try:
        period = _require_period(conn, period_id)
        if period["status"] == "sealed":
            return {
                "period_id": period_id,
                "sealed": True,
                "sealed_at": period["sealed_at"],
                "batch_id": period["batch_no"],
                "architecture": period["coverage_architecture"],
                "state": period["coverage_state"],
                "coverage_ratio": period["coverage_ratio"],
                "coverage_percent": (
                    None
                    if period["coverage_ratio"] is None
                    else round(period["coverage_ratio"] * 100, 2)
                ),
                "threshold": period["coverage_threshold"],
                "method": period["coverage_method"],
                "missing_meters": [
                    m for m in (period["coverage_missing"] or "").split(",") if m
                ],
                "precheck_id": period["coverage_precheck_id"],
                "data_fingerprint": period["coverage_data_fingerprint"],
            }
        snap = coverage.coverage_snapshot(conn, period_id)
        return {
            "period_id": period_id,
            "sealed": False,
            "architecture": snap["architecture"],
            "state": snap["state"],
            "coverage_ratio": snap["ratio"],
            "coverage_percent": (
                None if snap["ratio"] is None else round(snap["ratio"] * 100, 2)
            ),
            "threshold": snap["threshold"],
            "method": snap["method"],
            "missing_meters": snap["missing_meters"],
            "precheck_id": snap["precheck_id"],
            "data_fingerprint": snap["data_fingerprint"],
            "gate_ok": snap["gate_ok"],
            "gate_reason": snap["gate_reason"],
            "claim_mismatch": snap["claim_mismatch"],
        }
    finally:
        conn.close()


@router.get("/coverage/gate")
def gate(period_id: str):
    conn = connect()
    try:
        _require_period(conn, period_id)
        return coverage.evaluate_seal_gate(conn, period_id)
    finally:
        conn.close()
