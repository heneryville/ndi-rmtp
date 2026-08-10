import asyncio
import json
import sys
from pathlib import Path

import pytest

from ndi_rmtp.config import AdvancedConfig, AuthConfig, Config, StreamConfig
from ndi_rmtp.state import State
from ndi_rmtp.streamer import Streamer, build_args, QUALITY_PRESETS


FIXTURES = Path(__file__).parent / "fixtures"


def _fake_args(script: list[dict]) -> tuple[list[str], dict[str, str]]:
    """Returns (argv-prefix, env-overrides) for spawning the fake binary."""
    return (
        [sys.executable, str(FIXTURES / "fake_ndi_streamer.py")],
        {"FAKE_NDI_SCRIPT": json.dumps(script)},
    )


def _config() -> Config:
    return Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=False),
    )


# ---- build_args tests --------------------------------------------------------

def test_build_args_uses_real_ndi_streamer_flags(tmp_path):
    binary = tmp_path / "ndi-streamer"
    binary.touch()
    args = build_args(binary, "MY (SRC)", "rtmp://x/live", "key", "medium")
    # Source name + output URL + format selector
    assert "MY (SRC)" in args
    assert "rtmp://x/live/key" in args
    assert "-f" in args and "rtmp" in args
    # Codecs are the FFmpeg encoder names, not abstract codec names
    assert "libx264" in args
    assert "aac" in args
    # Bitrates are bits-per-second integers, not "4500k". Quality "medium"
    # is currently 1080p30 (see QUALITY_PRESETS in streamer.py).
    preset = QUALITY_PRESETS["medium"]
    assert "--video_bitrate" in args
    assert str(preset["v_bitrate_bps"]) in args
    assert "--audio_bitrate" in args
    assert str(preset["a_bitrate_bps"]) in args
    # Medium runs at the camera's full-bandwidth feed.
    assert "--bandwidth" in args
    bw_idx = args.index("--bandwidth")
    assert args[bw_idx + 1] == "highest"
    # No --x264-preset flag (ndi-streamer doesn't expose it)
    assert "--x264-preset" not in args


def test_build_args_handles_trailing_slash_in_rtmp_url(tmp_path):
    binary = tmp_path / "ndi-streamer"
    binary.touch()
    args = build_args(binary, "S", "rtmp://x/live/", "key", "low")
    assert "rtmp://x/live/key" in args


def test_quality_presets_are_bps_integers():
    for name in ("low", "medium", "high"):
        preset = QUALITY_PRESETS[name]
        assert isinstance(preset["v_bitrate_bps"], int)
        assert isinstance(preset["a_bitrate_bps"], int)
        # YouTube's 360p floor is ~400 kbps; bigger resolutions need
        # proportionally more. Keep a generic sanity check.
        assert preset["v_bitrate_bps"] >= 400_000
        # Each preset must reference a valid resolution.
        assert preset["resolution"] in ("360p30", "720p30", "1080p30")


# ---- start happy path --------------------------------------------------------

async def test_start_transitions_to_streaming_on_output_dump_pattern():
    """Streamer detects 'Output #0, flv, to ...' as the STREAMING signal."""
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "Output #0, flv, to 'rtmp://example/live/key':"},
        {"type": "stderr", "line": "  Stream #0:0: Video: h264, yuv420p, 640x360"},
        {"type": "sleep", "seconds": 0.5},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    for _ in range(50):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "STREAMING"
    assert streamer.status().uptime_s >= 0

    await streamer.stop()
    assert streamer.status().state == "IDLE"


async def test_immediate_nonzero_exit_goes_to_error():
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    for _ in range(50):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    assert streamer.status().last_error is not None
    assert "boom" in "\n".join(streamer.status().log_tail)


async def test_youtube_rejected_key_pattern_sets_specific_error():
    """If ndi-streamer/libav emits a YouTube-rejection pattern, surface it specifically."""
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "Output #0, flv, to 'rtmp://a.rtmp.youtube.com/...'"},
        {"type": "stderr", "line": "Server returned 403 Forbidden"},
        {"type": "exit", "code": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")

    for _ in range(50):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    assert "rejected" in (streamer.status().last_error or "").lower()


async def test_hanging_subprocess_sigkilled_after_grace():
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = [sys.executable, str(FIXTURES / "hanging_ndi_streamer.py")]
    streamer._spawn_env = {}
    streamer._stop_grace_s = 0.5  # speed up the test

    await streamer.start("SRC", "key", "medium")
    for _ in range(50):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)

    await streamer.stop()
    assert streamer.status().state == "IDLE"
    assert any("SIGKILL" in line for line in streamer.status().log_tail)


async def test_log_buffer_caps_at_max_lines():
    lines = [{"type": "stderr", "line": f"line {i}"} for i in range(500)]
    argv_prefix, env = _fake_args(lines + [{"type": "exit", "code": 0}])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    for _ in range(100):
        if streamer.status().state in ("IDLE", "ERROR"):
            break
        await asyncio.sleep(0.05)
    assert len(streamer.status().log_tail) <= 200


async def test_double_start_rejected():
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "Output #0, flv, to 'rtmp://example/live/key':"},
        {"type": "sleep", "seconds": 1},
    ])
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    with pytest.raises(RuntimeError):
        await streamer.start("SRC", "key", "medium")
    await streamer.stop()


async def test_auto_restart_retries_transient_crash():
    """Configured backoff: [0.1]. After one crash, streamer reattempts and succeeds."""
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=True, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)

    # Script: crash immediately, then on retry, run forever
    crash_argv, crash_env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    run_argv, run_env = _fake_args([
        {"type": "stderr", "line": "Output #0, flv, to 'rtmp://example/live/key':"},
        {"type": "sleep", "seconds": 2},
    ])

    call_count = {"n": 0}

    def next_spawn():
        call_count["n"] += 1
        return (crash_argv, crash_env) if call_count["n"] == 1 else (run_argv, run_env)

    streamer._next_spawn = next_spawn  # test seam

    await streamer.start("SRC", "key", "medium")
    # Wait up to 5 s for retry to land in STREAMING
    for _ in range(100):
        if streamer.status().state == "STREAMING":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "STREAMING"
    assert call_count["n"] >= 2

    await streamer.stop()


async def test_auto_restart_skipped_after_fatal_pattern():
    """If YouTube rejected the key, do not retry — even with auto_restart=True."""
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=True, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)

    argv, env = _fake_args([
        {"type": "stderr", "line": "Output #0, flv, to 'rtmp://example/live/key':"},
        {"type": "stderr", "line": "Server returned 403 Forbidden"},
        {"type": "exit", "code": 1},
    ])
    streamer._spawn_argv = argv
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    # Should land in ERROR and stay there
    for _ in range(40):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"

    # Wait long enough that a retry would have fired
    await asyncio.sleep(0.5)
    assert streamer.status().state == "ERROR"


async def test_start_timeout_before_output_opens_blames_stream_key():
    """If the RTMP output never opened (no OUTPUT_OPENED_MARKER), a timeout means
    the connect to YouTube hung — classify as the stream key, NOT the camera,
    even when an unrelated decoder 'No start code' line is present (the exact
    real-world misdiagnosis this replaced)."""
    from ndi_rmtp import streamer as streamer_mod
    # Speed up the timeout for the test
    original_timeout = streamer_mod.STARTING_TIMEOUT_S
    streamer_mod.STARTING_TIMEOUT_S = 0.5
    try:
        # Decoder chatter but NO "output connection opened" marker.
        argv_prefix, env = _fake_args([
            {"type": "stderr", "line": "[h264 @ 0x1] No start code is found."},
            {"type": "sleep", "seconds": 5},
        ])
        cfg = _config()
        streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
        streamer._spawn_argv = argv_prefix
        streamer._spawn_env = env

        await streamer.start("SRC", "key", "medium")
        for _ in range(40):
            if streamer.status().state == "ERROR":
                break
            await asyncio.sleep(0.05)
        assert streamer.status().state == "ERROR"
        err = (streamer.status().last_error or "").lower()
        # Point at the stream key / YouTube; must NOT blame the camera.
        assert "stream key" in err
        assert "camera" not in err
        await streamer.stop()
    finally:
        streamer_mod.STARTING_TIMEOUT_S = original_timeout


async def test_start_timeout_with_rtmp_error_points_at_youtube():
    """If stderr shows an RTMP connection failure, the timeout error should
    blame YouTube/the stream key — not the camera."""
    from ndi_rmtp import streamer as streamer_mod
    original_timeout = streamer_mod.STARTING_TIMEOUT_S
    streamer_mod.STARTING_TIMEOUT_S = 0.5
    try:
        argv_prefix, env = _fake_args([
            {"type": "stderr", "line": "[rtmp @ 0x1] Cannot open connection tcp://a.rtmp.youtube.com:1935"},
            {"type": "sleep", "seconds": 5},
        ])
        cfg = _config()
        streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
        streamer._spawn_argv = argv_prefix
        streamer._spawn_env = env

        await streamer.start("SRC", "key", "medium")
        for _ in range(40):
            if streamer.status().state == "ERROR":
                break
            await asyncio.sleep(0.05)
        assert streamer.status().state == "ERROR"
        err = (streamer.status().last_error or "").lower()
        assert "youtube" in err and "stream key" in err
        assert "camera isn't sending" not in err
        await streamer.stop()
    finally:
        streamer_mod.STARTING_TIMEOUT_S = original_timeout


async def test_start_timeout_after_output_opens_blames_camera():
    """Once the RTMP output has opened (marker printed) but STREAMING is never
    reached, the timeout means the camera isn't delivering decodable video — so
    blame the camera feed, not the stream key. Note the decoder 'No start code'
    line no longer drives the classification; the marker does."""
    from ndi_rmtp import streamer as streamer_mod
    original_timeout = streamer_mod.STARTING_TIMEOUT_S
    streamer_mod.STARTING_TIMEOUT_S = 0.5
    try:
        argv_prefix, env = _fake_args([
            {"type": "stderr", "line": "[startup] output connection opened"},
            {"type": "stderr", "line": "[h264 @ 0x1] No start code is found."},
            {"type": "sleep", "seconds": 5},
        ])
        cfg = _config()
        streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
        streamer._spawn_argv = argv_prefix
        streamer._spawn_env = env

        await streamer.start("SRC", "key", "medium")
        for _ in range(40):
            if streamer.status().state == "ERROR":
                break
            await asyncio.sleep(0.05)
        assert streamer.status().state == "ERROR"
        err = (streamer.status().last_error or "").lower()
        assert "camera" in err and "1080p" in err
        assert "stream key" not in err
        await streamer.stop()
    finally:
        streamer_mod.STARTING_TIMEOUT_S = original_timeout


async def test_start_timeout_with_403_classifies_as_rejected():
    """A 403 during startup (before Output #0) must classify as a rejected key
    so _supervise suppresses auto-retry."""
    from ndi_rmtp import streamer as streamer_mod
    original_timeout = streamer_mod.STARTING_TIMEOUT_S
    streamer_mod.STARTING_TIMEOUT_S = 0.5
    try:
        argv_prefix, env = _fake_args([
            {"type": "stderr", "line": "[rtmp] Server returned 403 Forbidden"},
            {"type": "sleep", "seconds": 5},
        ])
        cfg = _config()
        streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
        streamer._spawn_argv = argv_prefix
        streamer._spawn_env = env

        await streamer.start("SRC", "key", "medium")
        for _ in range(40):
            if streamer.status().state == "ERROR":
                break
            await asyncio.sleep(0.05)
        assert streamer.status().state == "ERROR"
        err = (streamer.status().last_error or "").lower()
        assert "rejected" in err
        await streamer.stop()
    finally:
        streamer_mod.STARTING_TIMEOUT_S = original_timeout


async def test_stop_from_error_dismisses_to_idle():
    """User clicking Stop while in ERROR clears the error and returns to IDLE."""
    cfg = _config()
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    argv_prefix, env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    streamer._spawn_argv = argv_prefix
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    for _ in range(40):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"

    await streamer.stop()
    assert streamer.status().state == "IDLE"
    assert streamer.status().last_error is None


async def test_auto_restart_disabled_in_config_skips_retry():
    cfg = Config(
        auth=AuthConfig(username="admin", password="x"),
        stream=StreamConfig(rtmp_url="rtmp://example/live"),
        advanced=AdvancedConfig(auto_restart=False, restart_backoff_s=[0.1]),
    )
    streamer = Streamer(binary_path=Path("unused"), config_getter=lambda: cfg)
    argv, env = _fake_args([
        {"type": "stderr", "line": "boom"},
        {"type": "exit", "code": 1},
    ])
    streamer._spawn_argv = argv
    streamer._spawn_env = env

    await streamer.start("SRC", "key", "medium")
    for _ in range(40):
        if streamer.status().state == "ERROR":
            break
        await asyncio.sleep(0.05)
    assert streamer.status().state == "ERROR"
    await asyncio.sleep(0.4)
    assert streamer.status().state == "ERROR"
