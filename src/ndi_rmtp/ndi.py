"""NDI source discovery via ndi-streamer.

The real ndi-streamer binary lists sources to stdout when run with no args,
then goes interactive (waits for a numeric source selection on stdin). We
spawn it, capture stdout with a short timeout, and kill the process.

Output format (confirmed in the Phase 0 spike,
docs/superpowers/2026-06-07-spike-results.md):

    Looking for sources.

    Available NDI sources:
        1. MACHINE-NAME (Source Name) (192.168.1.100:5961)
        2. OTHER (Another) (10.0.0.5:5961)

The numbered lines start with whitespace + integer + '.' + space.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Discovery: spawn ndi-streamer with no args. It lists sources and then
# blocks on a stdin prompt — we kill it after a short capture window.
_DISCOVERY_ARGS: tuple[str, ...] = ()

# Capture window: how long to read stdout before killing the subprocess.
#
# The first advertisement arrives within ~1-2s, but ndi-streamer deliberately
# keeps polling after that until the source count stops changing (its
# NDI_FIND_SETTLE_* window, worst case ~3.2s) before it prints the list. Cutting
# the subprocess off at 2s truncated the listing to whichever sender answered
# first, which is how a slow PTZ camera lost its dropdown entry to a laptop.
# Must stay comfortably above ndi-streamer's settle window.
_DISCOVERY_CAPTURE_S = 6.0

# Hard timeout: kill the subprocess if it doesn't exit after we've captured.
_DISCOVERY_KILL_GRACE_S = 1.0

# ndi-streamer prints the whole source list in one burst and then blocks on its
# interactive prompt, so stdout never reaches EOF and waiting out the full
# capture window would stall the dropdown for _DISCOVERY_CAPTURE_S every time.
# Once the first source line has arrived, a gap this long means the burst is
# over and we can stop reading.
_DISCOVERY_QUIET_S = 0.4

# Match a numbered source line.
# Example: "    1. MITCHELL-VIDERA (Scan Converter) (127.0.0.1:5962)"
# The name may contain parens (e.g. "MACHINE (Scan Converter)"), so we
# require the trailing IP:port group to anchor the address.
_SOURCE_RE = re.compile(
    r"^\s*\d+\.\s+(?P<name>.+?)\s+\((?P<addr>\d{1,3}(?:\.\d{1,3}){3}:\d+)\)\s*$"
)


@dataclass
class Source:
    name: str
    address: Optional[str] = None


class NDIDiscovery:
    def __init__(self, binary_path: Path, ttl_s: float = 5.0):
        self._binary = binary_path
        self._ttl_s = ttl_s
        self._cache: list[Source] = []
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()
        # Test seams (None in production):
        self._spawn_argv: Optional[list[str]] = None
        self._spawn_env: Optional[dict[str, str]] = None

    async def list(self) -> list[Source]:
        async with self._lock:
            now = time.monotonic()
            if self._cache and now - self._cached_at < self._ttl_s:
                return list(self._cache)
            sources = await self._run_discovery()
            self._cache = sources
            self._cached_at = now
            return list(sources)

    async def _run_discovery(self) -> list[Source]:
        if self._spawn_argv is not None:
            argv = self._spawn_argv
        else:
            argv = [str(self._binary), *_DISCOVERY_ARGS]
        env = os.environ.copy()
        if self._spawn_env:
            env.update(self._spawn_env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError:
            logger.error("ndi-streamer binary not found at %s", self._binary)
            return []

        lines_text: list[str] = []
        deadline = time.monotonic() + _DISCOVERY_CAPTURE_S
        seen_source = False
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Before the first source line, allow the full remaining budget
                # (ndi-streamer is still settling its list). After it, a short
                # gap means the burst is finished -- see _DISCOVERY_QUIET_S.
                wait_s = min(remaining, _DISCOVERY_QUIET_S) if seen_source else remaining
                try:
                    assert proc.stdout is not None
                    line = await asyncio.wait_for(proc.stdout.readline(), wait_s)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break  # EOF
                text = line.decode("utf-8", errors="replace")
                lines_text.append(text)
                if _SOURCE_RE.match(text.rstrip("\n")):
                    seen_source = True
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), _DISCOVERY_KILL_GRACE_S)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()

        sources: list[Source] = []
        for line in lines_text:
            m = _SOURCE_RE.match(line.rstrip("\n"))
            if m:
                sources.append(Source(name=m.group("name"), address=m.group("addr")))
        return sources
