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
            try:
                err = (resp.json() if resp.content else {}).get("error", {})
            except ValueError:
                err = {}  # non-JSON (proxy/HTML) error page
            if not isinstance(err, dict):
                err = {"status": str(err)}
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

    async def find_stream_by_key(self, ward: str, key: str, max_pages: int = 5) -> str | None:
        """The channel's existing stream whose ingest key is `key`, so the pasted key keeps working."""
        token = None
        for _ in range(max_pages):
            params = {"part": "id,cdn", "mine": "true", "maxResults": "50"}
            if token:
                params["pageToken"] = token
            data = await self._call(ward, "GET", "liveStreams", params=params)
            for item in data.get("items") or []:
                if item.get("cdn", {}).get("ingestionInfo", {}).get("streamName") == key:
                    return item["id"]
            token = data.get("nextPageToken")
            if not token:
                return None
        return None

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

    async def delete_broadcast(self, ward: str, broadcast_id: str) -> None:
        """For broadcasts that never went live (no video yet)."""
        await self._call(ward, "DELETE", "liveBroadcasts", params={"id": broadcast_id}, ok_404=True)

    async def delete_video(self, ward: str, video_id: str) -> None:
        await self._call(ward, "DELETE", "videos", params={"id": video_id}, ok_404=True)
