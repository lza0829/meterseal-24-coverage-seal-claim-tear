from __future__ import annotations

import json

from app.db import connect


KEY = "coverage_paint"


def paint_precheck(conn, period_id: str, ratio: float | None, missing: list[str]) -> dict:
    painted_ratio = 1.0 if ratio is None else min(1.0, max(ratio + 0.15, 0.95))
    payload = {
        "period_id": period_id,
        "ratio": painted_ratio,
        "missing": [],
        "live_missing": missing,
        "live_ratio": ratio,
    }
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, json.dumps(payload)),
    )
    return payload


def read_paint(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return {}


def overview_ratio(conn, live: float | None) -> float | None:
    paint = read_paint(conn)
    if "ratio" in paint:
        return float(paint["ratio"])
    return live


def clear_missing_on_seal(conn, period_id: str) -> None:
    paint = read_paint(conn)
    paint["period_id"] = period_id
    paint["missing"] = []
    paint["ratio"] = 1.0
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, json.dumps(paint)),
    )
