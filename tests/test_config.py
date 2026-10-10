import stat
from pathlib import Path

import pytest

from ndi_rmtp.config import (
    AuthConfig,
    Config,
    Credentials,
    DEFAULT_RTMP_URL,
    StreamConfig,
    Ward,
    WardCredentials,
    YoutubeConfig,
    credentials_path_for,
    update_ward_credentials,
)


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


# ---------------------------------------------------------------------------
# Device-local credentials overlay
#
# Keys live in credentials.toml, NOT config.toml, because install.sh overwrites
# config.toml on every south/north deploy. The load/save asymmetry below is the
# load-bearing part: overlaid secrets must never travel back into config.toml.
# ---------------------------------------------------------------------------

def _base_config(tmp_path: Path) -> Path:
    cfg = Config(
        auth=AuthConfig(username="admin", password="pw"),
        stream=StreamConfig(
            wards=[
                Ward(name="Alpha", key="PLACEHOLDER", channel_id=""),
                Ward(name="Beta", key="", channel_id=""),
            ],
            active_ward="Alpha",
        ),
    )
    path = tmp_path / "config.toml"
    cfg.save(path)
    return path


def test_credentials_overlay_wins_over_config_file(tmp_path):
    path = _base_config(tmp_path)
    creds = Credentials()
    creds.set_ward("Alpha", key="real-key-1234", channel_id="UCbLlSmx8Wvg85m8jPgruTuQ")
    creds.save(credentials_path_for(path))

    loaded = Config.load(path)
    alpha = loaded.stream.find_ward("Alpha")
    assert alpha.key == "real-key-1234"
    assert alpha.channel_id == "UCbLlSmx8Wvg85m8jPgruTuQ"


def test_saving_does_not_leak_overlaid_secrets_into_config_toml(tmp_path):
    """The regression that would quietly publish stream keys: the UI saves
    config on nearly every request (quality, active_ward, last_source)."""
    path = _base_config(tmp_path)
    creds = Credentials()
    creds.set_ward("Alpha", key="real-key-1234")
    creds.save(credentials_path_for(path))

    loaded = Config.load(path)
    assert loaded.stream.find_ward("Alpha").key == "real-key-1234"  # overlaid in memory
    loaded.stream.quality = "medium"
    loaded.save(path)

    raw = path.read_text()
    assert "real-key-1234" not in raw
    assert "PLACEHOLDER" in raw          # original file value preserved
    assert 'quality = "medium"' in raw   # the actual edit still persisted


def test_missing_credentials_file_is_fine(tmp_path):
    path = _base_config(tmp_path)
    loaded = Config.load(path)
    assert loaded.stream.find_ward("Alpha").key == "PLACEHOLDER"


def test_corrupt_credentials_file_does_not_break_boot(tmp_path):
    path = _base_config(tmp_path)
    credentials_path_for(path).write_text("this is not valid toml {{{")
    loaded = Config.load(path)
    assert [w.name for w in loaded.stream.wards] == ["Alpha", "Beta"]


def test_credentials_for_unknown_ward_are_ignored_but_kept(tmp_path):
    path = _base_config(tmp_path)
    creds = Credentials()
    creds.set_ward("Alpha", key="k")
    creds.set_ward("Renamed Away", key="still-here")
    creds.save(credentials_path_for(path))

    Config.load(path)  # must not raise on the unknown name
    again = Credentials.load(credentials_path_for(path))
    assert again.for_ward("Renamed Away").key == "still-here"


def test_empty_string_clears_but_none_leaves_alone(tmp_path):
    creds = Credentials()
    creds.set_ward("Alpha", key="k", channel_id="UCbLlSmx8Wvg85m8jPgruTuQ")
    creds.set_ward("Alpha", key=None)                 # untouched
    assert creds.for_ward("Alpha").key == "k"
    creds.set_ward("Alpha", key="")                   # explicit clear
    assert creds.for_ward("Alpha").key == ""
    assert creds.for_ward("Alpha").channel_id == "UCbLlSmx8Wvg85m8jPgruTuQ"


def test_credentials_file_is_written_0600(tmp_path):
    path = tmp_path / "credentials.toml"
    Credentials(wards={"Alpha": WardCredentials(key="secret")}).save(path)
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_credentials_path_follows_a_symlinked_config(tmp_path):
    real = tmp_path / "config.south.toml"
    real.write_text("")
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    assert credentials_path_for(link) == tmp_path / "credentials.toml"


def test_youtube_section_round_trips_and_defaults_off(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(auth=AuthConfig(username="a", password="b"))
    assert cfg.youtube.enabled is False
    cfg.youtube = YoutubeConfig(client_id="cid.apps.googleusercontent.com", client_secret="s")
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.youtube.client_id == "cid.apps.googleusercontent.com"
    assert loaded.youtube.enabled is True


def test_old_config_without_youtube_section_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[auth]\nusername = "a"\npassword = "b"\n')
    assert Config.load(path).youtube.enabled is False


def test_ward_youtube_fields_round_trip(tmp_path):
    path = tmp_path / "credentials.toml"
    creds = Credentials()
    creds.wards["W"] = WardCredentials(
        privacy="unlisted", yt_refresh_token="rt", yt_stream_id="sid",
        yt_stream_key="skey", yt_keep=["vid1"],
    )
    creds.save(path)
    w = Credentials.load(path).for_ward("W")
    assert (w.privacy, w.yt_refresh_token, w.yt_stream_key, w.yt_keep) == ("unlisted", "rt", "skey", ["vid1"])
    assert w.yt_connected and w.yt_linked


def test_needs_reconnect_is_linked_but_not_connected():
    w = WardCredentials(yt_needs_reconnect=True)
    assert w.yt_linked and not w.yt_connected


def test_credentials_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "credentials.toml"
    path.write_text('[wards.W]\nkey = "k"\nfuture_field = 1\n')
    assert Credentials.load(path).for_ward("W").key == "k"


def test_update_ward_credentials_persists(tmp_path):
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_stream_key="abc", privacy="private")
    w = Credentials.load(path).for_ward("W")
    assert (w.yt_stream_key, w.privacy) == ("abc", "private")


def test_config_save_never_writes_youtube_ward_fields(tmp_path):
    cfg_path = tmp_path / "config.toml"
    Config(
        auth=AuthConfig(username="a", password="b"),
        stream=StreamConfig(wards=[Ward(name="W", key="")]),
    ).save(cfg_path)
    update_ward_credentials(credentials_path_for(cfg_path), "W", yt_refresh_token="SECRET-RT", yt_stream_key="SECRET-SK")
    Config.load(cfg_path).save(cfg_path)
    text = cfg_path.read_text()
    assert "SECRET" not in text and "yt_" not in text


def test_concurrent_credential_updates_do_not_lose_fields(tmp_path):
    import threading
    path = tmp_path / "credentials.toml"
    update_ward_credentials(path, "W", yt_refresh_token="rt")
    def worker(i):
        for _ in range(20):
            update_ward_credentials(path, "W", privacy=("public", "unlisted")[i % 2])
            update_ward_credentials(path, f"X{i}", key=f"k{i}")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    creds = Credentials.load(path)
    assert creds.for_ward("W").yt_refresh_token == "rt"
    assert all(creds.for_ward(f"X{i}").key == f"k{i}" for i in range(6))
