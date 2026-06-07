"""Wire up config + streamer + discovery + FastAPI app.

Handles binary path resolution (project-local ./bin/ first, then /usr/local/bin)
and first-run config bootstrap.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from .app import build_app
from .config import Config
from .ndi import NDIDiscovery
from .streamer import Streamer

# cyndilib (PTZ) is optional — without it the PTZ routes return 503 but the app
# otherwise works. Helpful for environments where the wheel doesn't install.
try:
    from .ptz import PtzController  # noqa: F401
    _ptz_available = True
    _ptz_import_error: str | None = None
except Exception as _e:  # pragma: no cover
    PtzController = None  # type: ignore[assignment,misc]
    _ptz_available = False
    _ptz_import_error = repr(_e)

logger = logging.getLogger(__name__)


def resolve_binary() -> Path:
    """Resolve the ndi-streamer binary path.

    Order: $NDI_STREAMER_BIN, ./bin/ndi-streamer, /usr/local/bin/ndi-streamer.
    """
    env_override = os.environ.get("NDI_STREAMER_BIN")
    if env_override:
        return Path(env_override)
    cwd_local = Path.cwd() / "bin" / "ndi-streamer"
    if cwd_local.exists():
        return cwd_local
    return Path("/usr/local/bin/ndi-streamer")


def resolve_config_path() -> Path:
    """Resolve where the config lives.

    Order:
      1. $NDI_RMTP_CONFIG (explicit override — set by systemd in production)
      2. ./config.toml relative to the current working directory (project-local)
    """
    override = os.environ.get("NDI_RMTP_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "config.toml"


def ensure_config(config_path: Path) -> Config:
    if not config_path.exists():
        logger.warning("config not found at %s — bootstrapping", config_path)
        cfg, password = Config.bootstrap(config_path)
        print(
            f"\n[ndi-rmtp] first-run bootstrap\n"
            f"  admin username: admin\n"
            f"  admin password: {password}\n"
            f"  config path:    {config_path}\n",
            file=sys.stderr,
            flush=True,
        )
        return cfg
    try:
        return Config.load(config_path)
    except Exception as e:
        logger.error("config at %s is malformed (%s) — rebootstrapping", config_path, e)
        backup = config_path.with_suffix(".broken.toml")
        config_path.replace(backup)
        cfg, password = Config.bootstrap(config_path)
        print(
            f"\n[ndi-rmtp] broken config moved to {backup}, generated new admin password: {password}\n",
            file=sys.stderr,
            flush=True,
        )
        return cfg


def make_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = resolve_config_path()
    ensure_config(config_path)

    binary = resolve_binary()
    if not binary.exists():
        logger.warning(
            "ndi-streamer binary not found at %s — start will fail until it's installed",
            binary,
        )

    cfg = Config.load(config_path)
    discovery = NDIDiscovery(binary, ttl_s=float(cfg.advanced.discovery_ttl_s))
    streamer = Streamer(binary, config_getter=lambda: Config.load(config_path))

    ptz = None
    if _ptz_available:
        try:
            ptz = PtzController(config_getter=lambda: Config.load(config_path))
            logger.info("PTZ controller ready")
        except Exception as e:
            logger.warning("PTZ controller failed to initialize: %s", e)
            ptz = None
    else:
        logger.info("cyndilib not available (%s); PTZ disabled", _ptz_import_error)

    return build_app(
        config_path=config_path,
        streamer=streamer,
        discovery=discovery,
        ptz=ptz,
    )


app = make_app()
