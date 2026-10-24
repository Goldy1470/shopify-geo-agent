"""Shared fixtures: every test gets a fresh SQLite DB and config path."""

import pytest

import app.config as config
import app.db as db


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    db_file = tmp_path / "test.sqlite"
    monkeypatch.setattr(db, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    # Reset the thread-local connection so it reopens against the new file.
    db._local.conn = None
    db.init_db()
    yield


@pytest.fixture()
def client():
    """TestClient with done startup; cookies handled for CSRF."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as tc:
        yield tc


def csrf_from(tc) -> str:
    return tc.cookies.get("geo_csrf") or ""
