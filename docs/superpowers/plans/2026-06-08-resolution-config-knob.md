# Resolution config knob: 360p30 / 720p30 / 1080p30

**Started:** 2026-06-08
**Status:** Tabled mid-flight (user went to bed). Resume from here.

## Goal

Make the output resolution to YouTube configurable via `config.toml`,
with three valid values: `360p30`, `720p30`, `1080p30`. Default the new
field to `1080p30`. Camera is NDI HX3 capable of 1080p30; we currently
ship 360p because `ndi_streamer.c` asks the camera for its proxy stream.

## Why config-only, not UI

User picked the config-knob option over a UI dropdown. Keeps the change
small. UI can come later if needed.

## Current state of the codebase

- `main` is at `99718cc` (HX3 audio fix). Pushed. Pi is running this build.
- Pi installed binary is fine. Streams 360p30 with audio. Don't break it
  until we have the 1080p version validated.
- Mac `build/ndi_streamer/src/` was reset to upstream during this session
  (my `git checkout --` reverted both an in-progress edit and the
  audio-fix patch). **Before resuming, re-apply the committed patch:**

  ```sh
  git -C build/ndi_streamer apply scripts/ndi-streamer-no-audio.patch
  ```

## What's already done in this session (uncommitted, on Mac only)

Nothing — the in-progress C edits were reverted by the cleanup. Resume
fresh from the patched state.

## Plan

### 1. C source — `build/ndi_streamer/src/ndi_streamer.c`

Add three fields to `AppOptions`:

```c
char bandwidth[16];        // "lowest" or "highest"; default "lowest"
int  target_width;         // 0 = use source xres
int  target_height;        // 0 = use source yres
```

Register three new options in the `ProgramOption options[]` table:

```c
{ "bandwidth", "ndi recv bandwidth: 'lowest' (proxy ~360p) or 'highest' (full)", 0 },
{ "target_width",  "encoder output width (0 = native)",  0 },
{ "target_height", "encoder output height (0 = native)", 0 },
```

Parse them in `read_params` (same shape as the existing `audio_bitrate`
case — strtol for the ints).

Use `opts.bandwidth` to pick the NDIlib value when building
`recv_create_desc`:

```c
NDIlib_recv_bandwidth_e bw =
    (strcmp(opts.bandwidth, "highest") == 0)
        ? NDIlib_recv_bandwidth_highest
        : NDIlib_recv_bandwidth_lowest;
```

When the first video frame arrives, override `width`/`height` with the
target dims if set:

```c
int enc_w = opts.target_width  > 0 ? opts.target_width  : v_frame.xres;
int enc_h = opts.target_height > 0 ? opts.target_height : v_frame.yres;
ffmpeg_output_setup_video(fa_ctx, opts.video_encoder, enc_w, enc_h,
                          frame_rate, opts.video_bitrate);
```

`frame_converter.c`'s existing `sws_scale` call uses
`out_frame->width`/`height` (set from `codec_ctx->width`/`height`) as the
destination, so it already resizes for free.

Also remove the keepalive check on line ~257 that breaks the encoder
loop when input dims change — once the encoder is set up at the target
dims, the input dim changing doesn't matter (sws handles it). Or guard
that check on `opts.target_width == 0`.

### 2. Python — `src/ndi_rmtp/streamer.py`

Replace `QUALITY_PRESETS` with a 2D dict:

```python
# Bitrates per (resolution, quality), per YouTube's recommendations.
QUALITY_PRESETS: dict[str, dict[str, dict[str, int]]] = {
    "360p30": {
        "low":    {"v_bitrate_bps":   600_000, "a_bitrate_bps": 128_000},
        "medium": {"v_bitrate_bps": 1_200_000, "a_bitrate_bps": 128_000},
        "high":   {"v_bitrate_bps": 2_000_000, "a_bitrate_bps": 192_000},
    },
    "720p30": {
        "low":    {"v_bitrate_bps": 1_500_000, "a_bitrate_bps": 128_000},
        "medium": {"v_bitrate_bps": 3_000_000, "a_bitrate_bps": 128_000},
        "high":   {"v_bitrate_bps": 4_500_000, "a_bitrate_bps": 192_000},
    },
    "1080p30": {
        "low":    {"v_bitrate_bps": 3_000_000, "a_bitrate_bps": 128_000},
        "medium": {"v_bitrate_bps": 4_500_000, "a_bitrate_bps": 128_000},
        "high":   {"v_bitrate_bps": 6_000_000, "a_bitrate_bps": 192_000},
    },
}

RESOLUTION_DIMS = {
    "360p30":  (None, None,   "lowest"),   # None = use camera native (proxy)
    "720p30":  (1280, 720,    "highest"),  # ask camera for full, scale down
    "1080p30": (None, None,   "highest"),  # camera native at full bandwidth
}
```

Update `build_args` signature to take `resolution: str`, look up
`(width, height, bandwidth)` from `RESOLUTION_DIMS`, append:

```python
args += ["--bandwidth", bandwidth]
if width and height:
    args += ["--target_width", str(width), "--target_height", str(height)]
```

### 3. Config — `src/ndi_rmtp/config.py` and `config.toml`

In `StreamConfig`:

```python
resolution: str = "1080p30"
```

In `config.toml` `[stream]`:

```toml
resolution = "1080p30"
```

Valid values: `"360p30"`, `"720p30"`, `"1080p30"`. Anything else should
fall back to `1080p30` with a warning logged (don't crash — operator
might mistype).

### 4. Wiring — `src/ndi_rmtp/app.py`

`api_start` currently takes `quality` as a form arg. Add nothing to the
form; pull `resolution` from `cfg.stream.resolution`. Pass it through to
`streamer.start(...)`.

`Streamer.start(...)` signature gets a new `resolution` kwarg that
forwards to `build_args`.

### 5. Test on Pi

```sh
# Sync source, rebuild
scp build/ndi_streamer/src/*.{c,h} harris@192.168.110.42:/tmp/ndi-streamer-src/
ssh harris@192.168.110.42 'sudo cp /tmp/ndi-streamer-src/* /opt/ndi-rmtp/build/ndi_streamer/src/ && \
    sudo cmake --build /opt/ndi-rmtp/build/ndi_streamer/build --parallel && \
    sudo install -m 0755 /opt/ndi-rmtp/build/ndi_streamer/build/ndi-streamer /usr/local/bin/ndi-streamer'

# Test each resolution
# Edit /home/ndi-rmtp/.config/ndi-rmtp/config.toml, change [stream] resolution, restart
# Watch temp + dropped frames for ~2 min per resolution
ssh harris@192.168.110.42 'watch -n2 "vcgencmd measure_temp; vcgencmd get_throttled; \
    curl -s http://localhost:8080/api/status | python3 -c \"import json,sys; \
    d=json.load(sys.stdin); print(d[\\\"state\\\"], d.get(\\\"uptime_s\\\"))\""'
```

**Heat is the unknown.** Pi 4 doing zerolatency software x264 at 1080p30
plus HEVC decode is plausible but tight. User has heatsink+fan on the
way; if it hasn't arrived yet, 1080p will probably throttle within ~10
minutes. Document worst-case sustained temp at each resolution.

### 6. Patch + docs + commit

```sh
# Regenerate patch
git -C build/ndi_streamer diff > scripts/ndi-streamer-no-audio.patch

# Update CLAUDE.md: add a non-obvious section explaining
#   (a) bandwidth=lowest is the camera's proxy stream, not just a quality knob
#   (b) 720p is downscaled in our pipeline, not requested from the camera
#   (c) Pi 4 1080p30 needs active cooling

# Commit
git add -A
git commit -m "..."
git push
```

## Risks / open questions

- **Pi 4 thermal headroom for 1080p30.** Already throttled at 360p
  (`throttled=0xe0000` from earlier). May need heatsink+fan installed
  before this is usable.
- **Camera-side reconfiguration.** Some HX3 cameras only emit the proxy
  if no high-bandwidth subscriber exists. If both ndi-streamer and the
  PtzController/cyndilib are connected at different bandwidth levels,
  behavior may be camera-dependent. Test with both connected.
- **swr_scale CPU cost for 720p downscaling.** Downscaling 1080p →
  720p in software adds load on top of the encoder. Not free. If 1080p
  is tight, 720p might paradoxically not save much CPU because we still
  HEVC-decode the full stream then sws_scale down. (Encoding 720p IS
  cheaper than 1080p, so net should still be positive — but worth
  measuring.)

## Done criteria

- [ ] config.toml `resolution = "1080p30"` produces 1920×1080 output on
      YouTube
- [ ] config.toml `resolution = "720p30"` produces 1280×720 output
- [ ] config.toml `resolution = "360p30"` produces the current 640×360
      output (regression check)
- [ ] CLAUDE.md documents the three modes
- [ ] Patch regenerated, committed, pushed
- [ ] Pi sustained at chosen resolution for 5+ minutes without thermal
      throttle (with heatsink+fan)
