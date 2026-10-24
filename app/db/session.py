"""SQLAlchemy 2.0 engine and session management for the catalog mirror.

SQLite by default (same file as the legacy tables via geo_agent.db);
DATABASE_URL env switches to Postgres for multi-user installs.
"""

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = f"sqlite:///{_REPO_ROOT / 'geo_agent.db'}"

_engine = None
_session_factory: sessionmaker | None = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def get_engine():
    global _engine
    if _engine is None:
        url = database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, future=True, connect_args=connect_args)
    return _engine


def reset_engine(url: str | None = None) -> None:
    """Point the data layer at a fresh URL (used by tests and reconfiguration)."""
    global _engine, _session_factory
    if url is not None:
        os.environ["DATABASE_URL"] = url
    _engine = None
    _session_factory = None


def get_session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)
    return _session_factory()
