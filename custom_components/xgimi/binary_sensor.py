"""Binary sensor platform for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,  # pyright: ignore[reportPrivateImportUsage]
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import XgimiEntity
from .const import DOMAIN
from .coordinator import XgimiDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the XGIMI binary sensors."""
    coordinator: XgimiDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            XgimiPowerBinarySensor(coordinator),
            XgimiConnectivityBinarySensor(coordinator),
        ]
    )


class XgimiPowerBinarySensor(XgimiEntity, BinarySensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Binary sensor reporting whether the projector display is Awake."""

    _attr_name = "Power State"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the power state binary sensor."""
        super().__init__(coordinator, "power_state")

    @property
    def is_on(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return True when the projector is Awake (display on)."""
        if not self.coordinator.data:
            return False
        return bool(self.coordinator.data.get("awake", False))


class XgimiConnectivityBinarySensor(XgimiEntity, BinarySensorEntity):  # pyright: ignore[reportIncompatibleVariableOverride]
    """Binary sensor reporting whether the projector ADB daemon is reachable on the network."""

    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the connectivity binary sensor."""
        super().__init__(coordinator, "connectivity")

    @property
    def is_on(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Return True when ADB is reachable (Awake or Network Standby)."""
        if not self.coordinator.data:
            return False
        return bool(self.coordinator.data.get("online", False))
