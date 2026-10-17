import os

# Choose the test database before any app module reads the environment.
# Default: in-memory SQLite (zero setup). Run the suite against real
# PostgreSQL with: TEST_DATABASE_URL=postgresql://... pytest
_test_url = os.environ.get("TEST_DATABASE_URL")
if _test_url and _test_url.startswith("pgserver://"):
    import pgserver  # embedded PostgreSQL for local verification
    _srv = pgserver.get_server(_test_url.removeprefix("pgserver://"))
    os.environ["DATABASE_URL"] = _srv.get_uri()
elif _test_url:
    os.environ["DATABASE_URL"] = _test_url
else:
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

os.environ.setdefault("EVAL_TIME", "2026-10-16T15:00:00+05:30")
os.environ["FIXTURES_DIR"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures"))

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.base import create_all, drop_all, get_session_factory, init_engine
from app.ingestion.service import import_fixture_pack


@pytest.fixture()
def engine():
    eng = init_engine()
    drop_all()
    create_all()
    yield eng
    drop_all()


@pytest.fixture()
def session(engine):
    factory = get_session_factory()
    s = factory()
    yield s
    s.close()


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def loaded(session, settings):
    """Import the full fixture pack once and return the import results."""
    return import_fixture_pack(session, settings.fixtures_dir, settings.eval_time)


@pytest.fixture()
def client(engine):
    from app.api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded_client(client, settings):
    r = client.post("/imports/fixture-pack")
    assert r.status_code == 201, r.text
    return client
