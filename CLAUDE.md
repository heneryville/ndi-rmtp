# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A LAN appliance that pulls NDI video from a PTZ camera, re-broadcasts to YouTube
Live over RTMP, and exposes a web UI for control (start/stop, mute, sacrament
overlay, PTZ presets + manual + live preview). Built primarily for streaming
LDS sacrament meetings; multiple wards share one appliance via config-managed
ward entries that each carry their own YouTube stream key and overlay image.

Production target is Raspberry Pi 5 (not yet validated — see `TODO.md`).
Development target is Apple Silicon macOS.

## Run / test / build

```sh
# One-time dev setup (Mac).
./scripts/dev-setup-mac.sh

# Run the app. Reads config.toml at the project root (override with
# $NDI_RMTP_CONFIG). Listens on 0.0.0.0:8080.
uv run ndi-rmtp

# Tests. 68 unit tests run by default in ~6s; integration test is opt-in.
uv run pytest
uv run pytest tests/test_streamer.py -v          # one test file
uv run pytest tests/test_streamer.py::test_auto_restart_retries_transient_crash -v   # one test
uv run pytest -m integration                     # requires Docker + NDI_TEST_SOURCE env var

# Rebuild the patched ndi-streamer C binary (after editing build/ndi_streamer/src/).
cmake --build build/ndi_streamer/build --parallel

# When you change ndi-streamer C source, regenerate the patch so dev-setup
# can re-apply it on fresh clones.
git -C build/ndi_streamer diff > scripts/ndi-streamer-no-audio.patch
```

There's no separate lint command; we lean on `pytest`. Tests run against
pyenv's system Python because cyndilib's import (used by the PTZ module) is
optional — the test environment doesn't have it installed and `bootstrap.py`
catches that import error so the app degrades gracefully. **Don't** make
cyndilib a hard import in `app.py` or `streamer.py`; the wrapping in
`bootstrap.py` is load-bearing.

## Deploying to a Pi appliance

Follow [`deploy.md`](deploy.md) — the canonical deploy runbook (rsync +
`install.sh`, the vendored-`ndi_streamer` reset gotcha, and verification). SSH
hosts/credentials are in [`ssh.md`](ssh.md); first-time provisioning is in
`docs/pi-setup.md`.

**Every config deploy must bump `config_version`** (first line of
`config.<south|north>.toml`) — step 0 of `deploy.md`. It renders in the web-UI
footer as `config vN` so an operator can confirm the appliance actually loaded
the latest config; a stale number is the tell that a config edit never reached
the Pi. `Config.load` defaults it to `0` for older configs and `save()`
round-trips it.

## Architecture, the non-obvious parts

### Three different processes interact

1. **The Python FastAPI app** (uvicorn) — control plane only. Never touches
   video frames in the streaming hot path.
2. **`ndi-streamer`** (vendored C binary built from a patched clone of
   alim-zanibekov/ndi_streamer) — one of these is spawned per live stream.
   Receives NDI, encodes via libx264 + AAC, pushes RTMP. Spawned by
   `streamer.py:Streamer.start()` with `asyncio.create_subprocess_exec`.
3. **cyndilib's NDI receiver** (Python, in-process) — owned by
   `PtzController`. Used for PTZ commands and for the live JPEG preview
   pane. **This is a second NDI receiver attached to the same camera as
   ndi-streamer** — NDI supports this but it's a non-obvious resource cost
   on the camera.

### The ndi-streamer C binary is patched, not forked

`scripts/ndi-streamer-no-audio.patch` (filename is historical; it now contains
all our C modifications) gets applied to a clean upstream clone by
`scripts/dev-setup-mac.sh` and `scripts/install.sh`. The patch adds:
- `--no_audio` flag (omits the audio stream entirely)
- `--sacrament_image PATH` flag (loads an image at startup)
- SIGUSR1 handler that toggles "show sacrament image instead of live NDI"
- CBR + zerolatency x264 tuning (`nal-hrd=cbr`, `force-cfr=1`, `tune=zerolatency`)
- 2-second GOP (`gop_size = 60`)

If you edit `build/ndi_streamer/src/*.c`, you **must** regenerate the patch
file (command above) so the change survives `git -C build/ndi_streamer pull`
on the next dev-setup run.

### Runtime toggles use signals, not subprocess swaps

This is critical and easy to get wrong. Earlier iterations spawned a separate
ffmpeg subprocess to apply sacrament or mute changes; YouTube saw each
toggle as a new RTMP session and viewers got 30–60s of "no data."

Two signal-driven toggles share the pattern:

- **Sacrament mode** (`SIGUSR1`): `Streamer.set_sacrament_mode()` flips
  `sacrament_active` in the C binary, which swaps the pixel buffer fed to
  libx264 (and zeros audio samples). The RTMP session stays alive.
- **Audio mute** (`SIGUSR2`): `Streamer.set_muted()` flips `audio_muted`,
  which zeros outgoing audio samples but leaves the FLV audio stream
  itself running. The RTMP session stays alive.

Both Python flags reset to False on every fresh subprocess spawn to stay in
sync with C's `static volatile sig_atomic_t ... = 0` initialisers.

If you find yourself wanting to "just restart the subprocess" to apply a
config change, think hard — the seamless-transition property is the whole
point of the current design.

### Two pages share one camera card

`/` is the stream operator's page. `/camera` is a PTZ-only page for wards that
encode in OBS and use this appliance purely as a camera remote — it never
touches the streamer.

Both render the same camera controls from `templates/_ptz.html`, with behaviour
in `static/ptz.js`. That markup and JS used to be inline in `index.html`;
`camera.html` was built by extracting them, **not** by copying. Keep it that
way — `tests/test_template_invariants.py` fails if either page grows its own
copy. Two top-level copies of `ptz.js` would also double-bind the delegated
preset handler, firing every recall twice.

The partial takes two parameters at the include site (see its header comment).
When passing a boolean, use Jinja's plain `default(x)` — `default(x, true)`
substitutes for any *falsy* value and will silently swallow a `false`.

`static/mock.js` (`?mock=1`) is shared by both pages and is the only way to
work on either UI without a camera. Load it *before* `ptz.js`, which starts
polling as soon as it runs. Page-specific values reach it through the
`<script type="application/json" id="mock-seed">` block each template renders.

**`PtzController` connects to `config.stream.last_source`.** That used to be a
side effect of `POST /api/start`, which an OBS-only site never calls — so
`/api/config` accepts `last_source` and the camera page's source picker writes
it. It's deliberately not validated against the discovery cache; cameras drop
off the network for a beat and rejecting the write would lose the operator's
choice.

### Stream keys live in `credentials.toml`, not `config.toml`

Wards are **enumerated** in `config.toml` (`name`, `sacrament_image`) and can
only be added or renamed there. But `key` and `channel_id` are entered on
`/setup` and stored device-locally in `credentials.toml` beside it, which
overlays whatever the deployed config holds.

The split exists because `install.sh` replaces `config.toml` **wholesale** on a
south/north deploy. Keys kept there would be wiped on the next deploy — and
wiped with `REPLACE_WITH_…` placeholder text on any clone that lacks the
git-ignored `config.<deployment>.toml`. `install.sh` reports the file's
presence and must never write, copy or delete it.

Two asymmetries to preserve:

- **`Config.load` overlays, `Config.save` un-overlays.** `save()` writes back
  the ward key/channel_id that `config.toml` originally held (captured in
  `Config._file_credentials`, a `ClassVar` so `asdict()` can't serialize it).
  Without that, any ordinary save — and the UI saves on nearly every request to
  persist quality / active_ward / last_source — would copy device-local secrets
  into the deployed file. There is a test for exactly this.
- **A blank key means "leave alone", not "erase".** The form never shows the
  current key, so an untouched field must not wipe it; editing only the channel
  ID would otherwise take a ward off the air. Erasing is explicit (`key="-"`).

Keys are **write-only** in the UI: `/setup` renders only the last 4 characters
(the same way `CONFIG_CHANGELOG.md` identifies keys) and no endpoint ever
returns a full key. The page warns when `[auth] enabled = false`, since anyone
on the LAN could then overwrite a key and take a ward off the air.

### YouTube liveness only ever reports POSITIVES

`youtube.py` checks whether a ward's stream is live by fetching
`youtube.com/channel/<channel_id>/live` and reading its
`<link rel="canonical">`: a broadcast makes it a `watch?v=<id>` URL, nothing
live leaves it the plain channel URL.

**The UI must never render a negative as a problem.** `not_visible` and
`unavailable` mean "we cannot tell", never "you are not live" — because an
unlisted broadcast and a stream that never went live are byte-identical from
outside. There is deliberately no warning styling in the YouTube CSS block.

If you are tempted to add a "your stream isn't live!" alarm, these were all
tried and all fail:

- *Track whether the operator clicked our control-room link* — that is
  inventing state we never observe.
- *Only alarm wards we have seen live before* — a ward that has **never** gone
  live (nobody ever creates the broadcast) is indistinguishable from an
  unlisted one, so the worst-configured ward gets permanently silenced.
- *Declare visibility per ward in config* — visibility is a property of each
  **broadcast**, not the channel. A ward can be public one week, unlisted the
  next.

Sound detection needs the Live Streaming API with the ward's OAuth credentials
(`liveBroadcasts.list` sees unlisted broadcasts directly). Until that exists,
the appliance confirms what it can and stays quiet otherwise.

Three implementation facts, all measured against live pages — the unit test
fixtures alone will not catch you breaking these:

- **Only the canonical href may be parsed for a video id.** An idle channel's
  page still contains `watch?v=` links for other videos (Me279's has one ~769
  KB in), so a body-wide search invents broadcasts. This was a real bug.
- The canonical tag sits ~700–760 KB into a 0.8–1.3 MB page, so the read
  cannot be cheaply truncated. Truncating turns every channel into
  `not_visible`, which — because negatives are never surfaced — fails silently.
- We request gzip (1.26 MB → ~297 KB). `"isLiveNow"` is **not** present on this
  URL even though it appears on `/@handle/live`; `"isLive":true` is.

`channel_id` is public info, optional, and lives in the committed example
templates beside `key`. A missing or wrong value costs nothing.

### Config is the source of truth for the UI

`src/ndi_rmtp/app.py` reloads `config.toml` on **every** request via
`get_config()`. Edits to the file take effect on the next API call, no
restart needed (except changes that affect the running ndi-streamer's CLI
args — those need a stop+start cycle).

Two consequences:
- Tests don't need to mock config aggressively; they can just write to a
  `tmp_path / "config.toml"` fixture.
- A linter that "normalises" the TOML (reformats `[[stream.wards]]` blocks
  into inline tables, for example) won't break anything — `tomllib` reads
  both shapes.

### State machine lives in `state.py`, NOT in `streamer.py`

`streamer.py` owns the subprocess and reads stderr; `state.py:StateMachine`
owns the IDLE/STARTING/STREAMING/STOPPING/ERROR transitions with an explicit
valid-transition table. When making changes to lifecycle behaviour, modify
`state.py` first (with tests) and only then wire it through `streamer.py`.

### Async lock-ordering rules

`Streamer._lock` (`asyncio.Lock`) protects state transitions and the
subprocess handle. The rule that's load-bearing for correctness:
**never await on `proc.wait()` or `asyncio.sleep()` while holding the lock.**
`stop()` snapshots `proc` inside the lock, releases the lock, then awaits.
The auto-restart loop (`_retry_loop`) sleeps outside the lock between
attempts. If you change this, expect deadlocks under concurrent toggle.

### libndi needs FFmpeg 7 at runtime for HX / HX3 decode

`libndi.so.6` does not ship a built-in H.264/H.265 decoder. When the
incoming NDI stream is **NDI HX** (H.264) or **NDI HX3** (H.265) — which
is what every consumer PTZ camera in this price range produces — libndi
dlopens `libavcodec.so.61` / `libavutil.so.59` at first-frame time. Those
are **FFmpeg 7** SONAMEs.

Pi OS Bookworm ships **FFmpeg 5** (`libavcodec.so.59`). Mac Homebrew is
on FFmpeg 7 by default, so this asymmetry only shows up on the Pi. When
the FFmpeg-7 libs are missing, libndi silently falls back to producing a
1920×1080 UYVY frame containing a static "Video decoder not found" splash.
Every consumer of that stream (the live encoder *and* the preview pane)
faithfully forwards the splash to its output. The streamer reaches
`STREAMING` state because frames *are* arriving — they're just placeholders.

The Pi installer bridges this by installing `jellyfin-ffmpeg7` into its own
prefix (`/usr/lib/jellyfin-ffmpeg/`) and adding the lib dir to ldconfig.
The system FFmpeg 5 stays in place — different SONAMEs, no conflict. When
debugging "Video decoder not found", check:

1. `ldconfig -p | grep libavcodec.so.61` — must resolve to the jellyfin-ffmpeg path
2. `LD_DEBUG=files <any libndi recv tool> 2>&1 | grep avcodec` — must show `libavcodec.so.61 ... generating link map` (not just `dynamically loaded by` with no follow-up)

Remove the jellyfin-ffmpeg7 workaround when Pi OS Trixie is the install
target — Trixie has FFmpeg 7 in its base repo.

### HX/HX3 audio: NDIlib_recv_capture_v2 is broken on Linux aarch64

libndi 6.3.2's `NDIlib_recv_capture_v2` silently returns ~ -47 dBFS noise
(no actual decode) when receiving NDI HX or HX3 audio on Linux aarch64,
even when the same receiver delivers valid HX3 video. `NDIlib_recv_capture_v3`
with `NDIlib_audio_frame_v3_t` decodes the same stream correctly to FLTP
@ 48 kHz with real signal. Our patch to ndi-streamer (`scripts/ndi-streamer-no-audio.patch`)
switches the audio path to v3; the symptom on a v2 build is "video works,
YouTube has no audible audio."

### Audio codec time_base is AV_TIME_BASE_Q, not {1, sample_rate}

`ffmpeg_output_setup_audio` configures `audio_codec_ctx->time_base` as
`{1, 1000000}` (microseconds), the same as video. The upstream ndi-streamer
set the AVFrame's pts via `swr_next_pts(...)` which returns values in
swr's own internal time base (`1/(in_sr * out_sr)` ≈ 1/2.3 GHz). Those
numbers, interpreted in microseconds by the encoder, produced timestamps
roughly 50,000× too large. The FLV/RTMP muxer happily accepted them — the
ingest then either silently drops audio (YouTube's old behavior) or parks
the broadcast in "Preparing stream" indefinitely (newer behavior).

The fix: track a `audio_pts_samples` counter in microseconds —
`pts += nb_samples * 1e6 / sample_rate` per AAC frame — and write that
into the AVFrame pts. If you ever change `audio_codec_ctx->time_base`
in `ffmpeg_output_setup_audio`, you must also change the increment unit
in `fc_ndi_audio_frame_to_avframe` to match.

### AAC encoder lookahead emits a negative-pts priming packet

LAVF's native AAC encoder emits its first packet with a small negative pts
(~ -21 ms at 48 kHz with frame_size=1024) representing the encoder's
priming/lookahead samples. YouTube's RTMP ingest refuses to start a stream
whose first audio packet has a negative timestamp — the broadcast sits in
"Preparing stream" until either you stop, or the negative-pts packet
times out and gets discarded. Our patch drops audio packets with pts < 0
inside `send_packets`. The discarded samples are silent priming; nothing
audible is lost.

### PTZ preview shares the PtzController's receiver

There's only one cyndilib `Receiver` instance in the Python process; the
preview snapshot pulls frames via `FrameSync` from the same connection used
for PTZ commands. Don't open a third receiver unless you understand why two
are already there.

### Encoder settings are tuned for live, not quality

x264 in `tune=zerolatency` + `nal-hrd=cbr` mode. `bframes=0`,
`rc_lookahead=0`, `sliced-threads=1` — all latency-hostile features off.
`gop_size=60` (2-second keyframe) is YouTube's recommended interval.
CBR with `filler=1` keeps the actual bitrate pinned at target even on
static content (sacrament image). Changing these blindly will regress
live performance.

### Quality knob: one ladder, resolution + bitrate coupled

`streamer.py:QUALITY_PRESETS` is a single 1D dict keyed by quality —
`high` (default), `medium`, `low`. Each entry holds a resolution AND a
video+audio bitrate. There is no separate resolution config; the operator
picks quality and gets a sensible resolution for free.

The ladder is designed for *graceful degradation under network stress*,
not for fine-grained tuning:

- `high`   — 1080p30 @ 6 Mbps + 192 kbps audio (top of YouTube's 1080p range)
- `medium` — 1080p30 @ 3 Mbps + 128 kbps audio (bottom of same range; same
             picture, ~half the bandwidth)
- `low`    — 360p30  @ 600 kbps + 128 kbps audio (drops resolution; ~10x
             less total bandwidth than high — true escape hatch)

Each step lands inside YouTube's recommended range for its resolution.
Sending OVER YouTube's range triggers their "bitrate too high" warning
and may cause downscaled playback.

`RESOLUTION_DIMS` still maps a resolution string to
`(width, height, bandwidth)` for the ndi-streamer CLI flags. Two
non-obvious things to know about that mapping:

- `bandwidth=lowest` is the camera's **proxy stream** (typically ~360p,
  H.264, much smaller payload), not just a quality preference. `highest`
  is the camera's native feed. NDI HX/HX3 cameras decide independently
  which streams to publish at which bandwidths.
- 720p mode (currently unused by any quality preset) is the only
  resolution that *downscales* in our pipeline: asks the camera for the
  full-bandwidth feed (`highest`) and lets `sws_scale` inside
  ndi-streamer shrink the result to 1280×720 before encoding.

The first-frame dimensions check in the encoder loop (originally a
sanity bail-out when the input feed's resolution drifts) is gated on
`opts.target_width == 0` — when we're explicitly resizing, sws_scale
absorbs input-dim variation and the encoder/muxer stay stable.

## Config schema gotchas

- `[auth] enabled = false` disables HTTP Basic Auth entirely — the
  dependency factory uses `HTTPBasic(auto_error=False)` and bypasses the
  check when `enabled` is false. Don't change the `auto_error` setting.
- `[[stream.wards]]` is an array-of-tables but TOML linters often rewrite
  it as `wards = [{ name = "..." }, ...]` — both parse to the same Python
  shape, don't fight the linter.
- `Ward.sacrament_image` can be a relative path (resolved from
  `Path.cwd()`) or absolute. Empty string disables sacrament mode for
  that ward.
- `[ptz.presets]` slots are integers 0–99, matching the NDI preset index
  on the camera. Stores are managed by the camera's own native preset
  mechanism; we only recall.

## Things that look broken but are intentional

- `scripts/ndi-streamer-no-audio.patch` is a misleading filename. It now
  contains all our C modifications. `TODO.md` flags the rename.
- Config storage is split into **tracked templates** and **untracked live
  files**. Only `config.south.example.toml` / `config.north.example.toml`
  (placeholder keys) are committed. The live `config.south.toml`,
  `config.north.toml`, and the `config.toml` symlink hold real YouTube
  stream keys and are **git-ignored** — they live only in your working
  clone and on the appliances, never in git history. (This repo was scrubbed
  of the previously-committed keys with `git filter-repo` before going
  public; see the security note below.)
  - Local dev: `config.toml` is a symlink to `config.south.toml` so
    `uv run ndi-rmtp` "just works". `scripts/dev-setup-mac.sh` seeds
    `config.south.toml` from the template on a fresh clone.
  - Deploy: `scripts/install.sh` prompts north/south and copies your local
    `config.$DEPLOYMENT.toml` to `/home/ndi-rmtp/.config/ndi-rmtp/config.toml`
    (override via `NDI_RMTP_DEPLOYMENT={south,north,keep}`). If the live file
    is absent it falls back to the `.example` template with placeholder keys.
  - To add/rotate a key, edit your local `config.$DEPLOYMENT.toml` (never the
    `.example`), bump `config_version`, note it in `CONFIG_CHANGELOG.md`, and
    deploy. The changelog only records key *suffixes*, so it stays safe to
    commit.
- `~/Downloads/jordan_willows_sacrament_overlay.svg` is referenced
  historically in some docs as the source of truth for the sacrament
  template. The active SVGs in `sacrament/` are project-vendored.
- The integration test (`tests/test_integration.py`) skips by default and
  has never been run end-to-end. Don't trust that it actually passes
  against a real source until someone has run it with
  `NDI_TEST_SOURCE="..." uv run pytest -m integration`.

## When something doesn't work

YouTube playback issues are usually NOT our fault. Common culprits in order:
1. Network bufferbloat (check `networkQuality` — Responsiveness should be
   >300 RPM; under load on most home networks it's 50–100, which causes
   spinners).
2. YouTube's "Stream latency" setting in YouTube Studio. Normal mode
   tolerates jitter; Ultra low does not.
3. Camera audio not actually wired up (NDI delivers silent frames; we
   faithfully forward them — verify with `scripts/check-ndi-audio.py` if
   it exists, or the in-process numpy peak check we used during the
   "no sound" diagnostic).

`stop+start` the stream after any change to encoder settings, ward
selection, or `Streamer.start()` args — the existing subprocess is locked
to whatever CLI args it was spawned with.
