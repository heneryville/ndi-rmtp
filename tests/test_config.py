import stat
from pathlib import Path

import pytest

from ndi_rmtp.config import Config, DEFAULT_RTMP_URL


def test_bootstrap_creates_file_with_random_password(tmp_path):
    path = tmp_path / "config.toml"
    config, password = Config.bootstrap(path)
    assert path.exists()
    assert config.auth.username == "admin"
    assert config.auth.password == password
    assert len(password) >= 12
    assert config.stream.rtmp_url == DEFAULT_RTMP_URL


def test_bootstrap_two_calls_produce_different_passwords(tmp_path):
    _, p1 = Config.bootstrap(tmp_path / "a.toml")
    _, p2 = Config.bootstrap(tmp_path / "b.toml")
    assert p1 != p2


def test_load_round_trips(tmp_path):
    from ndi_rmtp.config import Ward
    path = tmp_path / "config.toml"
    original, _ = Config.bootstrap(path)
    original.stream.wards = [
        Ward(name="Primary", key="abc-123"),
        Ward(name="Backup", key="def-456"),
    ]
    original.stream.active_ward = "Primary"
    original.stream.last_source = "TEST (Source)"
    original.stream.quality = "high"
    original.save(path)

    loaded = Config.load(path)
    assert loaded.auth.username == original.auth.username
    assert loaded.auth.password == original.auth.password
    assert [k.name for k in loaded.stream.wards] == ["Primary", "Backup"]
    assert loaded.stream.find_ward("Backup").key == "def-456"
    assert loaded.stream.active_ward == "Primary"
    assert loaded.stream.active().key == "abc-123"
    assert loaded.stream.last_source == "TEST (Source)"
    assert loaded.stream.quality == "high"


def test_load_reads_config_version(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'config_version = 7\n'
        "[auth]\n"
        'username = "admin"\n'
        'password = "pw"\n'
        "enabled = false\n"
    )
    cfg = Config.load(path)
    assert cfg.config_version == 7


def test_config_version_defaults_to_zero_when_absent(tmp_path):
    # A config file with no config_version key (e.g. a hand-edited or older
    # config) must still load, defaulting the version to 0.
    path = tmp_path / "config.toml"
    cfg, _ = Config.bootstrap(path)
    assert cfg.config_version == 0


def test_save_round_trips_config_version(tmp_path):
    # The app rewrites config on start (last_source/quality); the version must
    # survive that round-trip so the deployed number isn't silently reset.
    path = tmp_path / "config.toml"
    cfg, _ = Config.bootstrap(path)
    cfg.config_version = 12
    cfg.save(path)
    assert Config.load(path).config_version == 12


def test_active_returns_none_when_unset(tmp_path):
    path = tmp_path / "config.toml"
    cfg, _ = Config.bootstrap(path)
    # Default bootstrap: no keys configured.
    assert cfg.stream.active() is None


def test_save_uses_mode_0600(tmp_path):
    path = tmp_path / "config.toml"
    config, _ = Config.bootstrap(path)
    config.save(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_save_is_atomic_no_partial_file_on_crash(tmp_path, monkeypatch):
    """If serialization fails mid-write, the original file must remain intact."""
    path = tmp_path / "config.toml"
    config, _ = Config.bootstrap(path)
    config.stream.last_source = "original"
    config.save(path)

    # Now sabotage tomli_w.dump to raise mid-save
    import tomli_w
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(tomli_w, "dump", boom)

    config.stream.last_source = "new"
    with pytest.raises(RuntimeError):
        config.save(path)

    # Original file unchanged
    loaded = Config.load(path)
    assert loaded.stream.last_source == "original"
    # No leftover temp files in the dir
    assert list(path.parent.glob(".tmp-*")) == []


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "does-not-exist.toml")


def test_bootstrap_creates_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "config.toml"
    Config.bootstrap(path)
    assert path.exists()
