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


async def test_non_json_error_body_is_youtube_error():
    with pytest.raises(YouTubeError) as e:
        await _client(lambda r: httpx.Response(503, text="<html>down</html>")).get_broadcast("W", "b1")
    assert e.value.status == 503


async def test_delete_broadcast_uses_live_broadcasts_and_tolerates_404():
    seen = {}
    def handler(req):
        seen["method"], seen["path"] = req.method, req.url.path
        return httpx.Response(404, json={"error": {"errors": [{"reason": "liveBroadcastNotFound"}]}})
    await _client(handler).delete_broadcast("W", "b1")
    assert seen == {"method": "DELETE", "path": "/youtube/v3/liveBroadcasts"}


async def test_find_stream_by_key_pages_and_matches():
    pages = []
    def handler(req):
        assert req.url.params["mine"] == "true"
        pages.append(req.url.params.get("pageToken"))
        if len(pages) == 1:
            return httpx.Response(200, json={"items": [{"id": "s0", "cdn": {"ingestionInfo": {"streamName": "other"}}}],
                                             "nextPageToken": "p1"})
        return httpx.Response(200, json={"items": [{"id": "s9", "cdn": {"ingestionInfo": {"streamName": "pasted-key"}}}]})
    assert await _client(handler).find_stream_by_key("W", "pasted-key") == "s9"
    assert pages == [None, "p1"]


async def test_find_stream_by_key_none_when_absent():
    h = lambda r: httpx.Response(200, json={"items": []})
    assert await _client(h).find_stream_by_key("W", "k") is None
