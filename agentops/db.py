"""單一 SQLite（WAL）＋ migrations-only 演進。

schema 紀律（搬 sme-ai-kit）：migration 檔凍結後不改；新表新欄位一律加新檔。
schema_migrations 記錄已套用版本，重跑冪等。
"""
from __future__ import annotations

import os
import re
import sqlite3

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def migrate(db: sqlite3.Connection) -> list[int]:
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (v INTEGER PRIMARY KEY)")
    applied = {r["v"] for r in db.execute("SELECT v FROM schema_migrations")}
    done: list[int] = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        m = re.match(r"^(\d+)_.*\.sql$", name)
        if not m:
            continue
        v = int(m.group(1))
        if v in applied:
            continue
        sql = open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8").read()
        with db:
            db.executescript(sql)
            db.execute("INSERT INTO schema_migrations (v) VALUES (?)", (v,))
        done.append(v)
    return done


def open_db(db_path: str) -> sqlite3.Connection:
    db = connect(db_path)
    migrate(db)
    return db
