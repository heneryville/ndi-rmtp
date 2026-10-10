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
