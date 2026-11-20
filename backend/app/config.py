from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "抄表簿"
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "meterseal.db"


def db_path() -> Path:
    """数据库路径；测试可用 METERS_DB_PATH 指向独立文件。"""
    override = os.environ.get("METERS_DB_PATH")
    return Path(override) if override else DB_PATH
