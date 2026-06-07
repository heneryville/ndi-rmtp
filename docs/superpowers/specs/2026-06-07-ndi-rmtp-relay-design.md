# NDI → YouTube RTMP Relay — Design

**Date:** 2026-06-07
**Status:** Approved for planning
**Production target:** Raspberry Pi 5 (4 GB or 8 GB), Raspberry Pi OS Bookworm/Trixie (aarch64)
**Supported dev/test target:** Apple Silicon macOS (arm64-darwin)

## Purpose

A small appliance that runs on a Raspberry Pi 5, pulls a single NDI video source off
the local network, transcodes it on the fly, and re-broadcasts to YouTube Live via
RTMP. A local web page controls it: pick the NDI source, paste a YouTube stream
key, and press Start/Stop.

## Non-goals

- Multi-source mixing, scenes, or graphics (use OBS for that).
- Multiple simultaneous outputs (single NDI in, single RTMP out).
- Cloud-managed control plane or user accounts (LAN-only, single password).
- Recording to disk.

## Architecture

Two tiers: a thin Python control plane and a native C streaming worker. The
control plane never touches video frames.

```
[NDI source on LAN]
        │  NDI/SpeedHQ over TCP
        ▼
┌────────────────────────────┐
│ ndi_streamer  (C process)  │   spawned/killed by the control plane
│   NDI SDK → libavcodec     │   x264 video encode, AAC audio encode
│   → libavformat (FLV/RTMP) │
└────────────────────────────┘
        │  RTMP/FLV
        ▼
[YouTube ingest: rtmp://a.rtmp.youtube.com/live2/<key>]

┌──────────────────────────────────────┐
│ FastAPI app (uvicorn, port 8080)     │
│  ─ Web UI (HTML + HTMX, no SPA)      │
│  ─ Source discovery                  │
│  ─ Subprocess lifecycle              │
│  ─ Config persistence                │
│  ─ Status polling endpoint           │
└──────────────────────────────────────┘
```

### Why this split

- **Performance**: zero-copy from NDI → libav → RTMP, all in native C. Python is
  not in the per-frame data path.
- **No FFmpeg fork**: `ndi_streamer` links the *vanilla* FFmpeg libraries shipped
  with Debian. No 1-3 hour custom FFmpeg build on every upgrade.
- **Simple control**: the streaming worker is a CLI binary. The control plane
  spawns it with `asyncio.create_subprocess_exec` and reads its stderr.

### Upstream dependency

- [`alim-zanibekov/ndi_streamer`](https://github.com/alim-zanibekov/ndi_streamer)
  — MIT-licensed C program, ~1 kLOC. Links `libavformat`, `libavcodec`,
  `libswresample`, `libswscale`, `libavutil`, plus the NDI SDK.
- If upstream stops responding, we have the right to vendor a fork in this repo.

### Risk: validate before building the control plane

The very first plan task is a **proof-of-life spike**: build `ndi_streamer` on
the Pi and stream the user's existing test NDI source to YouTube successfully
from the CLI. Only after the spike passes do we build the FastAPI layer. If the
spike fails, the fallback is the FFmpeg-with-libndi_newtek build (1-3 hr build
but well-documented).

## Components

| Component | Responsibility | Path |
|---|---|---|
| `app.py` | FastAPI app, routes, Basic Auth dependency | `src/ndi_rmtp/app.py` |
| `streamer.py` | Async subprocess control; stderr → ring buffer; state machine | `src/ndi_rmtp/streamer.py` |
| `ndi.py` | NDI source discovery; periodic refresh; TTL cache | `src/ndi_rmtp/ndi.py` |
| `config.py` | Load/save TOML config; atomic writes; chmod 0600 | `src/ndi_rmtp/config.py` |
| `auth.py` | HTTP Basic Auth against plain-text password in config | `src/ndi_rmtp/auth.py` |
| `templates/index.html` | Control UI, HTMX-driven live regions | `src/ndi_rmtp/templates/` |
| `static/` | CSS only; no JS framework | `src/ndi_rmtp/static/` |
| `scripts/install.sh` | Idempotent installer: deps, NDI SDK, build, systemd | `scripts/install.sh` |
| `scripts/ndi-rmtp.service` | systemd unit | `scripts/ndi-rmtp.service` |
| `tests/` | pytest + pytest-asyncio | `tests/` |

Each module has one clear job. `streamer.py` owns the state machine and the
subprocess; `app.py` owns HTTP and delegates everything else.

## Web UI

Single page with three regions, served from `/`:

```
┌─────────────────────────────────────────────────┐
│ NDI → YouTube Relay                  [ status ] │
├─────────────────────────────────────────────────┤
│ NDI Source: [ PC-NAME (Test Pattern)      ▼ ]  │ live-refreshed
│ Stream Key: [ ••••••••••••••••• ] [ Edit ]      │ masked, persisted
│ Quality:    ( ) Low  (•) Medium  ( ) High       │
│                                                 │
│             [   ▶ START STREAM   ]              │
├─────────────────────────────────────────────────┤
│ Status: STREAMING — 00:14:32                    │
│ Last log lines (live):                          │
│   frame=  4521 fps=30 bitrate=4801kbits/s ...   │
└─────────────────────────────────────────────────┘
```

- **Source dropdown**: populated from `/api/sources`. HTMX polls every 5 s when
  IDLE; pauses during streaming (the source is fixed for the session).
- **Stream key**: stored on disk; UI shows a masked placeholder. Clicking *Edit*
  reveals a text input. Saving writes to config and re-renders masked.
- **Quality preset**: maps to `ndi_streamer` flags (video bitrate, x264 preset,
  audio bitrate). See *Quality presets* below.
- **Start/Stop button**: toggles based on state. Disabled in transient states
  (`STARTING`, `STOPPING`).
- **Status block**: polled from `/api/status` every 2 s via HTMX. Shows state,
  uptime, last ~20 log lines.

### Routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Render `index.html` |
| GET | `/api/sources` | JSON: `[{name, address}]` |
| GET | `/api/status` | JSON: `{state, uptime_s, last_error, log_tail}` |
| POST | `/api/config` | Update stream key / quality preset (form-encoded) |
| POST | `/api/start` | Start streaming with current config |
| POST | `/api/stop` | Stop streaming |
| GET | `/healthz` | Liveness probe (no auth) |

All routes except `/healthz` and `/static/*` require HTTP Basic Auth.

## State machine

```
                ┌──────────┐
        ┌──────▶│   IDLE   │◀──────┐
        │       └────┬─────┘       │
   stop │        start             │ subprocess exits
        │            ▼             │ cleanly
        │       ┌──────────┐       │
        │       │ STARTING │       │
        │       └────┬─────┘       │
        │            │ subprocess  │
        │            │ printed     │
        │            │ "streaming" │
        │            ▼             │
        │       ┌──────────┐       │
        └───────│STREAMING ├───────┘
                └────┬─────┘
                     │ subprocess exits non-zero
                     │ or stops emitting frames
                     ▼
                ┌──────────┐
                │  ERROR   │── backoff → STARTING (if auto_restart)
                └────┬─────┘                or stay until user acks
                     │ user clicks "Dismiss"
                     ▼
                   IDLE
```

`STARTING` lasts until either `ndi_streamer` emits its first frame log line
(detected by stderr pattern) or a 10 s timeout fires (→ `ERROR`).

`STOPPING` is brief: SIGTERM, wait 5 s, SIGKILL if still alive.

## Configuration

File: `~/.config/ndi-rmtp/config.toml` (mode 0600, owned by the service user).

```toml
[auth]
username = "admin"
password = "set-on-first-run"    # plain text by design (LAN appliance)

[stream]
last_source = "PC-NAME (Test Pattern)"
stream_key  = "xxxx-xxxx-xxxx-xxxx"
rtmp_url    = "rtmp://a.rtmp.youtube.com/live2"
quality     = "medium"

[advanced]
auto_restart       = true
restart_backoff_s  = [2, 5, 15, 60]
discovery_ttl_s    = 5
status_poll_ms     = 2000
```

**Plain-text password rationale**: the file is chmod 0600 and only readable by
the `ndi-rmtp` system user. The threat model is "stop housemates from clicking
Stop mid-stream", not "defend against an attacker with shell access". A hash
would not help: an attacker who can read the file can also read the rest of the
config and the stream key anyway. Plain text is the honest representation.

**Atomic writes**: write to a temp file in the same directory, then `os.rename`.
Never leave a half-written config behind a crash.

### First-run bootstrap

When the service starts and the config file does not exist:
1. Generate a random 12-char alphanumeric password.
2. Write a config with `username="admin"` and that password.
3. Log the password to stderr and (if running interactively) print it to the
   console. The install script captures this and shows it to the operator once.

## Quality presets

Map to `ndi_streamer` flags. Concrete values are validated during the spike;
the table here is the working starting point.

| Preset | Resolution | Video bitrate | x264 preset | Audio bitrate |
|---|---|---|---|---|
| Low    | 720p30 | 2500 kbps | `superfast` | 128 kbps |
| Medium | 1080p30 | 4500 kbps | `superfast` | 128 kbps |
| High   | 1080p30 | 6000 kbps | `veryfast`  | 192 kbps |

Resolution comes from the NDI source if it matches; otherwise `ndi_streamer`
scales (or we let it pass through — confirmed during spike). All presets use
H.264 High profile, AAC-LC, 2 s keyframe interval (YouTube's recommendation).

## Error handling

| Scenario | Detection | Response |
|---|---|---|
| Subprocess exits non-zero while STREAMING | exit code captured | `ERROR`; if `auto_restart`, follow backoff schedule; surface last 20 stderr lines |
| YouTube rejects stream key | stderr pattern (`Server returned 403\|invalid stream key`) | `ERROR`; do **not** auto-retry; show clear "YouTube rejected the stream key" message |
| NDI source disappears mid-stream | subprocess exits + stderr mentions NDI / source loss | `ERROR`; same backoff path as generic crash; UI hint "source unreachable" |
| Selected NDI source missing at start | not in discovery cache | refuse to start, return 409 to UI with message |
| Subprocess hang on stop | wait 5 s after SIGTERM | SIGKILL; log it; transition to IDLE |
| Config file unreadable / malformed | exception on load | fall back to first-run bootstrap and log loudly |
| Pi loses network | NDI dies, RTMP dies | subprocess crash path; backoff retries pick it up when network returns |

The control plane never silently swallows an error. Every transition into
`ERROR` writes the cause to the log ring buffer so it appears in the UI.

## Logging & observability

- Service logs to stdout (captured by systemd → journald).
- `streamer.py` keeps a ring buffer (last 200 lines) of `ndi_streamer` stderr,
  exposed via `/api/status` for the UI.
- One structured log line per state transition (state, reason, exit code if
  any).

## Testing

Validation is phased: each phase has a clear pass/fail signal and unblocks
the next. Phases 0 and 2-5 are explicit checkpoints in the implementation
plan; phase 1 (automated tests) runs continuously.

### Phase 0 — Spike: prove the data path works (before any app code)

Goal: confirm `ndi_streamer` can pull the user's test NDI source and push to
RTMP on both Mac and Pi. If this fails, switch to the FFmpeg-with-libndi
fallback before sinking effort into the control plane.

Run on the Mac first, then repeat all steps on the Pi:

1. **Build**: clone and build `ndi_streamer`. Pass = a working binary.
2. **Source discovery**: run the binary with no args (or its `--list`
   equivalent — confirmed during this step). Pass = the user's test NDI
   source name appears in the output within 5 s.
3. **Local loop**: start an `nginx-rtmp` container locally
   (`docker run -p 1935:1935 tiangolo/nginx-rtmp`), run
   `ndi_streamer -n "<source>" -o rtmp://localhost/live/test`, watch with
   `ffplay rtmp://localhost/live/test`. Pass = video plays for ≥ 30 s with
   no FPS warnings in stderr.
4. **YouTube round-trip**: create a YouTube "unlisted" stream, run
   `ndi_streamer -n "<source>" -o "rtmp://a.rtmp.youtube.com/live2/<key>"`,
   open YouTube Studio. Pass = the stream goes live, health panel reads
   "Excellent" or "Good" for ≥ 60 s, audio is in sync.
5. **Sanity**: on the Pi only, run `htop` during step 4. Pass = total CPU
   load ≤ 80 % at 1080p30 Medium preset; SoC temp ≤ 75 °C.

If any step fails, the spike report (a short markdown in `docs/superpowers/`)
explains what failed and which fallback we pick (FFmpeg-libndi, GStreamer,
or roll-our-own).

### Phase 1 — Automated tests (continuous, run in CI and locally)

**Unit tests** (`pytest`, `pytest-asyncio`, no external services):

- `test_config.py` — load, save, atomic write, missing-file bootstrap, bad
  TOML → bootstrap, mode 0600 after write.
- `test_state_machine.py` — every valid transition; rejected transitions
  return an error and leave state unchanged.
- `test_streamer.py` — subprocess control against `tests/fixtures/fake_ndi_streamer.py`
  (a Python script that emits scripted stderr). Cover: clean start, immediate
  crash, bad-stream-key pattern detection, hang on stop → SIGKILL fallback,
  log ring-buffer rotation, exponential backoff schedule.
- `test_ndi_discovery.py` — fake binary emits scripted source lists; verify
  parsing, TTL cache, and refresh-on-expire.
- `test_auth.py` — Basic Auth accepts configured user, rejects wrong
  password, rejects missing header, exempts `/healthz` and `/static/*`.
- `test_routes.py` — FastAPI `TestClient`; verify each route's auth gate,
  response shape, and that POST `/api/start` calls `streamer.start` with the
  right args. Use a mocked `streamer` to keep tests fast.

Pass = `uv run pytest` is green on both Mac and Pi.

**Integration test** (`@pytest.mark.integration`, opt-in via
`pytest -m integration` or `RUN_INTEGRATION=1`):

- Requires an NDI source on the LAN (env var `NDI_TEST_SOURCE`) and Docker.
- Test starts `nginx-rtmp` in a container, spawns real `ndi_streamer` for
  10 s pointed at the local RTMP, then asserts:
  - FLV frames land at the RTMP sink (`ffprobe` reports a valid stream).
  - Video stream has H.264, audio has AAC.
  - Frame rate is within ±10 % of expected.
- After assertions, SIGTERM the subprocess and confirm it exits cleanly
  within 5 s.

Pass = integration test green on Mac with the user's test NDI source.

### Phase 2 — Manual end-to-end on Mac

Done from a browser on the Mac itself. This is the dev-loop acceptance.

1. `uv run uvicorn ndi_rmtp.app:app --reload --port 8080`.
2. Open `http://localhost:8080`, log in with the first-run password printed
   to stderr.
3. **Source dropdown** populates within 5 s and includes the user's test
   NDI source.
4. **Save stream key**: enter a YouTube unlisted key, save. Reload page —
   the key is still there (masked). Inspect `~/.config/ndi-rmtp/config.toml`,
   confirm mode 0600 and key present.
5. **Start**: click Start. State transitions visibly IDLE → STARTING →
   STREAMING within 10 s. Log tail shows `ndi_streamer` frame lines.
6. **YouTube confirmation**: stream is live in YouTube Studio within 30 s
   of clicking Start. Watch for 2 min, expect "Excellent" or "Good" health.
7. **Stop**: click Stop. State transitions STREAMING → STOPPING → IDLE
   within 5 s. YouTube Studio shows "Stream offline" within ~30 s.
8. **Restart persistence**: Ctrl-C uvicorn, run it again. Source selection,
   stream key, and quality preset are restored.

### Phase 3 — Error-path checklist (Mac is fine; cheaper than Pi)

Each item is a manual scenario with a specific expected UI/state response.

| Scenario | How to induce | Expected behavior |
|---|---|---|
| Bad YouTube stream key | Save a deliberately wrong key, Start | State → ERROR within ~5 s, UI shows "YouTube rejected the stream key", **no auto-retry**, Dismiss returns to IDLE |
| NDI source disappears mid-stream | Stop the test NDI source on its host | State → ERROR with "source unreachable"; auto-restart attempts per backoff; when source returns, next attempt succeeds → STREAMING |
| `ndi_streamer` killed externally | `pkill ndi_streamer` while STREAMING | State → ERROR; auto-restart kicks in per backoff |
| Wrong web password | Try logging in with wrong creds | 401, no info leak, no rate-limiter bypass |
| Concurrent Start clicks | Spam the Start button | Only one subprocess spawns; subsequent clicks are 409 or no-ops |
| Subprocess hang on stop | Fake binary that ignores SIGTERM (`tests/fixtures/`) | After 5 s, SIGKILL; state → IDLE; one log line records the SIGKILL |
| Malformed config | Manually corrupt `config.toml`, restart service | Service logs the parse error and rebootstraps with a new random password |
| Network drop mid-stream | Disable Wi-Fi for 30 s | ERROR + backoff; recovers on its own when network returns |

Pass = every row behaves as described, observed by hand.

### Phase 4 — Pi acceptance and burn-in

Done on the actual Raspberry Pi 5, accessed from a browser on the Mac via
`http://<pi-hostname>.local:8080`.

1. **Install from scratch**: run `scripts/install.sh` on a fresh Pi OS
   image. Pass = the script completes without manual intervention beyond
   the NDI SDK EULA prompt, prints the LAN URL, and prints the first-run
   admin password exactly once.
2. **Boot persistence**: reboot the Pi. Service starts automatically
   (`systemctl is-active ndi-rmtp` = `active`). UI is reachable.
3. **End-to-end stream**: repeat Phase 2 steps 2-7, but driving the Pi
   from a Mac browser. Pass criteria identical.
4. **30-minute burn-in**: stream the test NDI source to YouTube unlisted
   for 30 min continuous. During the run, on the Pi:
   - `htop`: total CPU ≤ 80 % at 1080p30 Medium; no single core pinned
     at 100 %.
   - `vcgencmd measure_temp`: ≤ 75 °C steady-state.
   - `free -h`: resident set stable, no leak.
   - YouTube Studio: health "Excellent" or "Good" for ≥ 95 % of the run,
     no dropped-frames warnings.
   Pass = all four hold for the full 30 min.
5. **Recovery after reboot mid-stream**: while STREAMING, `sudo reboot`.
   After boot, service comes back in IDLE (we deliberately do not
   auto-resume — too easy to start a stream the operator didn't intend).

### What we are NOT testing

- Streams above 1080p30 (not a goal for v1; revisit if needed).
- Long-running (multi-day) stability — the 30-min burn-in is the
  acceptance bar.
- Multiple simultaneous streams (explicitly a non-goal).
- Browsers other than current Firefox/Chrome/Safari on desktop. The UI is
  intentionally simple and should work, but is not part of acceptance.

## Deployment

`scripts/install.sh` is idempotent and runs on a stock Raspberry Pi OS Bookworm
or Trixie aarch64:

1. `apt-get update && apt-get install -y` build deps, `ffmpeg`,
   `libavformat-dev`, `libavcodec-dev`, `libswresample-dev`, `libswscale-dev`,
   `libavutil-dev`, `cmake`, `git`, `python3.11`.
2. Install `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
3. Prompt operator to accept the NewTek NDI EULA and download the NDI SDK for
   Linux ARM64 from <https://ndi.video/sdk/>. Extract NDI shared libs to
   `/usr/local/lib/`, headers to `/usr/local/include/ndi/`, then `ldconfig`.
4. `git clone` `ndi_streamer` to `/opt/ndi-rmtp/build/ndi_streamer`, build, and
   copy the binary to `/usr/local/bin/`.
5. Create system user `ndi-rmtp`; create `/opt/ndi-rmtp/`.
6. `uv sync` the Python app into `/opt/ndi-rmtp/.venv`.
7. Install `scripts/ndi-rmtp.service` to `/etc/systemd/system/`; `systemctl
   enable --now ndi-rmtp`.
8. Print the LAN URL (`http://<hostname>.local:8080`) and the first-run admin
   password.

systemd unit highlights: `User=ndi-rmtp`, `Restart=on-failure`,
`RestartSec=3`, `ProtectSystem=strict`, `ReadWritePaths=/home/ndi-rmtp/.config`.

## Development on Apple Silicon macOS

The same app runs on an Apple Silicon Mac for development and end-to-end
testing against the user's existing NDI source. This is **dev-only** — there
is no Mac equivalent of the systemd deployment, and `scripts/install.sh` is
Pi-only.

`scripts/dev-setup-mac.sh` (idempotent, run once per machine):

1. Verify Homebrew is installed.
2. `brew install ffmpeg cmake pkg-config uv`.
3. Prompt operator to download the **NDI SDK for Apple** from
   <https://ndi.video/sdk/> and accept the EULA. Install to the SDK's default
   `/Library/NDI SDK for Apple/`. The script symlinks
   `lib/macOS/libndi.dylib` to `/usr/local/lib/` and headers to
   `/usr/local/include/ndi/`.
4. `git clone` `ndi_streamer` into `build/ndi_streamer/`, build (CMake
   auto-detects the macOS libs), and copy the binary to `./bin/` inside the
   project (not `/usr/local/bin/` — we keep dev artifacts out of system paths).
5. `uv sync` the Python app.

Running locally:

```
uv run uvicorn ndi_rmtp.app:app --host 0.0.0.0 --port 8080 --reload
```

The app reads `./bin/ndi_streamer` first if it exists, otherwise falls back to
`/usr/local/bin/ndi_streamer`. This lets the same code path work on Mac
(project-local binary) and on the Pi (system-installed binary) without
configuration.

Config on Mac lives at `~/.config/ndi-rmtp/config.toml` — same as Pi.

### Cross-platform module behavior

- `streamer.py` resolves the `ndi_streamer` binary via the search-path rule
  above; everything else (asyncio subprocess, signals, stderr parsing) is
  identical on both OSes.
- `config.py` uses `pathlib` and platform-neutral `os.chmod(path, 0o600)`.
- `ndi.py` runs the same discovery command on both.
- The integration test fixture starts an `nginx-rtmp` container via Docker
  Desktop (Mac) or Docker Engine (Pi) — same compose file.

## Project layout

```
ndi-rmtp/
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-06-07-ndi-rmtp-relay-design.md
├── pyproject.toml                 # managed by uv
├── uv.lock
├── src/
│   └── ndi_rmtp/
│       ├── __init__.py
│       ├── app.py
│       ├── streamer.py
│       ├── ndi.py
│       ├── config.py
│       ├── auth.py
│       ├── templates/
│       │   └── index.html
│       └── static/
│           └── style.css
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   └── fake_ndi_streamer.py
│   ├── test_config.py
│   ├── test_state_machine.py
│   ├── test_streamer.py
│   ├── test_ndi_discovery.py
│   ├── test_auth.py
│   ├── test_routes.py
│   └── test_integration.py        # gated by @pytest.mark.integration
├── scripts/
│   ├── install.sh                 # Pi-only
│   ├── dev-setup-mac.sh           # Apple Silicon dev setup
│   └── ndi-rmtp.service
└── README.md
```

## Open questions / explicit deferrals

- **Source discovery binary**: we assume `ndi_streamer` can list sources via a
  flag. Confirmed during the spike; if not, we ship a tiny separate
  `ndi-find` helper that calls the NDI SDK directly (10-line C program from the
  SDK examples) or wrap one of the NDI SDK example binaries.
- **Pass-through resolution vs forced preset**: confirmed during spike. If
  `ndi_streamer` can encode at the source resolution unchanged, the "Quality"
  control picks bitrate only; if not, it forces a resolution.
- **YouTube ingest URL**: hard-coded to the primary ingest. If a user needs the
  backup ingest, they can edit `rtmp_url` in `config.toml` and restart.
