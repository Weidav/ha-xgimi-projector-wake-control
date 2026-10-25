"""The Standalone XGIMI Projector Wake & Control Home Assistant Integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOST, DEFAULT_HOST, DEFAULT_NAME, DOMAIN
from .coordinator import XgimiDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up XGIMI Projector from a config entry."""
    host = entry.options.get(CONF_HOST) or entry.data.get(CONF_HOST, DEFAULT_HOST)
    name = entry.data.get(CONF_NAME, DEFAULT_NAME)

    coordinator = XgimiDataUpdateCoordinator(hass, host, name, config_entry=entry)

    # Refresh data, but do not fail setup if projector is off in standby
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        _LOGGER.debug("XGIMI projector at %s is currently in standby during setup", host)
        coordinator.data = coordinator.default_state(online=False, awake=False)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and clean up coordinator timers and ADB sockets."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: XgimiDataUpdateCoordinator | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.async_shutdown()
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    coordinator: XgimiDataUpdateCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.check_and_reset_suppress_reload():
        return
    await hass.config_entries.async_reload(entry.entry_id)


class XgimiEntity(CoordinatorEntity[XgimiDataUpdateCoordinator]):
    """Base entity for XGIMI Projector."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: XgimiDataUpdateCoordinator, unique_suffix: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id if coordinator.config_entry is not None else coordinator.host
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"

    @property
    def device_info(self) -> DeviceInfo:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return device registry information for this XGIMI projector."""
        data = self.coordinator.data or {}
        connections: set[tuple[str, str]] = set()
        if mac := (data.get("mac") or self.coordinator.mac):
            connections.add((CONNECTION_NETWORK_MAC, mac))

        model = data.get("model") or self.coordinator.device_name
        sw_ver = data.get("sw_version") or "14"

        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            connections=connections,
            name=self.coordinator.device_name,
            manufacturer="XGIMI",
            model=model,
            sw_version=f"Android {sw_ver}",
        )
