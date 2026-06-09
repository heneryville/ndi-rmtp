# TODO

Outstanding work, roughly grouped by urgency for actually using this in
production. Items higher in each section are higher priority.

## Pi deployment (blocking real production use)

- [ ] **Run `scripts/install.sh` end-to-end on a real Raspberry Pi 5.**
      The script is written but unverified. Expect to find rough edges
      around the NDI SDK install (manual EULA step), the patch application
      step in the build, and the systemd unit's `ReadWritePaths` config
      now that the config file location moved.
- [ ] **30-minute burn-in on the Pi** (Phase 4 in the original plan).
      Stream a real source for half an hour, watch CPU temperature, dropped
      frames, memory growth, and YouTube stream health.
- [ ] **Verify cyndilib aarch64 wheel installs cleanly on Pi OS Bookworm/
      Trixie.** It has aarch64 wheels on PyPI but we haven't actually
      pulled them on the Pi yet.
- [ ] **Drop the `jellyfin-ffmpeg7` workaround once Pi OS Trixie is the
      install target.** Bookworm ships FFmpeg 5 (`libavcodec.so.59`) but
      libndi 6.3.2 dlopens FFmpeg 7's `libavcodec.so.61` for HX/HX3
      decode. The installer currently grabs jellyfin-ffmpeg7's prefixed
      build to fill that gap; Trixie's stock `libavcodec61` would replace
      it. See CLAUDE.md "libndi needs FFmpeg 7 at runtime for HX / HX3
      decode" for the full story.
- [ ] **systemd unit `ReadWritePaths` audit.** It currently grants write
      to `/home/ndi-rmtp/.config` only, which is correct for the env-var
      config path. But the running app also writes the sacrament PNG to
      `/tmp/ndi-rmtp-sacrament.png` — that's covered by `PrivateTmp=true`
      already, but worth double-checking that the patched ndi-streamer
      can also read it through the systemd sandboxing.

## Reliability & user experience

- [ ] **Network bufferbloat is the bottleneck under real-world conditions.**
      We saw 600-1000 ms latency under load on two different networks; this
      is not something we can fix in the app, but a deployment note in the
      README ("recommend enabling SQM on the router") would be useful.
- [ ] **Resolution drift in sacrament mode.** Sacrament canvas is hardcoded
      at 640×360. If the NDI source comes in at a different resolution
      (e.g., the UHD camera in a 1080p mode), the sacrament image is loaded
      at the encoder's actual dimensions but it'd be nice to scale the
      camera output to a consistent 640×360 too via an ndi-streamer patch.
- [ ] **Audio source troubleshooting in the UI.** When we tested, the
      camera was sending all-zero audio (no mic plugged in). The app
      faithfully forwarded the silence. A small "audio peak meter" widget
      on the dashboard would have made this diagnosable in seconds.
      `scripts/check-ndi-audio.py` exists but isn't surfaced to the
      operator.
- [ ] **STARTING timeout still fires occasionally during sacrament image
      pre-rasterization.** Investigation: is the rasterize blocking long
      enough to push past the 10-second `STARTING_TIMEOUT_S`? If so,
      either rasterize earlier (on app boot / ward change) or bump the
      timeout.

## UI polish

- [ ] **"Store current PTZ position to preset N" button.** The API endpoint
      (`POST /api/ptz/preset/{slot}/store`) exists but no UI button wires
      it up. Operator currently has to use the camera's native preset-
      store mechanism, which defeats the point of having the slot numbers
      in our config.
- [ ] **Audio peak / VU meter in the dashboard.** See above.
- [ ] **Preview frame rate is hardcoded at 5 fps (200 ms refresh).** Make
      this a config knob.
- [ ] **Stop button in ERROR state.** It currently dismisses the error
      back to IDLE, but visually it's still labeled "■ Stop" which is
      slightly misleading.

## Architecture & code quality

- [ ] **Vendor `ndi-streamer` instead of cloning upstream + patching.**
      Currently dev-setup clones from
      `https://github.com/alim-zanibekov/ndi_streamer.git` and applies our
      patch (`scripts/ndi-streamer-no-audio.patch`, despite the name now
      containing all C modifications). If upstream changes break our patch
      we lose the ability to bootstrap. Either fork into a sibling repo
      under heneryville/, or vendor the source as a git submodule pinned
      to a known commit.
- [ ] **Rename `scripts/ndi-streamer-no-audio.patch`** to something honest
      like `scripts/ndi-streamer-local.patch` — it stopped being just the
      no-audio patch several commits ago.
- [ ] **Integration test (`tests/test_integration.py`) has never run.**
      It's marked `@pytest.mark.integration` and requires Docker + a real
      NDI source. Run it once on the Mac to confirm it actually validates
      h.264 + aac arriving at a local nginx-rtmp.
- [ ] **`build_args` is still called with stream_key as a separate
      positional even though we removed the old caller — see if anything
      else can be cleaned up in `streamer.py` now that sacrament doesn't
      spawn ffmpeg.**
- [ ] **PtzController's NDI receiver is connected continuously** once the
      first /api/ptz/info call comes in (which happens at page load).
      That's a parallel receiver on the camera even when nothing is
      streaming. Consider a lazy/idle-disconnect pattern.

## Documentation

- [ ] **Operator runbook.** A short doc covering: how to start a stream,
      what to do if YouTube shows the spinner, how to add a new ward, how
      to record a new PTZ preset on the camera. Right now this knowledge
      only exists in the commit history.
- [ ] **Pi-specific install notes** once we've actually run the install
      script. Capture every step that required manual intervention so the
      next install can avoid re-discovering them.

## Nice to have (not blocking)

- [ ] **Multiple cameras.** Currently we assume one PTZ camera. A multi-
      camera setup would need source selection per-broadcast and possibly
      switching between sources mid-stream.
- [ ] **Recording.** The spec said "no recording" but it would not be
      crazy to also write the encoded stream to disk for archival.
- [ ] **Per-ward custom RTMP URLs.** Currently `rtmp_url` is global; YouTube
      is the only target. Other services (Facebook Live, custom RTMP
      endpoints) would need per-ward URLs.
