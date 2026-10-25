"""Button platform for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XgimiEntity
from .const import DOMAIN, KEYCODES
from .coordinator import XgimiDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XGIMI remote key, wake, and restart buttons."""
    coordinator: XgimiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    key_buttons: list[ButtonEntity] = [
        XgimiKeyButton(coordinator, suffix, name, code, icon) for suffix, (code, name, icon) in KEYCODES.items()
    ]
    async_add_entities(
        [
            *key_buttons,
            XgimiWakeButton(coordinator),
            XgimiRestartButton(coordinator),
        ]
    )


class XgimiKeyButton(XgimiEntity, ButtonEntity):
    """Button entity that injects an Android or XGIMI custom keycode over ADB."""

    def __init__(
        self,
        coordinator: XgimiDataUpdateCoordinator,
        suffix: str,
        name: str,
        keycode: int,
        icon: str,
    ) -> None:
        """Initialize the key button."""
        super().__init__(coordinator, f"key_{suffix}")
        self._attr_name = name
        self._attr_icon = icon
        self._keycode = keycode

    async def async_press(self) -> None:
        """Send the configured keycode over ADB."""
        if not await self.coordinator.async_send_key(self._keycode):
            raise HomeAssistantError(f"Failed to send key '{self._attr_name}' ({self._keycode}) to projector")


class XgimiWakeButton(XgimiEntity, ButtonEntity):
    """Button entity to explicitly trigger the two-stage WoWLAN + ADB wake burst."""

    _attr_name = "Wake Projector"
    _attr_icon = "mdi:power-on"

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the wake button."""
        super().__init__(coordinator, "button_wake")

    async def async_press(self) -> None:
        """Trigger the two-stage wake sequence."""
        if not await self.coordinator.async_turn_on():
            raise HomeAssistantError(f"Failed to wake XGIMI projector at {self.coordinator.host}")


class XgimiRestartButton(XgimiEntity, ButtonEntity):
    """Button entity to reboot the projector via ADB shell."""

    _attr_name = "Restart Projector"
    _attr_icon = "mdi:restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the restart button."""
        super().__init__(coordinator, "button_restart")

    async def async_press(self) -> None:
        """Reboot the projector."""
        if not await self.coordinator.async_reboot():
            raise HomeAssistantError(f"Failed to restart XGIMI projector at {self.coordinator.host}")
