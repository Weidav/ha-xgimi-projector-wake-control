"""Tests for the XGIMI Two-Stage Wake & ADB Coordinator (coordinator.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from custom_components.xgimi.adb_client import AdbConnectionError
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
        patch("custom_components.xgimi.coordinator._send_udp_knock", side_effect=_record_udp),
        patch("custom_components.xgimi.coordinator._send_tcp_knock", side_effect=_record_tcp),
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
    # Attempt 2: ADB reachable, mWakefulness=Asleep -> sends KEYCODE_WAKEUP, rechecks Asleep -> sends KEYCODE_POWER
    adb.async_shell = AsyncMock(
        side_effect=[
            AdbConnectionError("deep sleep"),
            "mWakefulness=Asleep",
            "mWakefulness=Asleep",
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
    assert coord._suppress_next_reload is True
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
