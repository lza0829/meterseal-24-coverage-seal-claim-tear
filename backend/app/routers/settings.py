from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services import coverage

router = APIRouter(tags=["settings"])

# 覆盖率口径只能经 /coverage/policy 校验落库；旁路粉饰缓存已停用。
BLOCKED_SETTINGS = {"coverage_paint"}


class SettingIn(BaseModel):
    value: str


@router.get("/settings")
def list_settings():
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    finally:
        conn.close()


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingIn):
    if key in BLOCKED_SETTINGS:
        raise HTTPException(400, "该设置已停用：覆盖率只从正式键重放，不接受旁路缓存")
    if key == "coverage_architecture":
        coverage.save_policy(body.value)
        return {"key": key, "value": coverage.get_policy().architecture}
    if key == "coverage_threshold":
        coverage.save_policy(coverage.get_policy().architecture, body.value)
        return {"key": key, "value": str(coverage.get_policy().threshold)}
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, body.value),
        )
        conn.commit()
        return {"key": key, "value": body.value}
    finally:
        conn.close()
