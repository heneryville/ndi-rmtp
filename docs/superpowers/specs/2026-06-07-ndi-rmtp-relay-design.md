# NDI → YouTube RTMP Relay — Design

**Date:** 2026-06-07
**Status:** Approved for planning
**Target hardware:** Raspberry Pi 5 (4 GB or 8 GB), Raspberry Pi OS Bookworm/Trixie (aarch64)

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

### Unit tests (pytest + pytest-asyncio)

- `test_config.py` — load, save, atomic write, missing-file bootstrap, bad
  TOML → bootstrap, permission bits after write.
- `test_state_machine.py` — every valid transition; rejected transitions
  return an error without changing state.
- `test_streamer.py` — subprocess control with a fake binary (`tests/fixtures/fake_ndi_streamer.py`)
  that prints scripted stderr lines. Cover: clean start, immediate crash,
  bad-stream-key pattern, SIGTERM/SIGKILL fallback, log ring buffer rotation.
- `test_ndi_discovery.py` — fake binary emits scripted source lists; verify
  parsing and TTL cache.
- `test_auth.py` — Basic Auth accepts the configured user, rejects others,
  rejects on missing header.
- `test_routes.py` — FastAPI `TestClient`; verify every route's auth gate,
  shape of responses, and that POST `/api/start` calls `streamer.start` with
  the right args.

### Integration test (`@pytest.mark.integration`, off by default)

- Requires an NDI source on the LAN (env var `NDI_TEST_SOURCE`) and a local
  `nginx-rtmp` container (started by the test).
- Spawns real `ndi_streamer` for ~10 s, asserts FLV frames land in the local
  RTMP sink, then SIGTERMs.

### Manual acceptance

- On the Pi, stream the user's test NDI source to a YouTube "unlisted" event.
  Confirm it shows up in YouTube Studio; let it run for 5 min; confirm clean
  stop.

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
│   ├── install.sh
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
