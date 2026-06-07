# Phase 0 Spike Results

**Date:** 2026-06-07
**Verdict:** ✅ PASSED — proceed with implementation
**Platform:** Apple Silicon Mac (Pi steps deferred to Phase 4 / Task 17)

## Summary

`ndi-streamer` (alim-zanibekov, MIT) reliably pulls an NDI stream and pushes it to RTMP using vanilla FFmpeg libraries. With our `MITCHELL-VIDERA (Scan Converter)` test source pointed at `127.0.0.1:5962`:

- **Local nginx-rtmp**: 852,529 bytes received in 9 seconds (clean upstream binary).
- **YouTube Live**: stream went live and was visible in YouTube Studio (a separate run pushed ~200 video frames + ~700 audio frames in 45 s).

Architecture decision (FastAPI control plane + `ndi-streamer` subprocess) stands.

## Key corrections to implementation plan

The plan made several CLI assumptions that turned out to be wrong. Tasks 6, 8, 13, 14 must be updated before/while executing them.

### Binary name

- **Plan said:** `ndi_streamer` (underscore)
- **Reality:** `ndi-streamer` (hyphen)
- **Where to fix:** Task 6 `build_args`, Task 8 discovery argv, Task 13/14 install scripts, Task 11 binary-resolver fallback paths.

### CLI flags

Plan assumed flags that don't exist (`--x264-preset`, kbit suffix bitrate). Real CLI is:

| Plan assumed | Reality |
|---|---|
| `-v h264` | `-v libx264` (FFmpeg encoder name, not codec name) |
| `-a aac` | `-a aac` (kept) |
| `-b 4500k` | `--video_bitrate 4500000` (bits per second, no suffix) |
| `-ab 128k` | `--audio_bitrate 128000` (bits per second) |
| `--x264-preset veryfast` | **Not exposed.** `ndi-streamer` hardcodes `preset=veryfast` for libx264 internally. |
| `-f rtmp` | Same — required to select RTMP output mode |

Verified working command:
```
ndi-streamer -n 127.0.0.1:5962 -f rtmp -v libx264 -a aac \
  --video_bitrate 2500000 --audio_bitrate 128000 \
  -o rtmp://a.rtmp.youtube.com/live2/<key>
```

**Implication for Task 6 quality presets:** we can only vary bitrate, not encoder preset. The spec's preset names (low/medium/high) stand, but they map only to bitrate values. The `preset` field in `QUALITY_PRESETS` is unused.

### Source discovery

Plan assumed `ndi-streamer --list` (list-and-exit). **There is no such flag.** Running with no args goes interactive: it prints a numbered list, then prompts on stdin for a selection, looping `"couldn't convert input \"\" to number"` forever if stdin is closed.

**Output format (confirmed):**
```
Looking for sources.

Available NDI sources:
    1. MITCHELL-VIDERA (Scan Converter) (127.0.0.1:5962)
```

Discovery strategy for Task 8: spawn the binary with stdin closed (or piped from `/dev/null`), capture stdout for ~2 seconds, kill the process, parse the numbered lines.

**Regex:** `^\s*\d+\.\s+(?P<name>.+?)\s+\((?P<addr>[0-9.]+:\d+)\)\s*$`

Update `_DISCOVERY_ARGS = ()` (no flags) and `_SOURCE_RE` accordingly.

### Stdout vs stderr

`ndi-streamer` writes the `Looking for sources` / `Available NDI sources:` / numbered list to **stdout**. The libx264 init lines and `Output #0, flv, ...` lines (libav-driven) go to **stderr**. We need to read both for full behavior coverage:
- `app.py` streamer log buffer should tail stderr (encoder + libav).
- `ndi.py` discovery should read stdout.

### Bandwidth mode

`ndi-streamer` hardcodes `NDIlib_recv_bandwidth_lowest` (proxy quality). For the user's 1920×1080 source this would mean 1/16-res proxy frames. With the screen-capture source (Scan Converter producing 640×134), it didn't matter.

**Decision:** acceptable for v1 as-is. If users complain about quality on full-resolution NDI sources, we patch `bandwidth_lowest` → `bandwidth_highest` and rebuild — a one-line C change to `src/ndi_streamer.c:64`. Defer until reported.

## Mac install path observations

- Homebrew cask `libndi` only installs the runtime `libndi.dylib` (universal arm64+x86_64) + license file to `/usr/local/lib/`. It does **not** include SDK headers.
- The brew cask downloads `libNDI_for_Mac.pkg` to `/opt/homebrew/Caskroom/libndi/latest/`. Running the pkg only installs the runtime.
- The full SDK (headers + examples + Linux+iOS+tvOS+visionOS libs) requires a separate download from <https://ndi.video/sdk/> with EULA acceptance. It installs to `/Library/NDI SDK for Apple/`.
- `ndi-streamer`'s `cmake/FindNDI.cmake` auto-discovers the SDK at the standard `/Library/NDI SDK for Apple/` path. `cmake ..` works without any env vars on Mac once the SDK is installed.
- FFmpeg 7.1.1 from Homebrew works without modification. Build takes ~5 s on M-series Mac.

Update Task 13 `dev-setup-mac.sh` to:
1. `brew install ffmpeg cmake pkg-config uv`
2. `brew install --cask libndi` (gets the runtime)
3. Prompt operator to install the SDK from ndi.video manually
4. `cmake -B build -S .` and `cmake --build build`

## Test source caveat

First spike attempt used `MITCHELL-VIDERA (Test Patterns)` — an NDI source that delivered **audio frames only, no video** (after a single startup frame for resolution discovery). This made `ndi-streamer` appear broken (encoder set up, RTMP connected, but only ~707 bytes of headers reached the sink). The fix was switching to a source that actually outputs continuous video (Scan Converter).

**Operational implication:** the UI should surface the streamer's frame-counter heartbeat (or similar liveness signal) so a user can tell whether the encoder is actually receiving frames vs. silently stalled on an audio-only source. The plan's existing log-tail panel already does this — the user will see `frame=` lines from libx264 when frames are flowing.

## What was deferred to Phase 4

- Build/test on Raspberry Pi 5 (Task 17). Mac validation is the gate we needed for the implementation plan to proceed; Pi validation gates the deliverable. Both must pass before declaring success.
- 30-minute burn-in (still in Task 17).
- The Pi-specific install path differs (apt deps, different NDI SDK package, aarch64 path). Already covered in Task 14.
