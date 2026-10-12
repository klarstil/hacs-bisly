"""Camera platform for hacs_bisly."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from custom_components.hacs_bisly.const import CAMERA_IMAGE_CACHE_WINDOW, CAMERA_IMAGE_URL, DOMAIN, MANUFACTURER, MODEL
from custom_components.hacs_bisly.data import DATA_STREAM_BRIDGE
from custom_components.hacs_bisly.utils import camera_slug
from homeassistant.components.camera import Camera, CameraEntityDescription, CameraEntityFeature
from homeassistant.helpers.device_registry import DeviceInfo

from . import webrtc

if TYPE_CHECKING:
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class _BislyCameraBase(Camera):
    """Shared Bisly camera behaviour: cloud snapshot and live-stream source."""

    _attr_has_entity_name = False
    _attr_is_streaming = False
    _attr_is_recording = False
    _attr_motion_detection_enabled = False
    _attr_supported_features: CameraEntityFeature = CameraEntityFeature.STREAM

    def __init__(self, coord, desc, cdata):
        super().__init__()
        self.entity_description = desc
        self.coordinator = coord
        self._cd = cdata
        self._cid = str(cdata.get("id", ""))
        self._cuid = cdata.get("camera_uuid", self._cid)
        self._attr_name = cdata.get("label", f"Camera {self._cid}")
        self._attr_unique_id = f"{coord.config_entry.entry_id}_camera_{self._cid}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coord.config_entry.entry_id}_camera_{self._cid}")},
            name=self._attr_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coord.config_entry.entry_id),
        )
        self._srv = coord.config_entry.runtime_data.client.server_id

    @property
    def _cli(self) -> Any:
        return self.coordinator.config_entry.runtime_data.client

    def _resolve_cuid(self) -> str:
        """Resolve the camera UUID from the coordinator data.

        The coordinator refreshes the camera list; use the latest UUID,
        falling back to the value captured at entity creation.
        """
        for cam in (self.coordinator.data or {}).get("cameras", []):
            if isinstance(cam, dict) and str(cam.get("id", "")) == self._cid:
                return str(cam.get("camera_uuid") or cam.get("id") or self._cid)
        return self._cuid

    async def async_camera_image(self, width=None, height=None):
        cuid = self._resolve_cuid()
        url = CAMERA_IMAGE_URL.format(server_id=self._srv, camera_id=cuid)
        url += f"?cb={math.floor(time.time() / CAMERA_IMAGE_CACHE_WINDOW)}"
        try:
            async with self._cli.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
        except aiohttp.ClientError, TimeoutError:
            pass
        return None

    async def stream_source(self) -> str | None:
        """Return the RTSP source of the stream.

        Only used when the camera has no native WebRTC implementation (i.e.
        aiortc is unavailable). Points at the ``bisly-stream-bridge`` app's
        per-camera RTSP endpoint so Home Assistant's go2rtc/HLS pipeline can
        stream it. Returns None when the bridge has not registered itself.
        """
        info = self.hass.data.get(DATA_STREAM_BRIDGE)
        if info is None:
            return None
        url = info.rtsp_url(self._cid)
        if url is None:
            # The bridge's camera list did not cover this id — derive the path
            # from the label, mirroring the app's own slugification.
            url = f"rtsp://{info.host}:{info.port}/{camera_slug(self._attr_name or f'camera_{self._cid}')}"
        return url


class BislyCamera(webrtc.WebRTCNativeMixin, _BislyCameraBase):
    """Bisly camera.

    Streams natively through an in-process aiortc peer connection when aiortc
    is importable; otherwise it exposes ``stream_source()`` so Home Assistant's
    go2rtc/HLS pipeline can stream the bridge app's RTSP.
    """


async def async_setup_entry(hass: HomeAssistant, entry: BislyConfigEntry, aae: AddEntitiesCallback) -> None:
    """Set up one Bisly camera entity per discovered camera."""
    cdata = (entry.runtime_data.coordinator.data or {}).get("cameras", [])
    aae(
        [
            BislyCamera(
                entry.runtime_data.coordinator,
                CameraEntityDescription(key=f"camera_{d.get('id', '')}", translation_key="bisly_camera"),
                d,
            )
            for d in cdata
            if d.get("id")
        ]
    )
