"""The internal API must be closed to anyone without the shared secret."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import require_internal_api_key
from tests.conftest import TEST_API_KEY


@pytest.fixture
def client(settings):
    app = FastAPI()

    @app.get("/guarded", dependencies=[Depends(require_internal_api_key)])
    async def guarded() -> dict[str, bool]:
        return {"ok": True}

    return TestClient(app)


def test_rejects_missing_key(client):
    assert client.get("/guarded").status_code == 401


def test_rejects_wrong_key(client):
    response = client.get("/guarded", headers={"X-Internal-Api-Key": "nope"})
    assert response.status_code == 401


def test_rejects_key_that_is_a_prefix_of_the_real_one(client):
    # Guards against a length-insensitive comparison.
    response = client.get("/guarded", headers={"X-Internal-Api-Key": TEST_API_KEY[:-1]})
    assert response.status_code == 401


def test_accepts_correct_key(client):
    response = client.get("/guarded", headers={"X-Internal-Api-Key": TEST_API_KEY})
    assert response.status_code == 200


def test_unset_key_fails_closed(client, monkeypatch):
    """An unconfigured INTERNAL_API_KEY must not mean 'allow everyone'."""
    from app.settings import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()

    response = client.get("/guarded", headers={"X-Internal-Api-Key": "anything"})
    assert response.status_code == 500

    get_settings.cache_clear()
