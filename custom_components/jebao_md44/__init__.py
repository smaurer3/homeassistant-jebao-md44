"""The Jebao MD-4.4 integration — local control over the LAN."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_DID,
    CONF_HOST,
    CONF_MAC,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MD44_CHANNEL_COUNT,
    MODEL_MD44,
    cal_factor,
)
from .md44 import MD44Device, MD44Error, ScheduleEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the MD-4.4 doser from a config entry."""
    host = entry.data.get(CONF_HOST)
    if not host:
        # Entries created by 1.x stored Gizwits cloud credentials and no
        # address. There is nothing to migrate them to, so say so plainly
        # instead of failing with a KeyError.
        raise ConfigEntryError(
            "This pump was added by version 1.x, which used the Gizwits cloud. "
            "Version 2 controls the pump locally: please delete this entry and "
            "add the integration again. No credentials are needed."
        )
    did = entry.data.get(CONF_DID)
    device_id = entry.data.get("device_id") or did or host
    mac_address = entry.data.get(CONF_MAC)
    firmware_version = entry.data.get("firmware_version")

    _LOGGER.info("Setting up Jebao MD-4.4 at %s", host)

    device = MD44Device(host=host, device_id=device_id, did=did)
    try:
        await device.connect(timeout=15.0)
    except MD44Error as err:
        await device.disconnect()
        raise ConfigEntryNotReady(f"Cannot reach pump at {host}: {err}") from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "device": device,
        "host": host,
        "device_id": device_id,
        "model": MODEL_MD44,
        "mac_address": mac_address,
        "firmware_version": firmware_version,
        # The schedule-slot services read entry.options for the 10x
        # precision toggle when converting real mL to raw bytes.
        "entry": entry,
        # Default the Calibration-amount slot before platforms load so the
        # paired "Value to enter in app" sensor never reads an empty bucket
        # during startup. The number entity overwrites this from its
        # restored state in async_added_to_hass.
        "dose_input": 1.0,
    }

    # Build the coordinator before forwarding to platforms so they all share
    # one. The "platform creates it if missing" pattern was racy:
    # async_forward_entry_setups runs platforms concurrently, they all see
    # "no coordinator yet", and each spins up its own.
    from .coordinator import JebaoDataUpdateCoordinator

    coordinator = JebaoDataUpdateCoordinator(
        hass, device, entry, device_id,
        entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


_QTY_ML = vol.All(
    vol.Any(int, float, vol.Coerce(float)),
    vol.Range(min=0.0, max=25.5),
)
_HOUR = vol.All(int, vol.Range(min=0, max=23))
_MINUTE = vol.All(int, vol.Range(min=0, max=59))
_CHANNEL = vol.All(int, vol.Range(min=1, max=MD44_CHANNEL_COUNT))
_SLOT = vol.All(int, vol.Range(min=1, max=24))


_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("entries"): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("hour"): _HOUR,
                        vol.Required("minute"): _MINUTE,
                        vol.Required("quantity_ml"): _QTY_ML,
                    }
                )
            ],
        ),
    }
)

_SET_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        # 1-based across the visible (non-empty) entries of the schedule.
        # Slot 1 = the entry shown first in the text entity. Slots beyond
        # the current count append a new entry (provided qty > 0).
        vol.Required("slot"): _SLOT,
        vol.Required("hour"): _HOUR,
        vol.Required("minute"): _MINUTE,
        vol.Required("quantity_ml"): _QTY_ML,
    }
)

_DELETE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("slot"): _SLOT,
    }
)

_GET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
    }
)

_GET_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("channel"): _CHANNEL,
        vol.Required("slot"): _SLOT,
    }
)


def _find_doser(hass: HomeAssistant, target: str):
    """Resolve a service call's device target to ``(device, config_entry)``.

    The device picker passes an HA device-registry ID, so we pull our
    domain's identifier out of the matched device and look up the stored
    MD44Device. Returns ``(None, None)`` if nothing matches.
    """
    registry = dr.async_get(hass)
    ha_device = registry.async_get(target)
    if ha_device is None:
        return None, None
    our_id: str | None = None
    for domain, identifier in ha_device.identifiers:
        if domain == DOMAIN:
            our_id = identifier
            break
    if our_id is None:
        return None, None
    for stored in hass.data.get(DOMAIN, {}).values():
        if stored.get("device_id") == our_id and isinstance(
            stored.get("device"), MD44Device
        ):
            return stored["device"], stored.get("entry")
    return None, None


def _ml_to_raw(quantity_ml: float, entry) -> int:
    """Convert real mL into the raw byte the firmware stores, applying the
    10x precision toggle if it is on for this entry."""
    factor = cal_factor(entry.options) if entry is not None else 1
    raw = int(round(float(quantity_ml) * factor))
    return max(0, min(255, raw))


def _raw_to_ml(raw: int, entry) -> float:
    """Inverse of ``_ml_to_raw``."""
    factor = cal_factor(entry.options) if entry is not None else 1
    return raw / factor if factor != 1 else float(raw)


def _entry_to_dict(slot: int, entry_obj, config_entry) -> dict:
    return {
        "slot": slot,
        "time": f"{entry_obj.hour:02d}:{entry_obj.minute:02d}",
        "hour": entry_obj.hour,
        "minute": entry_obj.minute,
        "quantity_ml": _raw_to_ml(entry_obj.quantity, config_entry),
    }


def _push_schedule_update(
    hass: HomeAssistant,
    device: MD44Device,
    channel_idx: int,
    entries: list,
) -> None:
    """Mirror a just-written schedule into the coordinator so the schedule
    text entity and the next-dose / count sensors re-render immediately
    rather than waiting for the pump's own report."""
    if device.state is not None:
        device.state.schedules[channel_idx] = list(entries)
    for stored in hass.data.get(DOMAIN, {}).values():
        if stored.get("device") is device:
            coord = stored.get("coordinator")
            if coord is not None and device.state is not None:
                coord.async_set_updated_data({"state": device.state})
            return


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "set_schedule"):
        return

    async def _handle_set_schedule(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("set_schedule: no MD-4.4 found with device_id %s", target_id)
            return
        entries = [
            ScheduleEntry(
                hour=e["hour"],
                minute=e["minute"],
                quantity=_ml_to_raw(e["quantity_ml"], entry),
            )
            for e in call.data["entries"]
        ]
        await device.set_schedule(channel - 1, entries)
        _push_schedule_update(hass, device, channel - 1, entries)

    async def _handle_set_slot(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_idx = int(call.data["slot"]) - 1
        hour = int(call.data["hour"])
        minute = int(call.data["minute"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("set_schedule_slot: no MD-4.4 found with device_id %s", target_id)
            return
        raw_qty = _ml_to_raw(call.data["quantity_ml"], entry)
        # device.state is kept current by the pump's own status reports, so
        # no pre-write read is needed.
        if device.state is None:
            _LOGGER.error("set_schedule_slot: device state not ready yet")
            return
        entries = list(device.state.schedules[channel - 1])
        new_entry = ScheduleEntry(hour=hour, minute=minute, quantity=raw_qty)
        if slot_idx < len(entries):
            if raw_qty == 0:
                # qty=0 means "delete this slot" so there is one service to
                # remember for normal edits.
                entries.pop(slot_idx)
            else:
                entries[slot_idx] = new_entry
        elif raw_qty > 0:
            if len(entries) >= 24:
                _LOGGER.error(
                    "set_schedule_slot: channel %d already has the max 24 entries",
                    channel,
                )
                return
            entries.append(new_entry)
        else:
            return  # past the end and qty=0 -> nothing to do
        await device.set_schedule(channel - 1, entries)
        _push_schedule_update(hass, device, channel - 1, entries)

    async def _handle_delete_slot(call: ServiceCall) -> None:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_idx = int(call.data["slot"]) - 1
        device, _entry = _find_doser(hass, target_id)
        if device is None:
            _LOGGER.error("delete_schedule_slot: no MD-4.4 found with device_id %s", target_id)
            return
        if device.state is None:
            _LOGGER.error("delete_schedule_slot: device state not ready yet")
            return
        entries = list(device.state.schedules[channel - 1])
        if slot_idx >= len(entries):
            _LOGGER.warning(
                "delete_schedule_slot: channel %d has only %d entries; nothing at slot %d",
                channel, len(entries), slot_idx + 1,
            )
            return
        entries.pop(slot_idx)
        await device.set_schedule(channel - 1, entries)
        _push_schedule_update(hass, device, channel - 1, entries)

    async def _handle_get_schedule(call: ServiceCall) -> dict:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            raise HomeAssistantError(
                f"get_schedule: no MD-4.4 found for device {target_id}"
            )
        if device.state is None:
            try:
                await device.update()
            except MD44Error as err:
                raise HomeAssistantError(f"get_schedule: read failed: {err}") from err
        entries = device.state.schedules[channel - 1]
        return {
            "channel": channel,
            "factor": cal_factor(entry.options) if entry is not None else 1,
            "entry_count": len(entries),
            "entries": [
                _entry_to_dict(i, e, entry) for i, e in enumerate(entries, start=1)
            ],
        }

    async def _handle_get_slot(call: ServiceCall) -> dict:
        target_id = call.data["device_id"]
        channel = int(call.data["channel"])
        slot_1based = int(call.data["slot"])
        device, entry = _find_doser(hass, target_id)
        if device is None:
            raise HomeAssistantError(
                f"get_schedule_slot: no MD-4.4 found for device {target_id}"
            )
        if device.state is None:
            try:
                await device.update()
            except MD44Error as err:
                raise HomeAssistantError(
                    f"get_schedule_slot: read failed: {err}"
                ) from err
        entries = device.state.schedules[channel - 1]
        if slot_1based - 1 >= len(entries):
            # Present-but-empty so automations can branch on ``exists``
            # instead of catching an error.
            return {"channel": channel, "slot": slot_1based, "exists": False}
        result = _entry_to_dict(slot_1based, entries[slot_1based - 1], entry)
        result["channel"] = channel
        result["exists"] = True
        return result

    hass.services.async_register(
        DOMAIN, "set_schedule", _handle_set_schedule, schema=_SET_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "set_schedule_slot", _handle_set_slot, schema=_SET_SLOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "delete_schedule_slot", _handle_delete_slot, schema=_DELETE_SLOT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, "get_schedule", _handle_get_schedule,
        schema=_GET_SCHEDULE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, "get_schedule_slot", _handle_get_slot,
        schema=_GET_SLOT_SCHEMA, supports_response=SupportsResponse.ONLY,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["device"].disconnect()
        _LOGGER.info("Disconnected from Jebao MD-4.4 at %s", data["host"])
    return unload_ok


def get_device_info(entry: ConfigEntry) -> DeviceInfo:
    device_id = entry.data.get("device_id", "unknown")
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=entry.title,
        manufacturer="Jebao",
        model=MODEL_MD44,
    )
