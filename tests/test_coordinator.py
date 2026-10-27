"""Tests for the XGIMI Two-Stage Wake & ADB Coordinator (coordinator.py)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from custom_components.xgimi.adb_client import AdbConnectionError, AdbError
from custom_components.xgimi.const import (
    CAST_WAKE_PORT,
    CONF_MAC,
    INPUT_SOURCE_APPS,
    INPUT_SOURCE_HDMI1,
    INPUT_SOURCE_HDMI2,
    KEYCODE_POWER,
    KEYCODE_WAKEUP,
    MDNS_WAKE_PORT,
    REMOTE_WAKE_PORT,
    WOL_DISCARD_PORT,
    WOL_WAKE_PORT,
)
from custom_components.xgimi.coordinator import (
    XgimiDataUpdateCoordinator,
    create_magic_packet,
    format_host,
    format_mac,
    parse_hdmi_inputs_from_dumpsys,
    parse_wakefulness,
    resolve_mac_from_arp,
)


def _make_fake_hass() -> MagicMock:
    """Create a minimal mocked HomeAssistant instance for unit tests."""
    hass = MagicMock()

    async def _async_add_executor_job(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_async_add_executor_job)
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    hass.config_entries = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    return hass


def test_mac_host_and_wol_packet_helpers() -> None:
    """Verify host formatting, MAC normalization, 102-byte magic packet, and ARP lookup."""
    assert format_host("192.168.1.50") == "192.168.1.50"
    assert format_host("fe80::1") == "[fe80::1]"
    assert format_host("[fe80::1]") == "[fe80::1]"

    assert format_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert format_mac("aa-bb-cc-dd-ee-ff") == "aa:bb:cc:dd:ee:ff"
    assert format_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert format_mac("112233445566") == "11:22:33:44:55:66"

    assert format_mac(None) is None
    assert format_mac("") is None
    assert format_mac("invalid_mac") is None
    assert format_mac("11:22:33:44:55") is None
    assert format_mac("gg:hh:ii:jj:kk:ll") is None

    pkt = create_magic_packet("aa:bb:cc:dd:ee:ff")
    assert pkt is not None
    assert len(pkt) == 102
    assert pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("aabbccddeeff")
    assert pkt[96:102] == bytes.fromhex("aabbccddeeff")
    assert create_magic_packet("invalid") is None

    fake_arp = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.50     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
        "192.168.1.51     0x1         0x0         00:00:00:00:00:00     *        eth0\n"
    )
    with (
        patch.object(Path, "open", mock_open(read_data=fake_arp)),
        patch(
            "socket.gethostbyname",
            side_effect=lambda h: "192.168.1.50" if h == "xgimi.local" else h,
        ),
    ):
        assert resolve_mac_from_arp("192.168.1.50") == "aa:bb:cc:dd:ee:ff"
        assert resolve_mac_from_arp("xgimi.local") == "aa:bb:cc:dd:ee:ff"
        assert resolve_mac_from_arp("192.168.1.51") is None
        assert resolve_mac_from_arp("192.168.1.99") is None
        assert resolve_mac_from_arp("[fe80::1]") is None


def test_parse_wakefulness_and_hdmi_inputs() -> None:
    """Verify dumpsys power wakefulness parsing and dumpsys tv_input HDMI discovery."""
    assert parse_wakefulness("mWakefulness=Awake\nDisplay Power: state=ON") is True
    assert parse_wakefulness("mWakefulness=Dreaming\nDisplay Power: state=ON") is True
    assert parse_wakefulness("mWakefulness=Asleep\nDisplay Power: state=OFF") is False
    assert parse_wakefulness("mWakefulness=Dozing\nDisplay Power: state=OFF") is False
    assert parse_wakefulness("Display Power: state=ON") is True
    assert parse_wakefulness("Display Power: state=OFF") is False
    assert parse_wakefulness("") is None

    # MediaTek TvInputInfo format
    dumpsys_tv = """
    TvInputInfo{mId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5, mParentId=null, mHdmiPortId=1}
    TvInputInfo{mId=com.mediatek.tvinput/.hdmi.HDMIInputService/CEC1, mParentId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5, mHdmiPortId=1}
    TvInputInfo{mId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6, mParentId=null, mHdmiPortId=2}
    """
    discovered = parse_hdmi_inputs_from_dumpsys(dumpsys_tv)
    assert discovered == {
        INPUT_SOURCE_HDMI1: "com.mediatek.tvinput/.hdmi.HDMIInputService/HW5",
        INPUT_SOURCE_HDMI2: "com.mediatek.tvinput/.hdmi.HDMIInputService/HW6",
    }

    # Standard AOSP TvInputInfo.toString() format with id=, parentId=, HdmiDeviceInfo[CEC:, and port_id:
    aosp_tv = """
    TvInputInfo{id=com.droidlogic.tvinput/.services.Hdmi1InputService/HW1, pkg=com.droidlogic.tvinput, hdmiInfo=HdmiDeviceInfo[Hardware: port_id: 1]}
    TvInputInfo{id=com.droidlogic.tvinput/.services.Hdmi1InputService/CEC4, parentId=com.droidlogic.tvinput/.services.Hdmi1InputService/HW1, hdmiInfo=HdmiDeviceInfo[CEC: logical_address: 0x04 port_id: 1]}
    TvInputInfo{id=com.droidlogic.tvinput/.services.Hdmi2InputService/HW2, pkg=com.droidlogic.tvinput, hdmiInfo=HdmiDeviceInfo[Hardware: port_id: 2]}
    """
    assert parse_hdmi_inputs_from_dumpsys(aosp_tv) == {
        INPUT_SOURCE_HDMI1: "com.droidlogic.tvinput/.services.Hdmi1InputService/HW1",
        INPUT_SOURCE_HDMI2: "com.droidlogic.tvinput/.services.Hdmi2InputService/HW2",
    }


@pytest.mark.asyncio
async def test_multi_vector_wowlan_knocks() -> None:
    """Verify Stage 1 WoWLAN sends UDP magic packets, mDNS queries, HTTP Cast knock, and TCP knocks."""
    hass = _make_fake_hass()
    adb = MagicMock()
    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    coord.mac = "aa:bb:cc:dd:ee:ff"

    udp_calls: list[tuple[str, int, bytes, int]] = []
    tcp_calls: list[tuple[str, int]] = []

    def _record_udp(host: str, port: int, payload: bytes = b"\x00", repeat: int = 2) -> None:
        udp_calls.append((host, port, payload, repeat))

    async def _record_tcp(host: str, port: int, timeout: float = 0.8) -> None:
        tcp_calls.append((host, port))

    with (
        patch("custom_components.xgimi.coordinator.send_udp_knock", side_effect=_record_udp),
        patch("custom_components.xgimi.coordinator.send_tcp_knock", side_effect=_record_tcp),
        patch.object(coord, "_send_cast_http_knock", AsyncMock()) as mock_http,
    ):
        await coord._send_wowlan_knocks()

    udp_ports = [c[1] for c in udp_calls]
    assert WOL_WAKE_PORT in udp_ports
    assert WOL_DISCARD_PORT in udp_ports
    assert CAST_WAKE_PORT in udp_ports
    assert REMOTE_WAKE_PORT in udp_ports
    assert MDNS_WAKE_PORT in udp_ports
    mock_http.assert_awaited_once()
    assert ("192.168.1.50", CAST_WAKE_PORT) in tcp_calls
    assert ("192.168.1.50", REMOTE_WAKE_PORT) in tcp_calls
    assert ("192.168.1.50", 5555) in tcp_calls


@pytest.mark.asyncio
async def test_two_stage_turn_on_and_idempotent_turn_off() -> None:
    """Verify two-stage wake (WoWLAN -> ADB KEYCODE_WAKEUP/POWER) and idempotent turn_off."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_send_key = AsyncMock()
    adb.async_close = AsyncMock()

    # Attempt 1: ADB unreachable (deep sleep)
    # Attempt 2: ADB reachable, mWakefulness=Asleep -> sends KEYCODE_WAKEUP, rechecks Asleep -> sends KEYCODE_POWER -> verifies Awake
    adb.async_shell = AsyncMock(
        side_effect=[
            AdbConnectionError("deep sleep"),
            "mWakefulness=Asleep",
            "mWakefulness=Asleep",
            "mWakefulness=Awake",
        ]
    )

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    with patch.object(coord, "_send_wowlan_knocks", AsyncMock()) as mock_knocks:
        ok = await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.05)

    assert ok is True
    mock_knocks.assert_awaited()
    assert [c.args[0] for c in adb.async_send_key.await_args_list] == [
        KEYCODE_WAKEUP,
        KEYCODE_POWER,
    ]
    assert coord.data["awake"] is True
    assert coord.data["online"] is True

    # Now test idempotent turn_off when Awake -> sends KEYCODE_POWER (26) once
    adb.async_send_key.reset_mock()
    adb.async_shell = AsyncMock(return_value="mWakefulness=Awake")
    assert await coord.async_turn_off() is True
    adb.async_send_key.assert_awaited_once_with(KEYCODE_POWER, timeout=4.0)
    assert coord.data["awake"] is False

    # Repeated turn_off when already Asleep -> MUST NOT send KEYCODE_POWER again
    adb.async_send_key.reset_mock()
    adb.async_shell = AsyncMock(return_value="mWakefulness=Asleep")
    assert await coord.async_turn_off() is True
    adb.async_send_key.assert_not_awaited()

    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_compound_poll_and_mac_persistence_and_commands() -> None:
    """Verify single-command ADB poll, Fast Boot standby state, MAC persistence, input switch, and reboot."""
    hass = _make_fake_hass()
    entry = MagicMock()
    entry.data = {"host": "192.168.1.50"}
    entry.options = {}

    adb = MagicMock()
    adb.async_send_key = AsyncMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(
        return_value=(
            "---WAKE---\n"
            "mWakefulness=Dozing\n"
            "Display Power: state=OFF\n"
            "---APP1---\n"
            "com.google.android.tv.inputplayer\n"
            "---APP2---\n"
            "null\n"
            "---HDMI---\n"
            "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
            "---META---\n"
            "XR14A\n"
            "14\n"
            "AA:BB:CC:11:22:33\n"
        )
    )

    coord = XgimiDataUpdateCoordinator(
        hass,
        "192.168.1.50",
        "XGIMI Horizon 20",
        config_entry=entry,
        adb_client=adb,
    )
    data = await coord._async_update_data()

    # In Fast Boot standby: online is True, awake is False!
    assert data["online"] is True
    assert data["awake"] is False
    assert data["input_source"] == INPUT_SOURCE_HDMI1
    assert data["model"] == "XR14A"
    assert data["sw_version"] == "14"
    assert data["mac"] == "aa:bb:cc:11:22:33"
    hass.config_entries.async_update_entry.assert_called_once()
    assert hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_MAC] == "aa:bb:cc:11:22:33"

    # Verify empty ro.product.model line does not shift sw_version or MAC into model
    coord2 = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    coord2.mac = None
    parsed_empty_model = coord2._parse_compound_poll(
        "---WAKE---\nmWakefulness=Awake\n---META---\n\n14\n11:22:33:44:55:66\n"
    )
    assert parsed_empty_model["model"] == "XGIMI Horizon 20"
    assert parsed_empty_model["sw_version"] == "14"
    assert parsed_empty_model["mac"] == "11:22:33:44:55:66"

    # Input switching to HDMI 2 and Apps
    adb.async_shell = AsyncMock(return_value="")
    assert await coord.async_set_input_source(INPUT_SOURCE_HDMI2) is True
    assert coord.data["input_source"] == INPUT_SOURCE_HDMI2
    assert "content://android.media.tv/passthrough/" in adb.async_shell.await_args_list[-1].args[0]

    assert await coord.async_set_input_source(INPUT_SOURCE_APPS) is True
    assert coord.data["input_source"] == INPUT_SOURCE_APPS
    assert "force-stop com.google.android.tv.inputplayer" in adb.async_shell.await_args_list[-1].args[0]

    assert await coord.async_set_input_source("InvalidSource") is False

    # Reboot
    assert await coord.async_reboot() is True
    assert coord.data["online"] is False
    assert coord.data["awake"] is False

    await coord.async_shutdown()
    assert len(coord._delayed_refresh_tasks) == 0


@pytest.mark.asyncio
async def test_concurrent_power_off_requests_coalesce() -> None:
    """Verify overlapping turn_off requests send KEYCODE_POWER only once."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_send_key = AsyncMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(return_value="mWakefulness=Awake")

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)

    res1, res2 = await asyncio.gather(coord.async_turn_off(), coord.async_turn_off())

    assert res1 is True
    assert res2 is True
    assert adb.async_send_key.await_count == 1
    adb.async_send_key.assert_awaited_once_with(KEYCODE_POWER, timeout=4.0)
    assert coord.data["awake"] is False
    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_unknown_power_state_handling() -> None:
    """Verify unknown wakefulness prevents sending power toggle and does not default to awake."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_send_key = AsyncMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(return_value="")

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)

    assert await coord.async_turn_off() is False
    adb.async_send_key.assert_not_awaited()

    poll_data = coord._parse_compound_poll("---WAKE---\nunrecognized power output\n---META---\nXR14A\n14\n")
    assert poll_data["awake"] is None
    assert poll_data["online"] is True
    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_rechecks_state_on_ambiguous_disconnect() -> None:
    """Verify turn_off reconnects and verifies state if connection drops during power key send."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_send_key = AsyncMock(side_effect=AdbConnectionError("socket dropped"))
    adb.async_close = AsyncMock()

    adb.async_shell = AsyncMock(
        side_effect=[
            "mWakefulness=Awake",
            "mWakefulness=Asleep",
        ]
    )

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    assert await coord.async_turn_off() is True
    assert adb.async_send_key.await_count == 1
    assert coord.data["awake"] is False
    await coord.async_shutdown()


def test_coordinator_constructor_no_blocking_arp_lookup() -> None:
    """Verify coordinator constructor does not call resolve_mac_from_arp synchronously."""
    hass = _make_fake_hass()
    with patch("custom_components.xgimi.coordinator.resolve_mac_from_arp") as mock_arp:
        coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20")
        assert coord.mac is None
        mock_arp.assert_not_called()


@pytest.mark.asyncio
async def test_mac_discovery_then_user_changes_host_reloads() -> None:
    """Verify MAC discovery during initial refresh does not suppress subsequent host change reload."""
    from custom_components.xgimi import DOMAIN, async_reload_entry

    hass = _make_fake_hass()
    entry = MagicMock()
    entry.entry_id = "test_entry_mac_reload"
    entry.data = {"host": "192.168.1.50"}
    entry.options = {}

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(
        return_value=("---WAKE---\nmWakefulness=Awake\n---META---\nXR14A\n14\naa:bb:cc:dd:ee:ff\n")
    )

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI", config_entry=entry, adb_client=adb)
    hass.data = {DOMAIN: {entry.entry_id: coord}}

    # Initial refresh discovers MAC
    await coord._async_update_data()
    assert coord.mac == "aa:bb:cc:dd:ee:ff"
    entry.data = {"host": "192.168.1.50", CONF_MAC: "aa:bb:cc:dd:ee:ff"}

    # Update listener triggers with same host -> does NOT reload
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_not_called()

    # Now user updates host in options flow -> MUST reload!
    entry.options = {"host": "192.168.1.60"}
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)

    # When user clears MAC address in options flow -> MUST reload!
    hass.config_entries.async_reload.reset_mock()
    entry.options = {"host": "192.168.1.50"}
    entry.data = {"host": "192.168.1.50"}
    await async_reload_entry(hass, entry)
    hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)

    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_android_input_switch_failure_reported_as_error() -> None:
    """Verify failed Android input switch outputs return False and do not change state."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(return_value="Error: Activity not started, unable to resolve Intent")

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    coord._current_input_source = INPUT_SOURCE_APPS

    success = await coord.async_set_input_source(INPUT_SOURCE_HDMI2)
    assert success is False
    assert coord._current_input_source == INPUT_SOURCE_APPS
    if coord.data:
        assert coord.data["input_source"] == INPUT_SOURCE_APPS

    # Output with warning brought to front is successful intent delivery
    adb.async_shell = AsyncMock(
        return_value="Warning: Activity not started, its current task has been brought to the front"
    )
    success_warn = await coord.async_set_input_source(INPUT_SOURCE_HDMI2)
    assert success_warn is True
    assert coord._current_input_source == INPUT_SOURCE_HDMI2

    # Output with command not found is a failure
    adb.async_shell = AsyncMock(return_value="/system/bin/sh: am: not found")
    success_not_found = await coord.async_set_input_source(INPUT_SOURCE_APPS)
    assert success_not_found is False

    # Output with permission denied is a failure
    adb.async_shell = AsyncMock(return_value="/system/bin/sh: am: Permission denied")
    success_perm = await coord.async_set_input_source(INPUT_SOURCE_APPS)
    assert success_perm is False

    # Output with broken pipe / failure calling service activity is a failure
    adb.async_shell = AsyncMock(return_value="cmd: Failure calling service activity: Broken pipe (32)")
    success_broken_pipe = await coord.async_set_input_source(INPUT_SOURCE_HDMI1)
    assert success_broken_pipe is False

    # Output with generic exit status failure
    adb.async_shell = AsyncMock(return_value="exit status 1")
    success_exit_code = await coord.async_set_input_source(INPUT_SOURCE_HDMI1)
    assert success_exit_code is False

    await coord.async_shutdown()


def test_is_android_shell_error_rules() -> None:
    """Verify is_android_shell_error properly identifies failures and allowed warnings."""
    from custom_components.xgimi.coordinator import is_android_shell_error

    assert is_android_shell_error("Permission denied") is True
    assert is_android_shell_error("cmd: Failure calling service activity: Broken pipe (32)") is True
    assert is_android_shell_error("Activity not started, intent aborted") is True
    assert is_android_shell_error("Error type 3") is True
    assert is_android_shell_error("exit status 1") is True
    assert is_android_shell_error("rc=127") is True
    assert is_android_shell_error("rc: 1") is True
    assert is_android_shell_error("exit: 127") is True
    assert is_android_shell_error("return code: 2") is True
    assert is_android_shell_error("Access denied") is True
    assert is_android_shell_error("Killed") is True
    assert is_android_shell_error("Segmentation fault") is True

    # Error takes precedence over informational foregrounding message
    assert (
        is_android_shell_error(
            "Warning: Activity not started, its current task has been brought to the front\n"
            "cmd: Failure calling service activity: Broken pipe (32)"
        )
        is True
    )

    # Allowed non-error informational states
    assert (
        is_android_shell_error("Warning: Activity not started, its current task has been brought to the front") is False
    )
    assert (
        is_android_shell_error(
            "Warning: Activity not started, intent has been delivered to currently running top-level activity"
        )
        is False
    )
    assert is_android_shell_error("") is False
    assert is_android_shell_error("Starting: Intent { act=android.intent.action.VIEW }") is False
    assert is_android_shell_error("exit status 0") is False
    assert is_android_shell_error("rc=0") is False


@pytest.mark.asyncio
async def test_active_hdmi_detection_on_start_and_remote_change() -> None:
    """Verify active HDMI 2 detected on start and when changed via remote."""
    from custom_components.xgimi.coordinator import parse_active_hdmi_input

    sample_hdmi2 = "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    sample_hdmi1 = "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
    assert parse_active_hdmi_input(sample_hdmi2) == INPUT_SOURCE_HDMI2
    assert parse_active_hdmi_input(sample_hdmi1) == INPUT_SOURCE_HDMI1
    assert parse_active_hdmi_input("") is None

    # Synthetic sample containing HDMI 1 before an explicit current-input marker for HDMI 2
    synthetic_hdmi1_before_mcurid_hdmi2 = (
        "TvInputInfo{mId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5, mHdmiPortId=1}\n"
        "mCurId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(synthetic_hdmi1_before_mcurid_hdmi2) == INPUT_SOURCE_HDMI2

    # Synthetic sample with currentInputId marker
    synthetic_current_input_id = (
        "inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
        "currentInputId: com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(synthetic_current_input_id) == INPUT_SOURCE_HDMI2

    # Synthetic sample with SessionState for HDMI 2 after HDMI 1 mention
    synthetic_session_hdmi2 = (
        "TvInputState: com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
        "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(synthetic_session_hdmi2) == INPUT_SOURCE_HDMI2

    # Active session line with mInputId
    synthetic_minputid_hdmi2 = "mInputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    assert parse_active_hdmi_input(synthetic_minputid_hdmi2) == INPUT_SOURCE_HDMI2

    # Explicit non-HDMI current input suppresses falling back to dormant HDMI sessions
    non_hdmi_current_with_hdmi_session = (
        "mCurId=com.google.android.tv.dtv/DTV\nSessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
    )
    assert parse_active_hdmi_input(non_hdmi_current_with_hdmi_session) is None

    # Ambiguous generic data (both inputs present without active session marker) -> None
    ambiguous_inputs = (
        "inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
        "inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(ambiguous_inputs) is None

    # Single generic registration line without active session marker -> None (unknown)
    single_registration_line = "TvInputInfo{mId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5, mHdmiPortId=1}\n"
    assert parse_active_hdmi_input(single_registration_line) is None

    # mCurId=null with generic non-session line -> None (no active session)
    mcurid_null_with_generic = "mCurId=null\ninputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
    assert parse_active_hdmi_input(mcurid_null_with_generic) is None

    # Conflicting active session markers -> None
    conflicting_sessions = (
        "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
        "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(conflicting_sessions) is None

    # mCurId=null falls through to active SessionState
    mcurid_null_with_session = "mCurId=null\nSessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
    assert parse_active_hdmi_input(mcurid_null_with_session) == INPUT_SOURCE_HDMI1

    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_close = AsyncMock()

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)
    poll_hdmi2 = (
        "---WAKE---\nmWakefulness=Awake\n"
        "---APP1---\ncom.google.android.tv.inputplayer\n"
        "---HDMI---\nSessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
        "---META---\nXR14A\n14\n"
    )
    data = coord._parse_compound_poll(poll_hdmi2)
    assert data["input_source"] == INPUT_SOURCE_HDMI2
    assert coord._current_input_source == INPUT_SOURCE_HDMI2

    poll_hdmi1 = (
        "---WAKE---\nmWakefulness=Awake\n"
        "---APP1---\ncom.google.android.tv.inputplayer\n"
        "---HDMI---\nSessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW5\n"
        "---META---\nXR14A\n14\n"
    )
    data1 = coord._parse_compound_poll(poll_hdmi1)
    assert data1["input_source"] == INPUT_SOURCE_HDMI1
    assert coord._current_input_source == INPUT_SOURCE_HDMI1

    # When multiple lines exist (including both HW5 and HW6), active session line wins
    multi_line = (
        "mCurId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
        "SessionState: inputId=com.mediatek.tvinput/.hdmi.HDMIInputService/HW6\n"
    )
    assert parse_active_hdmi_input(multi_line) == INPUT_SOURCE_HDMI2

    # When inputplayer is running but HDMI session cannot be reliably detected -> None (unknown)
    poll_unknown_hdmi = (
        "---WAKE---\nmWakefulness=Awake\n"
        "---APP1---\ncom.google.android.tv.inputplayer\n"
        "---HDMI---\n"
        "---META---\nXR14A\n14\n"
    )
    data_unknown = coord._parse_compound_poll(poll_unknown_hdmi)
    assert data_unknown["input_source"] is None
    assert coord._current_input_source is None

    await coord.async_shutdown()


@pytest.mark.asyncio
async def test_competing_power_off_followed_immediately_by_on() -> None:
    """Verify off followed immediately by on waits for hardware settle and ends awake."""
    hass = _make_fake_hass()
    adb = MagicMock()
    adb.async_close = AsyncMock()

    simulated_state = "Awake"
    settle_polls_left = 0

    async def fake_send_key(keycode: int, timeout: float = 4.0) -> None:
        nonlocal simulated_state, settle_polls_left
        if keycode == KEYCODE_POWER:
            if simulated_state == "Awake":
                settle_polls_left = 2
            elif simulated_state == "Asleep":
                simulated_state = "Awake"
        elif keycode == KEYCODE_WAKEUP and simulated_state == "Asleep":
            simulated_state = "Awake"

    async def fake_shell(cmd: str, timeout: float = 5.0, retry: bool | None = None) -> str:
        nonlocal simulated_state, settle_polls_left
        if "mWakefulness" in cmd or "dumpsys power" in cmd:
            if settle_polls_left > 0:
                settle_polls_left -= 1
                if settle_polls_left == 0:
                    simulated_state = "Asleep"
                return "mWakefulness=Awake"
            return f"mWakefulness={simulated_state}"
        return ""

    adb.async_send_key = AsyncMock(side_effect=fake_send_key)
    adb.async_shell = AsyncMock(side_effect=fake_shell)

    coord = XgimiDataUpdateCoordinator(hass, "192.168.1.50", "XGIMI Horizon 20", adb_client=adb)

    # 1. Turn off while awake
    assert await coord.async_turn_off() is True

    # 2. Immediately turn on while simulated hardware is still in settling transition
    with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
        assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.01) is True

    # Simulated hardware must have settled to Asleep, received wake key, and ended Awake
    assert simulated_state == "Awake"
    assert coord.data["awake"] is True
    assert coord.data["online"] is True

    # 3. If turn_on timeout expires while hardware is still settling off, return False
    assert await coord.async_turn_off() is True
    settle_polls_left = 50
    with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
        assert await coord.async_turn_on(wake_timeout=0.01, poll_interval=0.005) is False

    # 4. Turn on settles when ADB disconnects (AdbConnectionError) as hardware sleeps
    settle_polls_left = 0
    simulated_state = "Awake"
    assert await coord.async_turn_off() is True

    disconnect_once = True

    async def shell_with_disconnect(cmd: str, timeout: float = 5.0, retry: bool | None = None) -> str:
        nonlocal disconnect_once, simulated_state
        if "mWakefulness" in cmd or "dumpsys power" in cmd:
            if disconnect_once:
                disconnect_once = False
                simulated_state = "Asleep"
                raise AdbConnectionError("Connection closed by peer")
            return f"mWakefulness={simulated_state}"
        return ""

    adb.async_shell = AsyncMock(side_effect=shell_with_disconnect)
    with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
        assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.01) is True
    assert simulated_state == "Awake"
    assert coord.data["awake"] is True

    await coord.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AdbError, TimeoutError, OSError, AdbConnectionError])
async def test_competing_power_off_followed_by_on_transient_error(error_type: type[Exception]) -> None:
    """Verify transient ADB errors during shutdown settling do not cause early wake race."""
    awake = True
    off_requested = False
    injected_error = False
    keys: list[int] = []
    tasks: list[asyncio.Task[None]] = []

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.08)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_error
        if off_requested and not injected_error:
            injected_error = True
            raise error_type("Transient query failure while still awake")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, off_requested
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            off_requested = True
            tasks.append(asyncio.create_task(delayed_off()))
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            results = await asyncio.gather(
                coord.async_turn_off(),
                coord.async_turn_on(wake_timeout=1.0, poll_interval=0.01),
            )
        await asyncio.gather(*tasks)
        assert results == [True, True]
        assert awake is True
        assert coord.data["awake"] is True
        assert KEYCODE_WAKEUP in keys
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_on_multiple_transient_errors_settling() -> None:
    """Verify multiple transient errors during shutdown settling preserve pending shutdown."""
    awake = True
    off_requested = False
    injected_errors = 0
    keys: list[int] = []
    tasks: list[asyncio.Task[None]] = []

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.08)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        if off_requested and injected_errors < 2:
            injected_errors += 1
            raise AdbError("Transient query failure")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, off_requested
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            off_requested = True
            tasks.append(asyncio.create_task(delayed_off()))
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            results = await asyncio.gather(
                coord.async_turn_off(),
                coord.async_turn_on(wake_timeout=1.0, poll_interval=0.01),
            )
        await asyncio.gather(*tasks)
        assert results == [True, True]
        assert awake is True
        assert coord.data["awake"] is True
        assert KEYCODE_WAKEUP in keys
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_on_sustained_deep_sleep_disconnect() -> None:
    """Verify sustained connection loss during shutdown settling breaks to Stage 1 WoWLAN wake."""
    in_deep_sleep = False
    wowlan_called = False
    awake = False

    async def fake_wowlan() -> None:
        nonlocal in_deep_sleep, awake, wowlan_called
        wowlan_called = True
        in_deep_sleep = False
        awake = True

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal in_deep_sleep, awake
        if in_deep_sleep:
            raise AdbConnectionError("Connection closed: projector in deep sleep")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal in_deep_sleep, awake
        if keycode == KEYCODE_POWER:
            in_deep_sleep = True
            awake = False

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        awake = True
        with patch.object(coord, "_send_wowlan_knocks", side_effect=fake_wowlan):
            assert await coord.async_turn_off() is True
            assert in_deep_sleep is True
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.01) is True
        assert wowlan_called is True
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_on_interleaved_transient_errors_settling() -> None:
    """Verify interleaved transient errors reset error counter and wait for confirmed standby."""
    awake = True
    off_requested = False
    call_count = 0
    keys: list[int] = []
    tasks: list[asyncio.Task[None]] = []

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.08)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal call_count
        call_count += 1
        # Interleave transient errors: fail on call 1 and 3 while off_requested
        if off_requested and call_count in (1, 3):
            raise AdbError("Interleaved transient error")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, off_requested
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            off_requested = True
            tasks.append(asyncio.create_task(delayed_off()))
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            results = await asyncio.gather(
                coord.async_turn_off(),
                coord.async_turn_on(wake_timeout=1.0, poll_interval=0.01),
            )
        await asyncio.gather(*tasks)
        assert results == [True, True]
        assert awake is True
        assert coord.data["awake"] is True
        assert KEYCODE_WAKEUP in keys
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_on_transient_error_in_wake_loop() -> None:
    """Verify a transient error during Stage-2 wake poll retries properly and sends wake keys."""
    awake = True
    off_requested = False
    keys: list[int] = []
    tasks: list[asyncio.Task[None]] = []
    wake_error_injected = False

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.05)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal wake_error_injected
        # Once standby is reached (awake is False), inject a transient error in the wake loop
        if not awake and not wake_error_injected:
            wake_error_injected = True
            raise AdbError("Transient error during Stage-2 wake poll")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, off_requested
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            off_requested = True
            tasks.append(asyncio.create_task(delayed_off()))
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            results = await asyncio.gather(
                coord.async_turn_off(),
                coord.async_turn_on(wake_timeout=1.0, poll_interval=0.01),
            )
        await asyncio.gather(*tasks)
        assert results == [True, True]
        assert awake is True
        assert coord.data["awake"] is True
        assert KEYCODE_WAKEUP in keys
        assert wake_error_injected is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_immediately_on_three_temporary_errors_reproduction() -> None:
    """Reproduce Finding #2: 3 temporary connection errors during shutdown settling.

    Scenario:
    - Off -> immediately on, with three temporary connection failures.
    - Simulated projector finishes shutting down at 0.80 seconds.
    - Coordinator must NOT return success at 0.40s without sending a wake key.
    - Coordinator must wait for standby and send KEYCODE_WAKEUP.
    - Simulated projector and Home Assistant coordinator both end awake is True.
    """
    awake = True
    injected_errors = 0
    keys: list[int] = []
    shutdown_task: asyncio.Task[None] | None = None

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.80)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        # Inject 3 temporary connection failures after shutdown begins while device is shutting down
        if shutdown_task is not None and injected_errors < 3:
            injected_errors += 1
            raise AdbConnectionError("Temporary connection failure")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, shutdown_task
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            shutdown_task = asyncio.create_task(delayed_off())
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_off() is True
            turn_on_start = time.monotonic()
            assert await coord.async_turn_on(wake_timeout=5.0) is True
            elapsed = time.monotonic() - turn_on_start

        if shutdown_task:
            await shutdown_task

        assert injected_errors == 3
        # Must not have exited early at 0.40s; must have waited for shutdown to settle at ~0.80s
        assert elapsed >= 0.75
        # Wake key must have been sent
        assert KEYCODE_WAKEUP in keys
        # Both simulated projector and Home Assistant coordinator end awake is True
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AdbError, TimeoutError, OSError, AdbConnectionError])
async def test_competing_power_off_followed_by_on_three_transient_errors(error_type: type[Exception]) -> None:
    """Verify 3 transient ADB errors of various types preserve pending shutdown and send wake key."""
    awake = True
    off_requested = False
    injected_errors = 0
    keys: list[int] = []
    tasks: list[asyncio.Task[None]] = []

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.08)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        if off_requested and injected_errors < 3:
            injected_errors += 1
            raise error_type("Temporary connection failure")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, off_requested
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            off_requested = True
            tasks.append(asyncio.create_task(delayed_off()))
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            results = await asyncio.gather(
                coord.async_turn_off(),
                coord.async_turn_on(wake_timeout=1.0, poll_interval=0.01),
            )
        await asyncio.gather(*tasks)
        assert results == [True, True]
        assert injected_errors == 3
        assert awake is True
        assert coord.data["awake"] is True
        assert KEYCODE_WAKEUP in keys
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_immediately_on_three_errors_indeterminate_state() -> None:
    """Verify indeterminate dumpsys output (awake is None) during settling does not trigger early wake."""
    awake: bool | None = True
    injected_errors = 0
    keys: list[int] = []
    shutdown_task: asyncio.Task[None] | None = None

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.80)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        if shutdown_task is not None and injected_errors < 3:
            injected_errors += 1
            raise AdbConnectionError("Temporary connection failure")
        if shutdown_task is not None and injected_errors == 3:
            injected_errors += 1
            return ""  # Indeterminate output -> awake is None
        if awake is True:
            return "mWakefulness=Awake"
        if awake is False:
            return "mWakefulness=Asleep"
        return ""

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, shutdown_task
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            shutdown_task = asyncio.create_task(delayed_off())
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_off() is True
            turn_on_start = time.monotonic()
            assert await coord.async_turn_on(wake_timeout=5.0) is True
            elapsed = time.monotonic() - turn_on_start

        if shutdown_task:
            await shutdown_task

        assert injected_errors >= 3
        # Must not have exited early at 0.40s or on indeterminate output; must have waited for standby
        assert elapsed >= 0.75
        assert KEYCODE_WAKEUP in keys
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_immediately_on_wakeup_key_error_retry() -> None:
    """Verify that if KEYCODE_WAKEUP fails on first attempt, coordinator retries and completes wake."""
    awake = True
    injected_errors = 0
    wakeup_failed_once = False
    keys: list[int] = []
    shutdown_task: asyncio.Task[None] | None = None

    async def delayed_off() -> None:
        nonlocal awake
        await asyncio.sleep(0.40)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        if shutdown_task is not None and injected_errors < 3:
            injected_errors += 1
            raise AdbConnectionError("Temporary connection failure")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, shutdown_task, wakeup_failed_once
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            shutdown_task = asyncio.create_task(delayed_off())
        elif keycode == KEYCODE_WAKEUP:
            if not wakeup_failed_once:
                wakeup_failed_once = True
                raise AdbConnectionError("Transient socket error sending KEYCODE_WAKEUP")
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_off() is True
            assert await coord.async_turn_on(wake_timeout=5.0, poll_interval=0.1) is True

        if shutdown_task:
            await shutdown_task

        assert wakeup_failed_once is True
        assert keys.count(KEYCODE_WAKEUP) == 2
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_power_off_then_immediately_on_no_wowlan_flooding_during_settling() -> None:
    """Verify WoWLAN knocks are not sent repeatedly every 0.2s while waiting for hardware shutdown."""
    awake = True
    injected_errors = 0
    keys: list[int] = []
    shutdown_task: asyncio.Task[None] | None = None

    async def delayed_off() -> None:
        nonlocal awake
        # Longer shutdown window to test settle loop rate limiting
        await asyncio.sleep(0.80)
        awake = False

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal injected_errors
        if shutdown_task is not None and injected_errors < 3:
            injected_errors += 1
            raise AdbConnectionError("Temporary connection failure")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, shutdown_task
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            shutdown_task = asyncio.create_task(delayed_off())
        elif keycode == KEYCODE_WAKEUP:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    wowlan_mock = AsyncMock()
    try:
        with patch.object(coord, "_send_wowlan_knocks", wowlan_mock):
            assert await coord.async_turn_off() is True
            assert await coord.async_turn_on(wake_timeout=5.0) is True

        if shutdown_task:
            await shutdown_task

        # After breaking from settle loop, attempt 0 sends WoWLAN once; it must NOT flood every 0.4s
        assert wowlan_mock.call_count == 1
        assert KEYCODE_WAKEUP in keys
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_retries_when_first_wake_keys_dropped_by_waking_soc() -> None:
    """Verify turn_on does not return early if the first KEYCODE_WAKEUP/KEYCODE_POWER is ignored while waking."""
    awake = False
    power_key_attempts = 0
    keys: list[int] = []

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, power_key_attempts
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            power_key_attempts += 1
            # First KEYCODE_POWER is ignored by waking SoC; second attempt wakes the projector
            if power_key_attempts >= 2:
                awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02) is True

        assert power_key_attempts == 2
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_verifies_hardware_when_optimistic_awake_is_set() -> None:
    """Verify turn_on checks actual hardware wakefulness instead of blindly trusting _optimistic_awake."""
    awake = False
    keys: list[int] = []

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake
        keys.append(keycode)
        if keycode in (KEYCODE_WAKEUP, KEYCODE_POWER):
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        # Simulate stale optimistic awake state while hardware is actually still Asleep
        coord._optimistic_awake = True
        coord._power_transition_until = time.monotonic() + 10.0

        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02) is True

        assert awake is True
        assert KEYCODE_WAKEUP in keys
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_ignored_power_toggle_retried_and_succeeds() -> None:
    """Verify genuinely ignored KEYCODE_POWER retries after settle window and succeeds."""
    awake = False
    power_key_attempts = 0
    keys: list[int] = []

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake, power_key_attempts
        keys.append(keycode)
        if keycode == KEYCODE_POWER:
            power_key_attempts += 1
            # First KEYCODE_POWER is genuinely ignored by hardware; second attempt wakes device
            if power_key_attempts >= 2:
                awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.08) is True

        assert power_key_attempts == 2
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_delayed_transition_does_not_double_toggle() -> None:
    """Verify delayed wake transition over multiple settle polls does not trigger second KEYCODE_POWER."""
    poll_count_after_power = 0
    awake = False
    power_keys: list[int] = []

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal poll_count_after_power, awake
        if power_keys:
            poll_count_after_power += 1
            # Device takes 3 settle polls to transition from Asleep to Awake
            if poll_count_after_power >= 3:
                awake = True
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.2) is True

        # Exactly 1 KEYCODE_POWER must be sent (no double toggle)
        assert len(power_keys) == 1
        assert poll_count_after_power >= 3
        assert awake is True
        assert coord.data["awake"] is True
        assert coord.data["online"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_transient_adb_error_after_power_toggle_does_not_double_toggle() -> None:
    """Verify indeterminate state (None/error) after KEYCODE_POWER does not trigger another toggle."""
    awake = False
    power_keys: list[int] = []
    error_injected_count = 0

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal error_injected_count, awake
        if power_keys:
            if error_injected_count < 2:
                error_injected_count += 1
                raise AdbConnectionError("Transient socket disconnect during display ignition")
            if error_injected_count == 2:
                error_injected_count += 1
                return ""  # Indeterminate output -> returns None
            # After indeterminate period, ADB reconnects and reports confirmed Awake
            awake = True
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.06) is True

        # Indeterminate state must NOT trigger a second toggle; exactly 1 KEYCODE_POWER sent
        assert len(power_keys) == 1
        assert error_injected_count >= 3
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_inconclusive_state_timeout_returns_false_and_preserves_none() -> None:
    """Verify inconclusive state throughout timeout returns False and preserves actual state without inventing awake."""
    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=AdbConnectionError("Target completely unreachable"))
    adb.async_send_key = AsyncMock()

    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    # Set initial coordinator state with awake=None
    coord.data = coord.default_state(online=False, awake=None)

    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            result = await coord.async_turn_on(wake_timeout=0.05, poll_interval=0.01)

        assert result is False
        assert coord._optimistic_awake is None
        assert coord.data["awake"] is None
        assert coord.data["online"] is False
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_competing_turn_on_serialized_and_no_duplicate_toggle() -> None:
    """Verify competing turn_on requests are serialized by _power_lock and second request sees Awake."""
    awake = False
    power_keys: list[int] = []

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            # Fire two concurrent turn_on requests
            res1, res2 = await asyncio.gather(
                coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.08),
                coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.08),
            )

        assert res1 is True
        assert res2 is True
        # KEYCODE_POWER must only be sent once; second request sees projector is Awake
        assert len(power_keys) == 1
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_turn_on_power_key_timeout_during_slow_transition_does_not_double_toggle() -> None:
    """Verify a slow KEYCODE_POWER that times out while starting transition does not double-toggle."""
    awake = False
    power_keys: list[int] = []
    polls_after_power = 0

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal polls_after_power, awake
        if power_keys:
            polls_after_power += 1
            if polls_after_power == 1:
                # First poll during settle window still sees Asleep while transition is underway
                return "mWakefulness=Asleep"
            if polls_after_power == 2:
                # Second poll hits transient indeterminate state during display ignition
                raise TimeoutError("ADB busy during display ignition")
            awake = True
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
            # Command reaches projector and begins transition, but ADB call times out
            raise TimeoutError("Command response delayed")

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            assert await coord.async_turn_on(wake_timeout=2.0, poll_interval=0.02, power_on_settle_timeout=0.05) is True

        assert len(power_keys) == 1
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_persistent_adb_failure_after_power() -> None:
    """1. Persistent ADB failure after POWER: bounded failure, retries stop, lock released."""
    power_accepted = False
    shell_calls_after_power = 0

    async def shell(command: str, **kwargs: object) -> str:
        nonlocal shell_calls_after_power
        if power_accepted:
            shell_calls_after_power += 1
            raise AdbConnectionError("Persistent ADB read failure after POWER")
        return "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal power_accepted
        if keycode == KEYCODE_POWER:
            power_accepted = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.15, poll_interval=0.02, power_on_settle_timeout=0.04),
                timeout=0.8,
            )

        assert ok is False
        assert power_accepted is True
        assert coord._power_lock.locked() is False
        assert coord.data is not None
        assert coord.data["online"] is False
        assert coord.data["awake"] is None

        calls_at_exit = shell_calls_after_power
        await asyncio.sleep(0.05)
        assert shell_calls_after_power == calls_at_exit

        # Verify another power request can proceed without blocking
        adb.async_shell = AsyncMock(return_value="mWakefulness=Asleep")
        off_ok = await asyncio.wait_for(coord.async_turn_off(), timeout=0.5)
        assert off_ok is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_persistent_wakeup_command_failure() -> None:
    """2. Persistent WAKEUP command failure: bounded completion and lock release."""
    wakeup_attempts = 0

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal wakeup_attempts
        if keycode == KEYCODE_WAKEUP:
            wakeup_attempts += 1
            raise AdbError("Persistent WAKEUP failure")

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.15, poll_interval=0.02),
                timeout=0.8,
            )

        assert ok is False
        assert wakeup_attempts >= 1
        assert coord._power_lock.locked() is False
        assert coord.data is not None
        assert coord.data["online"] is True
        assert coord.data["awake"] is False

        attempts_at_exit = wakeup_attempts
        await asyncio.sleep(0.05)
        assert wakeup_attempts == attempts_at_exit

        # Subsequent power request proceeds without blocking
        off_ok = await asyncio.wait_for(coord.async_turn_off(), timeout=0.5)
        assert off_ok is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_working_adb_with_unknown_wakefulness() -> None:
    """3. Working ADB with unknown wakefulness: awake=None while online=True."""
    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(return_value="PowerManagerService: unrecognized power state output")
    adb.async_send_key = AsyncMock()

    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        # Case A: coord.data initially None
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok1 = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.10, poll_interval=0.02),
                timeout=0.8,
            )
        assert ok1 is False
        assert coord.data is not None
        assert coord.data["awake"] is None
        assert coord.data["online"] is True

        # Case B: coord.data initially populated
        coord.data = coord.default_state(online=False, awake=False)
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok2 = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.10, poll_interval=0.02),
                timeout=0.8,
            )
        assert ok2 is False
        assert coord.data["awake"] is None
        assert coord.data["online"] is True
        adb.async_send_key.assert_not_awaited()
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_accepted_but_delayed_power_no_double_toggle() -> None:
    """4. Accepted but delayed POWER: time-driven toggle after old settle window does not double-toggle."""
    awake = False
    power_keys: list[int] = []
    scheduled_tasks: list[asyncio.Task[None]] = []

    async def _delayed_toggle(delay: float) -> None:
        nonlocal awake
        await asyncio.sleep(delay)
        awake = not awake

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
            # Transition completes at 0.20s, which is after HEAD's 0.12s (0.6 * 0.2) settling window
            scheduled_tasks.append(asyncio.create_task(_delayed_toggle(0.20)))

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.6, poll_interval=0.02),
                timeout=1.5,
            )

        if scheduled_tasks:
            await asyncio.wait_for(asyncio.gather(*scheduled_tasks), timeout=1.0)

        assert ok is True
        assert power_keys == [KEYCODE_POWER]
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_ambiguous_command_delivery() -> None:
    """5. Ambiguous command delivery: verify state without blindly repeating POWER (before & after deadline)."""
    # Scenario A: Ambiguous timeout on POWER, transition completes after settle window but before deadline
    awake = False
    power_keys: list[int] = []
    scheduled_tasks: list[asyncio.Task[None]] = []

    async def _delayed_toggle(delay: float) -> None:
        nonlocal awake
        await asyncio.sleep(delay)
        awake = not awake

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key_timeout(keycode: int, **kwargs: object) -> None:
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
            scheduled_tasks.append(asyncio.create_task(_delayed_toggle(0.14)))
            raise TimeoutError("Ambiguous timeout after delivering KEYCODE_POWER")

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key_timeout)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.45, poll_interval=0.02, power_on_settle_timeout=0.04),
                timeout=1.2,
            )

        if scheduled_tasks:
            await asyncio.wait_for(asyncio.gather(*scheduled_tasks), timeout=1.0)

        assert ok is True
        assert power_keys == [KEYCODE_POWER]
        assert awake is True
        assert coord.data["awake"] is True

        # Scenario B: Ambiguous disconnect on POWER, transition completes AFTER overall wake deadline
        awake = False
        power_keys.clear()
        scheduled_tasks.clear()

        async def key_late_disconnect(keycode: int, **kwargs: object) -> None:
            if keycode == KEYCODE_POWER:
                power_keys.append(keycode)
                scheduled_tasks.append(asyncio.create_task(_delayed_toggle(0.25)))
                raise AdbConnectionError("Socket closed ambiguously during KEYCODE_POWER")

        adb.async_send_key = AsyncMock(side_effect=key_late_disconnect)
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok_late = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.12, poll_interval=0.02, power_on_settle_timeout=0.03),
                timeout=0.8,
            )

        assert ok_late is False
        assert power_keys == [KEYCODE_POWER]
        assert coord.data["awake"] is False

        if scheduled_tasks:
            await asyncio.wait_for(asyncio.gather(*scheduled_tasks), timeout=1.0)
        assert awake is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_genuinely_ignored_wake_commands_recover() -> None:
    """6. Genuinely ignored wake commands: recovery completes within a single turn_on request."""
    awake = False
    power_keys: list[int] = []
    start = time.monotonic()

    async def shell(command: str, **kwargs: object) -> str:
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
        # Waking SoC ignores all wake commands during initial 0.06s window
        if time.monotonic() - start < 0.06:
            return
        if keycode == KEYCODE_POWER:
            awake = True

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            ok = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.6, poll_interval=0.02, power_on_settle_timeout=0.08),
                timeout=1.5,
            )

        assert ok is True
        assert len(power_keys) == 2
        assert awake is True
        assert coord.data["awake"] is True
    finally:
        await coord.async_shutdown()


@pytest.mark.asyncio
async def test_regression_concurrent_power_requests_serialization_and_recovery() -> None:
    """7. Concurrent power requests: serialized, no duplicate POWER, failed wake does not block subsequent calls."""
    awake = False
    power_keys: list[int] = []
    fail_reads_after_power = True
    in_flight = 0
    max_in_flight = 0

    async def shell(command: str, **kwargs: object) -> str:
        if fail_reads_after_power and power_keys:
            raise AdbConnectionError("ADB read failed after POWER")
        return "mWakefulness=Awake" if awake else "mWakefulness=Asleep"

    async def key(keycode: int, **kwargs: object) -> None:
        nonlocal awake
        if keycode == KEYCODE_POWER:
            power_keys.append(keycode)
            if not fail_reads_after_power:
                await asyncio.sleep(0.03)
                awake = not awake

    adb = MagicMock()
    adb.async_close = AsyncMock()
    adb.async_shell = AsyncMock(side_effect=shell)
    adb.async_send_key = AsyncMock(side_effect=key)
    coord = XgimiDataUpdateCoordinator(_make_fake_hass(), "127.0.0.1", "Test", adb_client=adb)

    orig_read = coord._async_read_wakefulness

    async def tracked_read(timeout: float = 3.0) -> bool | None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            return await orig_read(timeout=timeout)
        finally:
            in_flight -= 1

    coord._async_read_wakefulness = tracked_read  # type: ignore[method-assign]
    try:
        with patch.object(coord, "_send_wowlan_knocks", AsyncMock()):
            # 1) Failed/timed-out wake must release lock and not block subsequent requests
            ok_fail = await asyncio.wait_for(
                coord.async_turn_on(wake_timeout=0.12, poll_interval=0.02, power_on_settle_timeout=0.03),
                timeout=0.8,
            )
            assert ok_fail is False
            assert coord._power_lock.locked() is False

            # 2) Recover ADB and run two concurrent turn_on requests -> serialized, single POWER
            fail_reads_after_power = False
            power_keys.clear()
            res1, res2 = await asyncio.wait_for(
                asyncio.gather(
                    coord.async_turn_on(wake_timeout=0.5, poll_interval=0.02),
                    coord.async_turn_on(wake_timeout=0.5, poll_interval=0.02),
                ),
                timeout=1.2,
            )
            assert res1 is True
            assert res2 is True
            assert max_in_flight == 1
            assert power_keys == [KEYCODE_POWER]
            assert awake is True

            # 3) Subsequent turn_off proceeds cleanly
            off_ok = await asyncio.wait_for(coord.async_turn_off(), timeout=0.8)
            assert off_ok is True
            assert awake is False
    finally:
        await coord.async_shutdown()
