"""Binary sensor platform for the Jebao MD-4.4 doser."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MD-4.4 binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_id = data["device_id"]
    model = data["model"]
    host = data["host"]
    mac_address = data.get("mac_address")
    firmware_version = data.get("firmware_version")
    coordinator = data["coordinator"]

    async_add_entities(
        [
            MD44OpenCircuitSensor(coordinator, device_id, model, host, mac_address, firmware_version),
            MD44FaultUartSensor(coordinator, device_id, model, host, mac_address, firmware_version),
        ]
    )


class MD44OpenCircuitSensor(JebaoEntity, BinarySensorEntity):
    """Open-circuit alert from the MD-4.4 (one of the dosing motors lost
    drive)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "open_circuit"

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_open_circuit"
        self._attr_name = "Open circuit"

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data.get("state")
        return bool(state and state.open_circuit)


class MD44FaultUartSensor(JebaoEntity, BinarySensorEntity):
    """UART fault between the MD-4.4 MCU and the WiFi module."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "fault_uart"

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_fault_uart"
        self._attr_name = "UART fault"

    @property
    def is_on(self) -> bool:
        state = self.coordinator.data.get("state")
        return bool(state and state.fault_uart)
