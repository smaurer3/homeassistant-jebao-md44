"""Data update coordinator for the Jebao MD-4.4 doser.

The pump pushes a status report over the LAN whenever anything changes, so
almost all updates arrive via ``MD44Device.on_push_state``. The polling
cycle here is only a backstop for a missed push or a silently dropped
socket; the device class runs its own keepalive and reconnect loop.
"""
from datetime import timedelta
import logging
from typing import Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_HOST, CONF_MAC, DEFAULT_SCAN_INTERVAL, DOMAIN
from .discovery import async_find_by_mac
from .md44 import MD44Device, MD44Error

_LOGGER = logging.getLogger(__name__)


class JebaoDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for one MD-4.4 doser."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: MD44Device,
        entry: ConfigEntry,
        device_id: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        self.device = device
        self.entry = entry
        self.device_id = device_id
        self._rediscovery_attempted = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

        device.on_push_state = self.push_state

    async def _async_update_data(self) -> dict:
        """Backstop poll. Also recovers a changed IP via LAN discovery."""
        try:
            if not self.device.is_connected:
                new_host = await self._try_rediscover()
                if new_host and new_host != self.device.host:
                    _LOGGER.info(
                        "MD-4.4 moved to %s; reconnecting", new_host
                    )
                    self.device.host = new_host
                    self.hass.config_entries.async_update_entry(
                        self.entry, data={**self.entry.data, CONF_HOST: new_host}
                    )
                    await self.device.disconnect()
                    await self.device.connect(timeout=10.0)

            state = await self.device.update()
            self._rediscovery_attempted = False
            return {"state": state}
        except MD44Error as err:
            raise UpdateFailed(f"Error communicating with pump: {err}") from err

    async def _try_rediscover(self) -> Optional[str]:
        """Broadcast for our pump by MAC, once per outage."""
        mac = self.entry.data.get(CONF_MAC)
        if not mac or self._rediscovery_attempted:
            return None
        self._rediscovery_attempted = True
        found = await async_find_by_mac(mac, timeout=4.0)
        return found.host if found else None

    def push_state(self, state) -> None:
        """Called by ``MD44Device.on_push_state`` when the pump reports a
        change. Rebinds ``self.data`` so every listening entity re-renders
        in the same tick, exactly as the polling path does."""
        self.async_set_updated_data({"state": state})
