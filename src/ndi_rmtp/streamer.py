"""Subprocess control for ndi-streamer.

Owns the streaming worker process: spawns it, watches its stderr for state
signals, supervises lifecycle, and exposes status.

The Phase 0 spike (docs/superpowers/2026-06-07-spike-results.md) confirmed:
- Binary is named `ndi-streamer` (hyphen).
- CLI flags are: -n <source>, -f rtmp, -v libx264, -a aac,
  --video_bitrate <bps>, --audio_bitrate <bps>, -o <url>
- ndi-streamer does NOT emit `frame=` progress lines (it uses libavcodec
  directly, not the ffmpeg CLI). The STREAMING signal is the av_dump_format
  output: a line starting with "Output #0,".
- A fatal RTMP rejection (e.g. YouTube bad key) appears as
  "Server returned 403" or "Invalid stream key" in stderr.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .state import State, StateMachine

logger = logging.getLogger(__name__)

# Signals that we've successfully started encoding to the RTMP sink.
STREAMING_PATTERNS = (b"Output #0,", b"Stream mapping:")
# Signals that YouTube/RTMP server refused — do not auto-retry.
FATAL_PATTERNS = (b"403 Forbidden", b"Invalid stream key", b"Server returned 403")

# ndi-streamer prints this the instant it opens its output — for RTMP that means
# the handshake to YouTube completed — and BEFORE it blocks waiting for the
# camera's first frame. Its presence is the clean signal that separates the two
# ways a start stalls (see _diagnose_start_timeout): if we never see it, the
# RTMP connect itself hung (stream key already live / wrong — a stall that emits
# no error of its own); if we see it but never reach STREAMING, the camera side
# isn't delivering video. Before we tracked this, a stuck RTMP connect was
# misread as a camera fault because an unrelated "No start code" decoder line
# happened to be present.
OUTPUT_OPENED_MARKER = b"output connection opened"

# Concrete libav connection errors — more specific than the silent RTMP hang.
_RTMP_ERROR_MARKERS = (
    "cannot open connection", "connection refused", "connection reset",
    "broken pipe", "error opening output", "i/o error", "end of file",
)

# How long a start may sit in STARTING before we give up and diagnose. Generous
# on purpose: a cold RTMP handshake to YouTube plus the camera's first-keyframe
# wait can legitimately take double-digit seconds, and the diagnosis at the end
# is now accurate, so waiting longer beats a premature (and previously wrong)
# error.
STARTING_TIMEOUT_S = 30.0
STOP_GRACE_S = 5.0
LOG_BUFFER_LINES = 200

# Quality knob: a single operator-facing degradation ladder. Each step
# couples a resolution with a video+audio bitrate sized to land inside
# YouTube's recommended range for that resolution. The intent is that an
# operator hitting buffering/dropouts can step DOWN the ladder
# (high -> medium -> low) to shed bandwidth and let the stream stay alive.
#
#   high   = best quality YouTube accepts (top of 1080p30 range)
#   medium = same resolution, half the bandwidth (bottom of 1080p30 range)
#   low    = drop to 360p30 for severely-degraded networks
#
# Sending OVER YouTube's recommended range triggers their "bitrate too
# high" warning and may cause downscaled playback; sending under-range
# just looks soft. Each entry below is dead-center inside the
# per-resolution range.
QUALITY_PRESETS: dict[str, dict[str, object]] = {
    "high":   {"resolution": "1080p30", "v_bitrate_bps": 6_000_000, "a_bitrate_bps": 192_000},
    "medium": {"resolution": "1080p30", "v_bitrate_bps": 3_000_000, "a_bitrate_bps": 128_000},
    "low":    {"resolution": "360p30",  "v_bitrate_bps":   600_000, "a_bitrate_bps": 128_000},
}

DEFAULT_QUALITY = "high"

# Per (resolution): (encoder_width, encoder_height, ndi_recv_bandwidth).
# - width/height = None → encoder tracks the source dims (no resize).
# - bandwidth = "lowest" → camera's proxy stream (~360p); "highest" = native.
# 720p is the only mode that downscales (1080p in, 720p out); kept here
# in case a future quality preset re-introduces a 720p step.
RESOLUTION_DIMS: dict[str, tuple[Optional[int], Optional[int], str]] = {
    "360p30":  (None, None, "lowest"),
    "720p30":  (1280, 720,  "highest"),
    "1080p30": (None, None, "highest"),
}


def build_args(
    binary: Path,
    source: str,
    rtmp_url: str,
    stream_key: str,
    quality: str,
    muted: bool = False,
    sacrament_image: Optional[Path] = None,
) -> list[str]:
    """Build the ndi-streamer argv. Validated against the real binary in Phase 0.

    `quality` is one of "low" | "medium" | "high" and pulls a coupled
    resolution + bitrate from QUALITY_PRESETS. Unknown quality falls back
    to DEFAULT_QUALITY with a warning logged (operator typo shouldn't crash).

    When `muted=True`, passes --no_audio. (Runtime mute is via SIGUSR2 — see
    Streamer.set_muted.)

    When `sacrament_image` is a path, passes --sacrament_image PATH so the
    binary loads the image at startup. Sacrament mode is then toggled via
    SIGUSR1 to the running subprocess; the RTMP session never restarts.
    """
    if quality not in QUALITY_PRESETS:
        logger.warning(
            "unknown quality %r; falling back to %s",
            quality, DEFAULT_QUALITY,
        )
        quality = DEFAULT_QUALITY
    preset = QUALITY_PRESETS[quality]
    resolution = preset["resolution"]
    width, height, bandwidth = RESOLUTION_DIMS[resolution]
    base = rtmp_url.rstrip("/")
    output = f"{base}/{stream_key}"
    args = [
        str(binary),
        "-n", source,
        "-f", "rtmp",
        "-v", "libx264",
        "-a", "aac",
        "--video_bitrate", str(preset["v_bitrate_bps"]),
        "--audio_bitrate", str(preset["a_bitrate_bps"]),
        "--bandwidth", bandwidth,
        "-o", output,
    ]
    if width is not None and height is not None:
        args += ["--target_width", str(width), "--target_height", str(height)]
    if muted:
        args.append("--no_audio")
    if sacrament_image is not None:
        args.extend(["--sacrament_image", str(sacrament_image)])
    return args


SACRAMENT_PNG_CACHE = Path("/tmp/ndi-rmtp-sacrament.png")


async def rasterize_svg(svg_path: Path, png_path: Path, width: int = 1920, height: int = 1080) -> None:
    """Rasterize an SVG to PNG via rsvg-convert. Also accepts an existing PNG/JPG
    by simply copying it (no rasterization needed)."""
    if svg_path.suffix.lower() in (".png", ".jpg", ".jpeg"):
        png_path.write_bytes(svg_path.read_bytes())
        return
    proc = await asyncio.create_subprocess_exec(
        "rsvg-convert",
        "-w", str(width), "-h", str(height),
        "--keep-aspect-ratio",
        str(svg_path),
        "-o", str(png_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"rsvg-convert failed (exit {proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        )


class StreamerStatus:
    def __init__(self, state: str, uptime_s: int, last_error: Optional[str], log_tail: list[str]):
        self.state = state
        self.uptime_s = uptime_s
        self.last_error = last_error
        self.log_tail = log_tail
        self.muted = False
        self.sacrament_mode = False

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "uptime_s": self.uptime_s,
            "last_error": self.last_error,
            "muted": self.muted,
            "sacrament_mode": self.sacrament_mode,
            "log_tail": self.log_tail,
        }


class Streamer:
    def __init__(self, binary_path: Path, config_getter: Callable[[], Config]):
        self._binary = binary_path
        self._get_config = config_getter
        self._sm = StateMachine()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._log_buffer: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_LINES)
        self._last_error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._stopping_requested = False
        self._stop_grace_s = STOP_GRACE_S
        self._last_start_params: Optional[tuple[str, str, str]] = None
        self._starting_watchdog_task: Optional[asyncio.Task] = None
        # Mute is in-memory only — defaults unmuted on every process start.
        self._muted = False
        # Sacrament mode: in-memory; defaults off on process start.
        self._sacrament_mode = False
        # Path to the PNG that was pre-rasterized for the current subprocess.
        # None if the active ward at start() time had no sacrament_image.
        self._last_sacrament_png: Optional[Path] = None
        # Test seams (None in production):
        self._spawn_argv: Optional[list[str]] = None
        self._spawn_env: Optional[dict[str, str]] = None
        self._next_spawn: Optional[Callable[[], tuple[list[str], dict[str, str]]]] = None

    def status(self) -> StreamerStatus:
        uptime = 0
        if self._sm.state == State.STREAMING and self._started_at is not None:
            uptime = int(time.monotonic() - self._started_at)
        s = StreamerStatus(
            state=self._sm.state.value,
            uptime_s=uptime,
            last_error=self._last_error,
            log_tail=list(self._log_buffer),
        )
        s.muted = self._muted
        s.sacrament_mode = self._sacrament_mode
        return s

    async def set_muted(self, muted: bool) -> None:
        """Toggle audio mute in the running ndi-streamer subprocess.

        Sends SIGUSR2, which the patched ndi-streamer interprets as an
        audio-mute toggle (zeros outgoing samples but keeps the audio
        stream alive). The subprocess and its RTMP session stay alive
        across the toggle, so YouTube sees no reconnect gap.

        Idempotent: if `muted` already matches our recorded state, no
        signal is sent (avoids desync on accidental double-clicks).
        """
        if muted == self._muted:
            return
        if self._proc is None or self._proc.returncode is not None:
            # Not streaming — just remember the desired state. (UI only
            # exposes mute while STREAMING, so this branch is defensive.)
            self._muted = muted
            return
        try:
            self._proc.send_signal(signal.SIGUSR2)
        except ProcessLookupError:
            # Subprocess just exited — fall back to deferred state.
            self._muted = muted
            return
        self._muted = muted

    async def set_sacrament_mode(self, enabled: bool) -> None:
        """Toggle sacrament mode in the running ndi-streamer subprocess.

        Sends SIGUSR1, which the patched ndi-streamer interprets as a toggle.
        The subprocess (and its RTMP session) stays alive across the toggle,
        so YouTube sees a seamless cut instead of a reconnect gap.

        Idempotent: if `enabled` already matches our recorded state, no signal
        is sent (so accidental double-clicks don't desync from the subprocess).
        """
        if enabled == self._sacrament_mode:
            return
        if self._proc is None or self._proc.returncode is not None:
            # Not streaming — just remember the desired state for the next start.
            self._sacrament_mode = enabled
            return
        # The PNG must already be on disk (we pre-rasterized at start()). If
        # the ward had no sacrament_image, the subprocess was started without
        # --sacrament_image and the signal will toggle a flag that does
        # nothing visible. We refuse that case for clarity.
        if enabled and self._last_sacrament_png is None:
            raise RuntimeError(
                "This ward doesn't have a sacrament screen set up. "
                "Please contact your Stake Technology Specialist."
            )
        try:
            self._proc.send_signal(signal.SIGUSR1)
        except ProcessLookupError:
            # Subprocess just exited — fall back to deferred state.
            self._sacrament_mode = enabled
            return
        self._sacrament_mode = enabled

    async def start(self, source: str, stream_key: str, quality: str) -> None:
        async with self._lock:
            if self._sm.state not in (State.IDLE, State.ERROR):
                raise RuntimeError(f"cannot start from state {self._sm.state.value}")
            self._last_error = None
            self._stopping_requested = False
            self._log_buffer.clear()
            self._sm.transition(State.STARTING, "user start")
            self._last_start_params = (source, stream_key, quality)

            if self._next_spawn is not None:
                argv, env_overrides = self._next_spawn()
            elif self._spawn_argv is not None:
                argv = self._spawn_argv
                env_overrides = self._spawn_env or {}
            else:
                cfg = self._get_config()
                # Pre-rasterize the active ward's sacrament SVG so the binary
                # can load it lazily on the first SIGUSR1. None if this ward
                # has no sacrament image.
                self._last_sacrament_png = None
                ward = cfg.stream.active()
                if ward is not None and ward.sacrament_image:
                    svg_path = Path(ward.sacrament_image).expanduser()
                    if not svg_path.is_absolute():
                        svg_path = Path.cwd() / svg_path
                    if svg_path.exists():
                        try:
                            await rasterize_svg(svg_path, SACRAMENT_PNG_CACHE)
                            self._last_sacrament_png = SACRAMENT_PNG_CACHE
                        except Exception as e:
                            logger.warning("sacrament rasterize failed: %s", e)
                argv = build_args(
                    self._binary, source, cfg.stream.rtmp_url,
                    stream_key, quality,
                    muted=False,  # signal-driven; SIGUSR2 toggles at runtime
                    sacrament_image=self._last_sacrament_png,
                )
                env_overrides = {}
            # New subprocess starts with sacrament_active = audio_muted = 0
            # in C. Reset our flags so the UI matches.
            self._sacrament_mode = False
            self._muted = False
            env = os.environ.copy()
            env.update(env_overrides)

            logger.info("spawning ndi-streamer: %s", argv)
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._reader_task = asyncio.create_task(self._read_stderr())
            self._supervisor_task = asyncio.create_task(self._supervise())
            self._starting_watchdog_task = asyncio.create_task(self._starting_watchdog())

    async def stop(self) -> None:
        async with self._lock:
            if self._sm.state not in (State.STARTING, State.STREAMING, State.ERROR):
                return
            self._stopping_requested = True
            # Forget the sacrament-mode flag on stop so the next start
            # doesn't surprise the operator with the SACRAMENT MODE overlay
            # left over from the previous run. The C side will also
            # initialise audio_muted=0/sacrament_active=0 on its next
            # spawn, so resetting Python state here keeps the UI honest
            # between stop and the next start.
            self._sacrament_mode = False
            if self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.STOPPING, "user stop")
            proc = self._proc
            if proc is None:
                if self._sm.state == State.STOPPING:
                    self._sm.transition(State.IDLE, "no proc to stop")
                elif self._sm.state == State.ERROR:
                    self._sm.transition(State.IDLE, "dismiss error")
                    self._last_error = None
                return

        # Lock released — now we can wait on the subprocess without blocking callers.
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), self._stop_grace_s)
        except asyncio.TimeoutError:
            self._log_buffer.append("[supervisor] subprocess did not exit on SIGTERM, sending SIGKILL")
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

        if self._supervisor_task is not None:
            try:
                await self._supervisor_task
            except Exception:
                pass

        if self._starting_watchdog_task is not None:
            self._starting_watchdog_task.cancel()
            try:
                await self._starting_watchdog_task
            except (asyncio.CancelledError, Exception):
                pass

        async with self._lock:
            if self._sm.state == State.STOPPING:
                self._sm.transition(State.IDLE, "stop complete")
            self._proc = None
            self._started_at = None

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for raw in self._proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self._log_buffer.append(line)
                # Forward the opt-in A/V-sync instrumentation to the journal so
                # `journalctl -u ndi-rmtp | grep avsync` captures it. ndi-streamer
                # only emits these lines when NDI_RMTP_AVSYNC_DEBUG is set in its
                # environment, so this is a no-op in normal operation.
                if b"[avsync]" in raw:
                    logger.info("%s", line)
                if self._sm.state == State.STARTING and any(p in raw for p in STREAMING_PATTERNS):
                    async with self._lock:
                        if self._sm.state == State.STARTING:
                            self._started_at = time.monotonic()
                            self._sm.transition(State.STREAMING, "first frame")
                            if self._starting_watchdog_task is not None:
                                self._starting_watchdog_task.cancel()
                if any(p in raw for p in FATAL_PATTERNS):
                    self._last_error = "YouTube rejected the stream key"
        except Exception as e:  # pragma: no cover
            logger.exception("stderr reader crashed: %s", e)

    def _diagnose_start_timeout(self) -> str:
        """Explain a STARTING timeout from what the subprocess printed.

        Reaching STREAMING takes two steps, in order: ndi-streamer first opens
        its output (for RTMP, the handshake to YouTube — it prints the
        OUTPUT_OPENED_MARKER), then it waits for the camera's first frame and
        writes the stream header ("Output #0,"). A stall in either step hits the
        same timeout, so we classify by how far it got:

        - Never opened the output → it hung on the RTMP connect. YouTube took
          the socket but never started publishing — almost always because this
          ward's stream key is already live (a broadcast left running in YouTube
          Studio) or is wrong. This stall prints no error of its own.
        - Opened the output but never reached STREAMING → the camera side isn't
          delivering decodable video.
        """
        blob = "\n".join(list(self._log_buffer)).lower()
        secs = int(STARTING_TIMEOUT_S)

        # YouTube explicitly rejected the key. Keep "rejected" in the text so
        # _supervise treats it as fatal and does not auto-retry.
        if any(m in blob for m in ("403 forbidden", "invalid stream key",
                                   "server returned 403")):
            return (
                "YouTube rejected this ward's stream key. Check the key in "
                "YouTube Studio, then try again."
            )

        if OUTPUT_OPENED_MARKER.decode() not in blob:
            # The RTMP output never opened. Prefer a concrete libav connection
            # error if we have one; otherwise it's the silent "key already
            # live / in use" hang that used to be misblamed on the camera.
            if any(m in blob for m in _RTMP_ERROR_MARKERS):
                return (
                    "Couldn't reach YouTube's streaming server. Check the "
                    "internet connection and this ward's stream key, then try "
                    "again. If it keeps happening, contact your Stake "
                    "Technology Specialist."
                )
            return (
                f"Couldn't go live within {secs} seconds: YouTube accepted the "
                "connection but never started the broadcast for this ward's "
                "stream key. That usually means the key is already live (a "
                "broadcast left running in YouTube Studio) or is in use "
                "elsewhere — end that broadcast in YouTube Studio, then try "
                "again. If the key is brand new, double-check it was entered "
                "correctly."
            )

        # The output opened, so RTMP/the key are fine — the camera side is the
        # problem: no decodable video arrived before the timeout.
        return (
            "Connected to YouTube, but no video arrived from the camera within "
            f"{secs} seconds. Make sure the camera is powered on and connected "
            "and that its NDI output is set to 1080p. Contact your Stake "
            "Technology Specialist if it persists."
        )

    async def _starting_watchdog(self) -> None:
        """If we don't reach STREAMING within STARTING_TIMEOUT_S, force ERROR."""
        await asyncio.sleep(STARTING_TIMEOUT_S)
        async with self._lock:
            if self._sm.state == State.STARTING:
                self._last_error = self._diagnose_start_timeout()
                self._sm.transition(State.ERROR, "starting timeout")
                proc = self._proc
            else:
                proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _supervise(self) -> None:
        assert self._proc is not None
        rc = await self._proc.wait()
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, 1.0)
            except (asyncio.TimeoutError, Exception):
                pass

        should_retry = False
        async with self._lock:
            if self._stopping_requested:
                return
            if self._sm.state in (State.STARTING, State.STREAMING):
                self._sm.transition(State.ERROR, f"crash rc={rc}")
                if not self._last_error:
                    tail = " | ".join(list(self._log_buffer)[-3:])
                    self._last_error = f"Subprocess exited rc={rc}: {tail}"
            self._proc = None
            self._started_at = None

            cfg = self._get_config()
            is_fatal = self._last_error and "rejected" in self._last_error.lower()
            should_retry = (
                cfg.advanced.auto_restart
                and not is_fatal
                and self._last_start_params is not None
            )

        if should_retry:
            asyncio.create_task(self._retry_loop())

    async def _retry_loop(self) -> None:
        cfg = self._get_config()
        for delay in cfg.advanced.restart_backoff_s:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._stopping_requested or self._sm.state != State.ERROR:
                    return
                if self._last_start_params is None:
                    return
                source, key, quality = self._last_start_params
                # Transition out of ERROR so start() will accept it
                self._sm.transition(State.IDLE, "auto-restart attempt")
            try:
                await self.start(source, key, quality)
            except RuntimeError:
                continue
            # Watch what happens: success (STREAMING) or another crash (ERROR)
            for _ in range(int(STARTING_TIMEOUT_S * 10) + 50):
                state = self._sm.state
                if state == State.STREAMING:
                    return
                if state == State.ERROR:
                    break
                await asyncio.sleep(0.1)
        # Exhausted backoff; remain in ERROR
