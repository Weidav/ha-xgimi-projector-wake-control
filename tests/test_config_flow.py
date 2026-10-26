"""Tests for XGIMI Config Flow and Options Flow (config_flow.py)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_NAME

from custom_components.xgimi.config_flow import (
    XgimiConfigFlow,
    XgimiOptionsFlowHandler,
    _validate_host,
)
from custom_components.xgimi.const import CONF_HOST, CONF_MAC


def _make_flow_hass() -> MagicMock:
    """Create a mocked HomeAssistant instance for config flow tests."""
    hass = MagicMock()

    async def _async_add_executor_job(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_async_add_executor_job)
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    hass.config_entries = MagicMock()
    return hass


def test_validate_host() -> None:
    """Verify host validation accepts IPs/hostnames and rejects schemes, paths, and userinfo."""
    assert _validate_host("192.168.1.100") is True
    assert _validate_host("xgimi.local") is True
    assert _validate_host("my-projector") is True
    assert _validate_host("[::1]") is True
    assert _validate_host("[2001:db8::1]") is True

    assert _validate_host("http://192.168.1.100") is False
    assert _validate_host("https://xgimi.local") is False
    assert _validate_host("192.168.1.100/api") is False
    assert _validate_host("xgimi.local?foo=bar") is False
    assert _validate_host("xgimi.local#frag") is False
    assert _validate_host("admin@xgimi.local") is False
    assert _validate_host("192.168.1.100 5555") is False
    assert _validate_host("") is False
    assert _validate_host("   ") is False


@pytest.mark.asyncio
async def test_options_flow_zero_arg_and_reconfigure() -> None:
    """Verify modern zero-arg XgimiOptionsFlowHandler and host/MAC reconfiguration."""
    handler = XgimiOptionsFlowHandler()
    assert handler is not None

    fake_hass = _make_flow_hass()
    fake_entry = MagicMock()
    fake_entry.entry_id = "test_entry"
    fake_entry.data = {CONF_HOST: "192.168.1.50", CONF_MAC: "aa:bb:cc:dd:ee:ff"}
    fake_entry.options = {}
    fake_hass.config_entries.async_get_known_entry.return_value = fake_entry

    handler.hass = fake_hass
    handler.handler = "test_entry"
    assert handler.config_entry == fake_entry

    # Show initial form
    form = await handler.async_step_init(None)
    assert form["type"] == "form"
    assert form["step_id"] == "init"

    # Invalid host and MAC validation errors
    err_res = await handler.async_step_init({CONF_HOST: "http://bad", CONF_MAC: "bad_mac"})
    assert err_res["type"] == "form"
    assert err_res["errors"] == {
        CONF_HOST: "invalid_host",
        CONF_MAC: "invalid_mac",
    }

    # Valid update normalizes MAC and updates entry
    ok_res = await handler.async_step_init({CONF_HOST: "192.168.1.60", CONF_MAC: "11-22-33-44-55-66"})
    assert ok_res["type"] == "create_entry"
    assert ok_res["data"] == {
        CONF_HOST: "192.168.1.60",
        CONF_MAC: "11:22:33:44:55:66",
    }
    fake_hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_user_validation_and_authorized_fast_path() -> None:
    """Verify user step validation, ARP/ADB MAC resolution, and direct entry creation when authorized."""
    flow = XgimiConfigFlow()
    flow.hass = _make_flow_hass()

    # Initial form
    res_init = await flow.async_step_user(None)
    assert res_init["type"] == "form"
    assert res_init["step_id"] == "user"

    # Invalid host & MAC
    res_err = await flow.async_step_user({CONF_HOST: "http://192.168.1.100", CONF_MAC: "invalid_mac_val"})
    assert res_err["type"] == "form"
    assert res_err["errors"] == {
        CONF_HOST: "invalid_host",
        CONF_MAC: "invalid_mac",
    }

    # Authorized projector with ADB MAC fallback
    fake_adb = MagicMock()
    fake_adb.async_probe_status = AsyncMock(return_value="authorized")
    fake_adb.async_shell = AsyncMock(return_value="AA:BB:CC:DD:EE:99\n")
    fake_adb.async_close = AsyncMock()

    with (
        patch(
            "custom_components.xgimi.config_flow.XgimiAdbClient",
            return_value=fake_adb,
        ),
        patch(
            "custom_components.xgimi.config_flow.resolve_mac_from_arp",
            return_value=None,
        ),
        patch.object(flow, "async_set_unique_id", AsyncMock()) as mock_uid,
        patch.object(flow, "_abort_if_unique_id_configured", MagicMock()),
    ):
        res_ok = await flow.async_step_user({CONF_HOST: "192.168.1.100", CONF_NAME: "Living Room XGIMI", CONF_MAC: ""})

    assert res_ok["type"] == "create_entry"
    assert res_ok["title"] == "Living Room XGIMI"
    assert res_ok["data"][CONF_HOST] == "192.168.1.100"
    assert res_ok["data"][CONF_MAC] == "aa:bb:cc:dd:ee:99"
    mock_uid.assert_awaited_once_with("aa:bb:cc:dd:ee:99")


@pytest.mark.asyncio
async def test_config_flow_wake_on_unreachable_and_interactive_authorize() -> None:
    """Verify WoWLAN wake during setup and two-step interactive ADB pairing flow."""
    flow = XgimiConfigFlow()
    flow.hass = _make_flow_hass()

    # Case 1: First probe unreachable -> WoWLAN sent -> still unreachable -> cannot_connect
    fake_adb_unreach = MagicMock()
    fake_adb_unreach.async_probe_status = AsyncMock(return_value="unreachable")
    fake_adb_unreach.async_close = AsyncMock()

    with (
        patch(
            "custom_components.xgimi.config_flow.XgimiAdbClient",
            return_value=fake_adb_unreach,
        ),
        patch(
            "custom_components.xgimi.config_flow._send_setup_wowlan_knocks",
            AsyncMock(),
        ) as mock_wake,
        patch("custom_components.xgimi.config_flow.asyncio.sleep", AsyncMock()),
    ):
        res_unreach = await flow.async_step_user({CONF_HOST: "192.168.1.100", CONF_MAC: "aa:bb:cc:dd:ee:ff"})

    assert res_unreach["type"] == "form"
    assert res_unreach["errors"] == {"base": "cannot_connect"}
    mock_wake.assert_awaited_once()

    # Case 2: Projector responds 'unauthorized' -> transitions to 'authorize' step
    fake_adb_pair = MagicMock()
    fake_adb_pair.async_probe_status = AsyncMock(return_value="unauthorized")
    # First authorize attempt returns False (user hasn't clicked Allow yet), second returns True
    fake_adb_pair.async_authorize = AsyncMock(side_effect=[False, False, True])
    fake_adb_pair.async_shell = AsyncMock(return_value="aa:bb:cc:dd:ee:ff")
    fake_adb_pair.async_close = AsyncMock()

    with patch(
        "custom_components.xgimi.config_flow.XgimiAdbClient",
        return_value=fake_adb_pair,
    ):
        res_step2 = await flow.async_step_user({CONF_HOST: "192.168.1.100", CONF_MAC: "AA:BB:CC:DD:EE:FF"})

    assert res_step2["type"] == "form"
    assert res_step2["step_id"] == "authorize"

    # User clicks Submit before approving on TV -> shows adb_not_authorized error
    res_not_yet = await flow.async_step_authorize({})
    assert res_not_yet["type"] == "form"
    assert res_not_yet["step_id"] == "authorize"
    assert res_not_yet["errors"] == {"base": "adb_not_authorized"}

    # User approves on TV and clicks Submit -> creates entry!
    with (
        patch.object(flow, "async_set_unique_id", AsyncMock()),
        patch.object(flow, "_abort_if_unique_id_configured", MagicMock()),
    ):
        res_paired = await flow.async_step_authorize({})

    assert res_paired["type"] == "create_entry"
    assert res_paired["data"][CONF_HOST] == "192.168.1.100"
    assert res_paired["data"][CONF_MAC] == "aa:bb:cc:dd:ee:ff"

    # Case 3: User closes/aborts config flow dialog while _auth_task is awaiting on-screen approval
    aborted_flow = XgimiConfigFlow()
    aborted_flow.hass = _make_flow_hass()
    hang_adb = MagicMock()
    hang_adb.close_transport_nowait = MagicMock()

    async def _slow_auth(timeout: float = 30.0) -> bool:
        await asyncio.sleep(timeout)
        return False

    hang_adb.async_authorize = AsyncMock(side_effect=_slow_auth)
    aborted_flow._adb = hang_adb
    aborted_flow._host = "192.168.1.100"

    await aborted_flow.async_step_authorize(None)
    auth_task = aborted_flow._auth_task
    assert auth_task is not None and not auth_task.done()

    aborted_flow.async_remove()
    await asyncio.sleep(0)
    assert auth_task.cancelled()
    assert aborted_flow._auth_task is None
    hang_adb.close_transport_nowait.assert_called_once()


def test_manifest_hacs_and_translations_validity() -> None:
    """Verify manifest.json hassfest key ordering, hacs.json schema, and translations parity."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "custom_components" / "xgimi" / "manifest.json").read_text(encoding="utf-8"))
    keys = list(manifest.keys())
    # Hassfest requires 'domain' first, 'name' second, and remaining keys sorted alphabetically
    assert keys[0] == "domain"
    assert keys[1] == "name"
    assert keys[2:] == sorted(keys[2:])
    assert manifest["domain"] == "xgimi"
    assert manifest["name"] == "XGIMI Projector Wake & Control"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"

    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "XGIMI Projector Wake & Control"
    assert hacs["homeassistant"] == "2024.12.0"
    assert hacs["render_readme"] is True

    strings = json.loads((root / "custom_components" / "xgimi" / "strings.json").read_text(encoding="utf-8"))
    en_trans = json.loads(
        (root / "custom_components" / "xgimi" / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert strings == en_trans


@pytest.mark.asyncio
async def test_config_and_options_flow_on_declared_minimum_ha_version() -> None:
    """Verify config and options flow compatibility on declared minimum HA version (2024.12.0)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["homeassistant"] == "2024.12.0"

    fake_hass = _make_flow_hass()
    fake_entry = MagicMock()
    fake_entry.entry_id = "test_min_ha"
    fake_entry.data = {CONF_HOST: "192.168.1.75", CONF_NAME: "Min HA Projector"}
    fake_entry.options = {}

    flow = XgimiConfigFlow()
    flow.hass = fake_hass
    options_flow = XgimiConfigFlow.async_get_options_flow(fake_entry)
    options_flow.hass = fake_hass

    assert options_flow.config_entry is fake_entry

    form = await options_flow.async_step_init(None)
    assert form["type"] == "form"
    assert form["step_id"] == "init"

    result = await options_flow.async_step_init({CONF_HOST: "192.168.1.80", CONF_MAC: "AA:BB:CC:DD:EE:88"})
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_HOST: "192.168.1.80", CONF_MAC: "aa:bb:cc:dd:ee:88"}
    fake_hass.config_entries.async_update_entry.assert_called_once()

    fake_adb = MagicMock()
    fake_adb.async_probe_status = AsyncMock(return_value="authorized")
    fake_adb.async_shell = AsyncMock(return_value="AA:BB:CC:DD:EE:88\n")
    fake_adb.async_close = AsyncMock()

    with (
        patch("custom_components.xgimi.config_flow.XgimiAdbClient", return_value=fake_adb),
        patch("custom_components.xgimi.config_flow.resolve_mac_from_arp", return_value=None),
        patch.object(flow, "async_set_unique_id", AsyncMock()) as mock_uid,
        patch.object(flow, "_abort_if_unique_id_configured", MagicMock()),
    ):
        res = await flow.async_step_user({CONF_HOST: "192.168.1.80", CONF_NAME: "Min HA Projector", CONF_MAC: ""})

    assert res["type"] == "create_entry"
    assert res["title"] == "Min HA Projector"
    assert res["data"][CONF_HOST] == "192.168.1.80"
    assert res["data"][CONF_MAC] == "aa:bb:cc:dd:ee:88"
    mock_uid.assert_awaited_once_with("aa:bb:cc:dd:ee:88")
