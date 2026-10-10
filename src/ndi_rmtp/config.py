"""TOML configuration persistence with atomic writes and 0600 file mode."""
from __future__ import annotations

import os
import secrets
import string
import tempfile
import threading
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar

import tomli_w

DEFAULT_RTMP_URL = "rtmp://a.rtmp.youtube.com/live2"
DEFAULT_BACKOFF_S = [2, 5, 15, 60]
PASSWORD_LEN = 12

# Device-local per-ward credentials, entered through /setup and NEVER deployed.
# Lives beside config.toml. `install.sh` must not touch it: config.toml is
# overwritten on every south/north deploy, so keeping keys there would wipe
# them on the next deploy — and wipe them with placeholder text on any clone
# that lacks the git-ignored config.<deployment>.toml.
CREDENTIALS_FILENAME = "credentials.toml"
# Sync routes run in the threadpool alongside event-loop writers, so read-modify-write needs a lock.
CREDENTIALS_LOCK = threading.RLock()


@dataclass
class AuthConfig:
    username: str
    password: str
    enabled: bool = True


@dataclass
class Ward:
    """A broadcast target: a YouTube stream key plus the sacrament overlay
    used during that ward's service. Wards can share keys (e.g., two wards
    meeting in the same building publish to the same channel).

    Wards are ENUMERATED in config.toml (name + sacrament_image), but `key` and
    `channel_id` are entered through /setup and stored device-locally in
    credentials.toml, which overlays whatever this file holds. Anything read
    off a Ward at runtime is the merged value; see Config.load/save."""
    name: str
    key: str
    sacrament_image: str = ""  # SVG/PNG/JPG path; empty disables sacrament mode for this ward
    # Public YouTube channel ID (`UC…`, 24 chars) — NOT a secret, unlike `key`,
    # so it belongs in the committed example templates. Optional: without it
    # the ward streams exactly as before, it just loses the one-tap link to its
    # Live Control Room and the "Live on YouTube" confirmation. It can never
    # cause a warning (see youtube.py), so a wrong value costs nothing.
    channel_id: str = ""


@dataclass
class StreamConfig:
    last_source: str = ""
    wards: list[Ward] = field(default_factory=list)
    active_ward: str = ""
    rtmp_url: str = DEFAULT_RTMP_URL
    quality: str = "high"

    def find_ward(self, name: str) -> Ward | None:
        return next((w for w in self.wards if w.name == name), None)

    def active(self) -> Ward | None:
        return self.find_ward(self.active_ward) if self.active_ward else None


@dataclass
class PtzPreset:
    """A named PTZ camera position. The slot is the NDI preset index (0-99)."""
    name: str
    slot: int


@dataclass
class PtzConfig:
    presets: list[PtzPreset] = field(default_factory=list)
    recall_speed: float = 0.10   # 0.0 slowest .. 1.0 fastest, used by preset recall
    move_speed: float = 0.2      # 0.0 .. 1.0, used by d-pad and zoom hold
    # Preset to snap to automatically when a stream starts, so viewers see a
    # sensible framing during YouTube's "Preparing stream" window instead of
    # whatever the camera was pointed at last. Matched by name against
    # `presets`; missing / empty / unmatched → no auto-recall.
    default_preset: str = "Stand Far"


@dataclass
class AdvancedConfig:
    auto_restart: bool = True
    restart_backoff_s: list[int] = field(default_factory=lambda: list(DEFAULT_BACKOFF_S))
    discovery_ttl_s: int = 5
    status_poll_ms: int = 2000


PRIVACY_CHOICES = ("public", "unlisted", "private")


@dataclass
class YoutubeConfig:
    """OAuth client for the YouTube integration; empty client_id hides the feature."""
    client_id: str = ""
    client_secret: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.client_id.strip())


def credentials_path_for(config_path: Path) -> Path:
    """Where the device-local credentials file sits for a given config path.

    Resolves symlinks first: in dev, `config.toml` is a symlink to
    `config.south.toml`, and we want the credentials file to land beside the
    real file rather than next to the link.
    """
    path = config_path.resolve() if config_path.is_symlink() else config_path
    return path.parent / CREDENTIALS_FILENAME


@dataclass
class WardCredentials:
    key: str = ""
    channel_id: str = ""
    privacy: str = "public"
    # Presence of a refresh token == ward is connected to YouTube.
    yt_refresh_token: str = ""
    yt_channel_title: str = ""
    yt_stream_id: str = ""
    yt_stream_key: str = ""
    yt_needs_reconnect: bool = False
    yt_keep: list[str] = field(default_factory=list)

    @property
    def yt_connected(self) -> bool:
        return bool(self.yt_refresh_token)

    @property
    def yt_linked(self) -> bool:
        return self.yt_connected or self.yt_needs_reconnect

    @classmethod
    def from_dict(cls, entry: dict) -> "WardCredentials":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in entry.items() if k in known})


@dataclass
class Credentials:
    """Per-ward secrets, keyed by ward name, stored outside config.toml.

    Ward *enumeration* stays in config.toml — this file only ever supplies the
    two values an operator can type in: the stream key and the channel ID.
    Entries for wards that no longer exist are kept on save (a ward renamed in
    config.toml and renamed back should not lose its key), but ignored on load.
    """
    wards: dict[str, WardCredentials] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        if not path.exists():
            return cls()
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError):
            # A corrupt credentials file must not take down the appliance —
            # the ward list and every other setting still work, and /setup
            # lets someone retype the keys.
            return cls()
        wards = {
            name: WardCredentials.from_dict(entry)
            for name, entry in (data.get("wards") or {}).items()
            if isinstance(entry, dict)
        }
        return cls(wards=wards)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump({"wards": {n: asdict(c) for n, c in self.wards.items()}}, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def for_ward(self, name: str) -> WardCredentials:
        return self.wards.get(name, WardCredentials())

    def set_ward(self, name: str, *, key: str | None = None, channel_id: str | None = None) -> None:
        """Update one ward. `None` means "leave alone" — distinct from `""`,
        which deliberately clears the value."""
        entry = self.wards.setdefault(name, WardCredentials())
        if key is not None:
            entry.key = key.strip()
        if channel_id is not None:
            entry.channel_id = channel_id.strip()


def update_ward_credentials(path: Path, ward: str, **changes) -> WardCredentials:
    """Load, change one ward's fields, save, under CREDENTIALS_LOCK."""
    with CREDENTIALS_LOCK:
        creds = Credentials.load(path)
        entry = creds.wards.setdefault(ward, WardCredentials())
        for name, value in changes.items():
            setattr(entry, name, value)
        creds.save(path)
        return entry


@dataclass
class Config:
    auth: AuthConfig
    stream: StreamConfig = field(default_factory=StreamConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)
    ptz: PtzConfig = field(default_factory=PtzConfig)
    youtube: YoutubeConfig = field(default_factory=YoutubeConfig)
    # A hand-incremented "config generation" stamp, bumped on every deploy (see
    # deploy.md) and shown in the UI so an operator can tell at a glance whether
    # the appliance actually picked up the latest deployed config. Defaults to 0
    # for older/hand-edited configs that predate this field.
    config_version: int = 0

    # Ward key/channel_id exactly as they appeared in config.toml, captured
    # BEFORE credentials.toml was overlaid. Deliberately NOT a dataclass field
    # so `asdict()` cannot serialize it. save() writes these back, which is what
    # stops a device-local secret from leaking into the deployed config file.
    _file_credentials: ClassVar[dict[str, tuple[str, str]]] = {}

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        stream_data = dict(data.get("stream", {}))
        stream_data["wards"] = [Ward(**w) for w in stream_data.get("wards", [])]
        ptz_data = dict(data.get("ptz", {}))
        ptz_data["presets"] = [PtzPreset(**p) for p in ptz_data.get("presets", [])]
        config = cls(
            auth=AuthConfig(**data["auth"]),
            stream=StreamConfig(**stream_data),
            advanced=AdvancedConfig(**data.get("advanced", {})),
            ptz=PtzConfig(**ptz_data),
            youtube=YoutubeConfig(**data.get("youtube", {})),
            config_version=data.get("config_version", 0),
        )
        config._file_credentials = {
            w.name: (w.key, w.channel_id) for w in config.stream.wards
        }
        # Device-local credentials win over whatever the deployed file carries
        # (usually a placeholder, or nothing at all).
        creds = Credentials.load(credentials_path_for(path))
        for ward in config.stream.wards:
            entry = creds.wards.get(ward.name)
            if entry is None:
                continue
            if entry.key:
                ward.key = entry.key
            if entry.channel_id:
                ward.channel_id = entry.channel_id
        return config

    def save(self, path: Path) -> None:
        # Resolve symlinks first so os.replace() rewrites the linked file
        # instead of clobbering the symlink itself. Dev machines ship with
        # `config.toml` as a symlink to `config.south.toml` (or north); an
        # atomic replace on the symlink would silently break the link and
        # divergently save future edits into a shadow file.
        if path.is_symlink():
            path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            data = asdict(self)
            # Put back whatever config.toml originally held for each ward's
            # key/channel_id. Without this, any ordinary save (the UI persists
            # quality / active_ward / last_source on nearly every request) would
            # copy device-local secrets into the deployed config file.
            originals = getattr(self, "_file_credentials", {})
            for ward in data.get("stream", {}).get("wards", []):
                original = originals.get(ward.get("name"))
                if original is not None:
                    ward["key"], ward["channel_id"] = original
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            # Roll back: remove temp file so we don't leak partial state
            tmp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def bootstrap(cls, path: Path) -> tuple["Config", str]:
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LEN))
        config = cls(auth=AuthConfig(username="admin", password=password))
        config.save(path)
        return config, password
