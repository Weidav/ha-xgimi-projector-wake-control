"""Switch platform for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XgimiEntity
from .const import DOMAIN
from .coordinator import XgimiDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the XGIMI Power Switch."""
    coordinator: XgimiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([XgimiPowerSwitch(coordinator)])


class XgimiPowerSwitch(XgimiEntity, SwitchEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Representation of the XGIMI Projector Power Switch (Two-Stage Wake & Idempotent Off)."""

    _attr_name = "Power"
    _attr_icon = "mdi:projector"

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the power switch."""
        super().__init__(coordinator, "power")

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Keep the power switch available so WoWLAN wake can be triggered from deep sleep."""
        return True

    @property
    def is_on(self) -> bool | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return True if the projector display is awake, None if unknown."""
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("awake")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the projector on via Two-Stage WoWLAN + ADB Wake."""
        if not await self.coordinator.async_turn_on():
            raise HomeAssistantError(f"Failed to wake XGIMI projector at {self.coordinator.host}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Put the projector into standby idempotently via ADB."""
        if not await self.coordinator.async_turn_off():
            raise HomeAssistantError(f"Failed to turn off XGIMI projector at {self.coordinator.host}")
