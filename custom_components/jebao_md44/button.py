"""Button platform for the Jebao MD-4.4 doser.

Exposes one button: ``Sync clock`` — writes HA's wall-clock time to the
pump's MCU. The pump's schedules fire against its own clock, so without
a sync it can drift far from real time.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity
from .md44 import MD44Error

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MD-4.4 buttons from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    device = data["device"]
    device_id = data["device_id"]
    model = data["model"]
    host = data["host"]
    mac_address = data.get("mac_address")
    firmware_version = data.get("firmware_version")
    coordinator = data["coordinator"]

    async_add_entities(
        [
            MD44SyncTimeButton(coordinator, device_id, model, host, device, mac_address, firmware_version),
        ]
    )


class MD44SyncTimeButton(JebaoEntity, ButtonEntity):
    """Push the local wall clock to the MD-4.4."""

    _attr_translation_key = "sync_time"
    _attr_icon = "mdi:clock-edit-outline"

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        device,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._device = device
        self._attr_unique_id = f"{device_id}_sync_time"
        self._attr_name = "Sync clock"

    async def async_press(self) -> None:
        try:
            await self._device.sync_time()
            await self.coordinator.async_request_refresh()
            _LOGGER.info("MD-4.4 clock synced")
        except MD44Error as err:
            _LOGGER.error("Failed to sync clock: %s", err)
