import asyncio
import json
import sys
from pathlib import Path

import pytest

from ndi_rmtp.ndi import NDIDiscovery, Source


FIXTURES = Path(__file__).parent / "fixtures"


def _make_discovery(
    lines: list[str],
    ttl_s: float = 0.2,
    delays: list[float] | None = None,
    hang: bool = False,
) -> NDIDiscovery:
    d = NDIDiscovery(binary_path=Path("unused"), ttl_s=ttl_s)
    d._spawn_argv = [sys.executable, str(FIXTURES / "fake_ndi_lister.py")]
    env = {"FAKE_NDI_LISTER": json.dumps(lines)}
    if delays:
        env["FAKE_NDI_LISTER_DELAYS"] = json.dumps(delays)
    if hang:
        env["FAKE_NDI_LISTER_HANG"] = "1"
    d._spawn_env = env
    return d


async def test_discovery_parses_real_ndi_streamer_output():
    """Matches the actual stdout format from the spike."""
    d = _make_discovery([
        "Looking for sources.",
        "",
        "Available NDI sources:",
        "    1. MITCHELL-VIDERA (Scan Converter) (127.0.0.1:5962)",
        "    2. OTHER-MACHINE (Camera 1) (10.0.0.5:5961)",
    ])
    sources = await d.list()
    names = [s.name for s in sources]
    assert "MITCHELL-VIDERA (Scan Converter)" in names
    assert "OTHER-MACHINE (Camera 1)" in names
    # Address parsing
    by_name = {s.name: s.address for s in sources}
    assert by_name["MITCHELL-VIDERA (Scan Converter)"] == "127.0.0.1:5962"
    assert by_name["OTHER-MACHINE (Camera 1)"] == "10.0.0.5:5961"


async def test_discovery_ignores_non_source_lines():
    d = _make_discovery([
        "Looking for sources.",
        "Welcome banner",
        "    1. ONE (X) (1.2.3.4:5961)",
        "Some other log line",
        "select source (r - retry, e - exit):",
    ])
    sources = await d.list()
    assert len(sources) == 1
    assert sources[0].name == "ONE (X)"
    assert sources[0].address == "1.2.3.4:5961"


async def test_discovery_uses_cache_within_ttl():
    d = _make_discovery(["    1. A (X) (1.1.1.1:5961)"], ttl_s=10.0)
    first = await d.list()
    # Swap the fixture script — if cache works, this should NOT be used
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(["    1. B (Y) (2.2.2.2:5961)"])}
    second = await d.list()
    assert [s.name for s in second] == [s.name for s in first]


async def test_discovery_refreshes_after_ttl_expires():
    d = _make_discovery(["    1. A (X) (1.1.1.1:5961)"], ttl_s=0.05)
    await d.list()
    await asyncio.sleep(0.1)
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(["    1. B (Y) (2.2.2.2:5961)"])}
    refreshed = await d.list()
    assert [s.name for s in refreshed] == ["B (Y)"]


async def test_discovery_returns_empty_on_no_sources():
    d = _make_discovery([
        "Looking for sources.",
        "No NDI sources found",
    ])
    sources = await d.list()
    assert sources == []


async def test_discovery_handles_address_with_dotted_ip_and_port():
    """Confirm the regex correctly captures IP:port format."""
    d = _make_discovery(["    1. SRC (Name) (192.168.1.100:5961)"])
    sources = await d.list()
    assert len(sources) == 1
    assert sources[0].name == "SRC (Name)"
    assert sources[0].address == "192.168.1.100:5961"


async def test_discovery_keeps_slow_source_that_arrives_after_the_first():
    """Regression: a slow sender must not be dropped from the listing.

    ndi-streamer used to exit its find loop on the first non-zero source count,
    so whichever sender answered first won and the rest were silently dropped.
    In the field a PTZ camera lost that race to a laptop on 19 of 20 attempts.
    The C side now settles the list before printing, which delays the whole
    burst -- the reader must wait long enough to still catch it.
    """
    d = _make_discovery(
        [
            "Looking for sources.",
            "Available NDI sources:",
            "    1. DARYLWARNERX (Studio Monitor 1) (192.168.111.254:5961)",
            "    2. UHD CAMERA (NDI HX3, 192.168.111.248) (192.168.111.248:5961)",
        ],
        # The settle window lands before the burst, well past the old 2s cutoff.
        delays=[0, 2.5, 0, 0],
    )
    names = [s.name for s in await d.list()]
    assert "DARYLWARNERX (Studio Monitor 1)" in names
    assert "UHD CAMERA (NDI HX3, 192.168.111.248)" in names, (
        "the slower camera was dropped from the listing"
    )


async def test_discovery_returns_promptly_when_binary_hangs_at_prompt():
    """The real binary never reaches EOF -- it blocks on 'select source'.

    Without a quiet-period cutoff the reader would burn the entire capture
    window on every single refresh, stalling the dropdown.
    """
    d = _make_discovery(
        [
            "Available NDI sources:",
            "    1. ONE (X) (1.2.3.4:5961)",
        ],
        hang=True,
    )
    start = asyncio.get_running_loop().time()
    sources = await d.list()
    elapsed = asyncio.get_running_loop().time() - start

    assert [s.name for s in sources] == ["ONE (X)"]
    assert elapsed < 3.0, f"returned in {elapsed:.1f}s; should stop once the burst ends"
