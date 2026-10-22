"""Config flow for the Jebao MD-4.4 doser (local control).

Setup is entirely local: we broadcast for pumps on the LAN and let the user
pick one, falling back to manual IP entry if discovery finds nothing (common
when Home Assistant sits on a different VLAN to the pump). No account or
cloud connectivity is required at any point.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_DEVICE_ID,
    CONF_DID,
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODEL_MD44,
    SUPPORTED_PRODUCT_KEYS,
)
from .discovery import DiscoveredPump, async_discover
from .md44 import MD44Device, MD44Error

_LOGGER = logging.getLogger(__name__)


async def _probe(host: str) -> MD44Device:
    """Connect to a candidate host and confirm it answers as an MD-4.4."""
    device = MD44Device(host=host)
    try:
        await device.connect(timeout=10.0)
        if device.state is None:
            raise MD44Error("pump did not report any state")
        return device
    except Exception:
        await device.disconnect()
        raise


class JebaoMD44ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Local-only config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, DiscoveredPump] = {}
        self._discovered: DiscoveredPump | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Broadcast for pumps, then offer a pick list (or manual entry)."""
        if user_input is not None:
            if user_input[CONF_HOST] == "__manual__":
                return await self.async_step_manual()
            pump = self._found[user_input[CONF_HOST]]
            return await self._create(pump.host, pump.did, pump.mac, pump.firmware)

        pumps = [p for p in await async_discover(timeout=4.0) if p.supported]
        configured = {e.unique_id for e in self._async_current_entries()}
        pumps = [p for p in pumps if p.did not in configured]
        if not pumps:
            return await self.async_step_manual()

        self._found = {p.did: p for p in pumps}
        options = [
            selector.SelectOptionDict(
                value=p.did, label=f"MD-4.4 at {p.host} ({p.mac_pretty})"
            )
            for p in pumps
        ]
        options.append(
            selector.SelectOptionDict(value="__manual__", label="Enter an IP manually")
        )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(pumps))},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual IP entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            device = None
            try:
                device = await _probe(host)
            except MD44Error as err:
                _LOGGER.debug("Probe of %s failed: %s", host, err)
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error probing %s", host)
                errors["base"] = "unknown"
            if device is not None:
                await device.disconnect()
                # Discovery gives us the DID/MAC; fall back to the host when
                # the pump could not be reached by broadcast.
                found = next(
                    (p for p in await async_discover(timeout=3.0) if p.host == host),
                    None,
                )
                return await self._create(
                    host,
                    found.did if found else host,
                    found.mac if found else None,
                    found.firmware if found else None,
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> FlowResult:
        """Recover a configured pump's address after a DHCP change."""
        mac = discovery_info.macaddress.lower().replace(":", "")
        for entry in self._async_current_entries():
            stored = (entry.data.get(CONF_MAC) or "").lower().replace(":", "")
            if stored and stored == mac:
                if entry.data.get(CONF_HOST) != discovery_info.ip:
                    _LOGGER.info(
                        "DHCP: updating %s address %s -> %s",
                        entry.title, entry.data.get(CONF_HOST), discovery_info.ip,
                    )
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_HOST: discovery_info.ip}
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_abort(reason="already_configured")
        return self.async_abort(reason="not_jebao_device")

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> FlowResult:
        """A background broadcast found a pump we do not have yet."""
        if discovery_info.get("product_key") not in SUPPORTED_PRODUCT_KEYS:
            return self.async_abort(reason="unsupported_model")
        self._discovered = DiscoveredPump(
            host=discovery_info["host"],
            did=discovery_info["did"],
            mac=discovery_info["mac"],
            firmware=discovery_info.get("firmware", ""),
            product_key=discovery_info["product_key"],
        )
        await self.async_set_unique_id(self._discovered.did)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: self._discovered.host}
        )
        self.context["title_placeholders"] = {"name": f"MD-4.4 ({self._discovered.host})"}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        assert self._discovered is not None
        pump = self._discovered
        if user_input is not None:
            return await self._create(pump.host, pump.did, pump.mac, pump.firmware)
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "host": pump.host,
                "mac": pump.mac_pretty,
                "firmware": pump.firmware or "unknown",
            },
        )

    async def _create(
        self, host: str, did: str, mac: str | None, firmware: str | None
    ) -> FlowResult:
        await self.async_set_unique_id(did)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return self.async_create_entry(
            title=f"MD-4.4 ({host})",
            data={
                CONF_HOST: host,
                CONF_DID: did,
                CONF_DEVICE_ID: did,
                CONF_MODEL: MODEL_MD44,
                CONF_MAC: mac,
                "firmware_version": firmware,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "JebaoMD44OptionsFlow":
        return JebaoMD44OptionsFlow()


class JebaoMD44OptionsFlow(config_entries.OptionsFlow):
    """Options: how often to poll as a backstop.

    The pump pushes changes as they happen, so this only covers a missed
    push or a silently dropped socket. It is all LAN traffic, so a short
    interval costs nothing externally.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "scan_interval",
                        default=self.config_entry.options.get(
                            "scan_interval", DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=30, max=3600)),
                }
            ),
        )
