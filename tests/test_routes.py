import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ndi_rmtp.app import build_app
from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig, Ward
from ndi_rmtp.ndi import Source


def _basic(user: str, password: str) -> dict[str, str]:
    raw = f"{user}:{password}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(
            wards=[Ward(name="Primary", key="abc-key")],
            active_ward="Primary",
            last_source="SRC",
        ),
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


def test_index_shows_config_version(tmp_path, streamer, discovery):
    # The deployed config's version stamp must be visible on the home page so an
    # operator can confirm the appliance picked up the latest deploy.
    config = Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(
            wards=[Ward(name="Primary", key="abc-key")],
            active_ward="Primary",
            last_source="SRC",
        ),
        advanced=AdvancedConfig(),
        config_version=42,
    )
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    app = build_app(config_path=config_path, streamer=streamer, discovery=discovery)
    client = TestClient(app)
    r = client.get("/", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "v42" in r.text


def test_api_status_returns_shape(client):
    r = client.get("/api/status", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    # Required keys; extra keys (muted, sacrament_mode, cpu_temp_c) are
    # added over time, so don't assert an exact set.
    assert {"state", "uptime_s", "last_error", "log_tail"} <= set(body)
    # cpu_temp_c is None on macOS dev/CI (no /sys/class/thermal); a float
    # on the Pi. The shape contract is "key exists, value is None or float".
    assert "cpu_temp_c" in body
    assert body["cpu_temp_c"] is None or isinstance(body["cpu_temp_c"], (int, float))


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


def test_api_config_switches_active_ward_and_quality(client, tmp_path):
    """Switching active_ward picks an existing entry from stream.wards (config-managed)."""
    # Extend the on-disk config with a second key so the switch has something to point to.
    config_path = tmp_path / "config.toml"
    cfg = Config.load(config_path)
    cfg.stream.wards.append(Ward(name="Backup", key="zzz-key"))
    cfg.save(config_path)

    r = client.post(
        "/api/config",
        headers=_basic("admin", "hunter2"),
        data={"active_ward": "Backup", "quality": "low"},
    )
    assert r.status_code == 200
    reloaded = Config.load(config_path)
    assert reloaded.stream.active_ward == "Backup"
    assert reloaded.stream.active().key == "zzz-key"
    assert reloaded.stream.quality == "low"


def test_api_config_rejects_unknown_active_ward(client):
    r = client.post(
        "/api/config",
        headers=_basic("admin", "hunter2"),
        data={"active_ward": "Does Not Exist"},
    )
    assert r.status_code == 400
    # Operator-facing error message escalates to the Stake Tech Specialist
    # rather than naming the ward — see the message rewrite commit.
    assert "stake" in r.text.lower()


def test_api_start_uses_active_wards_value(client, streamer):
    """The stream_key passed to streamer.start is the *value* of the active key,
    not its name."""
    r = client.post(
        "/api/start",
        headers=_basic("admin", "hunter2"),
        data={"source": "SRC1", "quality": "medium"},
    )
    assert r.status_code == 200
    kwargs = streamer.start.call_args.kwargs
    assert kwargs["stream_key"] == "abc-key"  # the .key of the "Primary" entry


def test_protected_routes_reject_bad_password(client):
    r = client.get("/api/status", headers=_basic("admin", "wrong"))
    assert r.status_code == 401

