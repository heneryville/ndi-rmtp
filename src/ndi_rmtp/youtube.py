"""Best-effort YouTube liveness check from a channel ID.

**This module only ever reports positives.** Seeing a broadcast proves one
exists; *not* seeing one proves nothing at all, because an unlisted broadcast
and a broadcast that never went live are byte-identical from outside. Callers
must render `NOT_VISIBLE` as "we can't tell", never as "you are not live".

That constraint is load-bearing and was arrived at the hard way. Three
plausible-sounding schemes for turning a negative into an alarm were tried and
all fail:

1. Track whether the operator clicked our "open control room" link — that is
   inventing state we never observe.
2. Only alarm wards we have previously seen live — a ward that has *never*
   gone live (nobody ever creates the broadcast) is indistinguishable from an
   unlisted one, so the worst-configured ward gets permanently silenced.
3. Declare visibility per ward in config — visibility is a property of each
   *broadcast*, not the channel. A ward can be public one week, unlisted the
   next.

All three founder on the same rock: unauthenticated, a negative observation
carries no information. Genuine detection needs the Live Streaming API with
the ward's OAuth credentials (`liveBroadcasts.list` sees unlisted broadcasts
directly). Until that exists, we confirm what we can and stay quiet otherwise.

The discriminator is the `<link rel="canonical">` on `/channel/<id>/live`.
With a broadcast, canonical carries `watch?v=<id>`; with nothing live it stays
the plain channel URL. Measured on real pages:

    NASA  (live)  canonical -> https://www.youtube.com/watch?v=M3HKLzjvKPc
    Me279 (idle)  canonical -> https://www.youtube.com/channel/UCbLlSmx8...

**Only the canonical href may be trusted.** The page body contains unrelated
`watch?v=` links (Me279's idle page has one at byte 769091 with nothing live),
so any body-wide search for a video id is a false-positive generator. That was
a real bug here, caught only by testing against live HTML.

Two other measured facts drive the implementation: the canonical tag sits
~700-760 KB into a 0.8-1.3 MB page, so the read cannot be cheaply truncated;
and the page gzips ~4.2x (1.26 MB -> 297 KB), so we ask for it compressed.
`"isLiveNow"` is NOT present on this URL despite appearing on `/@handle/live`
— `"isLive":true` is the flag that actually shows up here.

No new runtime dependency on purpose — one request every 30s does not justify
shipping an HTTP client to the Pi, so this uses urllib on a worker thread.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LIVE_URL = "https://www.youtube.com/channel/{channel_id}/live"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

# YouTube serves a consent interstitial to unknown clients; a desktop UA plus
# the CONSENT cookie gets the real page. Without both you get a consent shell
# whose canonical tells you nothing.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_CONSENT_COOKIE = "CONSENT=YES+1"

TIMEOUT_S = 10.0
CACHE_TTL_S = 20.0
# Wire cap: the page gzips to ~300 KB. This bounds what a single check can pull
# down if YouTube ever serves something pathological.
_MAX_WIRE_BYTES = 1_500_000
# Decompressed cap: pages run 0.8-1.3 MB and the canonical tag sits ~750 KB in,
# so truncating below this silently turns every channel into "not_visible" —
# which, because negatives are never surfaced as problems, would fail invisibly.
_MAX_BYTES = 2_500_000

# States. Only LIVE and SCHEDULED are claims; the rest are absence of knowledge.
LIVE = "live"                  # a broadcast is live right now
SCHEDULED = "scheduled"        # a broadcast exists but has not started
NOT_VISIBLE = "not_visible"    # nothing visible — says NOTHING about reality
UNAVAILABLE = "unavailable"    # could not check (offline, timeout, blocked)
UNCONFIGURED = "unconfigured"  # no channel_id for this ward

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

_CANONICAL_RE = re.compile(r'<link\s+rel="canonical"\s+href="([^"]*)"')
_WATCH_ID_RE = re.compile(r"watch\?v=([A-Za-z0-9_-]{11})")
_IS_LIVE_RE = re.compile(r'"isLive"\s*:\s*true')
_IS_UPCOMING_RE = re.compile(r'"isUpcoming"\s*:\s*true')


@dataclass(frozen=True)
class Liveness:
    state: str
    video_id: str = ""
    checked_at: float = 0.0

    @property
    def watch_url(self) -> str:
        return WATCH_URL.format(video_id=self.video_id) if self.video_id else ""

    @property
    def is_claim(self) -> bool:
        """True when this result asserts something. NOT_VISIBLE/UNAVAILABLE
        assert nothing and must never drive a warning in the UI."""
        return self.state in (LIVE, SCHEDULED)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "video_id": self.video_id,
            "watch_url": self.watch_url,
            "checked_at": self.checked_at,
        }


def valid_channel_id(channel_id: str) -> bool:
    return bool(channel_id) and bool(CHANNEL_ID_RE.match(channel_id.strip()))


def _parse(body: str) -> tuple[str, str]:
    """Return (state, video_id) for a fetched /live page body.

    The video id comes from the canonical href and NOWHERE else — see the
    module docstring; an idle channel's page still contains `watch?v=` links
    for unrelated videos, so searching the body would report phantom
    broadcasts.
    """
    m = _CANONICAL_RE.search(body)
    if not m:
        # No canonical at all means we did not get the page we expected
        # (consent wall, truncated read, layout change). Claim nothing.
        return NOT_VISIBLE, ""

    watch = _WATCH_ID_RE.search(m.group(1))
    if not watch:
        # Canonical stayed on the channel URL: nothing live or upcoming that we
        # can see. Says nothing about unlisted broadcasts.
        return NOT_VISIBLE, ""

    video_id = watch.group(1)
    if _IS_LIVE_RE.search(body):
        return LIVE, video_id
    if _IS_UPCOMING_RE.search(body):
        return SCHEDULED, video_id
    # A broadcast exists but neither flag parsed (page variants differ). Claim
    # the weaker thing. Under-claiming costs a green badge; over-claiming would
    # tell an operator they are live when they may not be.
    return SCHEDULED, video_id


def _fetch(channel_id: str) -> str:
    req = urllib.request.Request(
        LIVE_URL.format(channel_id=channel_id),
        headers={
            "User-Agent": _USER_AGENT,
            "Cookie": _CONSENT_COOKIE,
            "Accept-Language": "en-US,en;q=0.9",
            # The page is mostly inline JSON and compresses ~4.2x. urllib does
            # not negotiate this for us, and the uncompressed page is over a
            # megabyte every poll.
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read(_MAX_WIRE_BYTES)
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw[:_MAX_BYTES].decode("utf-8", errors="replace")


class LivenessChecker:
    """Caches one result per channel so the UI can poll freely.

    Failures are cached too, at the same TTL: when YouTube is unreachable the
    appliance is probably busy pushing video, and retrying every poll would
    make that worse for no gain.
    """

    def __init__(self, ttl_s: float = CACHE_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._cache: dict[str, Liveness] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, channel_id: str) -> asyncio.Lock:
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = self._locks[channel_id] = asyncio.Lock()
        return lock

    def cached(self, channel_id: str) -> Liveness | None:
        hit = self._cache.get(channel_id)
        if hit and (time.monotonic() - hit.checked_at) < self._ttl_s:
            return hit
        return None

    async def check(self, channel_id: str, *, force: bool = False) -> Liveness:
        channel_id = (channel_id or "").strip()
        if not valid_channel_id(channel_id):
            return Liveness(UNCONFIGURED, checked_at=time.monotonic())

        if not force:
            hit = self.cached(channel_id)
            if hit is not None:
                return hit

        # Single flight per channel: concurrent pollers wait rather than
        # stacking up identical requests.
        async with self._lock_for(channel_id):
            if not force:
                hit = self.cached(channel_id)
                if hit is not None:
                    return hit
            try:
                body = await asyncio.to_thread(_fetch, channel_id)
                state, video_id = _parse(body)
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                logger.debug("youtube liveness check failed for %s: %s", channel_id, exc)
                state, video_id = UNAVAILABLE, ""
            except Exception:  # pragma: no cover — never break the page
                logger.exception("unexpected error checking youtube liveness")
                state, video_id = UNAVAILABLE, ""

            result = Liveness(state, video_id, time.monotonic())
            self._cache[channel_id] = result
            return result
