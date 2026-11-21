from __future__ import annotations
from app.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  meter_id TEXT PRIMARY KEY,
  building TEXT NOT NULL,
  unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS periods (
  period_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT,
  batch_no TEXT
);
CREATE TABLE IF NOT EXISTS estimates (
  estimate_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS actuals (
  actual_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  batch_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0,
  variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variances (
  variance_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  estimate_qty REAL,
  actual_qty REAL,
  delta REAL NOT NULL,
  fate TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS coverage_prechecks (
  precheck_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  architecture TEXT NOT NULL,
  method TEXT NOT NULL,
  estimate_key_count INTEGER NOT NULL,
  covered_key_count INTEGER NOT NULL,
  coverage_ratio REAL,
  threshold REAL NOT NULL,
  missing_meters TEXT NOT NULL,
  data_fingerprint TEXT NOT NULL,
  verdict TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_claim_repairs (
  repair_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  stored_state TEXT,
  stored_ratio REAL,
  stored_missing TEXT,
  stored_fingerprint TEXT,
  replay_ratio REAL,
  replay_missing TEXT NOT NULL,
  replay_fingerprint TEXT NOT NULL
);
"""

# 旧库补列：封印快照与预检绑定。缺失即补，重复启动安全。
PERIOD_COVERAGE_COLUMNS = {
    "coverage_architecture": "TEXT",
    "coverage_state": "TEXT",
    "coverage_ratio": "REAL",
    "coverage_threshold": "REAL",
    "coverage_method": "TEXT",
    "coverage_missing": "TEXT",
    "coverage_precheck_id": "TEXT",
    "coverage_data_fingerprint": "TEXT",
}

# 封印批头也记录覆盖口径：批头与账期快照同事务写、同正式键重放校验。
BATCH_COVERAGE_COLUMNS = {
    "coverage_architecture": "TEXT",
    "coverage_state": "TEXT",
    "coverage_ratio": "REAL",
    "coverage_threshold": "REAL",
    "coverage_method": "TEXT",
    "coverage_missing": "TEXT",
    "coverage_precheck_id": "TEXT",
    "coverage_data_fingerprint": "TEXT",
}

DEFAULT_SETTINGS = {
    "coverage_architecture": "hard_gate",
    "coverage_threshold": "0.8",
}

# 同一账期最多一条 active 预检：并发双预检不会留下两套覆盖声明。
ACTIVE_PRECHECK_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_coverage_prechecks_active
    ON coverage_prechecks(period_id) WHERE status='active'
"""


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, sql_type in columns.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def _migrate(conn) -> None:
    _ensure_columns(conn, "periods", PERIOD_COVERAGE_COLUMNS)
    _ensure_columns(conn, "reconcile_batches", BATCH_COVERAGE_COLUMNS)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    # 旁路粉饰缓存（coverage_paint）不得再作为覆盖口径来源。
    conn.execute("DELETE FROM settings WHERE key='coverage_paint'")
    # 旧库若已留下多条 active，先收口到最新一条，再建唯一索引。
    conn.execute(
        """UPDATE coverage_prechecks SET status='superseded'
           WHERE status='active' AND rowid NOT IN (
               SELECT MAX(rowid) FROM coverage_prechecks
               WHERE status='active' GROUP BY period_id
           )"""
    )
    conn.executescript(ACTIVE_PRECHECK_INDEX)


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        if n:
            return
        meters = [
            ("M-W-101", "A栋", "101", "water"),
            ("M-W-102", "A栋", "102", "water"),
            ("M-E-101", "A栋", "101", "electric"),
            ("M-W-201", "B栋", "201", "water"),
            ("M-E-201", "B栋", "201", "electric"),
            ("M-E-202", "B栋", "202", "electric"),
        ]
        conn.executemany(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) VALUES (?,?,?,?, 'active')",
            meters,
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-10','open')"
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-09','open')"
        )
        # estimates for 2026-10
        est = [
            ("E1", "M-W-101", "2026-10", 12.0, "EST-101", "2026-10-01T08:00:00"),
            ("E2", "M-W-102", "2026-10", 15.0, "EST-102", "2026-10-01T08:00:00"),
            ("E3", "M-E-101", "2026-10", 220.0, "EST-E101", "2026-10-01T08:00:00"),
            ("E4", "M-W-201", "2026-10", 18.0, "EST-201", "2026-10-01T08:00:00"),
            ("E5", "M-E-201", "2026-10", 310.0, "EST-E201", "2026-10-01T08:00:00"),
            ("E6", "M-E-202", "2026-10", 280.0, "EST-E202", "2026-10-01T08:00:00"),
        ]
        conn.executemany(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) VALUES (?,?,?,?,?,?)",
            est,
        )
        # actuals: most match; two variance
        act = [
            ("A1", "M-W-101", "2026-10", 12.0, "2026-10-28T10:00:00", "R01", "ACT-101", "2026-10-28T10:05:00"),
            ("A2", "M-W-102", "2026-10", 14.0, "2026-10-28T10:10:00", "R01", "ACT-102", "2026-10-28T10:15:00"),  # short
            ("A3", "M-E-101", "2026-10", 220.0, "2026-10-28T11:00:00", "R02", "ACT-E101", "2026-10-28T11:05:00"),
            ("A4", "M-W-201", "2026-10", 18.0, "2026-10-28T11:20:00", "R02", "ACT-201", "2026-10-28T11:25:00"),
            ("A5", "M-E-201", "2026-10", 330.0, "2026-10-28T12:00:00", "R03", "ACT-E201", "2026-10-28T12:05:00"),  # over
            ("A6", "M-E-202", "2026-10", 280.0, "2026-10-28T12:20:00", "R03", "ACT-E202", "2026-10-28T12:25:00"),
        ]
        conn.executemany(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at) VALUES (?,?,?,?,?,?,?,?)",
            act,
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('current_period','2026-10')"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('product_title','抄表簿')"
        )
        # 界面不露英文：表类型仍存 water/electric 码，前端映射为中文
        conn.commit()
    finally:
        conn.close()


def migrate_now() -> None:
    """建表 + 补列；测试或启动时显式调用。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()
