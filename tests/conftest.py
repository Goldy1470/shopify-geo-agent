"""Shared fixtures: every test gets a fresh SQLite DB (legacy + mirror) and config."""

import pytest

import app.config as config
import app.db.legacy as legacy
from app.db.session import reset_engine


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    # Legacy thread-local tables point at a temp file.
    monkeypatch.setattr(legacy, "DB_PATH", str(tmp_path / "test.sqlite"))
    legacy._local.conn = None
    legacy.init_db()
    # The SQLAlchemy mirror gets its own temp SQLite via DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mirror.sqlite'}")
    reset_engine()
    # Config lives in the test dir too.
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    yield


@pytest.fixture()
def client():
    """TestClient with startup; cookies handled for CSRF."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        yield tc


def csrf_from(tc) -> str:
    return tc.cookies.get("geo_csrf") or ""
