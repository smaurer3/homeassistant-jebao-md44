"""Config flow for the Jebao MD-4.4 doser.

The pump's firmware doesn't accept LAN writes — control only works
through the Gizwits cloud — so the flow is two steps:

  1. Collect the user's Gizwits Aqua app email + password + region.
  2. Pick which doser bound to the account to add.

The same flow handles a future re-auth (e.g. if the user changes their
Gizwits password). HA will route them through ``async_step_user`` again.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_DEVICE_ID,
    CONF_DID,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
    DOMAIN,
    GIZWITS_REGIONS,
    MODEL_MD44,
)
from .gizwits_cloud import GizwitsAuthError, GizwitsCloudClient, GizwitsCloudError

_LOGGER = logging.getLogger(__name__)


class JebaoMD44ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Cloud-login config flow for the MD-4.4 doser."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._region: str = "us"
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: collect Gizwits credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._region = user_input.get(CONF_REGION, "us")

            session = async_get_clientsession(self.hass)
            client = GizwitsCloudClient(session, region=self._region)
            try:
                await client.login(self._username, self._password)
                self._devices = await client.get_bindings()
            except GizwitsAuthError:
                errors["base"] = "invalid_auth"
            except GizwitsCloudError as err:
                _LOGGER.error("Cloud error: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                return await self.async_step_pick()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_REGION, default="us"): vol.In(
                        list(GIZWITS_REGIONS.keys())
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: pick which doser on the account to add."""
        errors: dict[str, str] = {}

        if user_input is not None:
            did = user_input[CONF_DID]
            device = next((d for d in self._devices if d["did"] == did), None)
            if not device:
                errors["base"] = "device_not_found"
            else:
                await self.async_set_unique_id(did)
                self._abort_if_unique_id_configured()

                title = device.get("dev_alias") or device.get("product_name") or did
                return self.async_create_entry(
                    title=f"MD-4.4 ({title})",
                    data={
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_REGION: self._region,
                        CONF_DID: did,
                        CONF_DEVICE_ID: did,
                        CONF_MODEL: MODEL_MD44,
                        "mac_address": device.get("mac"),
                        "firmware_version": device.get("wifi_soft_version"),
                    },
                )

        if not self._devices:
            return self.async_abort(reason="no_devices_on_account")

        options = [
            selector.SelectOptionDict(
                value=d["did"],
                label=(
                    f"{d.get('dev_alias') or d.get('product_name') or 'Unknown'} "
                    f"({d['did']})"
                ),
            )
            for d in self._devices
        ]

        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "JebaoMD44OptionsFlow":
        return JebaoMD44OptionsFlow()


class JebaoMD44OptionsFlow(config_entries.OptionsFlow):
    """Options flow.

    Only useful knob right now is the fallback poll interval — used when
    the WebSocket subscription isn't currently connected. With WS up,
    the coordinator uses a much longer interval automatically.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        from .const import DEFAULT_SCAN_INTERVAL

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "scan_interval",
                        default=self.config_entry.options.get(
                            "scan_interval", DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                }
            ),
        )
