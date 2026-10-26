"""Select platform for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XgimiEntity
from .const import DOMAIN, INPUT_SOURCE_APPS, INPUT_SOURCES
from .coordinator import XgimiDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the XGIMI Input Source select entity."""
    coordinator: XgimiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([XgimiInputSourceSelect(coordinator)])


class XgimiInputSourceSelect(XgimiEntity, SelectEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Input source select entity (Apps, HDMI 1, HDMI 2)."""

    _attr_name = "Input Source"
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the input source select entity."""
        super().__init__(coordinator, "select_input_source")
        self._attr_options = list(INPUT_SOURCES)

    @property
    def current_option(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return the currently active input source."""
        if not self.coordinator.data:
            return INPUT_SOURCE_APPS
        current = self.coordinator.data.get("input_source")
        return current if current in INPUT_SOURCES else None

    async def async_select_option(self, option: str) -> None:
        """Switch projector input source via ADB intent."""
        if option not in INPUT_SOURCES:
            raise HomeAssistantError(f"Unsupported input source: {option}")
        if not await self.coordinator.async_set_input_source(option):
            raise HomeAssistantError(f"Failed to switch XGIMI projector input source to {option}")
