"""FastAPI app: routes, dependency injection, template rendering."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import make_auth_dependency
from .config import Config
from .ndi import NDIDiscovery
from .streamer import Streamer

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def build_app(
    config_path: Path,
    streamer: Streamer,
    discovery: NDIDiscovery,
) -> FastAPI:
    app = FastAPI(title="ndi-rmtp")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Config is reloaded on every request so disk edits are picked up.
    def get_config() -> Config:
        return Config.load(config_path)

    require_auth = make_auth_dependency(get_config)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def index(request: Request):
        cfg = get_config()
        masked_key = "•" * 12 if cfg.stream.stream_key else ""
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "config": cfg,
                "masked_key": masked_key,
                "qualities": ["low", "medium", "high"],
            },
        )

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def api_status():
        return streamer.status().to_dict()

    @app.get("/api/sources", dependencies=[Depends(require_auth)])
    async def api_sources():
        sources = await discovery.list()
        return [{"name": s.name, "address": s.address} for s in sources]

    @app.post("/api/start", dependencies=[Depends(require_auth)])
    async def api_start(
        source: str = Form(...),
        quality: str = Form(...),
    ):
        cfg = get_config()
        if not cfg.stream.stream_key:
            raise HTTPException(status_code=400, detail="Stream key not set")
        if quality not in ("low", "medium", "high"):
            raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")

        # Persist selection so it survives restarts
        cfg.stream.last_source = source
        cfg.stream.quality = quality
        cfg.save(config_path)

        try:
            await streamer.start(source=source, stream_key=cfg.stream.stream_key, quality=quality)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.post("/api/stop", dependencies=[Depends(require_auth)])
    async def api_stop():
        await streamer.stop()
        return {"ok": True}

    @app.post("/api/config", dependencies=[Depends(require_auth)])
    def api_config(
        stream_key: Optional[str] = Form(None),
        quality: Optional[str] = Form(None),
    ):
        cfg = get_config()
        if stream_key is not None:
            cfg.stream.stream_key = stream_key
        if quality is not None:
            if quality not in ("low", "medium", "high"):
                raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")
            cfg.stream.quality = quality
        cfg.save(config_path)
        return {"ok": True}

    return app
