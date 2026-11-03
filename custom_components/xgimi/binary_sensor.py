"""Binary sensor platform for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
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
            XgimiAdbConnectivityBinarySensor(coordinator),
        ]
    )


class XgimiPowerBinarySensor(XgimiEntity, BinarySensorEntity):
    """Binary sensor reporting whether the projector display is Awake."""

    _attr_name = "Power State"

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the power state binary sensor."""
        super().__init__(coordinator, "power_state")

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the power device class."""
        return BinarySensorDeviceClass.POWER

    @property
    def is_on(self) -> bool | None:
        """Return True when the projector is Awake (display on), None if unknown."""
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("awake")


class XgimiAdbConnectivityBinarySensor(XgimiEntity, BinarySensorEntity):
    """Binary sensor reporting whether the projector ADB daemon is reachable on the network."""

    _attr_name = "ADB Connectivity"

    def __init__(self, coordinator: XgimiDataUpdateCoordinator) -> None:
        """Initialize the ADB connectivity binary sensor."""
        super().__init__(coordinator, "adb_connectivity")

    @property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the connectivity device class."""
        return BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        """Return True when ADB is reachable (Awake or Network Standby)."""
        if not self.coordinator.data:
            return False
        return self.coordinator.data.get("online", False)
