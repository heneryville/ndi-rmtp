"""FastAPI app: routes, dependency injection, template rendering."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import youtube
from .broadcasts import PrepareError, YouTubeServices
from .auth import make_auth_dependency
from .config import PRIVACY_CHOICES, Config, Credentials, credentials_path_for, update_ward_credentials
from .ndi import NDIDiscovery
from .streamer import Streamer
from .youtube import LivenessChecker
from .youtube_auth import AuthError

# PtzController depends on cyndilib (an optional dependency). Only import for
# type hints when available; the runtime treats `ptz` as an opaque object.
if False:  # TYPE_CHECKING
    from .ptz import PtzController  # noqa: F401

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


_CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")

# Where we send an operator to create today's broadcast. Loading the Live
# Control Room is what mints it — YouTube will accept RTMP into a channel with
# no broadcast forever and never publish it, so this link is the fix, not a
# convenience. With no channel_id we can still send them to Studio, which lands
# on whichever account they are signed into (Create → Go Live from there).
YOUTUBE_CONTROL_ROOM_URL = "https://studio.youtube.com/channel/{channel_id}/livestreaming/dashboard"
YOUTUBE_STUDIO_URL = "https://studio.youtube.com/"


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


def _mask_key(key: str) -> str:
    """Show only enough of a stream key to tell two apart.

    Never send the full value to a browser: the page is reachable by anyone on
    the LAN when Basic auth is off, and an operator never needs to read a key
    back — they copy it from YouTube Studio and paste it in.
    """
    key = (key or "").strip()
    if not key or key.startswith("REPLACE_WITH"):
        return ""
    return f"…{key[-4:]}" if len(key) > 4 else "…"


def _yt_setup_fields(wc) -> dict:
    """What /setup may show about a ward's YouTube link; never tokens or keys."""
    return {
        "yt_connected": wc.yt_connected,
        "yt_needs_reconnect": wc.yt_needs_reconnect,
        "yt_channel_title": wc.yt_channel_title,
        "privacy": wc.privacy,
    }


def _clamp_speed(v: float) -> float:
    """Clamp PTZ speed values into NDI's expected [-1.0, 1.0] range."""
    return max(-1.0, min(1.0, float(v)))


def build_app(
    config_path: Path,
    streamer: Streamer,
    discovery: NDIDiscovery,
    ptz: Optional["PtzController"] = None,  # noqa: F821 — string-only forward ref
    youtube_services: Optional[YouTubeServices] = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(youtube_services.manager.run_sweeper()) if youtube_services else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ndi-rmtp", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    liveness = LivenessChecker()

    # Config is reloaded on every request so disk edits are picked up.
    def get_config() -> Config:
        return Config.load(config_path)

    require_auth = make_auth_dependency(get_config)

    def _yt_ward(cfg: Config, ward_name: str):
        """The ward's credentials if it uses the YouTube API path, else None."""
        if youtube_services is None or not cfg.youtube.enabled:
            return None
        w = Credentials.load(credentials_path_for(config_path)).for_ward(ward_name)
        return w if w.yt_linked else None

    # `no-cache` means "revalidate before reusing", NOT "don't cache" — the
    # browser still stores the file and StaticFiles' ETag turns each check into
    # a tiny 304. Without it, a code-only deploy (which leaves config_version
    # untouched, so there's no cache-buster to change) leaves operators staring
    # at a stale style.css until they hard-refresh. Revalidation is free on a
    # LAN appliance; a stale UI costs a service call.
    class _RevalidatingStatic(StaticFiles):
        def file_response(self, *args, **kwargs):  # type: ignore[override]
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/static", _RevalidatingStatic(directory=str(_STATIC_DIR)), name="static")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def index(request: Request):
        cfg = get_config()
        ward = cfg.stream.active()
        channel_id = ward.channel_id if ward else ""
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "config": cfg,
                "qualities": ["low", "medium", "high"],
                # Rendered server-side so the link works on first paint,
                # before /api/youtube has answered (or if it never does).
                "youtube_url": (
                    YOUTUBE_CONTROL_ROOM_URL.format(channel_id=channel_id)
                    if youtube.valid_channel_id(channel_id)
                    else YOUTUBE_STUDIO_URL
                ),
                "has_channel_id": youtube.valid_channel_id(channel_id),
            },
        )

    @app.get("/camera", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def camera(request: Request):
        """PTZ-only page for sites that encode in OBS rather than through this
        appliance. Shares the camera card with `/` (templates/_ptz.html) but
        exposes nothing that touches the streamer."""
        return templates.TemplateResponse(
            request,
            "camera.html",
            {"config": get_config()},
        )

    @app.get("/setup", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def setup(request: Request):
        """Per-ward stream key + channel ID, for the Stake Technology Specialist.

        Wards themselves are still enumerated in config.toml; this page only
        edits the two values a human has to copy out of YouTube Studio.
        """
        cfg = get_config()
        creds = Credentials.load(credentials_path_for(config_path))
        wards = [
            {
                **_yt_setup_fields(creds.for_ward(w.name)),
                "name": w.name,
                # Never send a key to the browser. The last 4 characters are
                # enough to tell two keys apart — CONFIG_CHANGELOG.md already
                # identifies keys that way — and are what an operator can check
                # against YouTube Studio without the secret leaving the device.
                "key_hint": _mask_key(w.key),
                "has_key": bool(w.key) and not w.key.startswith("REPLACE_WITH"),
                "key_is_local": bool(creds.for_ward(w.name).key),
                "channel_id": w.channel_id,
            }
            for w in cfg.stream.wards
        ]
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "config": cfg,
                "wards": wards,
                "credentials_path": str(credentials_path_for(config_path)),
                # Anyone on the LAN can reach this page when Basic auth is off.
                # Keys are write-only so nothing leaks, but a stranger could
                # still overwrite them, which takes a ward off the air.
                "auth_disabled": not cfg.auth.enabled,
                "youtube_enabled": youtube_services is not None and cfg.youtube.enabled,
                "privacy_choices": PRIVACY_CHOICES,
            },
        )

    @app.post("/api/ward-credentials", dependencies=[Depends(require_auth)])
    def api_ward_credentials(
        ward: str = Form(...),
        key: str = Form(""),
        channel_id: str = Form(""),
    ):
        cfg = get_config()
        if cfg.stream.find_ward(ward) is None:
            raise HTTPException(
                status_code=400,
                detail=f"No ward named {ward!r} on this appliance. Wards are listed in config.toml.",
            )
        channel_id = channel_id.strip()
        if channel_id and not youtube.valid_channel_id(channel_id):
            raise HTTPException(
                status_code=400,
                detail="That doesn't look like a channel ID. It starts with "
                       "'UC' and is 24 characters — copy it from the Studio address bar.",
            )

        creds_path = credentials_path_for(config_path)
        creds = Credentials.load(creds_path)
        # A blank key means "leave it alone", not "erase it" — the form never
        # shows the current key, so an untouched field must not wipe it. Erasing
        # is a separate, explicit action (send key="-").
        new_key = None
        if key == "-":
            new_key = ""
        elif key.strip():
            new_key = key.strip()
        creds.set_ward(ward, key=new_key, channel_id=channel_id)
        creds.save(creds_path)
        logger.info(
            "updated credentials for ward %r (key %s, channel_id %s)",
            ward,
            "changed" if new_key is not None else "unchanged",
            "set" if channel_id else "cleared",
        )
        return {"ok": True, "ward": ward}

    def _require_youtube_ward(ward: str) -> Config:
        cfg = get_config()
        if youtube_services is None or not cfg.youtube.enabled:
            raise HTTPException(status_code=404, detail="YouTube integration isn't configured on this appliance.")
        if cfg.stream.find_ward(ward) is None:
            raise HTTPException(status_code=400, detail=f"No ward named {ward!r} on this appliance.")
        return cfg

    @app.post("/api/youtube/connect", dependencies=[Depends(require_auth)])
    async def api_youtube_connect(ward: str = Form(...)):
        _require_youtube_ward(ward)
        try:
            return (await youtube_services.flows.begin(ward)).to_dict()
        except AuthError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/api/youtube/connect", dependencies=[Depends(require_auth)])
    def api_youtube_connect_status(ward: str):
        _require_youtube_ward(ward)
        st = youtube_services.flows.state(ward)
        return st.to_dict() if st else {"state": "none"}

    @app.post("/api/youtube/disconnect", dependencies=[Depends(require_auth)])
    async def api_youtube_disconnect(ward: str = Form(...)):
        _require_youtube_ward(ward)
        await youtube_services.manager.disconnect(ward)
        return {"ok": True}

    @app.post("/api/youtube/privacy", dependencies=[Depends(require_auth)])
    def api_youtube_privacy(ward: str = Form(...), privacy: str = Form(...)):
        _require_youtube_ward(ward)
        if privacy not in PRIVACY_CHOICES:
            raise HTTPException(status_code=400, detail=f"Privacy must be one of {', '.join(PRIVACY_CHOICES)}.")
        update_ward_credentials(credentials_path_for(config_path), ward, privacy=privacy)
        return {"ok": True}

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def api_status():
        data = streamer.status().to_dict()
        data["cpu_temp_c"] = _read_cpu_temp_c()
        return data

    @app.get("/api/sources", dependencies=[Depends(require_auth)])
    async def api_sources():
        sources = await discovery.list()
        return [{"name": s.name, "address": s.address} for s in sources]

    @app.get("/api/youtube", dependencies=[Depends(require_auth)])
    async def api_youtube():
        """Best-effort liveness for the active ward's channel.

        Returns a claim ("live"/"scheduled") or an admission of ignorance
        ("not_visible"/"unavailable"/"unconfigured"). The UI must never turn
        the latter into a warning — see youtube.py for why a negative
        observation is uninformative here.

        Always 200, even when the check fails: this is decoration on a page
        whose real job is streaming, and a YouTube outage must not surface as
        an error banner over a working stream.
        """
        cfg = get_config()
        ward = cfg.stream.active()
        if ward is not None and _yt_ward(cfg, ward.name) is not None:
            return await youtube_services.manager.status(ward.name)
        channel_id = ward.channel_id if ward else ""
        result = await liveness.check(channel_id)
        data = result.to_dict()
        data["channel_id"] = channel_id
        data["control_room_url"] = (
            YOUTUBE_CONTROL_ROOM_URL.format(channel_id=channel_id)
            if youtube.valid_channel_id(channel_id)
            else YOUTUBE_STUDIO_URL
        )
        data["mode"] = "scrape"
        return data

    @app.post("/api/start", dependencies=[Depends(require_auth)])
    async def api_start(
        source: str = Form(...),
        quality: str = Form(...),
        skip_youtube: bool = Form(False),
    ):
        cfg = get_config()
        active = cfg.stream.active()
        if active is None:
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

        # The broadcast must exist before the encoder connects or YouTube parks it in "Preparing stream".
        stream_key, watch_url = active.key, None
        yt_ward = _yt_ward(cfg, active.name)
        if yt_ward is not None:
            if skip_youtube:
                stream_key = yt_ward.yt_stream_key or active.key
            else:
                try:
                    prepared = await youtube_services.manager.prepare(active.name)
                except PrepareError as e:
                    raise HTTPException(status_code=424, detail=str(e))
                stream_key, watch_url = prepared.stream_key, prepared.watch_url
        if not stream_key or stream_key.startswith("REPLACE_WITH"):
            raise HTTPException(
                status_code=400,
                detail="No ward is selected or the selected ward isn't set up correctly. Please contact your Stake Technology Specialist.",
            )

        # Persist the friendly name so the UI restores the selection across restarts.
        cfg.stream.last_source = source
        cfg.stream.quality = quality
        cfg.save(config_path)

        connect_target = matched.address or matched.name

        try:
            await streamer.start(
                source=connect_target,
                stream_key=stream_key,
                quality=quality,
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True, "watch_url": watch_url}

    @app.post("/api/stop", dependencies=[Depends(require_auth)])
    async def api_stop():
        await streamer.stop()
        cfg = get_config()
        warning = None
        if _yt_ward(cfg, cfg.stream.active_ward) is not None:
            warning = await youtube_services.manager.finish(cfg.stream.active_ward)
        return {"ok": True, "warning": warning}

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
        last_source: Optional[str] = Form(None),
    ):
        cfg = get_config()
        if last_source is not None:
            # Deliberately unvalidated against the discovery cache. This is the
            # source PtzController connects to, and the /camera page writes it
            # whenever the operator picks a camera. Rejecting names that are
            # momentarily absent from discovery (cameras drop off the network
            # for a beat all the time) would lose the operator's choice.
            # /api/start takes the same permissive approach.
            cfg.stream.last_source = last_source
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
