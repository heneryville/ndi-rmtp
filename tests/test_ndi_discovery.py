import asyncio
import json
import sys
from pathlib import Path

import pytest

from ndi_rmtp.ndi import NDIDiscovery, Source


FIXTURES = Path(__file__).parent / "fixtures"


def _make_discovery(lines: list[str], ttl_s: float = 0.2) -> NDIDiscovery:
    d = NDIDiscovery(binary_path=Path("unused"), ttl_s=ttl_s)
    d._spawn_argv = [sys.executable, str(FIXTURES / "fake_ndi_lister.py")]
    d._spawn_env = {"FAKE_NDI_LISTER": json.dumps(lines)}
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
