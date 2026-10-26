"""Tests for XGIMI entities and entry setup/unload lifecycle (test_entities.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from custom_components.xgimi import (
    PLATFORMS,
    async_reload_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.xgimi.binary_sensor import (
    XgimiConnectivityBinarySensor,
    XgimiPowerBinarySensor,
)
from custom_components.xgimi.binary_sensor import (
    async_setup_entry as async_setup_binary_sensors,
)
from custom_components.xgimi.button import (
    XgimiKeyButton,
    XgimiRestartButton,
    XgimiWakeButton,
)
from custom_components.xgimi.button import (
    async_setup_entry as async_setup_buttons,
)
from custom_components.xgimi.const import (
    DOMAIN,
    INPUT_SOURCE_APPS,
    INPUT_SOURCE_HDMI1,
    INPUT_SOURCE_HDMI2,
    KEYCODE_AUTOFOCUS,
    KEYCODE_SETTINGS,
    KEYCODES,
)
from custom_components.xgimi.select import (
    XgimiInputSourceSelect,
)
from custom_components.xgimi.select import (
    async_setup_entry as async_setup_selects,
)
from custom_components.xgimi.switch import (
    XgimiPowerSwitch,
)
from custom_components.xgimi.switch import (
    async_setup_entry as async_setup_switches,
)


def _make_mock_coordinator() -> MagicMock:
    """Build a mocked XgimiDataUpdateCoordinator with default state."""
    coord = MagicMock()
    coord.host = "192.168.1.50"
    coord.device_name = "XGIMI Horizon 20"
    coord.mac = "aa:bb:cc:dd:ee:ff"
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "entry_123"
    coord.data = {
        "online": True,
        "awake": True,
        "input_source": INPUT_SOURCE_APPS,
        "running_app": "com.google.android.tv.launcher",
        "model": "XR14A",
        "sw_version": "14",
        "mac": "aa:bb:cc:dd:ee:ff",
    }
    coord.async_turn_on = AsyncMock(return_value=True)
    coord.async_turn_off = AsyncMock(return_value=True)
    coord.async_set_input_source = AsyncMock(return_value=True)
    coord.async_send_key = AsyncMock(return_value=True)
    coord.async_reboot = AsyncMock(return_value=True)
    coord.async_shutdown = AsyncMock()
    coord.adb_port = 5555
    coord.default_state = MagicMock(return_value={"online": False, "awake": False})
    return coord


@pytest.mark.asyncio
async def test_entry_setup_standby_fallback_and_unload() -> None:
    """Verify async_setup_entry succeeds even in standby and async_unload_entry cleans up."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.config_entries.async_reload = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "entry_123"
    entry.data = {"host": "192.168.1.50", "name": "XGIMI Horizon 20"}
    entry.options = {}

    fake_coord = _make_mock_coordinator()
    fake_coord.async_config_entry_first_refresh = AsyncMock(side_effect=ConfigEntryNotReady("standby"))
    fake_coord.default_state.return_value = {"online": False, "awake": False}
    fake_coord.mac = None

    with patch(
        "custom_components.xgimi.XgimiDataUpdateCoordinator",
        return_value=fake_coord,
    ):
        assert await async_setup_entry(hass, entry) is True

    assert hass.data[DOMAIN]["entry_123"] is fake_coord
    assert fake_coord.data == {"online": False, "awake": False}
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, PLATFORMS)

    # When options have same effective connection settings, reload is not triggered
    entry.options = {}
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()

    # When effective connection settings change (e.g. host changed), reload is triggered
    entry.options = {"host": "192.168.1.99"}
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("entry_123")

    assert await async_unload_entry(hass, entry) is True
    fake_coord.async_shutdown.assert_awaited_once()
    assert "entry_123" not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_power_switch_and_device_info() -> None:
    """Verify power switch state, availability in deep sleep, device_info, and error surfacing."""
    coord = _make_mock_coordinator()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry_123": coord}}
    entry = coord.config_entry

    added: list[XgimiPowerSwitch] = []
    await async_setup_switches(hass, entry, added.extend)
    assert len(added) == 1
    sw = added[0]

    assert sw.unique_id == "entry_123_power"
    assert sw.available is True
    assert sw.is_on is True

    dev_info = sw.device_info
    assert (DOMAIN, "entry_123") in dev_info["identifiers"]
    assert (CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:ff") in dev_info["connections"]
    assert dev_info["manufacturer"] == "XGIMI"
    assert dev_info["model"] == "XR14A"

    await sw.async_turn_on()
    coord.async_turn_on.assert_awaited_once()

    await sw.async_turn_off()
    coord.async_turn_off.assert_awaited_once()

    # Error surfacing when turn_on or turn_off fails
    coord.async_turn_on = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to wake"):
        await sw.async_turn_on()

    coord.async_turn_off = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to turn off"):
        await sw.async_turn_off()

    # Deep sleep: switch remains available while is_on is False
    coord.data = None
    assert sw.available is True
    assert sw.is_on is False


@pytest.mark.asyncio
async def test_binary_sensors_distinguish_fast_boot_standby() -> None:
    """Verify Power State and Connectivity sensors distinguish Awake, Standby, and Deep Sleep."""
    coord = _make_mock_coordinator()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry_123": coord}}
    entry = coord.config_entry

    sensors: list[XgimiPowerBinarySensor | XgimiConnectivityBinarySensor] = []
    await async_setup_binary_sensors(hass, entry, sensors.extend)
    assert len(sensors) == 2
    power_sensor, conn_sensor = sensors[0], sensors[1]

    # Awake: both True
    assert power_sensor.is_on is True
    assert conn_sensor.is_on is True

    # Fast Boot / Network Standby: Connectivity True, Power State False
    coord.data = {"online": True, "awake": False}
    assert power_sensor.is_on is False
    assert conn_sensor.is_on is True

    # Deep sleep: both False
    coord.data = {"online": False, "awake": False}
    assert power_sensor.is_on is False
    assert conn_sensor.is_on is False


@pytest.mark.asyncio
async def test_input_source_select() -> None:
    """Verify Input Source select options, switching, and error handling."""
    coord = _make_mock_coordinator()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry_123": coord}}
    entry = coord.config_entry

    selects: list[XgimiInputSourceSelect] = []
    await async_setup_selects(hass, entry, selects.extend)
    assert len(selects) == 1
    sel = selects[0]

    assert sel.options == [INPUT_SOURCE_APPS, INPUT_SOURCE_HDMI1, INPUT_SOURCE_HDMI2]
    assert sel.current_option == INPUT_SOURCE_APPS

    await sel.async_select_option(INPUT_SOURCE_HDMI1)
    coord.async_set_input_source.assert_awaited_once_with(INPUT_SOURCE_HDMI1)

    with pytest.raises(HomeAssistantError, match="Unsupported input source"):
        await sel.async_select_option("VGA")

    coord.async_set_input_source = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to switch"):
        await sel.async_select_option(INPUT_SOURCE_HDMI2)


@pytest.mark.asyncio
async def test_buttons_autofocus_settings_wake_and_restart() -> None:
    """Verify remote key buttons (Auto Focus 140, Settings 176), Wake, and Restart buttons."""
    coord = _make_mock_coordinator()
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry_123": coord}}
    entry = coord.config_entry

    buttons = []
    await async_setup_buttons(hass, entry, buttons.extend)
    assert len(buttons) == len(KEYCODES) + 2

    by_uid = {b.unique_id: b for b in buttons}
    autofocus_btn = by_uid["entry_123_key_autofocus"]
    settings_btn = by_uid["entry_123_key_settings"]
    wake_btn = by_uid["entry_123_button_wake"]
    restart_btn = by_uid["entry_123_button_restart"]

    assert isinstance(autofocus_btn, XgimiKeyButton)
    assert isinstance(wake_btn, XgimiWakeButton)
    assert wake_btn.name == "Force Wake Projector"
    assert isinstance(restart_btn, XgimiRestartButton)
    assert restart_btn.entity_category == EntityCategory.CONFIG

    await autofocus_btn.async_press()
    coord.async_send_key.assert_awaited_with(KEYCODE_AUTOFOCUS)

    await settings_btn.async_press()
    coord.async_send_key.assert_awaited_with(KEYCODE_SETTINGS)

    await wake_btn.async_press()
    coord.async_turn_on.assert_awaited_once()

    await restart_btn.async_press()
    coord.async_reboot.assert_awaited_once()

    # Failure cases raise HomeAssistantError
    coord.async_send_key = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to send key"):
        await autofocus_btn.async_press()

    coord.async_turn_on = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to wake"):
        await wake_btn.async_press()

    coord.async_reboot = AsyncMock(return_value=False)
    with pytest.raises(HomeAssistantError, match="Failed to restart"):
        await restart_btn.async_press()
