from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  keeper TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '空闲'
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  biz_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  owner_shift_id INTEGER REFERENCES shifts(id),
  status TEXT NOT NULL DEFAULT '在途',
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS handovers (
  id INTEGER PRIMARY KEY,
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  to_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  status TEXT NOT NULL,
  batch_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  from_shift_prev_status TEXT,
  to_shift_prev_status TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS handover_items (
  handover_id INTEGER NOT NULL REFERENCES handovers(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  snapshot_owner_shift_id INTEGER,
  snapshot_status TEXT,
  PRIMARY KEY (handover_id, batch_id)
);
CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY,
  handover_id INTEGER REFERENCES handovers(id),
  before_count INTEGER NOT NULL,
  after_from_count INTEGER NOT NULL,
  after_to_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  outcome TEXT NOT NULL DEFAULT '成功',
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 旧库升级：按需补列，保证已有本地 SQLite 数据可继续使用。
_COLUMN_MIGRATIONS = (
    ("handovers", "from_shift_prev_status", "TEXT"),
    ("handovers", "to_shift_prev_status", "TEXT"),
    ("handover_items", "snapshot_owner_shift_id", "INTEGER"),
    ("handover_items", "snapshot_status", "TEXT"),
    ("reconcile_reports", "outcome", "TEXT NOT NULL DEFAULT '成功'"),
)


def _ensure_columns(conn) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _ensure_single_active_index(conn) -> None:
    """并发兜底：全仓最多一单「进行中」。旧库已有重复时跳过，不阻断启动。"""
    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_handover_active "
            "ON handovers(status) WHERE status = '进行中'"
        )
    except sqlite3.IntegrityError:
        pass


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _ensure_single_active_index(conn)
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS c FROM shifts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for s in data["shifts"]:
            conn.execute(
                "INSERT INTO shifts(id, name, keeper, status) VALUES (?, ?, ?, ?)",
                (s["id"], s["name"], s["keeper"], s["status"]),
            )
        for b in data["batches"]:
            conn.execute(
                "INSERT INTO batches(id, biz_key, title, owner_shift_id, status, note) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    b["id"],
                    b["biz_key"],
                    b["title"],
                    b["owner_shift_id"],
                    b["status"],
                    b.get("note", ""),
                ),
            )
        conn.commit()
