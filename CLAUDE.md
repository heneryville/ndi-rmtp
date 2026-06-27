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

Bitrate presets in `streamer.py:QUALITY_PRESETS` are a 2D dict keyed by
`(resolution, quality)`. Each preset lands inside YouTube's recommended
range for that resolution — sending too HIGH a bitrate triggers YouTube's
"bitrate too high" warning AND can cause them to transcode the stream
down to a lower quality tier on playback.

### Resolution config knob: bandwidth + target dims

`[stream] resolution` in `config.toml` accepts `"360p30"`, `"720p30"`, or
`"1080p30"` (default `1080p30`). The Python side maps these to a
`(width, height, bandwidth)` triple via `RESOLUTION_DIMS` and passes them
to ndi-streamer as `--target_width`, `--target_height`, `--bandwidth`.

Two non-obvious things to know:

- `bandwidth=lowest` is the camera's **proxy stream** (typically ~360p,
  H.264, much smaller payload), not just a quality preference. `highest`
  is the camera's native feed. NDI HX/HX3 cameras decide independently
  which streams to publish at which bandwidths.
- 720p mode is the only resolution that *downscales* in our pipeline:
  it asks the camera for the full-bandwidth feed (`highest`) and lets
  `sws_scale` inside ndi-streamer shrink the result to 1280×720 before
  encoding. 360p and 1080p both ask the camera for natively-sized
  frames and don't resize at all.

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
- `config.toml` is in the repo (and in git history) **on purpose** — the
  user's distribution model is to clone with config baked in. The repo is
  private. If visibility ever changes, rotate keys first.
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
