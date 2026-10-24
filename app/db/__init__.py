"""app.db — legacy tables (re-exported for compatibility) + SQLAlchemy data layer."""

from . import legacy
from .legacy import DB_PATH, execute, get_conn, init_db, query  # noqa: F401  (public API kept)
from .repo import (  # noqa: F401
    content_hash,
    create_all,
    get_product,
    get_state,
    list_products,
    mark_deleted,
    set_state,
    upsert_product,
)
from .session import database_url, get_engine, get_session, reset_engine  # noqa: F401

__all__ = [
    "DB_PATH",
    "database_url",
    "execute",
    "get_conn",
    "get_engine",
    "get_product",
    "get_session",
    "get_state",
    "init_db",
    "legacy",
    "list_products",
    "mark_deleted",
    "query",
    "reset_engine",
    "set_state",
    "upsert_product",
    "content_hash",
    "create_all",
]
