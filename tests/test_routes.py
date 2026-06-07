import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ndi_rmtp.app import build_app
from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.ndi import Source


def _basic(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(stream_key="abc", last_source="SRC"),
        advanced=AdvancedConfig(),
    )


@pytest.fixture
def streamer():
    s = MagicMock()
    s.start = AsyncMock()
    s.stop = AsyncMock()
    s.status.return_value = MagicMock(
        to_dict=MagicMock(
            return_value={"state": "IDLE", "uptime_s": 0, "last_error": None, "log_tail": []}
        )
    )
    return s


@pytest.fixture
def discovery():
    d = MagicMock()
    d.list = AsyncMock(return_value=[
        Source(name="SRC1", address="10.0.0.5:5961"),
        Source(name="NEW_SRC", address="10.0.0.6:5961"),
    ])
    return d


@pytest.fixture
def client(tmp_path, config, streamer, discovery):
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    app = build_app(config_path=config_path, streamer=streamer, discovery=discovery)
    return TestClient(app)


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200


def test_index_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401


def test_index_renders_when_authed(client):
    r = client.get("/", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_status_returns_shape(client):
    r = client.get("/api/status", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"state", "uptime_s", "last_error", "log_tail"}


def test_api_sources_returns_list(client, discovery):
    r = client.get("/api/sources", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    names = [s["name"] for s in body]
    assert "SRC1" in names
    assert "NEW_SRC" in names


def test_api_start_calls_streamer_with_discovered_address(client, streamer, tmp_path):
    """The UI sends the friendly name; the route resolves it to the address
    (ndi-streamer's -n flag uses p_url_address, which needs IP:port)."""
    r = client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "SRC1", "quality": "medium"},
    )
    assert r.status_code == 200
    streamer.start.assert_awaited_once()
    args = streamer.start.call_args
    # The route must pass the discovered ADDRESS, not the friendly name.
    assert args.kwargs.get("source") == "10.0.0.5:5961"


def test_api_start_persists_source_and_quality(client, tmp_path, config):
    config_path = tmp_path / "config.toml"
    client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "NEW_SRC", "quality": "high"},
    )
    reloaded = Config.load(config_path)
    assert reloaded.stream.last_source == "NEW_SRC"
    assert reloaded.stream.quality == "high"


def test_api_start_rejects_unknown_source(client):
    r = client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "DOES NOT EXIST", "quality": "medium"},
    )
    assert r.status_code == 409
    assert "not found" in r.text.lower()


def test_api_stop_calls_streamer(client, streamer):
    r = client.post("/api/stop", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    streamer.stop.assert_awaited_once()


def test_api_config_updates_stream_key(client, tmp_path, config):
    config_path = tmp_path / "config.toml"
    r = client.post(
        "/api/config",
        headers=_basic("admin", "hunter2"),
        data={"stream_key": "new-key-123", "quality": "low"},
    )
    assert r.status_code == 200
    reloaded = Config.load(config_path)
    assert reloaded.stream.stream_key == "new-key-123"
    assert reloaded.stream.quality == "low"


def test_protected_routes_reject_bad_password(client):
    r = client.get("/api/status", headers=_basic("admin", "wrong"))
    assert r.status_code == 401
