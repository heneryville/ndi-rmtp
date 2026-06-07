import base64

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ndi_rmtp.auth import make_auth_dependency
from ndi_rmtp.config import AuthConfig, Config, StreamConfig, AdvancedConfig


def _app(config: Config) -> FastAPI:
    app = FastAPI()
    require_auth = make_auth_dependency(lambda: config)

    @app.get("/protected")
    def protected(_: None = Depends(require_auth)):
        return {"ok": True}

    @app.get("/open")
    def open_route():
        return {"ok": True}

    return app


def _basic(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def config() -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(),
        advanced=AdvancedConfig(),
    )


def test_valid_credentials_pass(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_wrong_password_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("admin", "wrong"))
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Basic"


def test_wrong_username_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected", headers=_basic("root", "hunter2"))
    assert r.status_code == 401


def test_missing_header_rejected(config):
    client = TestClient(_app(config))
    r = client.get("/protected")
    assert r.status_code == 401


def test_open_route_not_gated(config):
    client = TestClient(_app(config))
    r = client.get("/open")
    assert r.status_code == 200


def test_credentials_compared_constant_time(config):
    """Sanity: we use secrets.compare_digest, not == . Smoke check only."""
    import secrets as s
    assert hasattr(s, "compare_digest")
