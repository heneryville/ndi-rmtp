# ndi-rmtp

A small appliance that runs on an Apple Silicon Mac (dev) or a Raspberry Pi 5
(production), receives an NDI video stream from a PTZ-capable camera on the
LAN, and rebroadcasts it to YouTube Live over RTMP. A simple web UI controls
it: start/stop streaming, mute audio, toggle a per-ward "sacrament" overlay,
and drive the camera (presets + manual pan/tilt/zoom with a live preview).

Built for streaming small congregational worship services — multiple wards
share the same appliance, each with its own stream key and overlay.

## Architecture

```
[PTZ camera, NDI] ──┐
                    │  NDI over LAN
                    ▼
┌─────────────────────────────────────────────────────┐
│  ndi-streamer (patched C, vendored at build time)   │
│    NDI receive → libx264 (zerolatency, CBR) → AAC   │
│    → libavformat RTMP                               │
│  Toggleable in-process: SIGUSR1 swaps live feed     │
│  for a pre-loaded image (sacrament mode), no        │
│  subprocess restart, no YouTube reconnect.          │
└─────────────────────────────────────────────────────┘
                    │  RTMP/FLV
                    ▼
[rtmp://a.rtmp.youtube.com/live2/<key>]

┌──────────────────────────────────────────────────┐
│  FastAPI control plane (uvicorn, port 8080)      │
│   • Web UI (HTMX, no JS framework)               │
│   • Subprocess lifecycle (state machine)         │
│   • NDI source discovery                         │
│   • PTZ control + JPEG preview (cyndilib)        │
│   • Config persistence (TOML)                    │
└──────────────────────────────────────────────────┘
```

The Python side never touches video frames in the main data path. It spawns
and signals `ndi-streamer`. The PTZ preview pane uses `cyndilib` on a
separate low-bandwidth NDI receiver and serves JPEG snapshots over HTTP.

## Status

- ✅ Validated on Apple Silicon Mac against a real PTZ camera streaming to
  YouTube Live
- ✅ Sacrament-mode signal toggle: no RTMP reconnect, no "no data" gap
- ✅ Multi-ward config with per-ward overlays
- ✅ PTZ presets + d-pad + zoom + live preview pane
- ✅ Raspberry Pi 5 **install** validated end-to-end (Pi OS Lite arm64, Trixie):
  `scripts/install.sh` builds `ndi-streamer`, installs the systemd service, and
  serves the UI on port 80.
- ⚠️ Live streaming *from the Pi* to YouTube not yet exercised — needs the PTZ
  camera on the LAN. See `TODO.md`.

## Dev setup (Apple Silicon Mac)

```sh
./scripts/dev-setup-mac.sh
uv run ndi-rmtp
```

The dev-setup script:
1. Installs Homebrew packages: `ffmpeg`, `cmake`, `pkgconf`, `librsvg`,
   `libndi` cask, `uv`
2. Prompts you to install the NDI SDK for Apple manually (EULA gate)
3. Clones `ndi-streamer` from upstream, applies our patches
   (`scripts/ndi-streamer-no-audio.patch`), builds it, symlinks the binary
   into `./bin/`
4. Runs `uv sync --extra dev`
5. Seeds a local `config.south.toml` from `config.south.example.toml` and
   points `config.toml` at it (placeholder stream keys — edit before going
   live). The live config is git-ignored; see the Security note below.

Open <http://localhost:8080>. With auth disabled in `config.toml` (the
template default) the page loads without a login.

## Pi install

```sh
sudo ./scripts/install.sh
```

apt-based. Service runs as user `ndi-rmtp` under systemd. Config lives at
`/home/ndi-rmtp/.config/ndi-rmtp/config.toml` via the `NDI_RMTP_CONFIG` env
var set in the systemd unit. See `TODO.md` for what still needs to happen
here.

## Configuration

Everything is in `config.toml` at the repo root (or `$NDI_RMTP_CONFIG`).
Re-read on every request, so edits take effect on the next API call.

Notable sections:

- `[auth]` — HTTP Basic Auth. `enabled = false` disables auth entirely for
  trusted LAN use. Plain-text password by design (LAN appliance threat
  model — see the design spec).
- `[stream]` — `last_source`, `active_ward`, `quality`, `rtmp_url`. The
  active ward determines both the YouTube key in use and the sacrament
  overlay shown.
- `[[stream.wards]]` — each ward carries `name`, `key` (YouTube stream key),
  and `sacrament_image` (path to an SVG/PNG). Wards can share keys.
- `[ptz]` — `presets` (list of `{name, slot}` where slot is the NDI preset
  index 0–99), plus `recall_speed` and `move_speed`.
- `[advanced]` — auto-restart, backoff schedule, polling rates.

> **Security note.** YouTube stream keys and the admin password are secrets
> and are **not** committed. Only `config.south.example.toml` /
> `config.north.example.toml` (placeholder keys) are tracked; the live
> `config.toml` / `config.<south|north>.toml` are git-ignored. Copy a template
> to `config.<south|north>.toml` and fill in your own keys:
>
> ```sh
> cp config.south.example.toml config.south.toml   # then edit the key fields
> ```
>
> On a fresh clone, `scripts/dev-setup-mac.sh` seeds this for you.

## Concepts

- **Ward** — a broadcast target. The dropdown in the UI selects a ward, and
  starting a stream uses that ward's key + overlay.
- **Sacrament mode** — video is replaced with a still image (the active
  ward's SVG) and audio samples are zeroed. Implemented via `SIGUSR1` to
  the running encoder so the RTMP session stays alive; viewers see a
  seamless cut rather than YouTube's "reconnecting" gap.
- **PTZ presets** — integer slots 0–99 stored on the camera by its own
  preset-store mechanism. We label them and recall them; we don't currently
  expose a "store this position" button in the UI (the API exists, the
  button doesn't).

## Useful files

| File | Purpose |
|---|---|
| `docs/superpowers/specs/2026-06-07-ndi-rmtp-relay-design.md` | Original design spec |
| `docs/superpowers/plans/2026-06-07-ndi-rmtp-relay-implementation.md` | Implementation plan (mostly executed) |
| `docs/superpowers/2026-06-07-spike-results.md` | Phase 0 spike notes about the real `ndi-streamer` CLI |
| `scripts/ndi-streamer-no-audio.patch` | Our local patch to ndi-streamer (filename is historical — now contains all C modifications: no-audio flag, signal-driven sacrament toggle, CBR/zerolatency encoder tuning) |
| `sacrament/` | Ward-specific overlay SVGs |
| `TODO.md` | Outstanding work |

## License

MIT for our Python and shell code. `ndi-streamer` is MIT upstream; our
patch keeps that license. The NDI SDK and runtime are NewTek/NDI's
proprietary licenses — see <https://ndi.video>. libx264 is GPL v2+ and the
produced binary inherits GPL (see the license discussion in the design
doc).
