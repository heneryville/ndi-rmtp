"""FastAPI app: routes, dependency injection, template rendering."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import make_auth_dependency
from .config import Config
from .ndi import NDIDiscovery
from .streamer import Streamer

# PtzController depends on cyndilib (an optional dependency). Only import for
# type hints when available; the runtime treats `ptz` as an opaque object.
if False:  # TYPE_CHECKING
    from .ptz import PtzController  # noqa: F401

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


_CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


def _read_cpu_temp_c() -> Optional[float]:
    """Return CPU temperature in Celsius, or None if unavailable.

    Reads the standard Linux thermal interface (present on the Pi).
    Returns None on macOS dev (no /sys), so the UI just hides the chip.
    """
    try:
        millideg = int(_CPU_TEMP_PATH.read_text().strip())
        return round(millideg / 1000, 1)
    except (OSError, ValueError):
        return None


def _clamp_speed(v: float) -> float:
    """Clamp PTZ speed values into NDI's expected [-1.0, 1.0] range."""
    return max(-1.0, min(1.0, float(v)))


def build_app(
    config_path: Path,
    streamer: Streamer,
    discovery: NDIDiscovery,
    ptz: Optional["PtzController"] = None,  # noqa: F821 — string-only forward ref
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
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "config": cfg,
                "qualities": ["low", "medium", "high"],
            },
        )

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def api_status():
        data = streamer.status().to_dict()
        data["cpu_temp_c"] = _read_cpu_temp_c()
        return data

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
        active = cfg.stream.active()
        if active is None or not active.key:
            raise HTTPException(
                status_code=400,
                detail="No ward is selected or the selected ward isn't set up correctly. Please contact your Stake Technology Specialist.",
            )
        if quality not in ("low", "medium", "high"):
            raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")

        # Validate the source exists in the discovery cache and resolve its address.
        # ndi-streamer's -n flag puts its value into NDIlib_source_t.p_url_address,
        # which expects an IP:port URL. Passing the friendly NDI name (e.g.,
        # "MACHINE (Source)") causes NDI to fail to connect — the subprocess
        # then hangs producing no stderr output. Send the discovered address
        # instead, falling back to the name only if discovery has no address.
        sources = await discovery.list()
        matched = next((s for s in sources if s.name == source), None)
        if matched is None:
            raise HTTPException(
                status_code=409,
                detail="Camera not found on the network. Make sure it is powered on, then wait a moment and try again.",
            )

        # Persist the friendly name so the UI restores the selection across restarts.
        cfg.stream.last_source = source
        cfg.stream.quality = quality
        cfg.save(config_path)

        connect_target = matched.address or matched.name

        try:
            await streamer.start(
                source=connect_target,
                stream_key=active.key,
                quality=quality,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.post("/api/stop", dependencies=[Depends(require_auth)])
    async def api_stop():
        await streamer.stop()
        return {"ok": True}

    @app.post("/api/mute", dependencies=[Depends(require_auth)])
    async def api_mute(muted: bool = Form(...)):
        """Toggle audio output. If currently streaming, the subprocess restarts
        so the new audio mode takes effect — YouTube may briefly reconnect."""
        await streamer.set_muted(muted)
        return {"ok": True, "muted": muted}

    @app.post("/api/sacrament", dependencies=[Depends(require_auth)])
    async def api_sacrament(enabled: bool = Form(...)):
        """Toggle sacrament mode: video swapped for a still image, audio off."""
        try:
            await streamer.set_sacrament_mode(enabled)
        except (FileNotFoundError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, "sacrament_mode": enabled}

    # ---- PTZ ---------------------------------------------------------------

    @app.get("/api/ptz/info", dependencies=[Depends(require_auth)])
    async def api_ptz_info():
        if ptz is None:
            cfg = get_config()
            return {
                "supported": False,
                "connected": False,
                "source": None,
                "presets": [{"name": p.name, "slot": p.slot} for p in cfg.ptz.presets],
                "default_preset": cfg.ptz.default_preset,
                "recall_speed": cfg.ptz.recall_speed,
                "move_speed": cfg.ptz.move_speed,
            }
        return await ptz.info()

    @app.post("/api/ptz/preset/{slot}", dependencies=[Depends(require_auth)])
    async def api_ptz_recall(slot: int, speed: Optional[float] = Form(None)):
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera controls are unavailable. Make sure the camera is connected on the network.")
        if not (0 <= slot <= 99):
            raise HTTPException(status_code=400, detail="slot must be 0..99")
        await ptz.recall_preset(slot, speed)
        return {"ok": True}

    @app.post("/api/ptz/preset/{slot}/store", dependencies=[Depends(require_auth)])
    async def api_ptz_store(slot: int):
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera controls are unavailable. Make sure the camera is connected on the network.")
        if not (0 <= slot <= 99):
            raise HTTPException(status_code=400, detail="slot must be 0..99")
        await ptz.store_preset(slot)
        return {"ok": True}

    @app.post("/api/ptz/pan_tilt", dependencies=[Depends(require_auth)])
    async def api_ptz_pan_tilt(pan: float = Form(...), tilt: float = Form(...)):
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera controls are unavailable. Make sure the camera is connected on the network.")
        await ptz.pan_tilt(_clamp_speed(pan), _clamp_speed(tilt))
        return {"ok": True}

    @app.post("/api/ptz/zoom", dependencies=[Depends(require_auth)])
    async def api_ptz_zoom(speed: float = Form(...)):
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera controls are unavailable. Make sure the camera is connected on the network.")
        await ptz.zoom(_clamp_speed(speed))
        return {"ok": True}

    @app.post("/api/ptz/stop", dependencies=[Depends(require_auth)])
    async def api_ptz_stop():
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera controls are unavailable. Make sure the camera is connected on the network.")
        await ptz.stop()
        return {"ok": True}

    @app.get("/api/preview.jpg", dependencies=[Depends(require_auth)])
    async def api_preview():
        # 503s here are normal during boot and whenever the camera isn't on
        # the network yet; the UI hides the preview frame and silently
        # retries, so the operator never sees these unless they're looking
        # at the network tab.
        if ptz is None:
            raise HTTPException(status_code=503, detail="Camera not connected yet")
        jpg = await ptz.snapshot_jpeg()
        if jpg is None:
            raise HTTPException(status_code=503, detail="Waiting for first frame from camera")
        return Response(
            content=jpg,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.post("/api/config", dependencies=[Depends(require_auth)])
    def api_config(
        active_ward: Optional[str] = Form(None),
        quality: Optional[str] = Form(None),
        default_preset: Optional[str] = Form(None),
    ):
        cfg = get_config()
        if active_ward is not None:
            if cfg.stream.find_ward(active_ward) is None:
                raise HTTPException(
                    status_code=400,
                    detail="The selected ward is no longer set up on this appliance. Please contact your Stake Technology Specialist.",
                )
            cfg.stream.active_ward = active_ward
        if quality is not None:
            if quality not in ("low", "medium", "high"):
                raise HTTPException(status_code=400, detail=f"Bad quality {quality!r}")
            cfg.stream.quality = quality
        if default_preset is not None:
            # Empty string clears the default (no auto-recall on start); any
            # other value must match a configured preset name.
            if default_preset and not any(p.name == default_preset for p in cfg.ptz.presets):
                raise HTTPException(status_code=400, detail=f"Unknown preset {default_preset!r}")
            cfg.ptz.default_preset = default_preset
        cfg.save(config_path)
        return {"ok": True}

    return app
