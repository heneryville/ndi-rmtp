"""PTZ camera control over NDI via cyndilib.

A single Receiver is opened with the lowest-bandwidth mode (PTZ control packets
are tiny; we don't need full-quality video). The same receiver also feeds the
preview snapshot endpoint via FrameSync — the operator can see, in the web UI,
where the camera is actually pointing in near-real-time.

The Receiver lazily connects to the currently-selected stream source when the
first PTZ or preview call comes in, and reconnects if the source changes.

All cyndilib calls are synchronous; we run them on a background thread via
asyncio.to_thread so the FastAPI event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np
from cyndilib import (
    Finder,
    FrameSync,
    Receiver,
    RecvBandwidth,
    RecvColorFormat,
    VideoFrameSync,
)
from PIL import Image

from .config import Config

logger = logging.getLogger(__name__)

# How long the finder sweep waits for sources to advertise on first open.
_FINDER_WARMUP_S = 2.0
# How long to wait for the receiver to report `is_connected()` after set_source.
_CONNECT_TIMEOUT_S = 4.0


class PtzController:
    def __init__(self, config_getter: Callable[[], Config]):
        self._get_config = config_getter
        self._finder = Finder()
        self._finder.open()
        self._finder_warmed_at: Optional[float] = None
        # BGRX_BGRA color format → 4-byte pixels we can hand straight to PIL.
        # bandwidth_lowest → proxy-resolution frames; plenty for a preview pane.
        self._receiver = Receiver(
            color_format=RecvColorFormat.BGRX_BGRA,
            bandwidth=RecvBandwidth.lowest,
        )
        self._frame_sync = FrameSync(self._receiver)
        self._video_frame = VideoFrameSync()
        self._frame_sync.set_video_frame(self._video_frame)
        self._connected_to: Optional[str] = None
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._receiver.disconnect()
        except Exception:
            pass
        try:
            self._finder.close()
        except Exception:
            pass

    # ---- discovery / connection -------------------------------------------------

    def _find_source(self, name: str):
        """Return the cyndilib Source matching `name`, or None."""
        # Allow the finder a brief warmup the first time we look something up.
        if self._finder_warmed_at is None:
            time.sleep(_FINDER_WARMUP_S)
            self._finder_warmed_at = time.monotonic()
        for s in self._finder.iter_sources():
            if s.name == name:
                return s
        return None

    def _ensure_connected_sync(self, source_name: str) -> bool:
        """Connect the receiver to source_name if not already. Returns True
        on success; False if the source isn't visible or refuses connection."""
        with self._lock:
            if self._connected_to == source_name and self._receiver.is_connected():
                return True
            source = self._find_source(source_name)
            if source is None:
                logger.warning("PTZ: source %r not visible to Finder", source_name)
                return False
            try:
                self._receiver.set_source(source)
            except Exception as e:
                logger.exception("PTZ: set_source failed: %s", e)
                return False
            deadline = time.monotonic() + _CONNECT_TIMEOUT_S
            while time.monotonic() < deadline:
                if self._receiver.is_connected():
                    self._connected_to = source_name
                    return True
                time.sleep(0.1)
            logger.warning("PTZ: receiver did not connect to %r within timeout", source_name)
            return False

    async def _ensure_connected(self) -> tuple[bool, Optional[str]]:
        """Async wrapper. Returns (connected, source_name_used)."""
        cfg = self._get_config()
        source_name = cfg.stream.last_source
        if not source_name:
            return False, None
        ok = await asyncio.to_thread(self._ensure_connected_sync, source_name)
        return ok, source_name

    # ---- queries ----------------------------------------------------------------

    async def info(self) -> dict:
        cfg = self._get_config()
        connected, source_name = await self._ensure_connected()
        supported = False
        if connected:
            supported = await asyncio.to_thread(self._receiver.is_ptz_supported)
        return {
            "connected": connected,
            "source": source_name,
            "supported": supported,
            "presets": [{"name": p.name, "slot": p.slot} for p in cfg.ptz.presets],
            "recall_speed": cfg.ptz.recall_speed,
            "move_speed": cfg.ptz.move_speed,
            "default_preset": cfg.ptz.default_preset,
        }

    # ---- preview ----------------------------------------------------------------

    def _snapshot_jpeg_sync(self, quality: int = 70) -> Optional[bytes]:
        """Grab the latest frame from the receiver, encode as JPEG. Returns
        None if no frame is available yet."""
        with self._lock:
            # capture_video pulls the latest frame the receiver has buffered;
            # it does not block waiting for a fresh one.
            self._frame_sync.capture_video()
            vf = self._video_frame
            if vf.xres <= 0 or vf.yres <= 0:
                return None
            arr = vf.get_array()
            if arr is None or arr.size == 0:
                return None
            # BGRA (4 bytes per pixel) → RGB
            flat = np.asarray(arr, dtype=np.uint8)
            try:
                bgra = flat.reshape(vf.yres, vf.xres, 4)
            except ValueError:
                return None
            rgb = bgra[..., [2, 1, 0]]
            img = Image.fromarray(rgb, mode="RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=False)
            return buf.getvalue()

    async def snapshot_jpeg(self, quality: int = 70) -> Optional[bytes]:
        connected, _ = await self._ensure_connected()
        if not connected:
            return None
        return await asyncio.to_thread(self._snapshot_jpeg_sync, quality)

    # ---- motion -----------------------------------------------------------------

    async def pan_tilt(self, pan: float, tilt: float) -> None:
        """Continuous motion. Pan/tilt are in [-1.0, 1.0]. (0, 0) stops."""
        connected, _ = await self._ensure_connected()
        if not connected:
            return
        await asyncio.to_thread(self._receiver.ptz.pan_and_tilt, pan, tilt)

    async def zoom(self, speed: float) -> None:
        """Continuous zoom. Speed in [-1.0, 1.0]. 0 stops."""
        connected, _ = await self._ensure_connected()
        if not connected:
            return
        await asyncio.to_thread(self._receiver.ptz.zoom, speed)

    async def stop(self) -> None:
        await self.pan_tilt(0.0, 0.0)
        await self.zoom(0.0)

    async def recall_preset(self, slot: int, speed: Optional[float] = None) -> None:
        cfg = self._get_config()
        if speed is None:
            speed = cfg.ptz.recall_speed
        connected, _ = await self._ensure_connected()
        if not connected:
            return
        await asyncio.to_thread(self._receiver.ptz.recall_preset, slot, speed)

    async def store_preset(self, slot: int) -> None:
        connected, _ = await self._ensure_connected()
        if not connected:
            return
        await asyncio.to_thread(self._receiver.ptz.store_preset, slot)
