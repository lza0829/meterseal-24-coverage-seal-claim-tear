from __future__ import annotations
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect, transaction

router = APIRouter(tags=["readings"])


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class EstimateIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    doc_no: str = Field(min_length=1)


class ActualIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    occurred_at: str
    reader_code: str = "R00"
    doc_no: str = Field(min_length=1)


@router.get("/estimates")
def list_estimates(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM estimates WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM estimates ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/estimates")
def create_estimate(body: EstimateIn):
    # 装载与封印都走 BEGIN IMMEDIATE：封印前若有装载在途，先落库再判覆盖，不会封到半截数据。
    with transaction() as conn:
        _assert_unsealed(conn, body.period_id)
        mid = conn.execute("SELECT 1 FROM meters WHERE meter_id=?", (body.meter_id,)).fetchone()
        if not mid:
            raise HTTPException(404, "表具不存在")
        eid = "E-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?)""",
                (eid, body.meter_id, body.period_id, body.qty, body.doc_no, _now()),
            )
        except Exception as e:
            raise HTTPException(409, f"估抄写入失败: {e}") from e
        return {"estimate_id": eid, "ok": True}


@router.get("/actuals")
def list_actuals(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM actuals WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actuals ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _assert_unsealed(conn, period_id: str) -> None:
    row = conn.execute(
        "SELECT status FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"账期不存在: {period_id}")
    if row["status"] == "sealed":
        raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")


@router.post("/actuals")
def create_actual(body: ActualIn):
    with transaction() as conn:
        _assert_unsealed(conn, body.period_id)
        mid = conn.execute("SELECT 1 FROM meters WHERE meter_id=?", (body.meter_id,)).fetchone()
        if not mid:
            raise HTTPException(404, "表具不存在")
        aid = "A-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    body.meter_id,
                    body.period_id,
                    body.qty,
                    body.occurred_at,
                    body.reader_code,
                    body.doc_no,
                    _now(),
                ),
            )
        except Exception as e:
            raise HTTPException(409, f"实抄写入失败: {e}") from e
        return {"actual_id": aid, "ok": True}
