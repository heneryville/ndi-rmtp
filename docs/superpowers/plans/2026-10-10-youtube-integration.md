# YouTube Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opted-in wards sign in to YouTube once (device flow), and the appliance creates/binds the broadcast before the encoder connects, ends it on Stop, and deletes its recordings 7 days later.

**Architecture:** Three new modules: `youtube_auth.py` (device flow + tokens), `youtube_api.py` (thin REST client over `httpx`), and `broadcasts.py` (lifecycle + retention). `app.py` orchestrates them around the existing `Streamer`, never inside its lock. All new per-ward state lives in device-local `credentials.toml`. The OAuth client ID lives in a new `[youtube]` config section, and leaving it empty hides the feature.

**Tech Stack:** Python 3.11, FastAPI, httpx (promoted to a runtime dependency), pytest + pytest-asyncio (`asyncio_mode = "auto"`), `httpx.MockTransport` for every Google call in tests, Jinja2 + vanilla JS/htmx.

**Spec:** `docs/superpowers/specs/2026-10-10-youtube-integration-design.md`. Read it first.

## Global Constraints

- OAuth scope is exactly `https://www.googleapis.com/auth/youtube`. Device flow does not support `youtube.force-ssl`.
- Endpoints: `https://oauth2.googleapis.com/device/code`, `https://oauth2.googleapis.com/token`, `https://oauth2.googleapis.com/revoke`, API base `https://www.googleapis.com/youtube/v3`.
- Broadcast insert: `enableAutoStart=true`, `enableAutoStop=false`, `monitorStream.enableMonitorStream=false`, `latencyPreference="normal"`, `enableDvr=true`, `selfDeclaredMadeForKids=false`.
- Marker substring in the description: `ndi-rmtp; auto-deletes`. Full marker line: `Streamed by ndi-rmtp; auto-deletes 7 days after the meeting.`
- Title: `"{ward} Sacrament Meeting — {Mon D, YYYY}"`, at most 100 characters (truncate the ward name, never the date).
- Retention: 7 days after `actualEndTime`. The reuse window for an active or upcoming broadcast is 12 hours from `scheduledStartTime`.
- Privacy is one of `public` / `unlisted` / `private`, default `public`.
- Sweep runs 2 minutes after startup, then every 6 hours. List at most 10 pages × 50.
- Never `await` a network call while holding `Streamer._lock`. Orchestrate in `app.py`.
- `install.sh` must never touch `credentials.toml`, and `Config.save` must never write `yt_*` fields.
- No full stream key or refresh token is ever sent to the browser.
- Empty `[youtube] client_id` means the app behaves exactly as before this plan: no YouTube controls, and `/api/start` uses the manual key.
- Comments: one line, *why* not *what*; match the sparse density of the surrounding code.
- Run tests with `uv run pytest`. All existing tests must keep passing.

## Review Focus

1. **Wards sharing a channel:** ward A clicks Keep on a video and ward B's sweep sees the same video. Expect it **not** deleted. Pinned in Task 5 (`test_sweep_respects_keep_from_any_ward`).
2. **Last week's broadcast still `active`** (Stop never reached YouTube, or the Pi lost power). Expect next Sunday's Start to complete it and create a fresh one, not reuse it. Pinned in Task 4 (`test_prepare_completes_stale_active_and_creates_new`).
3. **Ward needs reconnect** (refresh token revoked or expired). Expect Start to refuse with a reconnect message and offer the fallback, never to silently stream to the manual key. Pinned in Task 6 (`test_start_needs_reconnect_offers_fallback`).
4. **Stop after an app restart mid-meeting** (no in-memory record). Expect `finish` to find and complete the ward's active broadcast. Pinned in Task 4 (`test_finish_without_record_looks_up_active`).
5. **Double-clicked Start.** Expect exactly one broadcast created. Pinned in Task 4 (`test_concurrent_prepare_creates_one_broadcast`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/ndi_rmtp/config.py` (modify) | `YoutubeConfig` section; new `WardCredentials` fields; `update_ward_credentials()` helper |
| `src/ndi_rmtp/youtube_auth.py` (create) | `TokenStore` (access token cache + refresh + revoke), `DeviceFlows` (device-code polling) |
| `src/ndi_rmtp/youtube_api.py` (create) | `YouTubeClient`, `Broadcast`, `YouTubeError`, `MARKER` |
| `src/ndi_rmtp/broadcasts.py` (create) | `BroadcastManager` (connect/disconnect/prepare/finish/status/sweep/past streams), `PrepareError`, `YouTubeServices` |
| `src/ndi_rmtp/app.py` (modify) | Wire start/stop/youtube status; `/api/youtube/*` endpoints; `/streams` page; sweeper lifespan |
| `src/ndi_rmtp/bootstrap.py` (modify) | Construct the services |
| `src/ndi_rmtp/templates/setup.html` (modify) | Connect / Disconnect / privacy per ward |
| `src/ndi_rmtp/templates/streams.html` (create) | Past streams page |
| `src/ndi_rmtp/templates/index.html` (modify) | Connected-ward YouTube UI, fallback start, stop warning |
| `src/ndi_rmtp/static/mock.js` (modify) | `?mock=1&yt=api` fakes the API-mode `/api/youtube` |
| `tests/test_youtube_auth.py`, `tests/test_youtube_api.py`, `tests/test_broadcasts.py` (create) | Unit tests over `httpx.MockTransport` |
| `tests/test_config.py`, `tests/test_routes.py` (modify) | Schema + route tests |
| `docs/youtube-oauth-setup.md` (create), `CLAUDE.md`, `config.*.example.toml`, `docs/users-guide.md` (modify) | Setup + docs |

---

### Task 1: Config schema — `[youtube]` section and YouTube ward credentials

**Files:**
- Modify: `pyproject.toml` (move `httpx>=0.27` into `dependencies`)
- Modify: `src/ndi_rmtp/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `YoutubeConfig(client_id: str = "", client_secret: str = "")` with property `enabled -> bool`
  - `Config.youtube: YoutubeConfig`
  - `WardCredentials` new fields: `privacy: str = "public"`, `yt_refresh_token: str = ""`, `yt_channel_title: str = ""`, `yt_stream_id: str = ""`, `yt_stream_key: str = ""`, `yt_needs_reconnect: bool = False`, `yt_keep: list[str]`. Properties: `yt_connected -> bool` (has refresh token) and `yt_linked -> bool` (connected or needs reconnect).
  - `PRIVACY_CHOICES = ("public", "unlisted", "private")`
  - `update_ward_credentials(path: Path, ward: str, **changes) -> WardCredentials`: load, apply, save, return.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_config.py`; add `YoutubeConfig, update_ward_credentials` to the import list)

```python
def test_youtube_section_round_trips_and_defaults_off(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(auth=AuthConfig(username="a", password="b"))
    assert cfg.youtube.enabled is False
    cfg.youtube = YoutubeConfig(client_id="cid.apps.googleusercontent.com", client_secret="s")
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.youtube.client_id == "cid.apps.googleusercontent.com"
    assert loaded.youtube.enabled is True


def test_old_config_without_youtube_section_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[auth]\nusername = "a"\npassword = "b"\n')
    assert Config.load(path).youtube.enabled is False


def test_ward_youtube_fields_round_trip(tmp_path):
    path = tmp_path / "credentials.toml"
    creds = Credentials()
    creds.wards["W"] = WardCredentials(
        privacy="unlisted", yt_refresh_token="rt", yt_stream_id="sid",
        yt_stream_key="skey", yt_keep=["vid1"],
    )
    creds.save(path)
    w = Credentials.load(path).for_ward("W")
    assert (w.privacy, w.yt_refresh_token, w.yt_stream_key, w.yt_keep) == ("unlisted", "rt", "skey", ["vid1"])
    assert w.yt_connected and w.yt_linked


def test_needs_reconnect_is_linked_but_not_connected():
    w = WardCredentials(yt_needs_reconnect=True)
    assert w.yt_linked and not w.yt_connected


def test_credentials_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "credentials.toml"
    path.write_text('[wards.W]\nkey = "k"\nfuture_field = 1\n')
    assert Credentials.load(path).for_ward("W").key == "k"


def test_update_ward_credentials_persists(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_stream_key="abc", privacy="private")
    w = Credentials.load(path).for_ward("W")
    assert (w.yt_stream_key, w.privacy) == ("abc", "private")


def test_config_save_never_writes_youtube_ward_fields(tmp_path):
    cfg_path = tmp_path / "config.toml"
    Config(
        auth=AuthConfig(username="a", password="b"),
        stream=StreamConfig(wards=[Ward(name="W", key="")]),
    ).save(cfg_path)
    update_ward_credentials(credentials_path_for(cfg_path), "W", yt_refresh_token="SECRET-RT", yt_stream_key="SECRET-SK")
    Config.load(cfg_path).save(cfg_path)
    text = cfg_path.read_text()
    assert "SECRET" not in text and "yt_" not in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k "youtube or update_ward or unknown_fields or reconnect"`
Expected: ImportError for `YoutubeConfig`.

- [ ] **Step 3: Implement**

In `pyproject.toml`, add `"httpx>=0.27",` to `dependencies` and remove it from `dev`. Run `uv lock`.

In `config.py`, below `AdvancedConfig`:

```python
PRIVACY_CHOICES = ("public", "unlisted", "private")


@dataclass
class YoutubeConfig:
    """OAuth client for the YouTube integration; empty client_id hides the feature."""
    client_id: str = ""
    client_secret: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.client_id.strip())
```

Replace `WardCredentials`:

```python
@dataclass
class WardCredentials:
    key: str = ""
    channel_id: str = ""
    privacy: str = "public"
    # Presence of a refresh token == ward is connected to YouTube.
    yt_refresh_token: str = ""
    yt_channel_title: str = ""
    yt_stream_id: str = ""
    yt_stream_key: str = ""
    yt_needs_reconnect: bool = False
    yt_keep: list[str] = field(default_factory=list)

    @property
    def yt_connected(self) -> bool:
        return bool(self.yt_refresh_token)

    @property
    def yt_linked(self) -> bool:
        return self.yt_connected or self.yt_needs_reconnect

    @classmethod
    def from_dict(cls, entry: dict) -> "WardCredentials":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in entry.items() if k in known})
```

In `Credentials.load`, replace the dict comprehension body with `name: WardCredentials.from_dict(entry)`.

Add after `Credentials`:

```python
def update_ward_credentials(path: Path, ward: str, **changes) -> WardCredentials:
    """Load, change one ward's fields, save. Synchronous so no await can interleave."""
    creds = Credentials.load(path)
    entry = creds.wards.setdefault(ward, WardCredentials())
    for name, value in changes.items():
        setattr(entry, name, value)
    creds.save(path)
    return entry
```

In `Config`: add field `youtube: YoutubeConfig = field(default_factory=YoutubeConfig)` after `ptz`, and in `load` pass `youtube=YoutubeConfig(**data.get("youtube", {}))`.

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/ndi_rmtp/config.py tests/test_config.py
git commit -m "Add [youtube] config section and per-ward YouTube credentials"
```

---

### Task 2: `youtube_auth.py` — token store and device flow

**Files:**
- Create: `src/ndi_rmtp/youtube_auth.py`
- Test: `tests/test_youtube_auth.py`

**Interfaces:**
- Consumes: `YoutubeConfig`, `Credentials`, `update_ward_credentials` (Task 1).
- Produces:
  - `SCOPE`, `DEVICE_CODE_URL`, `TOKEN_URL`, `REVOKE_URL`
  - `class ReauthRequired(Exception)`, `class AuthError(Exception)`
  - `TokenStore(http: httpx.AsyncClient, client: Callable[[], YoutubeConfig], creds_path: Path, clock: Callable[[], float] = time.monotonic)`
    - `async access_token(ward: str) -> str`
    - `seed(ward: str, tokens: dict) -> None`: persists `refresh_token`, clears `yt_needs_reconnect`, caches the access token
    - `forget(ward: str) -> None`: drops the cached access token
    - `async revoke(ward: str) -> None`: best-effort revoke, then clears all `yt_*` fields
  - `FlowState(state: str, user_code: str = "", verification_url: str = "", expires_in_s: int = 0, message: str = "")` with `to_dict()`. `state` is one of `pending|connected|denied|expired|error`.
  - `DeviceFlows(http, client: Callable[[], YoutubeConfig], on_tokens: Callable[[str, dict], Awaitable[None]], sleep=asyncio.sleep, clock=time.monotonic)`
    - `async begin(ward: str) -> FlowState`
    - `state(ward: str) -> FlowState | None`

- [ ] **Step 1: Write the failing tests** (`tests/test_youtube_auth.py`)

```python
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
    assert w.key == "manual" and not w.yt_linked and w.yt_stream_key == "" and w.yt_keep == []
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_youtube_auth.py -v`
Expected: `ModuleNotFoundError: ndi_rmtp.youtube_auth`.

- [ ] **Step 3: Implement** (`src/ndi_rmtp/youtube_auth.py`)

```python
"""Google OAuth for the YouTube integration: device-flow sign-in and token refresh."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .config import Credentials, WardCredentials, YoutubeConfig, update_ward_credentials

logger = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
REFRESH_MARGIN_S = 60


class ReauthRequired(Exception):
    """The ward's refresh token is missing or rejected; it must reconnect on /setup."""


class AuthError(Exception):
    pass


class TokenStore:
    def __init__(self, http: httpx.AsyncClient, client: Callable[[], YoutubeConfig],
                 creds_path: Path, clock: Callable[[], float] = time.monotonic):
        self._http = http
        self._client = client
        self._path = creds_path
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}

    def seed(self, ward: str, tokens: dict) -> None:
        update_ward_credentials(self._path, ward, yt_refresh_token=tokens["refresh_token"],
                                yt_needs_reconnect=False)
        self._cache[ward] = (tokens["access_token"], self._clock() + float(tokens.get("expires_in", 0)))

    def forget(self, ward: str) -> None:
        self._cache.pop(ward, None)

    async def access_token(self, ward: str) -> str:
        cached = self._cache.get(ward)
        if cached and cached[1] - REFRESH_MARGIN_S > self._clock():
            return cached[0]
        refresh = Credentials.load(self._path).for_ward(ward).yt_refresh_token
        if not refresh:
            raise ReauthRequired(ward)
        client = self._client()
        resp = await self._http.post(TOKEN_URL, data={
            "client_id": client.client_id, "client_secret": client.client_secret,
            "refresh_token": refresh, "grant_type": "refresh_token",
        })
        if resp.status_code == 400 and resp.json().get("error") == "invalid_grant":
            logger.warning("YouTube refresh token for %r was rejected; ward must reconnect", ward)
            update_ward_credentials(self._path, ward, yt_refresh_token="", yt_needs_reconnect=True)
            self.forget(ward)
            raise ReauthRequired(ward)
        if resp.status_code != 200:
            raise AuthError(f"token refresh failed: HTTP {resp.status_code}")
        data = resp.json()
        self._cache[ward] = (data["access_token"], self._clock() + float(data["expires_in"]))
        return data["access_token"]

    async def revoke(self, ward: str) -> None:
        refresh = Credentials.load(self._path).for_ward(ward).yt_refresh_token
        if refresh:
            try:
                await self._http.post(REVOKE_URL, data={"token": refresh})
            except httpx.HTTPError as e:
                # The local disconnect matters more than Google forgetting the grant.
                logger.warning("revoke for %r failed: %s", ward, e)
        blank = WardCredentials()
        update_ward_credentials(
            self._path, ward,
            **{f: getattr(blank, f) for f in WardCredentials.__dataclass_fields__ if f.startswith("yt_")},
        )
        self.forget(ward)


@dataclass
class FlowState:
    state: str
    user_code: str = ""
    verification_url: str = ""
    expires_in_s: int = 0
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class DeviceFlows:
    def __init__(self, http: httpx.AsyncClient, client: Callable[[], YoutubeConfig],
                 on_tokens: Callable[[str, dict], Awaitable[None]],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._http = http
        self._client = client
        self._on_tokens = on_tokens
        self._sleep = sleep
        self._clock = clock
        self._states: dict[str, FlowState] = {}
        self._expires: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def state(self, ward: str) -> FlowState | None:
        st = self._states.get(ward)
        if st and st.state == "pending":
            st.expires_in_s = max(0, int(self._expires[ward] - self._clock()))
        return st

    async def begin(self, ward: str) -> FlowState:
        old = self._tasks.pop(ward, None)
        if old:
            old.cancel()
        client = self._client()
        resp = await self._http.post(DEVICE_CODE_URL, data={"client_id": client.client_id, "scope": SCOPE})
        if resp.status_code != 200:
            raise AuthError(f"Google refused the sign-in request (HTTP {resp.status_code}): {resp.text[:200]}")
        d = resp.json()
        self._expires[ward] = self._clock() + float(d["expires_in"])
        st = FlowState("pending", d["user_code"], d["verification_url"], int(d["expires_in"]))
        self._states[ward] = st
        self._tasks[ward] = asyncio.create_task(self._poll(ward, d["device_code"], float(d.get("interval", 5))))
        return st

    async def _poll(self, ward: str, device_code: str, interval: float) -> None:
        client = self._client()
        st = self._states[ward]
        while True:
            await self._sleep(interval)
            if self._clock() >= self._expires[ward]:
                st.state, st.message = "expired", "The code expired before it was entered."
                return
            try:
                resp = await self._http.post(TOKEN_URL, data={
                    "client_id": client.client_id, "client_secret": client.client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                })
            except httpx.HTTPError:
                continue  # transient; keep polling until the code expires
            body = resp.json() if resp.content else {}
            if resp.status_code == 200:
                try:
                    await self._on_tokens(ward, body)
                except Exception as e:
                    logger.warning("YouTube connect for %r failed after sign-in: %s", ward, e)
                    st.state, st.message = "error", str(e)
                    return
                st.state = "connected"
                return
            err = body.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err == "access_denied":
                st.state, st.message = "denied", "Access was denied on the Google sign-in page."
                return
            if err == "expired_token":
                st.state, st.message = "expired", "The code expired before it was entered."
                return
            st.state, st.message = "error", f"Google returned {err or resp.status_code}"
            return
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_youtube_auth.py -v`
Expected: all pass. Then `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/youtube_auth.py tests/test_youtube_auth.py
git commit -m "Add device-flow sign-in and token refresh for YouTube"
```

---

### Task 3: `youtube_api.py` — REST client

**Files:**
- Create: `src/ndi_rmtp/youtube_api.py`
- Test: `tests/test_youtube_api.py`

**Interfaces:**
- Consumes: a token provider `Callable[[str], Awaitable[str]]` plus `forget(ward)` (both from `TokenStore`, Task 2).
- Produces:
  - `API_BASE`, `MARKER = "ndi-rmtp; auto-deletes"`, `MARKER_LINE = "Streamed by ndi-rmtp; auto-deletes 7 days after the meeting."`
  - `class YouTubeError(Exception)` with `.reason: str`, `.status: int`
  - `@dataclass Broadcast(id, title, description, privacy, life_cycle_status, bound_stream_id, scheduled_start: datetime | None, actual_end: datetime | None)` with properties `watch_url` and `is_ours`
  - `YouTubeClient(http, tokens: TokenStore)` with async methods:
    `my_channel(ward) -> tuple[str, str]`,
    `create_stream(ward, title) -> tuple[str, str]` returning `(stream_id, stream_key)`,
    `get_stream_key(ward, stream_id) -> str`,
    `create_broadcast(ward, *, title, description, privacy, start: datetime) -> Broadcast`,
    `bind(ward, broadcast_id, stream_id) -> None`,
    `get_broadcast(ward, broadcast_id) -> Broadcast | None`,
    `list_broadcasts(ward, status: str, max_pages: int = 10) -> list[Broadcast]`,
    `complete(ward, broadcast_id) -> None`,
    `delete_video(ward, video_id) -> None` (404 is success)

- [ ] **Step 1: Write the failing tests** (`tests/test_youtube_api.py`)

```python
import json
from datetime import datetime, timezone

import httpx
import pytest

from ndi_rmtp.youtube_api import MARKER_LINE, YouTubeClient, YouTubeError


class FakeTokens:
    def __init__(self): self.forgotten = []; self.n = 0
    async def access_token(self, ward): self.n += 1; return f"tok{self.n}"
    def forget(self, ward): self.forgotten.append(ward)


def _client(handler, tokens=None):
    return YouTubeClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), tokens or FakeTokens())


def _bc(id="b1", desc=MARKER_LINE, status="ready", stream="s1", start="2026-10-11T16:00:00Z", end=None):
    return {"id": id, "snippet": {"title": "T", "description": desc, "scheduledStartTime": start,
                                  **({"actualEndTime": end} if end else {})},
            "status": {"privacyStatus": "public", "lifeCycleStatus": status},
            "contentDetails": {"boundStreamId": stream}}


async def test_create_broadcast_sends_required_fields():
    seen = {}

    def handler(req):
        seen["params"] = dict(req.url.params); seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=_bc())

    b = await _client(handler).create_broadcast(
        "W", title="T", description="D", privacy="public",
        start=datetime(2026, 10, 11, 16, tzinfo=timezone.utc))
    body = seen["body"]
    assert seen["params"]["part"] == "snippet,status,contentDetails"
    assert seen["auth"] == "Bearer tok1"
    assert body["snippet"]["scheduledStartTime"] == "2026-10-11T16:00:00Z"
    assert body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    cd = body["contentDetails"]
    assert cd["enableAutoStart"] is True and cd["enableAutoStop"] is False
    assert cd["monitorStream"]["enableMonitorStream"] is False
    assert cd["latencyPreference"] == "normal" and cd["enableDvr"] is True
    assert b.id == "b1" and b.is_ours and b.watch_url == "https://www.youtube.com/watch?v=b1"


async def test_create_stream_returns_id_and_key():
    def handler(req):
        body = json.loads(req.content)
        assert body["cdn"] == {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"}
        assert body["contentDetails"]["isReusable"] is True
        return httpx.Response(200, json={"id": "s1", "cdn": {"ingestionInfo": {"streamName": "key-123"}}})
    assert await _client(handler).create_stream("W", "ndi-rmtp appliance") == ("s1", "key-123")


async def test_my_channel():
    h = lambda r: httpx.Response(200, json={"items": [{"id": "UCx", "snippet": {"title": "Ward Ch"}}]})
    assert await _client(h).my_channel("W") == ("UCx", "Ward Ch")


async def test_my_channel_without_channel_raises():
    h = lambda r: httpx.Response(200, json={"items": []})
    with pytest.raises(YouTubeError) as e:
        await _client(h).my_channel("W")
    assert e.value.reason == "noChannel"


async def test_error_body_maps_to_reason():
    h = lambda r: httpx.Response(403, json={"error": {"code": 403, "message": "m",
                                 "errors": [{"reason": "liveStreamingNotEnabled"}]}})
    with pytest.raises(YouTubeError) as e:
        await _client(h).bind("W", "b1", "s1")
    assert (e.value.reason, e.value.status) == ("liveStreamingNotEnabled", 403)


async def test_401_forgets_token_and_retries_once():
    tokens = FakeTokens(); calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        return httpx.Response(401, json={}) if len(calls) == 1 else httpx.Response(200, json={"items": []})

    assert await _client(handler, tokens).get_broadcast("W", "b1") is None
    assert calls == ["Bearer tok1", "Bearer tok2"] and tokens.forgotten == ["W"]


async def test_list_broadcasts_paginates_and_caps_pages():
    pages = []

    def handler(req):
        pages.append(req.url.params.get("pageToken"))
        assert req.url.params["broadcastStatus"] == "completed"
        assert req.url.params["broadcastType"] == "event"
        return httpx.Response(200, json={"items": [_bc(id=f"b{len(pages)}", end="2026-10-01T18:00:00Z")],
                                         "nextPageToken": f"p{len(pages)}"})

    out = await _client(handler).list_broadcasts("W", "completed", max_pages=3)
    assert [b.id for b in out] == ["b1", "b2", "b3"] and pages == [None, "p1", "p2"]
    assert out[0].actual_end == datetime(2026, 10, 1, 18, tzinfo=timezone.utc)


async def test_delete_video_treats_404_as_success():
    await _client(lambda r: httpx.Response(404, json={"error": {"errors": [{"reason": "videoNotFound"}]}})).delete_video("W", "v")


async def test_complete_posts_transition():
    seen = {}
    def handler(req):
        seen.update(req.url.params); seen["path"] = req.url.path
        return httpx.Response(200, json=_bc(status="complete"))
    await _client(handler).complete("W", "b1")
    assert seen["path"].endswith("/liveBroadcasts/transition") and seen["broadcastStatus"] == "complete"


async def test_unmarked_broadcast_is_not_ours():
    h = lambda r: httpx.Response(200, json={"items": [_bc(desc="streamed from OBS")]})
    assert (await _client(h).get_broadcast("W", "b1")).is_ours is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_youtube_api.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement** (`src/ndi_rmtp/youtube_api.py`)

```python
"""Thin YouTube Data API v3 client: only the live-streaming calls the appliance uses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .youtube_auth import TokenStore

API_BASE = "https://www.googleapis.com/youtube/v3"
MARKER = "ndi-rmtp; auto-deletes"
MARKER_LINE = "Streamed by ndi-rmtp; auto-deletes 7 days after the meeting."
_BROADCAST_PARTS = "id,snippet,status,contentDetails"


class YouTubeError(Exception):
    def __init__(self, reason: str, status: int, message: str = ""):
        super().__init__(f"{reason} (HTTP {status}) {message}".strip())
        self.reason = reason
        self.status = status


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Broadcast:
    id: str
    title: str
    description: str
    privacy: str
    life_cycle_status: str
    bound_stream_id: str
    scheduled_start: datetime | None
    actual_end: datetime | None

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def is_ours(self) -> bool:
        return MARKER in self.description

    @classmethod
    def from_api(cls, item: dict) -> "Broadcast":
        sn, st, cd = item.get("snippet", {}), item.get("status", {}), item.get("contentDetails", {})
        return cls(
            id=item["id"], title=sn.get("title", ""), description=sn.get("description", ""),
            privacy=st.get("privacyStatus", ""), life_cycle_status=st.get("lifeCycleStatus", ""),
            bound_stream_id=cd.get("boundStreamId", ""),
            scheduled_start=_ts(sn.get("scheduledStartTime")), actual_end=_ts(sn.get("actualEndTime")),
        )


class YouTubeClient:
    def __init__(self, http: httpx.AsyncClient, tokens: "TokenStore"):
        self._http = http
        self._tokens = tokens

    async def _call(self, ward: str, method: str, path: str, *, params: dict, body: Any = None,
                    ok_404: bool = False) -> dict:
        for attempt in (1, 2):
            token = await self._tokens.access_token(ward)
            resp = await self._http.request(method, f"{API_BASE}/{path}", params=params, json=body,
                                            headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 401 and attempt == 1:
                self._tokens.forget(ward)  # Google revoked the access token early; refresh once
                continue
            break
        if resp.status_code == 404 and ok_404:
            return {}
        if resp.status_code >= 400:
            err = (resp.json() if resp.content else {}).get("error", {})
            errors = err.get("errors") or [{}]
            raise YouTubeError(errors[0].get("reason") or str(err.get("status") or "unknown"),
                               resp.status_code, err.get("message", ""))
        return resp.json() if resp.content else {}

    async def my_channel(self, ward: str) -> tuple[str, str]:
        data = await self._call(ward, "GET", "channels", params={"part": "snippet", "mine": "true"})
        items = data.get("items") or []
        if not items:
            raise YouTubeError("noChannel", 200, "This Google account has no YouTube channel.")
        return items[0]["id"], items[0]["snippet"]["title"]

    async def create_stream(self, ward: str, title: str) -> tuple[str, str]:
        data = await self._call(ward, "POST", "liveStreams", params={"part": "snippet,cdn,contentDetails"}, body={
            "snippet": {"title": title},
            "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
            "contentDetails": {"isReusable": True},
        })
        return data["id"], data["cdn"]["ingestionInfo"]["streamName"]

    async def get_stream_key(self, ward: str, stream_id: str) -> str:
        data = await self._call(ward, "GET", "liveStreams", params={"part": "cdn", "id": stream_id})
        items = data.get("items") or []
        if not items:
            raise YouTubeError("streamNotFound", 404)
        return items[0]["cdn"]["ingestionInfo"]["streamName"]

    async def create_broadcast(self, ward: str, *, title: str, description: str, privacy: str,
                               start: datetime) -> Broadcast:
        data = await self._call(ward, "POST", "liveBroadcasts", params={"part": "snippet,status,contentDetails"}, body={
            "snippet": {"title": title, "description": description, "scheduledStartTime": _iso(start)},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            "contentDetails": {
                "enableAutoStart": True,
                "enableAutoStop": False,
                "monitorStream": {"enableMonitorStream": False},
                "latencyPreference": "normal",
                "enableDvr": True,
            },
        })
        return Broadcast.from_api(data)

    async def bind(self, ward: str, broadcast_id: str, stream_id: str) -> None:
        await self._call(ward, "POST", "liveBroadcasts/bind",
                         params={"part": "id,contentDetails", "id": broadcast_id, "streamId": stream_id})

    async def get_broadcast(self, ward: str, broadcast_id: str) -> Broadcast | None:
        data = await self._call(ward, "GET", "liveBroadcasts", params={"part": _BROADCAST_PARTS, "id": broadcast_id})
        items = data.get("items") or []
        return Broadcast.from_api(items[0]) if items else None

    async def list_broadcasts(self, ward: str, status: str, max_pages: int = 10) -> list[Broadcast]:
        out: list[Broadcast] = []
        token = None
        for _ in range(max_pages):
            params = {"part": _BROADCAST_PARTS, "broadcastStatus": status, "broadcastType": "event", "maxResults": "50"}
            if token:
                params["pageToken"] = token
            data = await self._call(ward, "GET", "liveBroadcasts", params=params)
            out.extend(Broadcast.from_api(i) for i in data.get("items") or [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    async def complete(self, ward: str, broadcast_id: str) -> None:
        await self._call(ward, "POST", "liveBroadcasts/transition",
                         params={"part": "status", "id": broadcast_id, "broadcastStatus": "complete"})

    async def delete_video(self, ward: str, video_id: str) -> None:
        await self._call(ward, "DELETE", "videos", params={"id": video_id}, ok_404=True)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_youtube_api.py -v && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/youtube_api.py tests/test_youtube_api.py
git commit -m "Add thin YouTube Live Streaming API client"
```

---

### Task 4: `broadcasts.py` — connect, prepare, finish, status

**Files:**
- Create: `src/ndi_rmtp/broadcasts.py`
- Test: `tests/test_broadcasts.py`

**Interfaces:**
- Consumes: `YouTubeClient`, `Broadcast`, `YouTubeError`, `MARKER_LINE` (Task 3); `TokenStore`, `ReauthRequired`, `AuthError`, `DeviceFlows` (Task 2); `Credentials`, `update_ward_credentials` (Task 1).
- Produces:
  - `class PrepareError(Exception)`, carrying a user-facing message
  - `@dataclass Prepared(broadcast_id: str, stream_key: str, watch_url: str)`
  - `broadcast_title(ward: str, day: date) -> str`
  - `BroadcastManager(client: YouTubeClient, tokens: TokenStore, creds_path: Path, now: Callable[[], datetime] = utcnow, today: Callable[[], date] = date.today, clock: Callable[[], float] = time.monotonic)`
    - `async complete_connection(ward: str, tokens: dict) -> None`
    - `async disconnect(ward: str) -> None`
    - `async prepare(ward: str) -> Prepared`
    - `async finish(ward: str) -> str | None`: returns a warning message or `None`
    - `async status(ward: str) -> dict`: `{"mode": "api", "connected": bool, "needs_reconnect": bool, "channel_title": str, "state": str, "watch_url": str}`
  - `@dataclass YouTubeServices(manager: BroadcastManager, flows: DeviceFlows)`

- [ ] **Step 1: Write the failing tests** (`tests/test_broadcasts.py`)

```python
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from ndi_rmtp.broadcasts import BroadcastManager, PrepareError, broadcast_title
from ndi_rmtp.config import Credentials, update_ward_credentials
from ndi_rmtp.youtube_api import MARKER_LINE, Broadcast, YouTubeError
from ndi_rmtp.youtube_auth import ReauthRequired

NOW = datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)


def bc(id, status="ready", stream="s1", ours=True, start=NOW, end=None, privacy="public"):
    return Broadcast(id=id, title=f"title {id}", description=MARKER_LINE if ours else "obs",
                     privacy=privacy, life_cycle_status=status, bound_stream_id=stream,
                     scheduled_start=start, actual_end=end)


class FakeClient:
    def __init__(self):
        self.lists = {"active": [], "upcoming": [], "completed": []}
        self.created, self.bound, self.completed, self.deleted = [], [], [], []
        self.fail_on = {}
        self.broadcasts = {}

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise self.fail_on[name]

    async def my_channel(self, ward): return ("UCchan", "Ward Channel")
    async def create_stream(self, ward, title): return ("s1", "ingest-key")
    async def get_stream_key(self, ward, sid): return "ingest-key"

    async def list_broadcasts(self, ward, status, max_pages=10):
        self._maybe_fail("list")
        return list(self.lists[status])

    async def create_broadcast(self, ward, *, title, description, privacy, start):
        self._maybe_fail("create")
        await asyncio.sleep(0)  # yield so concurrent prepares would interleave without the lock
        b = bc(f"new{len(self.created) + 1}", stream="", privacy=privacy)
        self.created.append((title, description, privacy))
        self.lists["upcoming"].append(b)
        return b

    async def bind(self, ward, bid, sid):
        self._maybe_fail("bind")
        self.bound.append((bid, sid))
        for b in self.lists["upcoming"]:
            if b.id == bid:
                b.bound_stream_id = sid

    async def get_broadcast(self, ward, bid): return self.broadcasts.get(bid)

    async def complete(self, ward, bid):
        self._maybe_fail("complete")
        self.completed.append(bid)

    async def delete_video(self, ward, vid):
        self._maybe_fail("delete")
        self.deleted.append(vid)


class FakeTokens:
    def __init__(self): self.seeded = []; self.revoked = []
    def seed(self, ward, tokens): self.seeded.append(ward)
    async def revoke(self, ward): self.revoked.append(ward)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt", yt_stream_id="s1",
                            yt_stream_key="ingest-key", privacy="unlisted")
    client = FakeClient()
    mgr = BroadcastManager(client, FakeTokens(), path, now=lambda: NOW,
                           today=lambda: date(2026, 10, 11))
    return mgr, client, path


def test_title_format_and_truncation():
    assert broadcast_title("Snow Springs Ward", date(2026, 10, 11)) == "Snow Springs Ward Sacrament Meeting — Oct 11, 2026"
    long = broadcast_title("X" * 200, date(2026, 10, 11))
    assert len(long) == 100 and long.endswith("Sacrament Meeting — Oct 11, 2026")


async def test_complete_connection_stores_channel_and_stream(tmp_path):
    path = tmp_path / "credentials.toml"
    mgr = BroadcastManager(FakeClient(), FakeTokens(), path)
    await mgr.complete_connection("W", {"refresh_token": "rt", "access_token": "a", "expires_in": 1})
    w = Credentials.load(path).for_ward("W")
    assert (w.channel_id, w.yt_channel_title, w.yt_stream_id, w.yt_stream_key) == ("UCchan", "Ward Channel", "s1", "ingest-key")


async def test_prepare_creates_and_binds_with_ward_privacy(setup):
    mgr, client, _ = setup
    p = await mgr.prepare("W")
    assert client.created[0][0] == "W Sacrament Meeting — Oct 11, 2026"
    assert MARKER_LINE in client.created[0][1] and client.created[0][2] == "unlisted"
    assert client.bound == [("new1", "s1")]
    assert (p.broadcast_id, p.stream_key) == ("new1", "ingest-key")


async def test_prepare_reuses_recent_upcoming_ours(setup):
    mgr, client, _ = setup
    client.lists["upcoming"] = [bc("old", start=NOW - timedelta(hours=1))]
    assert (await mgr.prepare("W")).broadcast_id == "old"
    assert client.created == []


async def test_prepare_reuses_recent_active_ours(setup):
    mgr, client, _ = setup
    client.lists["active"] = [bc("live1", status="live", start=NOW - timedelta(minutes=30))]
    assert (await mgr.prepare("W")).broadcast_id == "live1"


async def test_prepare_ignores_unmarked_or_other_stream(setup):
    mgr, client, _ = setup
    client.lists["upcoming"] = [bc("obs", ours=False), bc("other", stream="s2")]
    assert (await mgr.prepare("W")).broadcast_id == "new1"


async def test_prepare_completes_stale_active_and_creates_new(setup):
    mgr, client, _ = setup
    client.lists["active"] = [bc("lastweek", status="live", start=NOW - timedelta(days=7))]
    p = await mgr.prepare("W")
    assert client.completed == ["lastweek"] and p.broadcast_id == "new1"


async def test_concurrent_prepare_creates_one_broadcast(setup):
    mgr, client, _ = setup
    a, b = await asyncio.gather(mgr.prepare("W"), mgr.prepare("W"))
    assert len(client.created) == 1 and a.broadcast_id == b.broadcast_id


@pytest.mark.parametrize("exc,needle", [
    (YouTubeError("liveStreamingNotEnabled", 403), "live streaming"),
    (YouTubeError("quotaExceeded", 403), "quota"),
    (ReauthRequired("W"), "reconnect"),
])
async def test_prepare_maps_errors_to_messages(setup, exc, needle):
    mgr, client, _ = setup
    client.fail_on["create"] = exc
    with pytest.raises(PrepareError) as e:
        await mgr.prepare("W")
    assert needle in str(e.value).lower()


async def test_prepare_network_failure_is_prepare_error(setup):
    import httpx
    mgr, client, _ = setup
    client.fail_on["list"] = httpx.ConnectError("down")
    with pytest.raises(PrepareError, match="reach YouTube"):
        await mgr.prepare("W")


async def test_finish_completes_prepared_broadcast(setup):
    mgr, client, _ = setup
    await mgr.prepare("W")
    assert await mgr.finish("W") is None
    assert client.completed == ["new1"]


async def test_finish_without_record_looks_up_active(setup):
    mgr, client, _ = setup
    client.lists["active"] = [bc("live1", status="live")]
    await mgr.finish("W")
    assert client.completed == ["live1"]


@pytest.mark.parametrize("reason", ["redundantTransition", "invalidTransition"])
async def test_finish_tolerates_expected_transitions(setup, reason):
    mgr, client, _ = setup
    await mgr.prepare("W")
    client.fail_on["complete"] = YouTubeError(reason, 403)
    assert await mgr.finish("W") is None


async def test_finish_other_error_returns_warning(setup):
    mgr, client, _ = setup
    await mgr.prepare("W")
    client.fail_on["complete"] = YouTubeError("backendError", 500)
    assert "still show as live" in await mgr.finish("W")


async def test_status_reports_lifecycle_and_caches(setup):
    mgr, client, _ = setup
    await mgr.prepare("W")
    client.broadcasts["new1"] = bc("new1", status="live")
    s = await mgr.status("W")
    assert s["state"] == "live" and s["watch_url"].endswith("new1") and s["connected"]
    client.broadcasts["new1"] = bc("new1", status="complete")
    assert (await mgr.status("W"))["state"] == "live"  # within 5 s cache


async def test_status_with_no_broadcast(setup):
    mgr, _, _ = setup
    assert (await mgr.status("W"))["state"] == "none"


async def test_disconnect_revokes_and_forgets_record(setup):
    mgr, client, _ = setup
    await mgr.prepare("W")
    await mgr.disconnect("W")
    assert mgr._tokens.revoked == ["W"] and "W" not in mgr._active
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_broadcasts.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement** (`src/ndi_rmtp/broadcasts.py`)

```python
"""Per-ward YouTube broadcast lifecycle (create+bind before the encoder connects) and retention."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from .config import Credentials, update_ward_credentials
from .youtube_api import MARKER_LINE, Broadcast, YouTubeClient, YouTubeError
from .youtube_auth import AuthError, DeviceFlows, ReauthRequired, TokenStore

logger = logging.getLogger(__name__)

REUSE_WINDOW = timedelta(hours=12)
STATUS_CACHE_S = 5.0
TITLE_MAX = 100
STREAM_TITLE = "ndi-rmtp appliance"

_REASON_MESSAGES = {
    "liveStreamingNotEnabled": "This YouTube channel doesn't have live streaming turned on. Enable it in YouTube Studio (new channels wait 24 hours).",
    "quotaExceeded": "The appliance has used up today's YouTube quota. Use Start without YouTube setup.",
    "insufficientLivePermissions": "This Google account can't live stream on this channel.",
}


class PrepareError(Exception):
    pass


@dataclass
class Prepared:
    broadcast_id: str
    stream_key: str
    watch_url: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def broadcast_title(ward: str, day: date) -> str:
    suffix = f" Sacrament Meeting — {day:%b} {day.day}, {day.year}"
    return ward[: TITLE_MAX - len(suffix)] + suffix


class BroadcastManager:
    def __init__(self, client: YouTubeClient, tokens: TokenStore, creds_path: Path,
                 now: Callable[[], datetime] = _utcnow, today: Callable[[], date] = date.today,
                 clock: Callable[[], float] = time.monotonic):
        self._client = client
        self._tokens = tokens
        self._path = creds_path
        self._now = now
        self._today = today
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, str] = {}
        self._status_cache: dict[str, tuple[float, dict]] = {}

    def _ward(self, ward: str):
        return Credentials.load(self._path).for_ward(ward)

    async def complete_connection(self, ward: str, tokens: dict) -> None:
        self._tokens.seed(ward, tokens)
        channel_id, title = await self._client.my_channel(ward)
        stream_id, key = await self._client.create_stream(ward, STREAM_TITLE)
        update_ward_credentials(self._path, ward, channel_id=channel_id, yt_channel_title=title,
                                yt_stream_id=stream_id, yt_stream_key=key)
        logger.info("ward %r connected to YouTube channel %r", ward, title)

    async def disconnect(self, ward: str) -> None:
        await self._tokens.revoke(ward)
        self._active.pop(ward, None)
        self._status_cache.pop(ward, None)

    def _is_ours_on(self, b: Broadcast, stream_id: str) -> bool:
        return b.is_ours and b.bound_stream_id == stream_id

    def _recent(self, b: Broadcast) -> bool:
        return b.scheduled_start is not None and self._now() - b.scheduled_start < REUSE_WINDOW

    async def prepare(self, ward: str) -> Prepared:
        lock = self._locks.setdefault(ward, asyncio.Lock())
        async with lock:
            try:
                return await self._prepare(ward)
            except ReauthRequired:
                raise PrepareError("This ward needs to reconnect YouTube on the Ward setup page.")
            except YouTubeError as e:
                raise PrepareError(_REASON_MESSAGES.get(e.reason, f"YouTube refused to create the broadcast ({e.reason})."))
            except (httpx.HTTPError, AuthError) as e:
                raise PrepareError(f"Couldn't reach YouTube ({e}).")

    async def _prepare(self, ward: str) -> Prepared:
        w = self._ward(ward)
        if not w.yt_connected:
            raise ReauthRequired(ward)
        stream_key = w.yt_stream_key or await self._client.get_stream_key(ward, w.yt_stream_id)
        reuse = None
        for status in ("active", "upcoming"):
            for b in await self._client.list_broadcasts(ward, status):
                if not self._is_ours_on(b, w.yt_stream_id):
                    continue
                if self._recent(b):
                    reuse = reuse or b
                elif status == "active":
                    # Last week's broadcast never got its Stop; end it so it can't be reused.
                    logger.info("completing stale broadcast %s for %r", b.id, ward)
                    await self._client.complete(ward, b.id)
        if reuse is None:
            reuse = await self._client.create_broadcast(
                ward, title=broadcast_title(ward, self._today()),
                description=f"{ward} sacrament meeting.\n\n{MARKER_LINE}",
                privacy=w.privacy, start=self._now(),
            )
            await self._client.bind(ward, reuse.id, w.yt_stream_id)
            logger.info("created broadcast %s for %r", reuse.id, ward)
        self._active[ward] = reuse.id
        self._status_cache.pop(ward, None)
        return Prepared(reuse.id, stream_key, reuse.watch_url)

    async def finish(self, ward: str) -> str | None:
        try:
            bid = self._active.pop(ward, None)
            if bid is None:
                stream_id = self._ward(ward).yt_stream_id
                live = [b for b in await self._client.list_broadcasts(ward, "active") if self._is_ours_on(b, stream_id)]
                if not live:
                    return None
                bid = live[0].id
            await self._client.complete(ward, bid)
            logger.info("completed broadcast %s for %r", bid, ward)
        except YouTubeError as e:
            if e.reason in ("redundantTransition", "invalidTransition"):
                return None
            logger.warning("couldn't complete broadcast for %r: %s", ward, e)
            return "The stream stopped, but YouTube may still show as live. End it in YouTube Studio."
        except (httpx.HTTPError, ReauthRequired, AuthError) as e:
            logger.warning("couldn't complete broadcast for %r: %s", ward, e)
            return "The stream stopped, but YouTube may still show as live. End it in YouTube Studio."
        finally:
            self._status_cache.pop(ward, None)
        return None

    async def status(self, ward: str) -> dict:
        cached = self._status_cache.get(ward)
        if cached and self._clock() - cached[0] < STATUS_CACHE_S:
            return cached[1]
        w = self._ward(ward)
        data = {"mode": "api", "connected": w.yt_connected, "needs_reconnect": w.yt_needs_reconnect,
                "channel_title": w.yt_channel_title, "state": "none", "watch_url": ""}
        bid = self._active.get(ward)
        if bid and w.yt_connected:
            try:
                b = await self._client.get_broadcast(ward, bid)
                if b:
                    data["state"], data["watch_url"] = b.life_cycle_status, b.watch_url
            except (YouTubeError, httpx.HTTPError, ReauthRequired, AuthError) as e:
                data["state"] = "unavailable"
                logger.info("status check for %r failed: %s", ward, e)
        self._status_cache[ward] = (self._clock(), data)
        return data


@dataclass
class YouTubeServices:
    manager: BroadcastManager
    flows: DeviceFlows
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_broadcasts.py -v && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/broadcasts.py tests/test_broadcasts.py
git commit -m "Add BroadcastManager: create+bind before encoder, finish on stop"
```

---

### Task 5: Retention — sweep, past streams, keep, delete now

**Files:**
- Modify: `src/ndi_rmtp/broadcasts.py`
- Test: `tests/test_broadcasts.py`

**Interfaces:**
- Consumes: Task 4's `BroadcastManager` and test fixtures (`setup`, `bc`, `FakeClient`).
- Produces (methods on `BroadcastManager`):
  - `RETENTION = timedelta(days=7)`, `SWEEP_FIRST_DELAY_S = 120`, `SWEEP_INTERVAL_S = 6 * 3600`
  - `async sweep() -> list[str]`: returns deleted video IDs
  - `last_sweep: dict` (`{"at": iso str | None, "error": str | None}`)
  - `async past_streams(ward: str) -> list[dict]`: each `{"id", "title", "privacy", "status", "ended_at", "delete_on", "kept", "watch_url"}`, newest first
  - `set_keep(ward: str, video_id: str, keep: bool) -> None`
  - `async delete_now(ward: str, video_id: str) -> None`
  - `async run_sweeper(sleep=asyncio.sleep) -> None`: loops forever (cancelled on shutdown)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_broadcasts.py`)

```python
OLD_END = NOW - timedelta(days=8)
NEW_END = NOW - timedelta(days=2)


async def test_sweep_deletes_only_ours_older_than_seven_days(setup):
    mgr, client, _ = setup
    client.lists["completed"] = [
        bc("old", status="complete", end=OLD_END),
        bc("recent", status="complete", end=NEW_END),
        bc("obs", status="complete", end=OLD_END, ours=False),
    ]
    assert await mgr.sweep() == ["old"]
    assert client.deleted == ["old"]
    assert mgr.last_sweep["error"] is None and mgr.last_sweep["at"]


async def test_sweep_deletes_stale_upcoming_never_live(setup):
    mgr, client, _ = setup
    client.lists["upcoming"] = [bc("never", start=NOW - timedelta(days=8)), bc("fresh", start=NOW)]
    await mgr.sweep()
    assert client.deleted == ["never"]


async def test_sweep_respects_keep_from_any_ward(setup):
    mgr, client, path = setup
    update_ward_credentials(path, "Other", yt_keep=["old"])  # shared channel, kept by the other ward
    client.lists["completed"] = [bc("old", status="complete", end=OLD_END)]
    await mgr.sweep()
    assert client.deleted == []


async def test_sweep_skips_unconnected_wards(setup):
    mgr, client, path = setup
    update_ward_credentials(path, "W", yt_refresh_token="")
    client.lists["completed"] = [bc("old", status="complete", end=OLD_END)]
    await mgr.sweep()
    assert client.deleted == []


async def test_sweep_one_ward_failing_does_not_stop_others(tmp_path):
    path = tmp_path / "credentials.toml"
    for w in ("A", "B"):
        update_ward_credentials(path, w, yt_refresh_token="rt", yt_stream_id="s1")

    class Split(FakeClient):
        async def list_broadcasts(self, ward, status, max_pages=10):
            if ward == "A":
                raise YouTubeError("backendError", 500)
            return [bc("old", status="complete", end=OLD_END)] if status == "completed" else []

    client = Split()
    mgr = BroadcastManager(client, FakeTokens(), path, now=lambda: NOW)
    assert await mgr.sweep() == ["old"]
    assert "A" in mgr.last_sweep["error"]


async def test_past_streams_lists_ours_with_delete_date(setup):
    mgr, client, path = setup
    client.lists["completed"] = [bc("old", status="complete", end=OLD_END), bc("obs", status="complete", end=OLD_END, ours=False)]
    update_ward_credentials(path, "W", yt_keep=["old"])
    rows = await mgr.past_streams("W")
    assert [r["id"] for r in rows] == ["old"]
    assert rows[0]["kept"] is True and rows[0]["delete_on"] == (OLD_END + timedelta(days=7)).date().isoformat()


async def test_set_keep_toggles(setup):
    mgr, _, path = setup
    mgr.set_keep("W", "v1", True); mgr.set_keep("W", "v1", True)
    assert Credentials.load(path).for_ward("W").yt_keep == ["v1"]
    mgr.set_keep("W", "v1", False)
    assert Credentials.load(path).for_ward("W").yt_keep == []


async def test_delete_now_refuses_unmarked_video(setup):
    mgr, client, _ = setup
    client.broadcasts["obs"] = bc("obs", ours=False, status="complete")
    with pytest.raises(PrepareError):
        await mgr.delete_now("W", "obs")
    client.broadcasts["ours"] = bc("ours", status="complete")
    await mgr.delete_now("W", "ours")
    assert client.deleted == ["ours"]


async def test_delete_now_refuses_live_broadcast(setup):
    mgr, client, _ = setup
    client.broadcasts["live1"] = bc("live1", status="live")
    with pytest.raises(PrepareError):
        await mgr.delete_now("W", "live1")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_broadcasts.py -v -k "sweep or past_streams or keep or delete_now"`
Expected: `AttributeError: 'BroadcastManager' object has no attribute 'sweep'`.

- [ ] **Step 3: Implement** (add to `broadcasts.py`)

Module constants, beside `REUSE_WINDOW`:

```python
RETENTION = timedelta(days=7)
SWEEP_FIRST_DELAY_S = 120
SWEEP_INTERVAL_S = 6 * 3600
```

In `__init__` add `self.last_sweep: dict = {"at": None, "error": None}`.

Methods:

```python
    def _expired(self, b: Broadcast) -> bool:
        if b.life_cycle_status == "complete":
            return b.actual_end is not None and self._now() - b.actual_end > RETENTION
        # Created but never went live: age it from its scheduled start.
        return b.life_cycle_status in ("ready", "created") and b.scheduled_start is not None \
            and self._now() - b.scheduled_start > RETENTION

    async def sweep(self) -> list[str]:
        creds = Credentials.load(self._path)
        # Wards sharing a channel see the same broadcasts, so a keep from any ward protects it.
        kept = {vid for w in creds.wards.values() for vid in w.yt_keep}
        deleted, errors = [], []
        for name, w in creds.wards.items():
            if not w.yt_connected:
                continue
            try:
                for status in ("completed", "upcoming"):
                    for b in await self._client.list_broadcasts(name, status):
                        if b.is_ours and b.id not in kept and b.id not in deleted and self._expired(b):
                            await self._client.delete_video(name, b.id)
                            logger.info("retention: deleted %s %r for ward %r", b.id, b.title, name)
                            deleted.append(b.id)
            except (YouTubeError, httpx.HTTPError, ReauthRequired, AuthError) as e:
                logger.warning("retention sweep failed for %r: %s", name, e)
                errors.append(f"{name}: {e}")
        self.last_sweep = {"at": self._now().isoformat(timespec="seconds"),
                           "error": "; ".join(errors) or None}
        return deleted

    async def run_sweeper(self, sleep=asyncio.sleep) -> None:
        await sleep(SWEEP_FIRST_DELAY_S)
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("retention sweep crashed")
            await sleep(SWEEP_INTERVAL_S)

    async def past_streams(self, ward: str) -> list[dict]:
        w = self._ward(ward)
        rows = []
        for status in ("completed", "upcoming"):
            for b in await self._client.list_broadcasts(ward, status):
                if not b.is_ours:
                    continue
                basis = b.actual_end or b.scheduled_start
                rows.append({
                    "id": b.id, "title": b.title, "privacy": b.privacy, "status": b.life_cycle_status,
                    "ended_at": b.actual_end.isoformat() if b.actual_end else None,
                    "delete_on": (basis + RETENTION).date().isoformat() if basis else None,
                    "kept": b.id in w.yt_keep, "watch_url": b.watch_url,
                })
        rows.sort(key=lambda r: r["ended_at"] or "9999", reverse=True)
        return rows

    def set_keep(self, ward: str, video_id: str, keep: bool) -> None:
        current = [v for v in self._ward(ward).yt_keep if v != video_id]
        update_ward_credentials(self._path, ward, yt_keep=current + [video_id] if keep else current)

    async def delete_now(self, ward: str, video_id: str) -> None:
        b = await self._client.get_broadcast(ward, video_id)
        # Only ever delete what this appliance created, and never a broadcast people are watching.
        if b is None or not b.is_ours:
            raise PrepareError("That video wasn't created by this appliance, so it won't be deleted from here.")
        if b.life_cycle_status in ("live", "liveStarting", "testing", "testStarting"):
            raise PrepareError("That broadcast is live right now. Stop it before deleting.")
        await self._client.delete_video(ward, video_id)
        self.set_keep(ward, video_id, False)
        logger.info("deleted %s %r for ward %r on request", video_id, b.title, ward)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_broadcasts.py -v && uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/broadcasts.py tests/test_broadcasts.py
git commit -m "Add 7-day retention sweep, keep list, and delete-now"
```

---

### Task 6: Wire start / stop / status into the app

**Files:**
- Modify: `src/ndi_rmtp/app.py` (`build_app` signature, `/api/start`, `/api/stop`, `/api/youtube`, lifespan)
- Modify: `src/ndi_rmtp/bootstrap.py` (`make_app`)
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `YouTubeServices`, `PrepareError`, `Prepared` (Task 4); `Credentials`, `credentials_path_for`, `update_ward_credentials`, `YoutubeConfig` (Task 1).
- Produces:
  - `build_app(config_path, streamer, discovery, ptz=None, youtube: Optional[YouTubeServices] = None)`
  - `POST /api/start` accepts an optional `skip_youtube: bool = Form(False)`. It returns **424** with `detail` = the message when prepare fails (the UI keys the fallback off 424), and returns `{"ok": True, "watch_url": str | None}`.
  - `POST /api/stop` returns `{"ok": True, "warning": str | None}`.
  - `GET /api/youtube` returns `manager.status(ward)` (with `mode: "api"`) for a linked ward when YouTube is enabled; otherwise the existing payload plus `"mode": "scrape"`.
  - Helper inside `build_app`: `def _yt_ward(cfg, ward_name) -> WardCredentials | None` returns the ward's credentials when YouTube is enabled and the ward is linked, else `None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_routes.py`)

```python
from ndi_rmtp.broadcasts import PrepareError, Prepared, YouTubeServices
from ndi_rmtp.config import YoutubeConfig, update_ward_credentials

AUTH = _basic("admin", "hunter2")


@pytest.fixture
def yt():
    manager = MagicMock()
    manager.prepare = AsyncMock(return_value=Prepared("b1", "api-key", "https://www.youtube.com/watch?v=b1"))
    manager.finish = AsyncMock(return_value=None)
    manager.status = AsyncMock(return_value={"mode": "api", "connected": True, "state": "live",
                                             "watch_url": "u", "needs_reconnect": False, "channel_title": "C"})
    return YouTubeServices(manager=manager, flows=MagicMock())


def _yt_client(tmp_path, config, streamer, discovery, yt, *, enabled=True, linked=True, **ward_fields):
    config_path = tmp_path / "config.toml"
    if enabled:
        config.youtube = YoutubeConfig(client_id="cid", client_secret="s")
    config.save(config_path)
    if linked:
        update_ward_credentials(credentials_path_for(config_path), "Primary",
                                yt_refresh_token="rt", yt_stream_key="api-key", **ward_fields)
    elif ward_fields:
        update_ward_credentials(credentials_path_for(config_path), "Primary", **ward_fields)
    return TestClient(build_app(config_path=config_path, streamer=streamer, discovery=discovery, youtube=yt))


def test_start_prepares_broadcast_before_spawning(tmp_path, config, streamer, discovery, yt):
    order = []
    yt.manager.prepare.side_effect = lambda w: order.append("prepare") or Prepared("b1", "api-key", "u")
    streamer.start.side_effect = lambda **kw: order.append("start")
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    r = c.post("/api/start", data={"source": "SRC1", "quality": "high"}, headers=AUTH)
    assert r.status_code == 200 and order == ["prepare", "start"]
    assert streamer.start.call_args.kwargs["stream_key"] == "api-key"


def test_start_prepare_failure_is_424_and_does_not_spawn(tmp_path, config, streamer, discovery, yt):
    yt.manager.prepare.side_effect = PrepareError("quota gone")
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    r = c.post("/api/start", data={"source": "SRC1", "quality": "high"}, headers=AUTH)
    assert r.status_code == 424 and r.json()["detail"] == "quota gone"
    streamer.start.assert_not_called()


def test_start_skip_youtube_streams_to_ingest_key(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    r = c.post("/api/start", data={"source": "SRC1", "quality": "high", "skip_youtube": "true"}, headers=AUTH)
    assert r.status_code == 200
    yt.manager.prepare.assert_not_called()
    assert streamer.start.call_args.kwargs["stream_key"] == "api-key"


def test_start_needs_reconnect_offers_fallback(tmp_path, config, streamer, discovery, yt):
    yt.manager.prepare.side_effect = PrepareError("This ward needs to reconnect YouTube")
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False, yt_needs_reconnect=True)
    r = c.post("/api/start", data={"source": "SRC1", "quality": "high"}, headers=AUTH)
    assert r.status_code == 424
    streamer.start.assert_not_called()


def test_start_unlinked_ward_uses_manual_key(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    c.post("/api/start", data={"source": "SRC1", "quality": "high"}, headers=AUTH)
    yt.manager.prepare.assert_not_called()
    assert streamer.start.call_args.kwargs["stream_key"] == "abc-key"


def test_youtube_disabled_ignores_linked_ward(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, enabled=False)
    c.post("/api/start", data={"source": "SRC1", "quality": "high"}, headers=AUTH)
    yt.manager.prepare.assert_not_called()
    assert streamer.start.call_args.kwargs["stream_key"] == "abc-key"


def test_stop_finishes_after_streamer_stops(tmp_path, config, streamer, discovery, yt):
    order = []
    streamer.stop.side_effect = lambda: order.append("stop")
    yt.manager.finish.side_effect = lambda w: order.append("finish") or "may still show as live"
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    r = c.post("/api/stop", headers=AUTH)
    assert order == ["stop", "finish"] and r.json()["warning"] == "may still show as live"


def test_api_youtube_uses_api_status_for_linked_ward(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    assert c.get("/api/youtube", headers=AUTH).json()["mode"] == "api"


def test_api_youtube_scrape_mode_for_unlinked_ward(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    assert c.get("/api/youtube", headers=AUTH).json()["mode"] == "scrape"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_routes.py -v -k "prepare or skip_youtube or reconnect or unlinked or disabled_ignores or finishes or api_youtube_uses or scrape_mode"`
Expected: `TypeError: build_app() got an unexpected keyword argument 'youtube'`.

- [ ] **Step 3: Implement**

In `app.py` imports: add `import asyncio`, `from contextlib import asynccontextmanager`, `from .broadcasts import PrepareError, YouTubeServices`.

Change the signature and app construction:

```python
def build_app(
    config_path: Path,
    streamer: Streamer,
    discovery: NDIDiscovery,
    ptz: Optional["PtzController"] = None,  # noqa: F821 — string-only forward ref
    youtube: Optional[YouTubeServices] = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(youtube.manager.run_sweeper()) if youtube else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ndi-rmtp", lifespan=lifespan)
```

After `require_auth = ...`:

```python
    def _yt_ward(cfg: Config, ward_name: str):
        if youtube is None or not cfg.youtube.enabled:
            return None
        w = Credentials.load(credentials_path_for(config_path)).for_ward(ward_name)
        return w if w.yt_linked else None
```

Replace `/api/start`. The parameter list gains `skip_youtube`, the early `active.key` check moves below the key resolution, and the body is otherwise unchanged:

```python
    @app.post("/api/start", dependencies=[Depends(require_auth)])
    async def api_start(
        source: str = Form(...),
        quality: str = Form(...),
        skip_youtube: bool = Form(False),
    ):
        cfg = get_config()
        active = cfg.stream.active()
        if active is None:
            raise HTTPException(
                status_code=400,
                detail="No ward is selected or the selected ward isn't set up correctly. Please contact your Stake Technology Specialist.",
            )
        if quality not in ("low", "medium", "high"):
            raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")

        # (existing discovery comment + lookup block, unchanged)
        sources = await discovery.list()
        matched = next((s for s in sources if s.name == source), None)
        if matched is None:
            raise HTTPException(
                status_code=409,
                detail="Camera not found on the network. Make sure it is powered on, then wait a moment and try again.",
            )

        # The broadcast must exist before the encoder connects or YouTube parks it in "Preparing stream".
        stream_key, watch_url = active.key, None
        yt_ward = _yt_ward(cfg, active.name)
        if yt_ward is not None:
            if skip_youtube:
                stream_key = yt_ward.yt_stream_key or active.key
            else:
                try:
                    prepared = await youtube.manager.prepare(active.name)
                except PrepareError as e:
                    raise HTTPException(status_code=424, detail=str(e))
                stream_key, watch_url = prepared.stream_key, prepared.watch_url
        if not stream_key or stream_key.startswith("REPLACE_WITH"):
            raise HTTPException(
                status_code=400,
                detail="No ward is selected or the selected ward isn't set up correctly. Please contact your Stake Technology Specialist.",
            )

        cfg.stream.last_source = source
        cfg.stream.quality = quality
        cfg.save(config_path)

        connect_target = matched.address or matched.name
        try:
            await streamer.start(source=connect_target, stream_key=stream_key, quality=quality)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True, "watch_url": watch_url}
```

Keep the existing explanatory comment about discovery addresses above the `sources = ...` line.

Replace `/api/stop`:

```python
    @app.post("/api/stop", dependencies=[Depends(require_auth)])
    async def api_stop():
        await streamer.stop()
        cfg = get_config()
        warning = None
        if _yt_ward(cfg, cfg.stream.active_ward) is not None:
            warning = await youtube.manager.finish(cfg.stream.active_ward)
        return {"ok": True, "warning": warning}
```

In `/api/youtube`, at the top of the body after `ward = cfg.stream.active()`:

```python
        if ward is not None and _yt_ward(cfg, ward.name) is not None:
            return await youtube.manager.status(ward.name)
```

and before `return data` add `data["mode"] = "scrape"`.

In `bootstrap.py`, `make_app`, before `return build_app(...)`:

```python
    youtube_services = _make_youtube(config_path)
```

pass `youtube=youtube_services`, and add the module function:

```python
def _make_youtube(config_path: Path) -> YouTubeServices:
    http = httpx.AsyncClient(timeout=20.0)
    client_cfg = lambda: Config.load(config_path).youtube
    creds_path = credentials_path_for(config_path)
    tokens = TokenStore(http, client_cfg, creds_path)
    manager = BroadcastManager(YouTubeClient(http, tokens), tokens, creds_path)
    flows = DeviceFlows(http, client_cfg, on_tokens=manager.complete_connection)
    return YouTubeServices(manager=manager, flows=flows)
```

with imports `import httpx`, `from .broadcasts import BroadcastManager, YouTubeServices`, `from .config import credentials_path_for`, `from .youtube_api import YouTubeClient`, `from .youtube_auth import DeviceFlows, TokenStore`. The services are always built. Whether YouTube is enabled is decided per request from `config.youtube`, so editing `client_id` takes effect without a restart.

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -q`
Expected: all pass, including the pre-existing `/api/start` tests (they don't pass `youtube`).

- [ ] **Step 5: Commit**

```bash
git add src/ndi_rmtp/app.py src/ndi_rmtp/bootstrap.py tests/test_routes.py
git commit -m "Create and bind the YouTube broadcast before spawning the encoder"
```

---

### Task 7: `/setup` — connect, disconnect, privacy

**Files:**
- Modify: `src/ndi_rmtp/app.py` (setup view context + four endpoints)
- Modify: `src/ndi_rmtp/templates/setup.html`
- Modify: `src/ndi_rmtp/static/style.css` (small `.yt-connect` block)
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `YouTubeServices.flows.begin/state`, `manager.disconnect` (Tasks 2, 4); `PRIVACY_CHOICES`, `update_ward_credentials` (Task 1); `AuthError` (Task 2).
- Produces endpoints (all `require_auth`, all **404** when `cfg.youtube.enabled` is false or `youtube is None`, all 400 for an unknown ward):
  - `POST /api/youtube/connect` (form `ward`) returns `FlowState.to_dict()`, or 502 with the `AuthError` message
  - `GET /api/youtube/connect?ward=` returns `FlowState.to_dict()`, or `{"state": "none"}`
  - `POST /api/youtube/disconnect` (form `ward`) returns `{"ok": True}`
  - `POST /api/youtube/privacy` (form `ward`, `privacy`) returns `{"ok": True}`, or 400 for a bad value
- The setup template context gains `youtube_enabled: bool` and per-ward `yt_connected`, `yt_needs_reconnect`, `yt_channel_title`, `privacy`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_routes.py`)

```python
from ndi_rmtp.youtube_auth import FlowState


def test_setup_hides_youtube_when_disabled(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, enabled=False, linked=False)
    assert "Connect YouTube" not in c.get("/setup", headers=AUTH).text
    assert c.post("/api/youtube/connect", data={"ward": "Primary"}, headers=AUTH).status_code == 404


def test_setup_shows_connect_for_unlinked_ward(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    assert "Connect YouTube" in c.get("/setup", headers=AUTH).text


def test_setup_shows_connected_channel_and_never_tokens(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, yt_channel_title="Ward Channel")
    html = c.get("/setup", headers=AUTH).text
    assert "Ward Channel" in html and "Disconnect" in html
    assert "rt" not in html.split("<main>")[1].split("</main>")[0].replace("rt ", "")  # crude guard
    assert "api-key" not in html


def test_connect_begins_device_flow(tmp_path, config, streamer, discovery, yt):
    yt.flows.begin = AsyncMock(return_value=FlowState("pending", "ABCD-EFGH", "https://www.google.com/device", 1800))
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    r = c.post("/api/youtube/connect", data={"ward": "Primary"}, headers=AUTH)
    assert r.json()["user_code"] == "ABCD-EFGH"
    yt.flows.begin.assert_awaited_once_with("Primary")


def test_connect_status_none_before_begin(tmp_path, config, streamer, discovery, yt):
    yt.flows.state = MagicMock(return_value=None)
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    assert c.get("/api/youtube/connect", params={"ward": "Primary"}, headers=AUTH).json() == {"state": "none"}


def test_connect_rejects_unknown_ward(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, linked=False)
    assert c.post("/api/youtube/connect", data={"ward": "Nope"}, headers=AUTH).status_code == 400


def test_disconnect_calls_manager(tmp_path, config, streamer, discovery, yt):
    yt.manager.disconnect = AsyncMock()
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    assert c.post("/api/youtube/disconnect", data={"ward": "Primary"}, headers=AUTH).status_code == 200
    yt.manager.disconnect.assert_awaited_once_with("Primary")


def test_privacy_saves_and_validates(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    assert c.post("/api/youtube/privacy", data={"ward": "Primary", "privacy": "unlisted"}, headers=AUTH).status_code == 200
    assert Credentials.load(credentials_path_for(tmp_path / "config.toml")).for_ward("Primary").privacy == "unlisted"
    assert c.post("/api/youtube/privacy", data={"ward": "Primary", "privacy": "secret"}, headers=AUTH).status_code == 400
```

Replace the crude guard line in `test_setup_shows_connected_channel_and_never_tokens` with a sentinel: pass `yt_refresh_token="RT-SENTINEL-9f3"` through `_yt_client`, then `assert "RT-SENTINEL-9f3" not in html`. To allow that, change `_yt_client` so `linked=True` applies `yt_refresh_token="rt"` only when the caller didn't supply one (`ward_fields.setdefault("yt_refresh_token", "rt")`).

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_routes.py -v -k "setup_hides or setup_shows or connect or disconnect or privacy"`
Expected: failures (404s and missing text).

- [ ] **Step 3: Implement endpoints** (in `app.py`, after `api_ward_credentials`; import `PRIVACY_CHOICES, update_ward_credentials` from `.config` and `AuthError` from `.youtube_auth`)

```python
    def _require_youtube_ward(ward: str) -> Config:
        cfg = get_config()
        if youtube is None or not cfg.youtube.enabled:
            raise HTTPException(status_code=404, detail="YouTube integration isn't configured on this appliance.")
        if cfg.stream.find_ward(ward) is None:
            raise HTTPException(status_code=400, detail=f"No ward named {ward!r} on this appliance.")
        return cfg

    @app.post("/api/youtube/connect", dependencies=[Depends(require_auth)])
    async def api_youtube_connect(ward: str = Form(...)):
        _require_youtube_ward(ward)
        try:
            return (await youtube.flows.begin(ward)).to_dict()
        except AuthError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/api/youtube/connect", dependencies=[Depends(require_auth)])
    def api_youtube_connect_status(ward: str):
        _require_youtube_ward(ward)
        st = youtube.flows.state(ward)
        return st.to_dict() if st else {"state": "none"}

    @app.post("/api/youtube/disconnect", dependencies=[Depends(require_auth)])
    async def api_youtube_disconnect(ward: str = Form(...)):
        _require_youtube_ward(ward)
        await youtube.manager.disconnect(ward)
        return {"ok": True}

    @app.post("/api/youtube/privacy", dependencies=[Depends(require_auth)])
    def api_youtube_privacy(ward: str = Form(...), privacy: str = Form(...)):
        _require_youtube_ward(ward)
        if privacy not in PRIVACY_CHOICES:
            raise HTTPException(status_code=400, detail=f"Privacy must be one of {', '.join(PRIVACY_CHOICES)}.")
        update_ward_credentials(credentials_path_for(config_path), ward, privacy=privacy)
        return {"ok": True}
```

In the `setup()` view, extend each ward dict (`wc = creds.for_ward(w.name)`):

```python
                "yt_connected": wc.yt_connected,
                "yt_needs_reconnect": wc.yt_needs_reconnect,
                "yt_channel_title": wc.yt_channel_title,
                "privacy": wc.privacy,
```

and add `"youtube_enabled": youtube is not None and cfg.youtube.enabled,` plus `"privacy_choices": PRIVACY_CHOICES,` to the template context.

- [ ] **Step 4: Implement the template**

In `setup.html`, inside each `.ward-card`, **above** the `<form class="ward-form">`, add:

```html
      {% if youtube_enabled %}
      <div class="yt-connect" data-ward="{{ w.name }}">
        {% if w.yt_connected %}
        <p class="yt-connected">Connected to YouTube as <strong>{{ w.yt_channel_title }}</strong>.
          Start creates and ends this ward's broadcast automatically.</p>
        <label>Broadcast privacy
          <select class="yt-privacy">
            {% for p in privacy_choices %}
            <option value="{{ p }}" {% if p == w.privacy %}selected{% endif %}>{{ p|capitalize }}</option>
            {% endfor %}
          </select>
        </label>
        <button type="button" class="btn-secondary yt-disconnect">Disconnect YouTube</button>
        {% else %}
        {% if w.yt_needs_reconnect %}
        <p class="err">YouTube sign-in for this ward stopped working. Reconnect to keep automatic broadcasts.</p>
        {% endif %}
        <button type="button" class="btn-primary yt-connect-btn">{% if w.yt_needs_reconnect %}Reconnect YouTube{% else %}Connect YouTube{% endif %}</button>
        <div class="yt-code" hidden>
          <p>On a phone or computer, go to <a class="yt-code-url" target="_blank" rel="noopener"></a>
            and enter <strong class="yt-code-value"></strong></p>
          <p class="hint">Sign in as this ward's YouTube account and pick its channel. If Google
            warns the app isn't verified, choose Advanced → Continue.
            <span class="yt-code-countdown"></span></p>
        </div>
        {% endif %}
      </div>
      {% endif %}
```

Wrap the existing Stream key `<label>` in `{% if not (youtube_enabled and w.yt_connected) %}…{% else %}<p class="hint">The stream key is managed by YouTube while connected. Disconnect to use a pasted key.</p>{% endif %}`.

Append to the page `<script>`:

```js
    // ---- YouTube connect (device flow) ---------------------------------
    async function postForm(url, fields) {
      const r = await fetch(url, { method: "POST", body: new URLSearchParams(fields) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      return data;
    }
    document.querySelectorAll(".yt-connect").forEach((box) => {
      const ward = box.dataset.ward;
      const btn = box.querySelector(".yt-connect-btn");
      const code = box.querySelector(".yt-code");
      btn?.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          let st = await postForm("/api/youtube/connect", { ward });
          code.hidden = false;
          box.querySelector(".yt-code-value").textContent = st.user_code;
          const a = box.querySelector(".yt-code-url");
          a.href = st.verification_url; a.textContent = st.verification_url.replace(/^https?:\/\//, "");
          while (st.state === "pending") {
            box.querySelector(".yt-code-countdown").textContent =
              `Code expires in ${Math.ceil(st.expires_in_s / 60)} min.`;
            await new Promise(r => setTimeout(r, 3000));
            st = await fetch(`/api/youtube/connect?ward=${encodeURIComponent(ward)}`).then(r => r.json());
          }
          if (st.state === "connected") { location.reload(); return; }
          showFlash(st.message || "Sign-in didn't finish. Try again.");
        } catch (e) {
          showFlash(e.message);
        }
        code.hidden = true;
        btn.disabled = false;
      });
      box.querySelector(".yt-disconnect")?.addEventListener("click", async () => {
        try { await postForm("/api/youtube/disconnect", { ward }); location.reload(); }
        catch (e) { showFlash(e.message); }
      });
      box.querySelector(".yt-privacy")?.addEventListener("change", async (evt) => {
        try { await postForm("/api/youtube/privacy", { ward, privacy: evt.target.value }); }
        catch (e) { showFlash(e.message); }
      });
    });
```

Add to `style.css` (reuse the existing tokens; check the file for the variable names it already uses for spacing and muted text):

```css
.yt-connect { display: grid; gap: .5rem; margin-bottom: 1rem; }
.yt-code-value { font-size: 1.4rem; letter-spacing: .1em; }
```

- [ ] **Step 5: Run tests, then check the page by hand**

Run: `uv run pytest -q`
Expected: all pass.

Manual: with `[youtube] client_id = "x"` in a scratch config, run `uv run ndi-rmtp`, open `/setup` and confirm the Connect button renders. Remove the scratch value afterwards.

- [ ] **Step 6: Commit**

```bash
git add src/ndi_rmtp/app.py src/ndi_rmtp/templates/setup.html src/ndi_rmtp/static/style.css tests/test_routes.py
git commit -m "Add YouTube connect/disconnect/privacy to /setup"
```

---

### Task 8: `/streams` — past streams page

**Files:**
- Modify: `src/ndi_rmtp/app.py`
- Create: `src/ndi_rmtp/templates/streams.html`
- Modify: `src/ndi_rmtp/templates/setup.html` and `index.html` footers (add a `Past streams` link, only when YouTube is enabled)
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `manager.past_streams`, `set_keep`, `delete_now`, `last_sweep` (Task 5); `_require_youtube_ward` (Task 7).
- Produces:
  - `GET /streams` (HTML, 404 when YouTube is disabled)
  - `GET /api/youtube/streams` returns `{"wards": [{"ward": str, "channel_title": str, "streams": [...], "error": str | None}], "last_sweep": {...}}`, for linked wards only
  - `POST /api/youtube/streams/{video_id}/keep` (form `ward`, `keep: bool`) returns `{"ok": True}`
  - `POST /api/youtube/streams/{video_id}/delete` (form `ward`) returns `{"ok": True}`, or 409 with the `PrepareError` message

- [ ] **Step 1: Write the failing tests** (append to `tests/test_routes.py`)

```python
def test_streams_page_404_when_disabled(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt, enabled=False)
    assert c.get("/streams", headers=AUTH).status_code == 404


def test_streams_api_lists_linked_wards(tmp_path, config, streamer, discovery, yt):
    yt.manager.past_streams = AsyncMock(return_value=[{"id": "v1", "title": "T", "kept": False}])
    yt.manager.last_sweep = {"at": None, "error": None}
    c = _yt_client(tmp_path, config, streamer, discovery, yt, yt_channel_title="C")
    data = c.get("/api/youtube/streams", headers=AUTH).json()
    assert data["wards"][0]["ward"] == "Primary" and data["wards"][0]["streams"][0]["id"] == "v1"


def test_streams_api_reports_per_ward_error(tmp_path, config, streamer, discovery, yt):
    yt.manager.past_streams = AsyncMock(side_effect=YouTubeError("backendError", 500))
    yt.manager.last_sweep = {"at": None, "error": None}
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    row = c.get("/api/youtube/streams", headers=AUTH).json()["wards"][0]
    assert row["streams"] == [] and "backendError" in row["error"]


def test_keep_and_delete(tmp_path, config, streamer, discovery, yt):
    yt.manager.set_keep = MagicMock()
    yt.manager.delete_now = AsyncMock(side_effect=PrepareError("is live"))
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    assert c.post("/api/youtube/streams/v1/keep", data={"ward": "Primary", "keep": "true"}, headers=AUTH).status_code == 200
    yt.manager.set_keep.assert_called_once_with("Primary", "v1", True)
    r = c.post("/api/youtube/streams/v1/delete", data={"ward": "Primary"}, headers=AUTH)
    assert r.status_code == 409 and r.json()["detail"] == "is live"
```

Add `from ndi_rmtp.youtube_api import YouTubeError` to the imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_routes.py -v -k "streams or keep_and_delete"`
Expected: 404 or 405 failures.

- [ ] **Step 3: Implement endpoints** (in `app.py`; import `httpx`, `YouTubeError`, and `ReauthRequired`)

```python
    @app.get("/streams", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def streams_page(request: Request):
        cfg = get_config()
        if youtube is None or not cfg.youtube.enabled:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(request, "streams.html", {"config": cfg})

    @app.get("/api/youtube/streams", dependencies=[Depends(require_auth)])
    async def api_youtube_streams():
        cfg = get_config()
        if youtube is None or not cfg.youtube.enabled:
            raise HTTPException(status_code=404)
        rows = []
        for w in cfg.stream.wards:
            wc = _yt_ward(cfg, w.name)
            if wc is None:
                continue
            row = {"ward": w.name, "channel_title": wc.yt_channel_title, "streams": [], "error": None}
            try:
                row["streams"] = await youtube.manager.past_streams(w.name)
            except (YouTubeError, httpx.HTTPError, ReauthRequired) as e:
                row["error"] = str(e) or type(e).__name__
            rows.append(row)
        return {"wards": rows, "last_sweep": youtube.manager.last_sweep}

    @app.post("/api/youtube/streams/{video_id}/keep", dependencies=[Depends(require_auth)])
    def api_youtube_keep(video_id: str, ward: str = Form(...), keep: bool = Form(...)):
        _require_youtube_ward(ward)
        youtube.manager.set_keep(ward, video_id, keep)
        return {"ok": True}

    @app.post("/api/youtube/streams/{video_id}/delete", dependencies=[Depends(require_auth)])
    async def api_youtube_delete(video_id: str, ward: str = Form(...)):
        _require_youtube_ward(ward)
        try:
            await youtube.manager.delete_now(ward, video_id)
        except PrepareError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}
```

- [ ] **Step 4: Implement `streams.html`**

Copy the `<head>`, `<header>`, flash `div`, footer and `showFlash` script from `setup.html`, changing the title to `Past streams — NDI → YouTube Relay`, the `h1` to `Past streams`, and the back link to `← Back to stream`. Body:

```html
  <main>
    <section class="card">
      <p class="setup-intro">Broadcasts this appliance created. Each is deleted from YouTube
        7 days after the meeting unless it's kept. Videos made any other way are never touched.</p>
      <p class="hint" id="sweep-status"></p>
    </section>
    <div id="ward-list"><section class="card"><p>Loading…</p></section></div>
  </main>
```

Script (after `showFlash`):

```js
    const list = document.getElementById("ward-list");
    const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    async function post(url, fields) {
      const r = await fetch(url, { method: "POST", body: new URLSearchParams(fields) });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${r.status}`); }
    }
    async function load() {
      const data = await fetch("/api/youtube/streams").then(r => r.json());
      const s = data.last_sweep || {};
      document.getElementById("sweep-status").textContent =
        s.error ? `Last cleanup failed: ${s.error}` : (s.at ? `Last cleanup: ${new Date(s.at).toLocaleString()}` : "");
      if (!data.wards.length) { list.innerHTML = `<section class="card"><p>No wards are connected to YouTube. Connect one on <a href="/setup">Ward setup</a>.</p></section>`; return; }
      list.innerHTML = data.wards.map(w => `
        <section class="card">
          <h2>${esc(w.ward)} <small class="hint">${esc(w.channel_title)}</small></h2>
          ${w.error ? `<p class="err">Couldn't load from YouTube: ${esc(w.error)}</p>` : ""}
          ${w.streams.length ? `<ul class="stream-list">${w.streams.map(v => `
            <li data-ward="${esc(w.ward)}" data-id="${esc(v.id)}">
              <a href="${esc(v.watch_url)}" target="_blank" rel="noopener">${esc(v.title)}</a>
              <span class="hint">${esc(v.privacy)} · ${v.kept ? "kept" : (v.delete_on ? "deletes " + esc(v.delete_on) : esc(v.status))}</span>
              <button type="button" class="btn-secondary act-keep" data-keep="${v.kept ? "false" : "true"}">${v.kept ? "Unkeep" : "Keep"}</button>
              <button type="button" class="btn-secondary act-delete">Delete now</button>
              <span class="confirm" hidden>Delete permanently? <button type="button" class="btn-secondary act-confirm">Yes, delete</button></span>
            </li>`).join("")}</ul>` : (w.error ? "" : "<p class='hint'>No broadcasts yet.</p>")}
        </section>`).join("");
    }
    list.addEventListener("click", async (evt) => {
      const li = evt.target.closest("li[data-id]");
      if (!li) return;
      const { ward, id } = li.dataset;
      try {
        if (evt.target.classList.contains("act-keep")) {
          await post(`/api/youtube/streams/${encodeURIComponent(id)}/keep`, { ward, keep: evt.target.dataset.keep });
        } else if (evt.target.classList.contains("act-delete")) {
          li.querySelector(".confirm").hidden = false; return;  // in-page confirm; never a browser dialog
        } else if (evt.target.classList.contains("act-confirm")) {
          await post(`/api/youtube/streams/${encodeURIComponent(id)}/delete`, { ward });
        } else return;
        await load();
      } catch (e) { showFlash(e.message); }
    });
    load().catch(e => showFlash(`Couldn't load streams: ${e.message}`));
```

Footer links: in `index.html` and `setup.html`, beside the existing footer link, add `{% if youtube_enabled %}<span class="footer-sep">·</span><a class="footer-link" href="/streams">Past streams</a>{% endif %}`. Pass `"youtube_enabled": youtube is not None and cfg.youtube.enabled` into the `index` view context too.

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ndi_rmtp/app.py src/ndi_rmtp/templates/streams.html src/ndi_rmtp/templates/setup.html src/ndi_rmtp/templates/index.html tests/test_routes.py
git commit -m "Add Past streams page with keep and delete-now"
```

---

### Task 9: Main page — connected-ward YouTube UI, fallback start, stop warning

**Files:**
- Modify: `src/ndi_rmtp/templates/index.html`
- Modify: `src/ndi_rmtp/static/mock.js`
- Test: `tests/test_template_invariants.py`, `tests/test_routes.py`

**Interfaces:**
- Consumes: `/api/youtube` with `mode: "api" | "scrape"` (Task 6); `/api/start` 424 and `skip_youtube` (Task 6); `/api/stop` `warning` (Task 6).
- Produces: DOM ids `yt-api-card` (setup side), `yt-api-help` (streaming side), `yt-fallback-btn`.

UI behaviour (the spec's UI section):
- `mode === "api"`: hide `#yt-setup-card` and `#yt-help-card`, and show `#yt-api-card` with "Connected to *channel*. Start creates today's broadcast." When `needs_reconnect` is set, it reads "YouTube sign-in stopped working. Reconnect on Ward setup." The live pill shows when `state === "live"`, with `watch_url` and a Copy link button. While streaming and not live, `.yt-api-status` shows `Waiting for YouTube…`. If `state !== "live"` and status `uptime_s > 120`, show `#yt-api-help`: "YouTube hasn't started the broadcast. Try Stop, then Start."
- `mode === "scrape"`: the existing behaviour, unchanged.
- In API mode, poll `/api/youtube` every 10 s while STARTING/STREAMING and every 60 s otherwise. Keep the existing 30 s / 120 s cadence for scrape mode. Re-poll immediately on ward `change`.
- `/api/start` returns 424: show the flash message and reveal `#yt-fallback-btn` ("Start without YouTube setup"). Clicking it POSTs the start form plus `skip_youtube=true`. Hide the button on the next successful start.
- `/api/stop` succeeds with a `warning`: `showFlash(warning)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_template_invariants.py`:

```python
def test_api_mode_elements_present():
    html = _template_source()
    for el_id in ("yt-api-card", "yt-api-help", "yt-fallback-btn"):
        assert f'id="{el_id}"' in html
    assert "skip_youtube" in _script_source(html)
    assert 'mode === "api"' in _script_source(html)
```

In `tests/test_routes.py`:

```python
def test_index_renders_with_youtube_enabled(tmp_path, config, streamer, discovery, yt):
    c = _yt_client(tmp_path, config, streamer, discovery, yt)
    html = c.get("/", headers=AUTH).text
    assert 'id="yt-api-card"' in html and "/streams" in html
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_template_invariants.py tests/test_routes.py -v -k "api_mode or youtube_enabled"`
Expected: FAIL.

- [ ] **Step 3: Implement markup**

After `#yt-setup-card` in `index.html`:

```html
    {# Connected wards: the appliance creates the broadcast itself, so no control-room step. #}
    <section class="card setup-only" id="yt-api-card" hidden>
      <h2>YouTube</h2>
      <div class="yt-line">
        <span class="yt-dot" aria-hidden="true"></span>
        <div class="yt-body">
          <div class="yt-headline" id="yt-api-headline">Connected</div>
          <div class="yt-sub" id="yt-api-note">Start creates today's broadcast automatically.</div>
        </div>
      </div>
      <button type="button" class="btn-secondary" id="yt-fallback-btn" hidden>Start without YouTube setup</button>
    </section>
```

After `#yt-help-card`:

```html
    {# API mode observes the broadcast directly, so a negative here is real (see youtube.py). #}
    <section class="card streaming-only" id="yt-api-help" hidden>
      <h2>YouTube hasn't started the broadcast</h2>
      <p class="yt-sub">The encoder is sending, but YouTube still hasn't gone live. Try Stop, then Start.</p>
    </section>
```

Inside `.chip-right`, before the pill, add `<span class="yt-api-status hint" hidden></span>`, and after the pill add `<button type="button" class="chip-copy" id="yt-copy-btn" hidden>Copy link</button>`.

- [ ] **Step 4: Implement script**

In the YouTube section of the inline script, split `pollYouTube` so that after fetching `data` it branches:

```js
    const ytApiCard = document.getElementById("yt-api-card");
    const ytApiHelp = document.getElementById("yt-api-help");
    const ytApiStatus = document.querySelector(".yt-api-status");
    const ytCopy = document.getElementById("yt-copy-btn");
    const ytFallback = document.getElementById("yt-fallback-btn");
    let _ytMode = "scrape";
    let _lastUptime = 0;

    function renderApi(data) {
      ytCard.hidden = true; ytHelp.hidden = true; ytApiCard.hidden = false;
      document.getElementById("yt-api-headline").textContent = data.needs_reconnect
        ? "YouTube sign-in stopped working" : `Connected to ${data.channel_title || "YouTube"}`;
      document.getElementById("yt-api-note").textContent = data.needs_reconnect
        ? "Reconnect on Ward setup, or use Start without YouTube setup."
        : "Start creates today's broadcast automatically.";
      const live = data.state === "live";
      ytPill.hidden = !live;
      if (live) ytPill.href = data.watch_url;
      ytCopy.hidden = !data.watch_url;
      ytCopy.dataset.url = data.watch_url || "";
      const streaming = document.body.dataset.state === "STREAMING" || document.body.dataset.state === "STARTING";
      ytApiStatus.hidden = !streaming || live;
      ytApiStatus.textContent = data.state === "none" ? "Creating broadcast…" : "Waiting for YouTube…";
      ytApiHelp.hidden = !(document.body.dataset.state === "STREAMING" && !live && _lastUptime > 120);
    }
```

In `pollYouTube`, right after parsing `data`:

```js
      _ytMode = data.mode || "scrape";
      if (_ytMode === "api") { renderApi(data); return; }
      ytApiCard.hidden = true; ytApiHelp.hidden = true; ytCard.hidden = false;
```

In the `/api/status` handler, store `_lastUptime = data.uptime_s || 0;`.

Change `scheduleYouTube`'s delay to:

```js
      const active = ["STARTING", "STREAMING"].includes(document.body.dataset.state);
      const delay = _ytMode === "api" ? (active ? 10000 : 60000) : (document.body.dataset.state === "STREAMING" ? 30000 : 120000);
```

Ward change: inside the existing `wardSelect` change listener, add `setTimeout(() => pollYouTube(), 300);` so it runs after the `/api/config` save.

Copy button:

```js
    ytCopy?.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(ytCopy.dataset.url); ytCopy.textContent = "Copied"; }
      catch (_) { showFlash(ytCopy.dataset.url); }
      setTimeout(() => { ytCopy.textContent = "Copy link"; }, 2000);
    });
```

Fallback and stop warning: in the existing `htmx:responseError` listener, after computing `msg`, add `if (evt.detail.pathInfo?.requestPath === "/api/start" && xhr.status === 424) ytFallback.hidden = false;`. Then add:

```js
    ytFallback?.addEventListener("click", async () => {
      const form = document.getElementById("start-form");
      const body = new FormData(form);
      body.set("skip_youtube", "true");
      const r = await fetch("/api/start", { method: "POST", body });
      if (r.ok) { ytFallback.hidden = true; return; }
      const d = await r.json().catch(() => ({}));
      showFlash(d.detail || `Start failed (HTTP ${r.status}).`);
    });
    document.body.addEventListener("htmx:afterRequest", (evt) => {
      if (!evt.detail?.successful) return;
      const path = evt.detail.pathInfo?.requestPath;
      if (path === "/api/start") ytFallback.hidden = true;
      if (path !== "/api/stop") return;
      try { const d = JSON.parse(evt.detail.xhr.responseText); if (d.warning) showFlash(d.warning); } catch (_) {}
    });
```

The existing Reconnect button (scrape mode) stays as is.

- [ ] **Step 5: mock.js**

In the `/api/youtube` branch, when `new URLSearchParams(location.search).get("yt") === "api"`, return:

```js
      return { status: 200, json: {
        mode: "api", connected: true, needs_reconnect: false, channel_title: "Mock Ward Channel",
        state: mock.state === "STREAMING" && mock.uptime > 15 ? "live" : (mock.state === "IDLE" ? "none" : "ready"),
        watch_url: mock.state === "IDLE" ? "" : "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      } };
```

Use whatever field names `mock.js` already uses for state and uptime (read its `statusPayload()` first).

- [ ] **Step 6: Run tests, then check by hand**

Run: `uv run pytest -q`. Expected: all pass.

Manual: `uv run ndi-rmtp`, then open `/?mock=1&yt=api` and `/?mock=1`. Check API mode: card text, Start → "Waiting for YouTube…" → live pill + Copy. Check scrape mode looks exactly as before. Take one screenshot of each with the `run` skill or Playwright.

- [ ] **Step 7: Commit**

```bash
git add src/ndi_rmtp/templates/index.html src/ndi_rmtp/static/mock.js tests/test_template_invariants.py tests/test_routes.py
git commit -m "Show API-mode YouTube status on the stream page with a fallback start"
```

---

### Task 10: Docs, templates, and real-channel verification

**Files:**
- Create: `docs/youtube-oauth-setup.md`
- Modify: `config.south.example.toml`, `config.north.example.toml` (add a `[youtube]` placeholder block)
- Modify: `CLAUDE.md` (new architecture subsection; amend the "only ever reports POSITIVES" section)
- Modify: `docs/users-guide.md` (short "Wards connected to YouTube" section)

- [ ] **Step 1: Write `docs/youtube-oauth-setup.md`**

```markdown
# YouTube integration — one-time Google setup

Do this once for the stake. Both appliances share the same OAuth client.

1. Go to console.cloud.google.com and create a project (e.g. "Stake Streaming").
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. APIs & Services → OAuth consent screen: User type **External**. Fill in the app
   name and support email. Scopes: add `https://www.googleapis.com/auth/youtube`.
4. On the consent screen, click **Publish app** so the status is **In production**.
   Do NOT submit for verification. Leaving it in Testing makes every ward's sign-in
   expire after 7 days.
5. APIs & Services → Credentials → Create credentials → OAuth client ID → type
   **TVs and Limited Input devices**. Copy the client ID and secret.
6. In your local `config.south.toml` and `config.north.toml` (never the
   `.example` files), add:

       [youtube]
       client_id = "…apps.googleusercontent.com"
       client_secret = "…"

   Bump `config_version`, note it in `CONFIG_CHANGELOG.md`, and deploy per `deploy.md`.
7. On each appliance, open **Ward setup → Connect YouTube** for each ward. Sign in as
   the ward's YouTube account. Google will say the app isn't verified: choose
   **Advanced → Go to (app name)**. Pick the ward's channel if asked.

Quota: the default 10,000 units/day covers about 8 wards streaming weekly. Check
APIs & Services → YouTube Data API v3 → Quotas after the first Sunday.

To undo: Disconnect on Ward setup, or remove `[youtube]` from the config to hide the
feature entirely (wards fall back to pasted keys).
```

- [ ] **Step 2: Example templates**

Append to both `config.*.example.toml`:

```toml
# Optional. OAuth client for the YouTube integration (docs/youtube-oauth-setup.md).
# Leave client_id empty and the appliance behaves exactly as before: pasted keys only.
[youtube]
client_id = ""
client_secret = ""
```

- [ ] **Step 3: CLAUDE.md**

Add a subsection under "Architecture, the non-obvious parts" titled `### YouTube integration (OAuth) creates the broadcast before the encoder`. Cover these points, one or two sentences each:
- Per-ward device-flow sign-in; tokens and the ingest key live in `credentials.toml` (`yt_*`); `[youtube] client_id` empty hides everything.
- `/api/start` calls `BroadcastManager.prepare` **before** `streamer.start` (the 2026-09-12 ordering fix). It returns 424 on failure and never spawns; `skip_youtube` is the fallback.
- `enableAutoStop=false` so the auto-restart backoff never ends a broadcast; `finish` runs only on explicit Stop.
- Retention only deletes broadcasts carrying the `ndi-rmtp; auto-deletes` description marker, 7 days after `actualEndTime`; a keep from any ward protects a video.
- The Google project must stay **In production (unverified)**, because Testing expires refresh tokens in 7 days.

In "YouTube liveness only ever reports POSITIVES", add one line: "This applies to wards **not** connected via OAuth. Connected wards use `liveBroadcasts.list`, which sees unlisted broadcasts, so their status may show negatives."

- [ ] **Step 4: users-guide**

Add a short section: connected wards just press Start; the watch link appears with a Copy button once live; if Start says YouTube couldn't create the broadcast, use **Start without YouTube setup** and create the broadcast in Studio as before; recordings delete themselves after 7 days unless kept on Past streams.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add docs/youtube-oauth-setup.md config.south.example.toml config.north.example.toml CLAUDE.md docs/users-guide.md
git commit -m "Document YouTube OAuth setup and the API-mode lifecycle"
```

- [ ] **Step 7: Real-channel verification (human + agent, on the dev Mac or South Pi with Test Ward)**

This step needs the Google Cloud setup from Step 1 and a real camera. Run it and record the results in the PR or branch notes. Don't claim success without them.

1. Connect Test Ward on `/setup`. Confirm the channel title shows and `credentials.toml` gains `yt_*` fields. Confirm `config.toml` doesn't.
2. Start. Confirm in Studio (only now open Studio) that a broadcast titled `Test Ward Sacrament Meeting — <date>` exists, is bound, and goes **live without anyone touching Studio** within ~30 s. The stream page shows the live pill.
3. While live, kill `ndi-streamer` (`pkill -f ndi-streamer`). Confirm auto-restart reconnects and the **same** broadcast continues (same watch URL).
4. Stop. Confirm the broadcast goes to Complete in Studio.
5. Start again within 12 hours of a failed Start (e.g. block the camera so the encoder fails, then fix it). Confirm the upcoming broadcast is reused, not duplicated.
6. Temporarily set `RETENTION = timedelta(minutes=5)` locally (do not commit), wait, call `await manager.sweep()` from a REPL or trigger a restart and wait 2 min, and confirm only the appliance's broadcast is deleted.
7. Disconnect. Confirm the manual-key Start path works as before.
```

---

## Self-Review Notes

- Spec coverage: auth (T2), client (T3), prepare/finish/status (T4), retention, keep and delete-now (T5), start/stop wiring + fallback (T6), `/setup` (T7), `/streams` (T8), main page (T9), setup doc + CLAUDE.md + manual checklist (T10). Storage (T1). Error table rows: prepare failure → 424 + fallback (T6/T9); ReauthRequired (T4/T6); liveStreamingNotEnabled message (T4); finish failure warning (T4/T6/T9); device-flow expired/denied (T2/T7); sweep failure shown on `/streams` only (T5/T8).
- Type names checked across tasks: `Prepared`, `PrepareError`, `YouTubeServices(manager, flows)`, `FlowState.to_dict()`, `WardCredentials.yt_linked`, `update_ward_credentials`, `_yt_ward`, `_require_youtube_ward`.
