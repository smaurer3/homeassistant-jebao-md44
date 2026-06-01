"""Data update coordinator for the Jebao MD-4.4 doser.

Most of the work is done by the WebSocket push subscription started in
``MD44Device.connect``; the polling cycle here is a slow safety net for
the case where the socket drops.
"""
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .md44 import MD44Device, MD44Error

# When the WebSocket push subscription is live, the coordinator only needs
# to poll occasionally as a safety net — pushes carry state in near-real
# time. Five minutes is a comfortable heartbeat.
PUSH_FALLBACK_POLL_SECONDS = 300

_LOGGER = logging.getLogger(__name__)


class JebaoDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for one MD-4.4 doser.

    Behaviour switches between two intervals based on whether the WS
    subscription is currently connected:
      * WS up: poll every 5 minutes (safety net only — pushes carry the
        actual state changes).
      * WS down: fall back to the user-configured ``scan_interval``
        (default 30 s) so the integration keeps working even if the
        socket is wedged.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device: MD44Device,
        entry: ConfigEntry,
        device_id: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize coordinator."""
        self.device = device
        self.entry = entry
        self.device_id = device_id

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

        # The WS client started inside MD44Device fires this callback
        # whenever ``s2c_noti`` arrives — we forward to
        # async_set_updated_data so every listener entity re-renders in
        # the same tick.
        device.on_push_state = self.push_md44_state

    async def _async_update_data(self) -> dict:
        """Fetch the current state via REST as a fallback / safety net."""
        try:
            if not self.device.is_connected:
                _LOGGER.warning("Connection lost, attempting to reconnect...")
                try:
                    await self.device.connect(timeout=5.0)
                    _LOGGER.info("Reconnected successfully")
                except MD44Error as err:
                    raise UpdateFailed(f"Failed to reconnect: {err}") from err

            state = await self.device.update()
            # If the WebSocket subscription is live the next poll can
            # wait much longer — pushes carry the truth in the meantime.
            # If it isn't, stay on the user-configured interval as a
            # working fallback.
            if self.device.ws_connected:
                self.update_interval = timedelta(
                    seconds=PUSH_FALLBACK_POLL_SECONDS
                )
            else:
                self.update_interval = timedelta(
                    seconds=self.entry.options.get(
                        "scan_interval", DEFAULT_SCAN_INTERVAL
                    )
                )
            return {"state": state}

        except MD44Error as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    def push_md44_state(self, state) -> None:
        """Called by ``MD44Device.on_push_state`` when a ``s2c_noti`` push
        message arrives over the WebSocket. Rebinds ``self.data`` so all
        listening entities re-render against the new state in the same
        tick, exactly like the regular polling path does."""
        self.async_set_updated_data({"state": state})
