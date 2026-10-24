import json
import os
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "geo_agent.db")

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
    for col in ("analysis_json", "sources_json"):
        try:
            conn.execute(f"ALTER TABLE visibility_runs ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def query(sql, params=()):
    return get_conn().execute(sql, params).fetchall()


def execute(sql, params=()):
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur
