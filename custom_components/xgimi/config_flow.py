"""Config flow and Options flow for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback

from .adb_client import AdbError, XgimiAdbClient
from .const import (
    CAST_WAKE_PORT,
    CONF_ADB_PORT,
    CONF_HOST,
    CONF_MAC,
    DEFAULT_ADB_PORT,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DOMAIN,
    REMOTE_WAKE_PORT,
    WOL_DISCARD_PORT,
    WOL_WAKE_PORT,
)
from .coordinator import (
    create_magic_packet,
    format_mac,
    resolve_mac_from_arp,
    send_mdns_knock,
    send_tcp_knock,
    send_udp_knock,
)

_LOGGER = logging.getLogger(__name__)


def _validate_host(host: str) -> bool:
    """Validate configured host as hostname or IP literal without scheme, path or userinfo."""
    if not host or not host.strip():
        return False
    h = host.strip()
    return not ("://" in h or "/" in h or "@" in h or "?" in h or "#" in h or " " in h)


async def _send_setup_wowlan_knocks(hass: HomeAssistant, host: str, mac: str | None) -> None:
    """Send Stage-1 WoWLAN wake burst if projector is asleep during config flow setup."""
    magic_payload = create_magic_packet(mac) or (b"\xff" * 102)

    def _sync_udp() -> None:
        send_udp_knock(host, WOL_WAKE_PORT, payload=magic_payload, repeat=3)
        send_udp_knock(host, WOL_DISCARD_PORT, payload=magic_payload, repeat=2)
        send_udp_knock(host, CAST_WAKE_PORT, payload=magic_payload, repeat=2)
        send_udp_knock(host, REMOTE_WAKE_PORT, payload=magic_payload, repeat=2)
        send_mdns_knock(host)

    await hass.async_add_executor_job(_sync_udp)
    await asyncio.gather(
        send_tcp_knock(host, CAST_WAKE_PORT),
        send_tcp_knock(host, REMOTE_WAKE_PORT),
        send_tcp_knock(host, DEFAULT_ADB_PORT),
        return_exceptions=True,
    )


async def _try_read_adb_mac(adb: XgimiAdbClient) -> str | None:
    """Attempt to read wlan0 MAC address over an authorized ADB connection."""
    with contextlib.suppress(AdbError, TimeoutError, OSError):
        raw = await adb.async_shell("cat /sys/class/net/wlan0/address 2>/dev/null", timeout=3.0)
        for line in raw.splitlines():
            if (mac := format_mac(line)) and mac != "00:00:00:00:00:00":
                return mac
    return None


class XgimiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Standalone XGIMI Projector Wake & Control."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow state."""
        self._host: str = DEFAULT_HOST
        self._name: str = DEFAULT_NAME
        self._mac: str | None = None
        self._adb: XgimiAdbClient | None = None
        self._auth_task: asyncio.Task[bool] | None = None

    @callback
    def async_remove(self) -> None:
        """Clean up any pending ADB authorization task and socket if flow is closed/aborted."""
        if self._auth_task is not None and not self._auth_task.done():
            self._auth_task.cancel()
        self._auth_task = None
        if self._adb is not None:
            self._adb.close_transport_nowait()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> XgimiOptionsFlowHandler:
        """Get the options flow for this handler (modern zero-arg constructor)."""
        return XgimiOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle Step 1: Host IP, Name, optional MAC, and initial ADB probe."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            name = user_input.get(CONF_NAME, DEFAULT_NAME).strip() or DEFAULT_NAME
            raw_mac = user_input.get(CONF_MAC, "").strip()

            if not _validate_host(host):
                errors[CONF_HOST] = "invalid_host"

            mac: str | None = None
            if raw_mac:
                mac = format_mac(raw_mac)
                if mac is None:
                    errors[CONF_MAC] = "invalid_mac"

            if not errors:
                if not mac:
                    mac = await self.hass.async_add_executor_job(resolve_mac_from_arp, host)

                adb = XgimiAdbClient(host, port=DEFAULT_ADB_PORT, hass=self.hass)
                status = await adb.async_probe_status(timeout=3.0)

                if status == "unreachable":
                    # Wake network interface via WoWLAN if projector is in standby
                    await _send_setup_wowlan_knocks(self.hass, host, mac)
                    await asyncio.sleep(1.5)
                    status = await adb.async_probe_status(timeout=3.0)

                if status == "authorized":
                    if not mac:
                        mac = await _try_read_adb_mac(adb)
                    await adb.async_close()
                    return await self._async_finish_entry(host, name, mac)

                if status == "unauthorized":
                    self._host = host
                    self._name = name
                    self._mac = mac
                    self._adb = adb
                    return await self.async_step_authorize()

                await adb.async_close()
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST,
                    default=(user_input or {}).get(CONF_HOST, DEFAULT_HOST),
                ): str,
                vol.Optional(
                    CONF_NAME,
                    default=(user_input or {}).get(CONF_NAME, DEFAULT_NAME),
                ): str,
                vol.Optional(
                    CONF_MAC,
                    default=(user_input or {}).get(CONF_MAC, ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=cast(Any, schema),
            errors=errors,
        )

    async def async_step_authorize(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle Step 2: Interactive ADB authorization prompt on the projector screen."""
        errors: dict[str, str] = {}
        if self._adb is None:
            self._adb = XgimiAdbClient(self._host, port=DEFAULT_ADB_PORT, hass=self.hass)

        if user_input is None:
            # Trigger the on-screen ADB Allow dialog in the background while user views instructions
            if self._auth_task is None or self._auth_task.done():
                self._auth_task = self.hass.async_create_task(self._adb.async_authorize(timeout=30.0))
            return self.async_show_form(
                step_id="authorize",
                data_schema=cast(Any, vol.Schema({})),
                description_placeholders={"host": self._host},
            )

        authorized = False
        if self._auth_task is not None:
            if self._auth_task.done():
                with contextlib.suppress(Exception):
                    authorized = bool(self._auth_task.result())
            else:
                # Wait up to 5s for the in-flight authorization prompt or cancel and verify
                with contextlib.suppress(TimeoutError, Exception):
                    authorized = await asyncio.wait_for(asyncio.shield(self._auth_task), timeout=5.0)
                if not self._auth_task.done():
                    self._auth_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._auth_task
            self._auth_task = None

        if not authorized:
            authorized = await self._adb.async_authorize(timeout=10.0)

        if authorized:
            if not self._mac:
                self._mac = await _try_read_adb_mac(self._adb)
            await self._adb.async_close()
            return await self._async_finish_entry(self._host, self._name, self._mac)

        # Re-trigger background authorization prompt for the next attempt
        self._auth_task = self.hass.async_create_task(self._adb.async_authorize(timeout=30.0))
        errors["base"] = "adb_not_authorized"
        return self.async_show_form(
            step_id="authorize",
            data_schema=cast(Any, vol.Schema({})),
            errors=errors,
            description_placeholders={"host": self._host},
        )

    async def _async_finish_entry(self, host: str, name: str, mac: str | None) -> ConfigFlowResult:
        """Set unique ID and create the ConfigEntry."""
        unique_id = mac or host.lower()
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        data: dict[str, Any] = {
            CONF_HOST: host,
            CONF_NAME: name,
            CONF_ADB_PORT: DEFAULT_ADB_PORT,
        }
        if mac:
            data[CONF_MAC] = mac

        return self.async_create_entry(title=name, data=data)


class XgimiOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for XGIMI Projector Wake & Control using HA's modern zero-arg pattern."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the XGIMI Projector options (host & MAC address)."""
        errors: dict[str, str] = {}

        current_host = self.config_entry.options.get(CONF_HOST, self.config_entry.data.get(CONF_HOST, DEFAULT_HOST))
        current_mac = self.config_entry.options.get(CONF_MAC, self.config_entry.data.get(CONF_MAC, ""))

        if user_input is not None:
            host = user_input.get(CONF_HOST, "").strip()
            raw_mac = user_input.get(CONF_MAC, "").strip()

            if not _validate_host(host):
                errors[CONF_HOST] = "invalid_host"

            mac: str | None = None
            if raw_mac:
                mac = format_mac(raw_mac)
                if mac is None:
                    errors[CONF_MAC] = "invalid_mac"

            if not errors:
                if not mac:
                    mac = await self.hass.async_add_executor_job(resolve_mac_from_arp, host)
                new_data = {**self.config_entry.data, CONF_HOST: host}
                if mac:
                    new_data[CONF_MAC] = mac
                elif CONF_MAC in new_data and not raw_mac:
                    new_data.pop(CONF_MAC, None)

                options_data: dict[str, Any] = {CONF_HOST: host}
                if mac:
                    options_data[CONF_MAC] = mac

                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data, options=options_data)
                return self.async_create_entry(title="", data=options_data)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_HOST,
                    default=(user_input or {}).get(CONF_HOST, current_host),
                ): str,
                vol.Optional(
                    CONF_MAC,
                    default=(user_input or {}).get(CONF_MAC, current_mac or ""),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=cast(Any, schema),
            errors=errors,
        )
