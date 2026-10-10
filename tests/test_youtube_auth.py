import json
from pathlib import Path

import httpx
import pytest

from ndi_rmtp.config import Credentials, YoutubeConfig, update_ward_credentials
from ndi_rmtp.youtube_auth import (
    DEVICE_CODE_URL, REVOKE_URL, TOKEN_URL, DeviceFlows, ReauthRequired, TokenStore,
)

CLIENT = YoutubeConfig(client_id="cid", client_secret="sec")


def _http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


async def test_access_token_refreshes_and_caches(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt")
    calls = []

    def handler(req):
        calls.append(dict(httpx.QueryParams(req.content.decode())))
        return httpx.Response(200, json={"access_token": "at1", "expires_in": 3600})

    clock = Clock()
    store = TokenStore(_http(handler), lambda: CLIENT, path, clock=clock)
    assert await store.access_token("W") == "at1"
    assert await store.access_token("W") == "at1"
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "refresh_token" and calls[0]["refresh_token"] == "rt"
    clock.t += 3600 - 30  # inside the 60 s safety margin
    await store.access_token("W")
    assert len(calls) == 2


async def test_invalid_grant_marks_needs_reconnect(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt")
    store = TokenStore(
        _http(lambda r: httpx.Response(400, json={"error": "invalid_grant"})),
        lambda: CLIENT, path,
    )
    with pytest.raises(ReauthRequired):
        await store.access_token("W")
    w = Credentials.load(path).for_ward("W")
    assert w.yt_needs_reconnect and not w.yt_refresh_token


async def test_access_token_without_refresh_token_requires_reauth(tmp_path):
    store = TokenStore(_http(lambda r: httpx.Response(500)), lambda: CLIENT, tmp_path / "c.toml")
    with pytest.raises(ReauthRequired):
        await store.access_token("W")


async def test_seed_persists_and_caches(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_needs_reconnect=True)
    store = TokenStore(_http(lambda r: httpx.Response(500)), lambda: CLIENT, path)
    store.seed("W", {"refresh_token": "rt", "access_token": "at", "expires_in": 3600})
    assert await store.access_token("W") == "at"
    w = Credentials.load(path).for_ward("W")
    assert w.yt_refresh_token == "rt" and not w.yt_needs_reconnect


async def test_revoke_clears_youtube_fields_but_keeps_manual_key(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", key="manual", yt_refresh_token="rt", yt_stream_key="sk", yt_keep=["v"])
    seen = []
    store = TokenStore(_http(lambda r: seen.append(str(r.url)) or httpx.Response(200)), lambda: CLIENT, path)
    await store.revoke("W")
    w = Credentials.load(path).for_ward("W")
    assert w.key == "manual" and not w.yt_linked and w.yt_stream_key == ""
    assert seen and seen[0].startswith(REVOKE_URL)


async def test_revoke_survives_network_failure(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt")

    def boom(req): raise httpx.ConnectError("down")

    await TokenStore(_http(boom), lambda: CLIENT, path).revoke("W")
    assert not Credentials.load(path).for_ward("W").yt_linked


def _device_handler(token_responses):
    responses = iter(token_responses)

    def handler(req):
        if str(req.url) == DEVICE_CODE_URL:
            body = dict(httpx.QueryParams(req.content.decode()))
            assert body["scope"] == "https://www.googleapis.com/auth/youtube"
            return httpx.Response(200, json={
                "device_code": "dc", "user_code": "ABCD-EFGH",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800, "interval": 5,
            })
        assert str(req.url) == TOKEN_URL
        status, payload = next(responses)
        return httpx.Response(status, json=payload)
    return handler


async def _drain(flows, ward):
    await flows._tasks[ward]


async def test_device_flow_pending_slow_down_then_connected():
    got, sleeps = [], []

    async def on_tokens(ward, tokens): got.append((ward, tokens["refresh_token"]))
    async def fake_sleep(s): sleeps.append(s)

    flows = DeviceFlows(_http(_device_handler([
        (428, {"error": "authorization_pending"}),
        (403, {"error": "slow_down"}),
        (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
    ])), lambda: CLIENT, on_tokens, sleep=fake_sleep)
    st = await flows.begin("W")
    assert (st.state, st.user_code) == ("pending", "ABCD-EFGH")
    await _drain(flows, "W")
    assert got == [("W", "rt")]
    assert flows.state("W").state == "connected"
    assert sleeps[0] == 5 and sleeps[2] == 10  # slow_down adds 5 s


async def test_device_flow_denied():
    async def on_tokens(w, t): raise AssertionError("must not be called")
    async def fake_sleep(s): pass
    flows = DeviceFlows(_http(_device_handler([(403, {"error": "access_denied"})])),
                        lambda: CLIENT, on_tokens, sleep=fake_sleep)
    await flows.begin("W"); await _drain(flows, "W")
    assert flows.state("W").state == "denied"


async def test_device_flow_expires():
    clock = Clock()
    async def on_tokens(w, t): pass
    async def fake_sleep(s): clock.t += 2000
    flows = DeviceFlows(_http(_device_handler([(428, {"error": "authorization_pending"})] * 5)),
                        lambda: CLIENT, on_tokens, sleep=fake_sleep, clock=clock)
    await flows.begin("W"); await _drain(flows, "W")
    assert flows.state("W").state == "expired"


async def test_device_flow_on_tokens_failure_is_error_state():
    async def on_tokens(w, t): raise RuntimeError("liveStreamingNotEnabled")
    async def fake_sleep(s): pass
    flows = DeviceFlows(_http(_device_handler([(200, {"access_token": "a", "refresh_token": "r", "expires_in": 1})])),
                        lambda: CLIENT, on_tokens, sleep=fake_sleep)
    await flows.begin("W"); await _drain(flows, "W")
    st = flows.state("W")
    assert st.state == "error" and "liveStreamingNotEnabled" in st.message


async def test_revoke_keeps_the_keep_list(tmp_path):
    # Kept recordings must survive a disconnect, or the next sweep deletes them.
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt", yt_keep=["v"])
    await TokenStore(_http(lambda r: httpx.Response(200)), lambda: CLIENT, path).revoke("W")
    assert Credentials.load(path).for_ward("W").yt_keep == ["v"]


async def test_non_json_refresh_error_is_auth_error(tmp_path):
    from ndi_rmtp.youtube_auth import AuthError
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt")
    store = TokenStore(_http(lambda r: httpx.Response(502, text="<html>bad gateway</html>")), lambda: CLIENT, path)
    with pytest.raises(AuthError):
        await store.access_token("W")


async def test_device_flow_non_json_poll_response_is_error_state():
    async def on_tokens(w, t): pass
    async def fake_sleep(s): pass

    def handler(req):
        if str(req.url) == DEVICE_CODE_URL:
            return httpx.Response(200, json={"device_code": "dc", "user_code": "U", "verification_url": "v",
                                             "expires_in": 1800, "interval": 5})
        return httpx.Response(502, text="<html>bad gateway</html>")

    flows = DeviceFlows(_http(handler), lambda: CLIENT, on_tokens, sleep=fake_sleep)
    await flows.begin("W"); await _drain(flows, "W")
    assert flows.state("W").state == "error"
