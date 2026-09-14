import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from ndi_rmtp.app import build_app
from ndi_rmtp.config import (
    AdvancedConfig,
    AuthConfig,
    Config,
    Credentials,
    StreamConfig,
    Ward,
    credentials_path_for,
)
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



# ---- /camera : PTZ-only page for operators who encode in OBS ----------------


def test_camera_page_requires_auth(client):
    r = client.get("/camera")
    assert r.status_code == 401


def test_camera_page_renders_when_authed(client):
    r = client.get("/camera", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_camera_page_lists_configured_presets(tmp_path, streamer, discovery):
    """Preset names render server-side so the page is useful on first paint,
    before /api/ptz/info comes back (or at all, when PTZ is unavailable)."""
    from ndi_rmtp.config import PtzConfig, PtzPreset

    config = Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(wards=[Ward(name="Primary", key="abc-key")]),
        ptz=PtzConfig(presets=[PtzPreset(name="Pulpit", slot=1)]),
        advanced=AdvancedConfig(),
    )
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    client = TestClient(
        build_app(config_path=config_path, streamer=streamer, discovery=discovery)
    )
    r = client.get("/camera", headers=_basic("admin", "hunter2"))
    assert "Pulpit" in r.text


def test_camera_page_omits_stream_controls(client):
    """The whole point of this page: OBS owns the encode, so nothing here may
    start, stop, or reconfigure the appliance's own stream."""
    r = client.get("/camera", headers=_basic("admin", "hunter2"))
    # Guard the guard: a 404 body trivially contains none of these.
    assert r.status_code == 200
    for path in ("/api/start", "/api/stop", "/api/sacrament", "/api/mute"):
        assert path not in r.text, f"{path} must not be reachable from /camera"


def test_api_config_persists_last_source(client, tmp_path):
    """The camera page's source picker writes here. PtzController connects to
    config.stream.last_source, which previously only /api/start ever set — so
    a site that never streams through the appliance had no way to choose one."""
    r = client.post(
        "/api/config",
        headers=_basic("admin", "hunter2"),
        data={"last_source": "CAM (Channel 1)"},
    )
    assert r.status_code == 200
    assert Config.load(tmp_path / "config.toml").stream.last_source == "CAM (Channel 1)"


def test_camera_page_keeps_card_visible_when_ptz_unsupported(client):
    """/camera must not hide the camera card the way / does.

    The main page can hide it — there's a stream to run either way. On /camera
    hiding it leaves a blank page with no explanation, so the card stays up and
    ptz.js writes the reason into the preview placeholder instead.
    """
    r = client.get("/camera", headers=_basic("admin", "hunter2"))
    card = r.text[r.text.index('id="ptz-card"'):][:200]
    assert 'data-hide-when-unsupported="false"' in card
    assert "hidden" not in card.split(">")[0]


def test_index_hides_card_until_ptz_reports_supported(client):
    """The counterpart: / keeps the opt-in hiding behaviour it always had."""
    r = client.get("/", headers=_basic("admin", "hunter2"))
    card = r.text[r.text.index('id="ptz-card"'):][:200]
    assert 'data-hide-when-unsupported="true"' in card
    assert "hidden" in card.split(">")[0]


def test_camera_page_shows_config_version(tmp_path, streamer, discovery):
    """Same contract the home page has (test_index_shows_config_version).

    config_version exists so an operator can confirm at a glance that the
    appliance loaded the latest deploy. An OBS-only ward never opens `/`, so
    without the stamp here they have no way to check.
    """
    config = Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(wards=[Ward(name="Primary", key="abc-key")]),
        advanced=AdvancedConfig(),
        config_version=42,
    )
    config_path = tmp_path / "config.toml"
    config.save(config_path)
    client = TestClient(
        build_app(config_path=config_path, streamer=streamer, discovery=discovery)
    )
    r = client.get("/camera", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "v42" in r.text


def test_static_assets_are_revalidated_not_blindly_cached(client):
    """A code-only deploy changes style.css but not config_version.

    With no Cache-Control the browser may reuse a cached stylesheet without
    revalidating, so a deployed CSS fix appears not to have landed until the
    operator hard-refreshes. `no-cache` forces a conditional request; the ETag
    keeps it cheap.
    """
    r = client.get("/static/style.css")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("etag")

    # A conditional re-request must come back 304, i.e. revalidation is cheap.
    again = client.get("/static/style.css", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304


# ---------------------------------------------------------------------------
# YouTube liveness route
#
# The route must never fail loudly: it decorates a page whose real job is
# streaming, so a YouTube outage has to read as "we don't know", not as an
# error over a working stream.
# ---------------------------------------------------------------------------

def _ward_config(tmp_path, channel_id: str) -> Path:
    cfg = Config(
        auth=AuthConfig(username="admin", password="hunter2"),
        stream=StreamConfig(
            wards=[Ward(name="Primary", key="abc-key", channel_id=channel_id)],
            active_ward="Primary",
        ),
        advanced=AdvancedConfig(),
    )
    path = tmp_path / "config.toml"
    cfg.save(path)
    return path


def test_api_youtube_reports_unconfigured_without_a_channel_id(
    tmp_path, streamer, discovery
):
    config_path = _ward_config(tmp_path, "")
    app = build_app(config_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.get("/api/youtube", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "unconfigured"
    # Still hands back somewhere useful to send the operator.
    assert body["control_room_url"].startswith("https://studio.youtube.com/")


def test_api_youtube_links_to_the_wards_control_room(
    tmp_path, streamer, discovery, monkeypatch
):
    from ndi_rmtp import youtube

    monkeypatch.setattr(youtube, "_fetch", lambda _: "<html></html>")
    channel = "UCbLlSmx8Wvg85m8jPgruTuQ"
    config_path = _ward_config(tmp_path, channel)
    app = build_app(config_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.get("/api/youtube", headers=_basic("admin", "hunter2"))
    body = r.json()
    assert channel in body["control_room_url"]
    assert body["control_room_url"].endswith("/livestreaming/dashboard")


def test_api_youtube_stays_200_when_youtube_is_unreachable(
    tmp_path, streamer, discovery, monkeypatch
):
    import urllib.error

    from ndi_rmtp import youtube

    def boom(_):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(youtube, "_fetch", boom)
    config_path = _ward_config(tmp_path, "UCbLlSmx8Wvg85m8jPgruTuQ")
    app = build_app(config_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.get("/api/youtube", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert r.json()["state"] == "unavailable"


def test_api_youtube_requires_auth(tmp_path, streamer, discovery):
    config_path = _ward_config(tmp_path, "")
    app = build_app(config_path, streamer, discovery)
    with TestClient(app) as c:
        assert c.get("/api/youtube").status_code == 401


# ---------------------------------------------------------------------------
# /setup — per-ward credentials
# ---------------------------------------------------------------------------

def _setup_app(tmp_path, streamer, discovery, *, key="REPLACE_WITH_YOUR_YOUTUBE_STREAM_KEY",
               auth_enabled=True):
    cfg = Config(
        auth=AuthConfig(username="admin", password="hunter2", enabled=auth_enabled),
        stream=StreamConfig(
            wards=[Ward(name="Primary", key=key), Ward(name="Second", key="")],
            active_ward="Primary",
        ),
        advanced=AdvancedConfig(),
    )
    config_path = tmp_path / "config.toml"
    cfg.save(config_path)
    return build_app(config_path, streamer, discovery), config_path


def test_setup_page_lists_wards_from_config(tmp_path, streamer, discovery):
    app, _ = _setup_app(tmp_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.get("/setup", headers=_basic("admin", "hunter2"))
    assert r.status_code == 200
    assert "Primary" in r.text and "Second" in r.text


def test_setup_page_never_sends_the_stream_key_to_the_browser(tmp_path, streamer, discovery):
    """The page is reachable by anyone on the LAN when auth is off, and an
    operator never needs to read a key back."""
    secret = "abcd-efgh-ijkl-mnop-qrst"
    app, config_path = _setup_app(tmp_path, streamer, discovery, key=secret)
    with TestClient(app) as c:
        r = c.get("/setup", headers=_basic("admin", "hunter2"))
    assert secret not in r.text
    assert "qrst" in r.text          # last 4 only, enough to tell keys apart


def test_setup_page_warns_when_auth_is_disabled(tmp_path, streamer, discovery):
    app, _ = _setup_app(tmp_path, streamer, discovery, auth_enabled=False)
    with TestClient(app) as c:
        r = c.get("/setup")
    assert "unprotected" in r.text.lower()


def test_saving_a_key_writes_credentials_not_config(tmp_path, streamer, discovery):
    app, config_path = _setup_app(tmp_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.post(
            "/api/ward-credentials",
            data={"ward": "Primary", "key": "real-key-9999", "channel_id": ""},
            headers=_basic("admin", "hunter2"),
        )
    assert r.status_code == 200
    assert "real-key-9999" not in config_path.read_text()
    creds = Credentials.load(credentials_path_for(config_path))
    assert creds.for_ward("Primary").key == "real-key-9999"
    # And the running app now serves the overlaid value.
    assert Config.load(config_path).stream.find_ward("Primary").key == "real-key-9999"


def test_blank_key_leaves_the_saved_one_alone(tmp_path, streamer, discovery):
    """The form never shows the current key, so an untouched field must not
    wipe it — otherwise editing only the channel ID takes a ward off the air."""
    app, config_path = _setup_app(tmp_path, streamer, discovery)
    headers = _basic("admin", "hunter2")
    with TestClient(app) as c:
        c.post("/api/ward-credentials",
               data={"ward": "Primary", "key": "keep-me-1234", "channel_id": ""},
               headers=headers)
        c.post("/api/ward-credentials",
               data={"ward": "Primary", "key": "", "channel_id": "UCbLlSmx8Wvg85m8jPgruTuQ"},
               headers=headers)
    creds = Credentials.load(credentials_path_for(config_path))
    assert creds.for_ward("Primary").key == "keep-me-1234"
    assert creds.for_ward("Primary").channel_id == "UCbLlSmx8Wvg85m8jPgruTuQ"


def test_key_can_be_explicitly_cleared(tmp_path, streamer, discovery):
    app, config_path = _setup_app(tmp_path, streamer, discovery)
    headers = _basic("admin", "hunter2")
    with TestClient(app) as c:
        c.post("/api/ward-credentials",
               data={"ward": "Primary", "key": "bye-1234", "channel_id": ""}, headers=headers)
        c.post("/api/ward-credentials",
               data={"ward": "Primary", "key": "-", "channel_id": ""}, headers=headers)
    assert Credentials.load(credentials_path_for(config_path)).for_ward("Primary").key == ""


def test_rejects_a_bad_channel_id(tmp_path, streamer, discovery):
    app, _ = _setup_app(tmp_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.post("/api/ward-credentials",
                   data={"ward": "Primary", "key": "", "channel_id": "not-a-channel"},
                   headers=_basic("admin", "hunter2"))
    assert r.status_code == 400
    assert "channel ID" in r.json()["detail"]


def test_rejects_a_ward_not_in_config(tmp_path, streamer, discovery):
    """Wards are enumerated in config.toml; this page must not invent them."""
    app, config_path = _setup_app(tmp_path, streamer, discovery)
    with TestClient(app) as c:
        r = c.post("/api/ward-credentials",
                   data={"ward": "Nonexistent", "key": "x", "channel_id": ""},
                   headers=_basic("admin", "hunter2"))
    assert r.status_code == 400
    assert not credentials_path_for(config_path).exists()


def test_setup_routes_require_auth(tmp_path, streamer, discovery):
    app, _ = _setup_app(tmp_path, streamer, discovery)
    with TestClient(app) as c:
        assert c.get("/setup").status_code == 401
        assert c.post("/api/ward-credentials", data={"ward": "Primary"}).status_code == 401
