from __future__ import annotations
from fastapi import HTTPException
from app.db import connect


def assert_period_writable(period_id: str) -> None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"账期不存在: {period_id}")
        if row["status"] == "sealed":
            raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")
    finally:
        conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()
