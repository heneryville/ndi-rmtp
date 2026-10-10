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
RETENTION = timedelta(days=7)
SWEEP_FIRST_DELAY_S = 120
SWEEP_INTERVAL_S = 6 * 3600
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
        self.last_sweep: dict = {"at": None, "error": None}

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


@dataclass
class YouTubeServices:
    manager: BroadcastManager
    flows: DeviceFlows
