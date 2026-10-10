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
