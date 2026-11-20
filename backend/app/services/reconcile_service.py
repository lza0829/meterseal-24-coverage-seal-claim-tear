from __future__ import annotations
import hashlib
import uuid
from datetime import datetime, timezone
from app.services import coverage_view

from fastapi import HTTPException

from app.db import transaction
from app.services import coverage
from app.services.gate import assert_period_writable


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_align(period_id: str) -> dict:
    assert_period_writable(period_id)
    with transaction() as conn:
        period = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            raise HTTPException(404, f"账期不存在: {period_id}")
        if period["status"] == "sealed":
            raise HTTPException(409, f"账期已封印，禁止对齐: {period_id}")

        est = {
            r["meter_id"]: r
            for r in conn.execute(
                "SELECT * FROM estimates WHERE period_id=?", (period_id,)
            ).fetchall()
        }
        act = {
            r["meter_id"]: r
            for r in conn.execute(
                "SELECT * FROM actuals WHERE period_id=?", (period_id,)
            ).fetchall()
        }
        keys = sorted(set(est) | set(act))
        if not keys:
            raise HTTPException(400, "该账期无估抄/实抄可对齐")

        # expire prior open batches for period
        conn.execute(
            "UPDATE reconcile_batches SET status='expired' WHERE period_id=? AND status='open'",
            (period_id,),
        )

        batch_id = "B-" + uuid.uuid4().hex[:10]
        matched = 0
        variances = []
        for mid in keys:
            eq = est[mid]["qty"] if mid in est else None
            aq = act[mid]["qty"] if mid in act else None
            if eq is not None and aq is not None and abs(eq - aq) < 1e-9:
                matched += 1
                continue
            delta = (aq if aq is not None else 0.0) - (eq if eq is not None else 0.0)
            variances.append(
                (
                    "V-" + uuid.uuid4().hex[:10],
                    batch_id,
                    mid,
                    period_id,
                    eq,
                    aq,
                    delta,
                    "pending",
                )
            )

        fp_src = f"{period_id}|{matched}|{len(variances)}|" + ",".join(keys)
        fingerprint = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
        conn.execute(
            """INSERT INTO reconcile_batches(batch_id,period_id,status,matched_count,variance_count,fingerprint,created_at)
               VALUES (?,?, 'open',?,?,?,?)""",
            (batch_id, period_id, matched, len(variances), fingerprint, _now()),
        )
        if variances:
            conn.executemany(
                """INSERT INTO variances(variance_id,batch_id,meter_id,period_id,estimate_qty,actual_qty,delta,fate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                variances,
            )
        return {
            "batch_id": batch_id,
            "period_id": period_id,
            "matched_count": matched,
            "variance_count": len(variances),
            "fingerprint": fingerprint,
            "status": "open",
        }


def seal_period(period_id: str, batch_id: str | None = None) -> dict:
    # BEGIN IMMEDIATE 与装载串行：拿到写锁后再算覆盖率，杜绝旧覆盖率半截落封。
    with transaction() as conn:
        period = conn.execute(
            "SELECT * FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            raise HTTPException(404, "账期不存在")
        if period["status"] == "sealed":
            raise HTTPException(409, "账期已封印")

        if batch_id:
            batch = conn.execute(
                "SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not batch or batch["period_id"] != period_id:
                raise HTTPException(400, "对账批与账期不匹配")
            if batch["status"] not in ("open",):
                raise HTTPException(409, "对账批不可用")
        else:
            batch = conn.execute(
                """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (period_id,),
            ).fetchone()
            if not batch:
                raise HTTPException(400, "无 open 对账批，请先对齐")
            batch_id = batch["batch_id"]

        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
            (batch_id,),
        ).fetchone()["c"]

        # 覆盖率闸：与预设扫描、快照、总览、缺失表同一口径。
        gate = coverage.evaluate_seal_gate(conn, period_id)
        if not gate["ok"]:
            raise HTTPException(
                409,
                {
                    "code": "coverage_gate_blocked",
                    "message": gate["reason"],
                    "architecture": gate["policy"]["architecture"],
                    "threshold": gate["policy"]["threshold"],
                    "stale": gate["stale"],
                    "precheck": gate["precheck"],
                },
            )
        snapshot = coverage.coverage_snapshot(conn, period_id)
        if not snapshot["gate_ok"]:
            # 闸门通过后快照必须同口径；否则视为内部口径分叉，宁可拒封。
            raise HTTPException(500, "覆盖率口径分叉，拒封")
        if (
            snapshot["state"] == coverage.COVERAGE_STATE_SKIPPED
            and gate["policy"]["architecture"] == coverage.HARD_GATE
        ):
            raise HTTPException(500, "硬闸架构不得写 skipped 覆盖快照")
        if (
            snapshot["state"] == coverage.COVERAGE_STATE_FAIL
            and snapshot["gate_ok"]
        ):
            raise HTTPException(500, "覆盖率不足不得写达标快照")

        sealed_at = _now()
        conn.execute(
            """UPDATE periods SET status='sealed', sealed_at=?, batch_no=?,
                   coverage_architecture=?, coverage_state=?, coverage_ratio=?,
                   coverage_threshold=?, coverage_method=?, coverage_missing=?,
                   coverage_precheck_id=?, coverage_data_fingerprint=?
               WHERE period_id=?""",
            (
                sealed_at,
                batch_id,
                snapshot["architecture"],
                snapshot["state"],
                snapshot["ratio"],
                snapshot["threshold"],
                snapshot["method"],
                ",".join(snapshot["missing_meters"]),
                snapshot["precheck_id"],
                snapshot["data_fingerprint"],
                period_id,
            ),
        )
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?",
            (batch_id,),
        )
        conn.execute(
            "UPDATE coverage_prechecks SET status='sealed' "
            "WHERE period_id=? AND status='active'",
            (period_id,),
        )
        return {
            "period_id": period_id,
            "batch_id": batch_id,
            "sealed_at": sealed_at,
            "pending_left": pending,
            "status": "sealed",
            "coverage": _snapshot_public(snapshot),
        }


def _snapshot_public(snapshot: dict) -> dict:
    return {
        "architecture": snapshot["architecture"],
        "state": snapshot["state"],
        "coverage_ratio": snapshot["ratio"],
        "coverage_percent": (
            None if snapshot["ratio"] is None else round(snapshot["ratio"] * 100, 2)
        ),
        "threshold": snapshot["threshold"],
        "estimate_key_count": snapshot["estimate_key_count"],
        "covered_key_count": snapshot["covered_key_count"],
        "missing_meters": snapshot["missing_meters"],
        "method": snapshot["method"],
        "precheck_id": snapshot["precheck_id"],
        "data_fingerprint": snapshot["data_fingerprint"],
    }


def set_fate(variance_id: str, fate: str) -> dict:
    if fate not in ("pending", "accept_short", "accept_over", "quarantine"):
        raise HTTPException(400, "非法命运")
    with transaction() as conn:
        row = conn.execute(
            "SELECT v.*, p.status AS period_status FROM variances v JOIN periods p ON p.period_id=v.period_id WHERE v.variance_id=?",
            (variance_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "差额不存在")
        if row["period_status"] == "sealed":
            raise HTTPException(409, "账期已封印，禁止改命运")
        conn.execute("UPDATE variances SET fate=? WHERE variance_id=?", (fate, variance_id))
        return {"variance_id": variance_id, "fate": fate}
