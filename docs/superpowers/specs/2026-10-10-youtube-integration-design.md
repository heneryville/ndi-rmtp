# YouTube Integration — Design

Date: 2026-10-10 · Branch: `youtube-oauth-broadcast`

## Goal

Let a ward that opts in run its whole YouTube lifecycle from the appliance:
sign in once, press Start and be live (no Studio tab, no "Preparing stream"),
press Stop and the broadcast ends, and have its recordings deleted 7 days later
without anyone remembering to do it.

Wards that don't opt in keep working exactly as today: paste a stream key on
`/setup`, press Start.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Who signs in | Each ward signs in separately, even when wards share a channel. Shared-channel wards are signed in by the admin to the same channel. |
| Sign-in mechanism | Google OAuth **device flow** (code shown on appliance, entered at google.com/device). The appliance has no public HTTPS URL, so redirect flows are out. Device flow supports the `youtube` scope. |
| Google app status | One Google Cloud project for the stake, consent screen set to **In production, unverified**. Users click through "Google hasn't verified this app" once; 100-user cap is fine. Avoids Testing mode's 7-day refresh-token expiry. Tokens then live until 6 months unused. |
| When the broadcast is created | **Just-in-time on Start**, before the encoder connects. |
| Privacy | Per-ward setting on `/setup` (`public` / `unlisted` / `private`), default **public**. |
| Retention | **Automatic, only broadcasts this appliance created**, deleted 7 days after they end. A Past Streams page lists them, with Delete now and Keep. |
| Architecture | Thin in-process client over `httpx` (no Google client libraries, no new processes). |
| Recognising our broadcasts | A marker line in the broadcast description, rediscovered via `liveBroadcasts.list`. Survives a Pi reimage, unlike a local ID list. |

## Why creation must precede the encoder

Proven 2026-09-12 (memory: `youtube-preparing-stream-needs-a-broadcast-resource`):
YouTube auto-starts a persistent-key broadcast on ingest only if the broadcast
already exists when the encoder connects. Encoder-first sits in "Preparing
stream" forever with health "Excellent". This integration fixes that by
construction: create + bind, *then* spawn `ndi-streamer`.

## Architecture

Three new modules, one per concern. None of them touch `Streamer` internals;
orchestration happens in the app layer so no network call is ever awaited while
`Streamer._lock` is held.

```
app.py ──► broadcasts.py (BroadcastManager) ──► youtube_api.py (YouTubeClient)
   │              │                                     │
   │              └──► streamer.start/stop              └──► youtube_auth.py (tokens)
   └──► youtube_auth.py (device-flow sign-in for /setup)
```

### `youtube_auth.py` — OAuth

- `DeviceFlow.begin(ward)` → POST `oauth2.googleapis.com/device/code` with
  scope `https://www.googleapis.com/auth/youtube`; returns `user_code`,
  `verification_url`, `expires_in`, `interval`.
- A background task polls `oauth2.googleapis.com/token`
  (`grant_type=urn:ietf:params:oauth:grant-type:device_code`), honouring
  `authorization_pending` and `slow_down`, until success, denial, or expiry.
  One pending flow per ward; starting a new one cancels the old.
- `TokenStore.access_token(ward)` returns a cached access token, refreshing
  with the ward's refresh token when within 60 s of expiry. Cache is
  in-memory; refresh tokens are persisted (see Storage).
- `invalid_grant` on refresh marks the ward **needs reconnect** (refresh token
  cleared, flag persisted) and raises `ReauthRequired`.
- `revoke(ward)` POSTs `oauth2.googleapis.com/revoke`, best-effort.

### `youtube_api.py` — REST client

A `YouTubeClient(token_provider, http: httpx.AsyncClient)` with only the calls
we use. Every method takes the ward name, fetches its token, and raises a
typed `YouTubeError(reason, status)` built from Google's error body
(`error.errors[0].reason`).

| Method | Endpoint | Quota (approx.) |
|---|---|---|
| `my_channel()` | `channels.list part=snippet mine=true` | 1 |
| `create_stream(title)` | `liveStreams.insert` — `cdn.ingestionType=rtmp`, `resolution=variable`, `frameRate=variable`, `contentDetails.isReusable=true` | 50 |
| `get_stream(id)` | `liveStreams.list id=` | 1 |
| `create_broadcast(title, description, privacy)` | `liveBroadcasts.insert` (fields below) | 50 |
| `bind(broadcast_id, stream_id)` | `liveBroadcasts.bind` | 50 |
| `get_broadcast(id)` / `list_broadcasts(status)` | `liveBroadcasts.list` (`id=` or `broadcastStatus=` + `broadcastType=event`, paginated, `maxResults=50`) | 1 |
| `complete(id)` | `liveBroadcasts.transition broadcastStatus=complete` | 50 |
| `delete_video(id)` | `videos.delete` (broadcast id == video id; deletes the recording) | 50 |

Quota figures are the commonly cited write=50 / read=1 costs; the reference pages don't state them, so confirm in the Cloud console after the first week. A normal week per ward uses about 300 units. The project's default quota is
10,000 units/day, so ~8 wards fit with a wide margin.

Broadcast fields on insert:

- `snippet.title`: `"{ward} Sacrament Meeting — {Mon D, YYYY}"` (local date, ≤100 chars, truncated ward name if needed)
- `snippet.scheduledStartTime`: now (UTC)
- `snippet.description`: one human line plus the marker line `Streamed by ndi-rmtp; auto-deletes 7 days after the meeting.`
  The literal substring `ndi-rmtp; auto-deletes` is the marker.
- `status.privacyStatus`: the ward's setting
- `status.selfDeclaredMadeForKids`: `false`
- `contentDetails.enableAutoStart`: `true` (ingest flips it live)
- `contentDetails.enableAutoStop`: `false` — an auto-restart backoff (up to 60 s) must not end the broadcast
- `contentDetails.monitorStream.enableMonitorStream`: `false` (skip the testing state)
- `contentDetails.latencyPreference`: `normal` (tolerates jitter; see CLAUDE.md)
- `contentDetails.enableDvr`: `true`

### `broadcasts.py` — `BroadcastManager`

Owns the per-ward lifecycle and the retention job. One `asyncio.Lock` per
ward guards go-live so a double-clicked Start can't create two broadcasts.

**`prepare(ward) -> Prepared(broadcast_id, stream_key)`**, called by
`/api/start` before `streamer.start()`:

1. `list_broadcasts("active")` + `("upcoming")`. If one carries our marker,
   is bound to the ward's stream, and was scheduled within the last **12
   hours**, **reuse it** (covers: a Start whose encoder failed, a crash, an
   app restart mid-meeting). Reuse means no duplicates and the watch link
   doesn't change mid-meeting. A marked, bound `active` broadcast older than
   12 hours is last week's leftover (Stop never reached YouTube): `complete`
   it first so it can't be reused and the sweep picks it up.
2. Otherwise `create_broadcast` then `bind` to the ward's reusable stream.
3. Return the stream's ingest key (cached in storage; re-read via
   `get_stream` if missing).

**`finish(ward)`**, called by `/api/stop` *after* `streamer.stop()` returns:
`complete(broadcast_id)` for the broadcast the last `prepare` returned. If the
app restarted since, it has no record, so it looks up the ward's marked, bound
`active` broadcast instead. `redundantTransition` → fine. `invalidTransition`
(never went live) → leave it; the next `prepare` reuses it. Any other error
is logged and shown, but Stop itself always succeeds: the encoder is already
down.

Encoder crashes and auto-restart do not call `finish`. The broadcast stays
open across the retry loop (that's why `enableAutoStop=false`).

**`status(ward)`** for the main page: `get_broadcast(id)` →
`lifeCycleStatus` (`ready`, `testStarting`, `liveStarting`, `live`,
`complete`) plus the watch URL. Polled by the UI every 10 s while starting,
every 60 s once live. Cached 5 s server-side so several open tabs don't
multiply calls.

**Retention, `sweep()`:** runs 2 minutes after app start and every 6 hours.
For each connected ward: `list_broadcasts("completed")`, keep those with the
marker, `actualEndTime` older than 7 days, and not in **any** ward's keep list (wards sharing a channel see the same broadcasts),
then `delete_video` each. Marked `upcoming` broadcasts more than 7 days old (created but never went live) are deleted the same way. Listing stops after 10 pages (500 broadcasts), since results are newest-first. A 404 means already deleted (wards sharing a
channel both see the same broadcast) and counts as success. Every deletion is
logged with ward, video id and title. One ward's failure never stops the
others.

### Interaction with the existing liveness check

`youtube.py` stays as-is for wards **not** connected. For connected wards the
UI uses `BroadcastManager.status` instead.

This relaxes the "only ever report positives" rule **for connected wards
only**, and only because the rule's own premise no longer holds: with OAuth,
`liveBroadcasts.list` sees unlisted broadcasts directly, so a negative is now
an observation, not a guess. The one warning: still not `live` 120 s after
the encoder reached `STREAMING` → "YouTube hasn't started the broadcast. Try
Stop, then Start." Unconnected wards keep the no-warning behaviour.

## Storage

Everything new is device-local, in `credentials.toml` (which `install.sh`
never touches, mode 0600). `WardCredentials` grows:

```toml
[wards."Jordan Willows 7th Ward"]
key = ""                      # manual key, unchanged; unused while connected
channel_id = "UC…"            # auto-filled from channels.list on connect
privacy = "public"            # public | unlisted | private
yt_refresh_token = "…"        # presence == connected
yt_channel_title = "Jordan Willows 7th Ward"
yt_stream_id = "…"
yt_stream_key = "…"           # ingest key of the appliance's reusable stream
yt_needs_reconnect = false
yt_keep = ["videoId1"]        # exempt from the 7-day sweep
```

The **effective stream key** becomes: `yt_stream_key` if connected, otherwise
`key`. `Config.load` overlays only `key`/`channel_id` today. The new fields
are read from `Credentials` directly by the YouTube modules and never enter
`Config`, so `Config.save`'s un-overlay logic is untouched.

The OAuth client goes in `config.toml` under a new section, which the
deployed `config.<south|north>.toml` carries (git-ignored) and the
`.example` templates carry as placeholders:

```toml
[youtube]
client_id = ""
client_secret = ""   # device-flow clients are "public"; still kept out of git
```

Empty `client_id` hides every YouTube-connect control. The appliance then
behaves exactly as it does today.

## UI

- **`/setup`, per ward:** "Connect YouTube" → shows the code, the URL
  `google.com/device`, and a countdown; flips to "Connected to *channel
  title*" when done. Connected wards show a privacy selector and a Disconnect
  button, and hide the manual key field (with a note that disconnecting
  restores it). Needs-reconnect shows a Reconnect button instead.
- **`/` main page, connected ward:** a YouTube line under the status:
  `Creating broadcast…` → `Waiting for YouTube…` → `Live on YouTube` with
  the watch link and a Copy button. The Studio control-room link and the
  liveness chip stay for unconnected wards only.
- **`/streams` (new):** per connected ward, the appliance's broadcasts
  (title, date, privacy, "deletes on <date>" or "kept"), with Keep/Unkeep and
  Delete now. Delete now uses an in-page confirm, not a browser dialog.

## Error handling

| Situation | Behaviour |
|---|---|
| `prepare` fails (network, quota, API error) | Start does **not** spawn the encoder. The UI shows the reason and a **Start without YouTube setup** button that streams to the ward's ingest key the old way (operator creates the broadcast in Studio, as today). |
| `ReauthRequired` | Same as above, plus "This ward needs to reconnect YouTube on /setup." |
| `liveStreamingNotEnabled` | A specific message: the channel must enable live streaming in Studio (24-hour wait on new channels). |
| `finish` fails | Stop succeeds; a banner says the broadcast may still show as live and links to Studio. The next `prepare` reuses or the sweep ignores it (only `completed` are swept). |
| Device flow expires / denied | `/setup` shows "Code expired" or "Access denied" and a retry button. |
| Sweep failure | Logged; retried next run. Never surfaced on `/` (it isn't the operator's problem on Sunday). Shown on `/streams` as "Last cleanup failed: <reason>". |

## Testing

- **Unit, with `httpx.MockTransport`:** device flow (pending → slow_down →
  success; denied; expired), token refresh and `invalid_grant`, each
  `YouTubeClient` method's request shape and error mapping,
  `BroadcastManager.prepare` (reuse active, reuse upcoming, create+bind,
  ignore unmarked broadcasts), `finish` transition outcomes, sweep (age
  cutoff, marker filter, keep list, 404-is-success, one ward failing).
- **App tests:** `/api/start` calls prepare *before* `streamer.start` and
  doesn't spawn on prepare failure; the fallback start path; `/api/stop`
  order; `/setup` hides YouTube controls when `client_id` is empty;
  `Config.save` never writes `yt_*` fields to config.toml.
- **Manual, against Test Ward on a real channel** (checklist in the plan):
  connect, Start → live without opening Studio, kill the encoder mid-stream
  and confirm auto-restart keeps the same broadcast, Stop → complete, sweep
  with a temporarily shortened cutoff.

## Out of scope

- Scheduling broadcasts ahead of time or recurring auto-creation.
- Thumbnails, chat moderation, captions, analytics.
- Sharing one sign-in across wards (decided: per-ward).
- Changing the encoder or `Streamer` lifecycle.
- Google app verification (decided: unverified production).

## Setup prerequisite (one-time, by the admin)

New doc `docs/youtube-oauth-setup.md`: create a Google Cloud project, enable
YouTube Data API v3, configure the External consent screen with the `youtube`
scope, **publish to In production** (skip verification), create an OAuth
client of type **TVs and Limited Input devices**, and put its ID and secret
in `config.<south|north>.toml` under `[youtube]`.
