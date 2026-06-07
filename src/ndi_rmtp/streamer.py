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

STARTING_TIMEOUT_S = 10.0
STOP_GRACE_S = 5.0
LOG_BUFFER_LINES = 200

# Bitrates are in bits per second (ndi-streamer expects integers, no "k" suffix).
QUALITY_PRESETS: dict[str, dict[str, int]] = {
    "low":    {"v_bitrate_bps": 2_500_000, "a_bitrate_bps": 128_000},
    "medium": {"v_bitrate_bps": 4_500_000, "a_bitrate_bps": 128_000},
    "high":   {"v_bitrate_bps": 6_000_000, "a_bitrate_bps": 192_000},
}


def build_args(
    binary: Path,
    source: str,
    rtmp_url: str,
    stream_key: str,
    quality: str,
) -> list[str]:
    """Build the ndi-streamer argv. Validated against the real binary in Phase 0."""
    preset = QUALITY_PRESETS[quality]
    base = rtmp_url.rstrip("/")
    output = f"{base}/{stream_key}"
    return [
        str(binary),
        "-n", source,
        "-f", "rtmp",
        "-v", "libx264",
        "-a", "aac",
        "--video_bitrate", str(preset["v_bitrate_bps"]),
        "--audio_bitrate", str(preset["a_bitrate_bps"]),
        "-o", output,
    ]


class StreamerStatus:
    def __init__(self, state: str, uptime_s: int, last_error: Optional[str], log_tail: list[str]):
        self.state = state
        self.uptime_s = uptime_s
        self.last_error = last_error
        self.log_tail = log_tail

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "uptime_s": self.uptime_s,
            "last_error": self.last_error,
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
        # Test seams (None in production):
        self._spawn_argv: Optional[list[str]] = None
        self._spawn_env: Optional[dict[str, str]] = None
        self._next_spawn: Optional[Callable[[], tuple[list[str], dict[str, str]]]] = None

    def status(self) -> StreamerStatus:
        uptime = 0
        if self._sm.state == State.STREAMING and self._started_at is not None:
            uptime = int(time.monotonic() - self._started_at)
        return StreamerStatus(
            state=self._sm.state.value,
            uptime_s=uptime,
            last_error=self._last_error,
            log_tail=list(self._log_buffer),
        )

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
                argv = build_args(
                    self._binary, source, self._get_config().stream.rtmp_url, stream_key, quality
                )
                env_overrides = {}
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

    async def _starting_watchdog(self) -> None:
        """If we don't reach STREAMING within STARTING_TIMEOUT_S, force ERROR."""
        await asyncio.sleep(STARTING_TIMEOUT_S)
        async with self._lock:
            if self._sm.state == State.STARTING:
                self._last_error = "Timed out waiting for first frame"
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
