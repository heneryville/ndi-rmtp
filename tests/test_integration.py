"""End-to-end: real ndi-streamer pushes to a local nginx-rtmp container.

Skipped unless `-m integration` and the env var NDI_TEST_SOURCE is set.
Requires Docker.

Notes from Phase 0 spike (docs/superpowers/2026-06-07-spike-results.md):
- The binary is `ndi-streamer` (hyphen).
- The binary defaults to `NDIlib_recv_bandwidth_lowest` (proxy quality). For a
  small-window NDI source (like NDI Scan Converter), it streams at the source's
  native low resolution. For full-HD sources it would deliver 1/16-res proxy
  frames — acceptable for the integration test (which only asserts that h264
  and aac arrive at the RTMP sink, not specific dimensions).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ndi_rmtp.bootstrap import resolve_binary
from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.streamer import Streamer


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ndi_source() -> str:
    src = os.environ.get("NDI_TEST_SOURCE")
    if not src:
        pytest.skip("NDI_TEST_SOURCE not set")
    return src


@pytest.fixture(scope="module")
def docker():
    if not shutil.which("docker"):
        pytest.skip("Docker not available")
    return shutil.which("docker")


@pytest.fixture(scope="module")
def rtmp_server(docker):
    name = "ndi-rmtp-it-rtmp"
    subprocess.run([docker, "rm", "-f", name], check=False, capture_output=True)
    subprocess.run(
        [docker, "run", "-d", "--rm", "--name", name, "-p", "1935:1935", "tiangolo/nginx-rtmp"],
        check=True,
    )
    # Give nginx a moment to be ready to accept publish.
    time.sleep(2)
    yield "rtmp://localhost/live"
    subprocess.run([docker, "rm", "-f", name], check=False, capture_output=True)


@pytest.fixture(scope="module")
def binary_path() -> Path:
    p = resolve_binary()
    if not p.exists():
        pytest.skip(f"ndi-streamer not built at {p} — run scripts/dev-setup-mac.sh")
    return p


async def test_real_ndi_to_local_rtmp(binary_path, rtmp_server, ndi_source):
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url=rtmp_server),
        advanced=AdvancedConfig(auto_restart=False),
    )
    streamer = Streamer(binary_path=binary_path, config_getter=lambda: cfg)

    await streamer.start(source=ndi_source, stream_key="it-test", quality="low")
    # Wait for STREAMING (detected via "Output #0," pattern from libav).
    for _ in range(100):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.1)
    assert streamer.status().state == "STREAMING", \
        f"never reached STREAMING; log tail: {streamer.status().log_tail[-5:]}"

    # Let frames flow for 5 s.
    await asyncio.sleep(5.0)

    # Verify the RTMP sink has the stream.
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "rtmp://localhost/live/it-test"],
        capture_output=True, text=True, timeout=10,
    )
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout).get("streams", [])
    codecs = [s.get("codec_name") for s in streams]
    assert "h264" in codecs, f"no h264 in stream: {codecs}"
    assert "aac" in codecs, f"no aac in stream: {codecs}"

    await streamer.stop()
    assert streamer.status().state == "IDLE"
