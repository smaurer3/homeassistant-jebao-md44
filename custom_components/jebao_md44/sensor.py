"""Sensor platform for the Jebao MD-4.4 doser."""
from __future__ import annotations

import datetime as dt
import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MD44_CHANNEL_COUNT,
    cal_factor,
    signal_cal_factor_changed,
    signal_dose_input_changed,
)
from .coordinator import JebaoDataUpdateCoordinator
from .entity import JebaoEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MD-4.4 sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    device_id = data["device_id"]
    model = data["model"]
    host = data["host"]
    mac_address = data.get("mac_address")
    firmware_version = data.get("firmware_version")
    coordinator = data["coordinator"]

    entities: list[SensorEntity] = [
        MD44CalibrationChannelSensor(coordinator, device_id, model, host, mac_address, firmware_version),
        MD44Calib1Sensor(coordinator, device_id, model, host, mac_address, firmware_version),
        MD44ClockSensor(coordinator, device_id, model, host, mac_address, firmware_version),
        MD44DoseAppValueSensor(coordinator, device_id, model, host, entry, mac_address, firmware_version),
    ]
    for idx in range(MD44_CHANNEL_COUNT):
        entities.append(
            MD44ScheduleCountSensor(coordinator, device_id, model, host, idx, mac_address, firmware_version)
        )
        entities.append(
            MD44NextScheduleSensor(coordinator, device_id, model, host, idx, mac_address, firmware_version)
        )
    async_add_entities(entities)


class MD44CalibrationChannelSensor(JebaoEntity, SensorEntity):
    """Which channel is currently armed for calibration (1..4).

    The cloud returns the value as a localized string like ``"校准1"``
    (Chinese for "Calibration 1") or sometimes the English equivalent
    depending on account locale. We extract the trailing digit so the
    sensor surfaces a clean integer 1..4.
    """

    _attr_translation_key = "calibration_channel"
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_cal_channel"
        self._attr_name = "Calibration channel"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None or not state.cal_set:
            return None
        import re
        match = re.search(r"(\d+)", state.cal_set)
        return int(match.group(1)) if match else None


class MD44Calib1Sensor(JebaoEntity, SensorEntity):
    """The currently-stored calibration value (10..100)."""

    _attr_translation_key = "calib1"
    _attr_icon = "mdi:cup-water"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_calib1"
        self._attr_name = "Calibration value"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return state.calib1


class MD44ClockSensor(JebaoEntity, SensorEntity):
    """The clock value last written to the pump.

    ``YMDData`` / ``HMSData`` are 4-byte fields laid out as
    ``[20, YY, MM, DD]`` and ``[0, HH, MM, SS]`` — byte 0 of the date is
    the century and byte 0 of the time is reserved (it is not a weekday).

    These are a write-only latch: they hold whatever was last written and
    do not tick, so this shows *when the clock was last set* (by this
    integration or by the official app), not the pump's current time. The
    pump's running clock is internal and not exposed. All zeros means the
    clock has never been set, which shows as "unset".
    """

    _attr_translation_key = "clock"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, device_id, model, host, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._attr_unique_id = f"{device_id}_clock"
        self._attr_name = "Device clock"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        ymd_hex = state.ymd or ""
        hms_hex = state.hms or ""
        if len(ymd_hex) < 8 or len(hms_hex) < 8:
            return None
        if ymd_hex == "00000000" and hms_hex == "00000000":
            return "unset"
        try:
            ymd = bytes.fromhex(ymd_hex)
            hms = bytes.fromhex(hms_hex)
        except ValueError:
            return None
        if len(ymd) < 4 or len(hms) < 4:
            return None
        year = ymd[0] * 100 + ymd[1]
        return (
            f"{year:04d}-{ymd[2]:02d}-{ymd[3]:02d} "
            f"{hms[1]:02d}:{hms[2]:02d}:{hms[3]:02d}"
        )


class MD44ScheduleCountSensor(JebaoEntity, SensorEntity):
    """Number of programmed schedule entries on one channel."""

    _attr_icon = "mdi:calendar-clock"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id, model, host, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._idx = idx
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_sched_count_{ch}"
        self._attr_name = f"Channel {ch} schedules"
        self._attr_translation_key = f"sched_count_{ch}"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        return len(state.schedules[self._idx])


class MD44NextScheduleSensor(JebaoEntity, SensorEntity):
    """The next-up schedule entry for one channel, as HH:MM (Qmg)."""

    _attr_icon = "mdi:clock-time-four-outline"

    def __init__(self, coordinator, device_id, model, host, idx, mac_address=None, firmware_version=None) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._idx = idx
        ch = idx + 1
        self._attr_unique_id = f"{device_id}_next_sched_{ch}"
        self._attr_name = f"Channel {ch} next dose"
        self._attr_translation_key = f"next_sched_{ch}"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        if state is None:
            return None
        entries = state.schedules[self._idx]
        if not entries:
            return None
        # Sort by time-of-day so we always pick the chronologically next one,
        # not the next one in the order the user added them in the app.
        sorted_entries = sorted(entries, key=lambda e: (e.hour, e.minute))
        now = dt.datetime.now().time()
        upcoming = [e for e in sorted_entries if (e.hour, e.minute) >= (now.hour, now.minute)]
        nxt = upcoming[0] if upcoming else sorted_entries[0]
        suffix = "" if upcoming else " (tomorrow)"
        return f"{nxt.hour:02d}:{nxt.minute:02d} ({nxt.quantity_ml:g} mL){suffix}"


class MD44DoseAppValueSensor(JebaoEntity, SensorEntity):
    """Companion to ``MD44DoseInputNumber`` (number.py).

    Reads the "Desired dose" number entity that lives on this same device
    and multiplies it by the active calibration factor. With the 10x
    toggle on, this is the integer you should type into the Jebao app's
    schedule field to get the actual mL volume you typed into the input.

    Examples (factor=10):
        Desired dose = 1.4 → Required app value = 14
        Desired dose = 0.3 → Required app value = 3

    With the 10x toggle off, this just mirrors the input verbatim.
    """

    _attr_translation_key = "dose_app_value"
    _attr_icon = "mdi:calculator-variant-outline"
    # Sensors live under Diagnostic; EntityCategory.CONFIG only applies to
    # entities the user changes (numbers, switches, selects). On recent HA
    # builds the frontend hides the state of CONFIG-category sensors as
    # "Unavailable" because semantically they aren't supposed to have one —
    # the value was rendering blank as a result. Keep it Diagnostic; the
    # paired Configuration card still shows the Calibration amount input
    # right above the device's Diagnostic block on the device page.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "mL"

    @property
    def available(self) -> bool:
        # Pure derived value — never depends on the coordinator's last
        # update, so don't inherit CoordinatorEntity's availability check.
        return True

    def __init__(
        self,
        coordinator: JebaoDataUpdateCoordinator,
        device_id: str,
        model: str,
        host: str,
        entry,
        mac_address: str | None = None,
        firmware_version: str | None = None,
    ) -> None:
        super().__init__(coordinator, device_id, model, host, mac_address, firmware_version)
        self._entry = entry
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_dose_app_value"
        self._attr_name = "Calibration amount to enter in app"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Re-render immediately on either of: Calibration-amount changing,
        # or the 10x precision toggle flipping — both change what we'd
        # display without touching the coordinator's data.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_dose_input_changed(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_cal_factor_changed(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )

    def _desired_ml(self) -> float:
        """The current value of the paired Calibration-amount input.

        Falls back to 1.0 if the number entity hasn't published yet (race
        on first paint) or the bucket got cleared by reload. That matches
        the input's own default so the sensor never has to show "unknown"
        on first render.
        """
        bucket = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        value = bucket.get("dose_input", 1.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    @property
    def native_value(self) -> float:
        desired = self._desired_ml()
        factor = cal_factor(self._entry.options)
        # Factor=1: identity (user already enters whole mL into the app).
        # Factor=10: scale up so the user can type a fractional desired mL
        # and read the integer they need to type into the app.
        value = desired * factor
        # Keep one decimal in case factor=1 (we never get here non-int with
        # factor=10 because desired * 10 of a 0.1-step value is whole).
        return round(value, 1) if value != int(value) else int(value)

    @property
    def extra_state_attributes(self) -> dict:
        factor = cal_factor(self._entry.options)
        desired = self._desired_ml()
        return {
            "factor": factor,
            "desired_ml": desired,
            "hint": (
                f"10x mode is {'ON' if factor == 10 else 'OFF'}. "
                "Type the number above into the Jebao app's schedule (or the "
                "Channel N schedule text in HA) to actually dispense the "
                "'Desired dose' you set."
            ),
        }
