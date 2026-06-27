"""TOML configuration persistence with atomic writes and 0600 file mode."""
from __future__ import annotations

import os
import secrets
import string
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

DEFAULT_RTMP_URL = "rtmp://a.rtmp.youtube.com/live2"
DEFAULT_BACKOFF_S = [2, 5, 15, 60]
PASSWORD_LEN = 12


@dataclass
class AuthConfig:
    username: str
    password: str
    enabled: bool = True


@dataclass
class Ward:
    """A broadcast target: a YouTube stream key plus the sacrament overlay
    used during that ward's service. Wards can share keys (e.g., two wards
    meeting in the same building publish to the same channel). Managed in
    config.toml only, never via the UI."""
    name: str
    key: str
    sacrament_image: str = ""  # SVG/PNG/JPG path; empty disables sacrament mode for this ward


@dataclass
class StreamConfig:
    last_source: str = ""
    wards: list[Ward] = field(default_factory=list)
    active_ward: str = ""
    rtmp_url: str = DEFAULT_RTMP_URL
    quality: str = "medium"

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
    recall_speed: float = 0.25   # 0.0 slowest .. 1.0 fastest, used by preset recall
    move_speed: float = 0.2      # 0.0 .. 1.0, used by d-pad and zoom hold


@dataclass
class AdvancedConfig:
    auto_restart: bool = True
    restart_backoff_s: list[int] = field(default_factory=lambda: list(DEFAULT_BACKOFF_S))
    discovery_ttl_s: int = 5
    status_poll_ms: int = 2000


@dataclass
class Config:
    auth: AuthConfig
    stream: StreamConfig = field(default_factory=StreamConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)
    ptz: PtzConfig = field(default_factory=PtzConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        stream_data = dict(data.get("stream", {}))
        stream_data["wards"] = [Ward(**w) for w in stream_data.get("wards", [])]
        ptz_data = dict(data.get("ptz", {}))
        ptz_data["presets"] = [PtzPreset(**p) for p in ptz_data.get("presets", [])]
        return cls(
            auth=AuthConfig(**data["auth"]),
            stream=StreamConfig(**stream_data),
            advanced=AdvancedConfig(**data.get("advanced", {})),
            ptz=PtzConfig(**ptz_data),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".toml", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                tomli_w.dump(asdict(self), f)
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
