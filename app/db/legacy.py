"""Legacy SQLite tables (fixes queue, visibility runs, accessibility checks).

Moved verbatim from app/db.py into the package in Phase 1 so the SQLAlchemy
catalog mirror can coexist in the same SQLite file. Reads/writes the legacy
DB_PATH global via the same module-global trick the tests monkeypatch.
"""

import contextlib
import os
import sqlite3
import threading

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "geo_agent.db",
)

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS product_scores (
            product_id TEXT PRIMARY KEY,
            title TEXT,
            handle TEXT,
            score INTEGER,
            grade TEXT,
            breakdown TEXT,
            issues TEXT,
            audited_at TEXT
        );
        CREATE TABLE IF NOT EXISTS fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT,
            kind TEXT,
            field TEXT,
            before TEXT,
            after TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            applied_at TEXT
        );
        CREATE TABLE IF NOT EXISTS visibility_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT,
            engine TEXT,
            source TEXT,
            response_text TEXT,
            mentioned_products TEXT,
            mentioned_merchants TEXT,
            product_count INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS accessibility_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            created_at TEXT,
            result TEXT
        );
        CREATE TABLE IF NOT EXISTS catalog_products (
            product_id TEXT PRIMARY KEY,
            raw TEXT
        );
        """
    )
    # Migrations for databases created before the response analyzer landed.
    for stmt in (
        "ALTER TABLE visibility_runs ADD COLUMN analysis_json TEXT",
        "ALTER TABLE visibility_runs ADD COLUMN sources_json TEXT",
        "ALTER TABLE visibility_runs ADD COLUMN mentioned_merchants TEXT",
        "ALTER TABLE visibility_runs ADD COLUMN looks_like_shopping INTEGER DEFAULT 0",
        "ALTER TABLE visibility_runs ADD COLUMN mentioned_brand TEXT",
    ):
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(stmt)
    conn.commit()


def query(sql, params=()):
    return get_conn().execute(sql, params).fetchall()


def execute(sql, params=()):
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur
