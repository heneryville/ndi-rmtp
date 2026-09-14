"""YouTube liveness checks.

The invariant under test throughout: the module makes POSITIVE claims only.
`not_visible` and `unavailable` must never be reported as a claim, because an
unlisted broadcast is indistinguishable from one that never went live.
"""
from __future__ import annotations

import urllib.error

import pytest

from ndi_rmtp import youtube
from ndi_rmtp.youtube import Liveness, LivenessChecker

CHANNEL = "UCbLlSmx8Wvg85m8jPgruTuQ"

# Shapes taken from real fetches. The /live URL does NOT redirect (verified —
# the final URL is unchanged); YouTube serves the watch page's content there and
# points canonical at it. With nothing live, canonical stays the channel URL.
LIVE_PAGE = (
    '<html><head><link rel="canonical" href="https://www.youtube.com/watch?v=M3HKLzjvKPc">'
    '</head><body>{"isLive":true,"isLiveContent":true}</body></html>'
)
UPCOMING_PAGE = (
    '<html><head><link rel="canonical" href="https://www.youtube.com/watch?v=CPMXE_Kzt1o">'
    '</head><body>{"isLive":false,"isUpcoming":true}</body></html>'
)
IDLE_PAGE = (
    '<html><head><link rel="canonical" href="https://www.youtube.com/channel/' + CHANNEL + '">'
    '</head><body>no broadcast here</body></html>'
)


def test_parses_live_page():
    assert youtube._parse(LIVE_PAGE) == (youtube.LIVE, "M3HKLzjvKPc")


def test_parses_upcoming_page_as_scheduled():
    assert youtube._parse(UPCOMING_PAGE) == (youtube.SCHEDULED, "CPMXE_Kzt1o")


def test_channel_page_with_nothing_live_is_not_visible():
    state, video_id = youtube._parse(IDLE_PAGE)
    assert state == youtube.NOT_VISIBLE
    assert video_id == ""


def test_idle_page_containing_unrelated_watch_links_is_not_visible():
    """Regression: an idle channel page still carries `watch?v=` links for
    other videos (Me279's real page has one ~769 KB in). Only the canonical
    href may be trusted, or we invent broadcasts that do not exist."""
    page = (
        '<link rel="canonical" href="https://www.youtube.com/channel/' + CHANNEL + '">'
        '<a href="/watch?v=8mS__5mPq7U">some older upload</a>'
        '<a href="/watch?v=XFDCjJmhFYo">another one</a>'
    )
    assert youtube._parse(page) == (youtube.NOT_VISIBLE, "")


def test_page_without_a_canonical_claims_nothing():
    """Consent wall, truncated read or layout change — all must be silent."""
    assert youtube._parse("<html>totally unexpected</html>") == (youtube.NOT_VISIBLE, "")


def test_video_present_but_no_flags_under_claims():
    """Page variants differ; when neither flag parses we must claim the weaker
    thing. Telling an operator they are live when they might not be is the one
    error this module must never make."""
    page = '<link rel="canonical" href="https://www.youtube.com/watch?v=abcdefghijk">'
    state, video_id = youtube._parse(page)
    assert state == youtube.SCHEDULED
    assert video_id == "abcdefghijk"


@pytest.mark.parametrize("state", [youtube.NOT_VISIBLE, youtube.UNAVAILABLE, youtube.UNCONFIGURED])
def test_negative_states_are_not_claims(state):
    assert Liveness(state).is_claim is False


@pytest.mark.parametrize("state", [youtube.LIVE, youtube.SCHEDULED])
def test_positive_states_are_claims(state):
    assert Liveness(state).is_claim is True


@pytest.mark.parametrize("channel_id", ["", "   ", "nope", "UC-too-short", "x" * 24])
def test_rejects_bad_channel_ids(channel_id):
    assert youtube.valid_channel_id(channel_id) is False


def test_accepts_real_channel_id():
    assert youtube.valid_channel_id(CHANNEL) is True


async def test_missing_channel_id_is_unconfigured_without_fetching(monkeypatch):
    def explode(_):  # pragma: no cover — must not be reached
        raise AssertionError("should not hit the network without a channel id")

    monkeypatch.setattr(youtube, "_fetch", explode)
    result = await LivenessChecker().check("")
    assert result.state == youtube.UNCONFIGURED


async def test_network_failure_degrades_to_unavailable(monkeypatch):
    def boom(_):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(youtube, "_fetch", boom)
    result = await LivenessChecker().check(CHANNEL)
    assert result.state == youtube.UNAVAILABLE
    assert result.is_claim is False


async def test_unexpected_error_degrades_to_unavailable(monkeypatch):
    """A parser bug or a surprise exception must not take down the page."""
    def boom(_):
        raise ValueError("something we never anticipated")

    monkeypatch.setattr(youtube, "_fetch", boom)
    result = await LivenessChecker().check(CHANNEL)
    assert result.state == youtube.UNAVAILABLE


async def test_result_is_cached_within_ttl(monkeypatch):
    calls = []

    def fake(_):
        calls.append(1)
        return LIVE_PAGE

    monkeypatch.setattr(youtube, "_fetch", fake)
    checker = LivenessChecker(ttl_s=60)
    first = await checker.check(CHANNEL)
    second = await checker.check(CHANNEL)
    assert first.state == second.state == youtube.LIVE
    assert len(calls) == 1


async def test_force_bypasses_cache(monkeypatch):
    calls = []

    def fake(_):
        calls.append(1)
        return LIVE_PAGE

    monkeypatch.setattr(youtube, "_fetch", fake)
    checker = LivenessChecker(ttl_s=60)
    await checker.check(CHANNEL)
    await checker.check(CHANNEL, force=True)
    assert len(calls) == 2


async def test_failures_are_cached_too(monkeypatch):
    """When YouTube is unreachable the appliance is probably busy pushing
    video; retrying on every poll would make that worse for no gain."""
    calls = []

    def boom(_):
        calls.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(youtube, "_fetch", boom)
    checker = LivenessChecker(ttl_s=60)
    await checker.check(CHANNEL)
    await checker.check(CHANNEL)
    assert len(calls) == 1


def test_watch_url_only_when_a_video_is_known():
    assert Liveness(youtube.LIVE, "abcdefghijk").watch_url.endswith("abcdefghijk")
    assert Liveness(youtube.NOT_VISIBLE).watch_url == ""
