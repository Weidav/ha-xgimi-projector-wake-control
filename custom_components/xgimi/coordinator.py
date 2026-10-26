"""DataUpdateCoordinator and Two-Stage WoWLAN + ADB engine for XGIMI Projectors."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .adb_client import AdbError, XgimiAdbClient
from .const import (
    CAST_WAKE_PORT,
    CONF_ADB_PORT,
    CONF_MAC,
    DEFAULT_ADB_PORT,
    DEFAULT_HDMI_INPUT_IDS,
    INPUT_SOURCE_APPS,
    INPUT_SOURCE_HDMI1,
    INPUT_SOURCE_HDMI2,
    INPUT_SOURCES,
    KEYCODE_POWER,
    KEYCODE_WAKEUP,
    MDNS_WAKE_PORT,
    REMOTE_WAKE_PORT,
    WOL_DISCARD_PORT,
    WOL_WAKE_PORT,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=10)

# Compound one-liner executed once per poll cycle over the pooled ADB connection
POLL_SHELL_COMMAND = (
    "echo '---WAKE---'; "
    "dumpsys power | grep -E 'mWakefulness=|Display Power'; "
    "echo '---APP1---'; "
    "getprop sys.mtk.running.application; "
    "echo '---APP2---'; "
    "settings get global running_package_name; "
    "echo '---META---'; "
    "getprop ro.product.model; "
    "getprop ro.build.version.release; "
    "cat /sys/class/net/wlan0/address 2>/dev/null"
)

# Minimal standard mDNS PTR query for _googlecast._tcp.local
MDNS_CAST_QUERY = (
    b"\x00\x00"  # Transaction ID
    b"\x00\x00"  # Flags: Standard query
    b"\x00\x01"  # Questions: 1
    b"\x00\x00"  # Answer RRs
    b"\x00\x00"  # Authority RRs
    b"\x00\x00"  # Additional RRs
    b"\x0b_googlecast\x04_tcp\x05local\x00"
    b"\x00\x0c"  # Type: PTR (12)
    b"\x80\x01"  # Class: IN, unicast response requested
)


def format_host(host: str) -> str:
    """Format host for URLs, bracket IPv6 literals if needed."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def format_mac(mac: str | None) -> str | None:
    """Format and validate a MAC address as xx:xx:xx:xx:xx:xx or return None."""
    if not mac:
        return None
    cleaned = mac.replace(":", "").replace("-", "").replace(".", "").strip().lower()
    if len(cleaned) == 12 and all(c in "0123456789abcdef" for c in cleaned):
        return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    return None


def create_magic_packet(mac: str | None) -> bytes | None:
    """Create a standard 102-byte Wake-on-LAN magic packet."""
    formatted = format_mac(mac)
    if not formatted:
        return None
    mac_bytes = bytes.fromhex(formatted.replace(":", ""))
    return b"\xff" * 6 + mac_bytes * 16


def resolve_mac_from_arp(host: str) -> str | None:
    """Attempt to resolve MAC address for host from local /proc/net/arp table."""
    clean_host = host.strip("[]")
    if ":" in clean_host:
        return None
    ip = clean_host
    with contextlib.suppress(OSError):
        parts = clean_host.split(".")
        if not (len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)):
            ip = socket.gethostbyname(clean_host)

    with contextlib.suppress(OSError), Path("/proc/net/arp").open(encoding="utf-8") as arp_file:
        for line in arp_file:
            parts = line.split()
            if len(parts) >= 4 and parts[0] == ip:
                candidate = parts[3]
                mac = format_mac(candidate)
                if mac and mac != "00:00:00:00:00:00":
                    return mac
    return None


def _send_udp_knock(host: str, port: int, payload: bytes = b"\x00", repeat: int = 2) -> None:
    """Send best-effort UDP wake knock datagrams (unicast + IPv4 broadcast)."""
    clean_host = host.strip("[]")
    is_ipv6 = ":" in clean_host
    sock_family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    try:
        with socket.socket(sock_family, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            if not is_ipv6:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(repeat):
                sock.sendto(payload, (clean_host, port))
    except OSError as err:
        _LOGGER.debug(
            "UDP wake knock to %s:%s failed: %s (%s)",
            host,
            port,
            type(err).__name__,
            err,
        )

    # Also send broadcast UDP for IPv4 to bypass stale ARP tables on sleeping targets
    if not is_ipv6 and clean_host != "255.255.255.255":
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b_sock:
            b_sock.setblocking(False)
            b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(repeat):
                b_sock.sendto(payload, ("255.255.255.255", port))


def _send_mdns_knock(host: str) -> None:
    """Send mDNS multicast and broadcast queries to wake Android TV Cast WoWLAN."""
    clean_host = host.strip("[]")
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as m_sock,
    ):
        m_sock.setblocking(False)
        m_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        m_sock.sendto(MDNS_CAST_QUERY, ("224.0.0.251", MDNS_WAKE_PORT))

    _send_udp_knock(clean_host, MDNS_WAKE_PORT, payload=MDNS_CAST_QUERY, repeat=1)


async def _send_tcp_knock(host: str, port: int, timeout: float = 0.8) -> None:
    """Send a quick TCP SYN connection attempt to trigger MediaTek WoWLAN."""
    clean_host = host.strip("[]")
    with contextlib.suppress(TimeoutError, OSError):
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(clean_host, port),
            timeout=timeout,
        )
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)


send_udp_knock = _send_udp_knock
send_mdns_knock = _send_mdns_knock
send_tcp_knock = _send_tcp_knock


def parse_wakefulness(output: str) -> bool | None:
    """Parse Android dumpsys power output into True (Awake/Dreaming), False (Asleep/Dozing), or None."""
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if "mWakefulness=" in stripped:
            val = stripped.split("mWakefulness=", 1)[1].split()[0].strip(",;").lower()
            if val in ("awake", "dreaming"):
                return True
            if val in ("asleep", "dozing"):
                return False
    for line in output.splitlines():
        stripped = line.strip()
        if "Display Power:" in stripped and "state=" in stripped:
            val = stripped.split("state=", 1)[1].split()[0].strip(",;").upper()
            if val in ("ON", "DOZE", "OFF"):
                return val == "ON"
    return None


def parse_hdmi_inputs_from_dumpsys(dumpsys_output: str) -> dict[str, str]:
    """Extract root hardware HDMI 1 and HDMI 2 passthrough input IDs from dumpsys tv_input.

    Matches InputService.java: selects root HDMI TvInputInfo entries (no parentId)
     across both MediaTek (mId=/mParentId=/mHdmiPortId=) and standard AOSP
    (id=/parentId=/port_id:) dumpsys formats so child CEC logical devices do not
    shadow the physical HDMI ports.
    """
    discovered: dict[str, str] = {}
    if not dumpsys_output:
        return discovered

    # Match lines or blocks describing TvInputInfo
    for block in re.split(r"(?=TvInputInfo\{)", dumpsys_output):
        if "TvInputInfo" not in block:
            continue
        # Skip CEC child inputs that have a non-null parentId or CEC HdmiDeviceInfo
        parent_match = re.search(r"\b(?:mParentId|parentId)\s*[=:]\s*([^\s,}\]]+)", block)
        if parent_match and parent_match.group(1) not in ("null", "None", ""):
            continue
        if "HdmiDeviceInfo[CEC:" in block:
            continue

        id_match = re.search(r"\b(?:mId|id)\s*[=:]\s*([^\s,}\]]+)", block)
        if not id_match:
            continue
        input_id = id_match.group(1)

        port_match = re.search(
            r"\b(?:mHdmiPortId|hdmiPortId|portId|port_id|port)\s*[=:]\s*([12])\b",
            block,
        )
        if port_match:
            port_num = port_match.group(1)
            label = INPUT_SOURCE_HDMI1 if port_num == "1" else INPUT_SOURCE_HDMI2
            discovered.setdefault(label, input_id)
        elif "HW5" in input_id or input_id.upper().endswith("HDMI1"):
            discovered.setdefault(INPUT_SOURCE_HDMI1, input_id)
        elif "HW6" in input_id or input_id.upper().endswith("HDMI2"):
            discovered.setdefault(INPUT_SOURCE_HDMI2, input_id)

    return discovered


class XgimiDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator managing two-stage WoWLAN wake, ADB state polling, and command dispatch."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        name: str,
        *,
        config_entry: ConfigEntry | None = None,
        adb_client: XgimiAdbClient | None = None,
        adb_port: int = DEFAULT_ADB_PORT,
    ) -> None:
        """Initialize the XGIMI coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=SCAN_INTERVAL,
            config_entry=config_entry,
        )
        self.host = host.strip()
        self.device_name = name
        self.adb_port = (
            config_entry.options.get(
                CONF_ADB_PORT,
                config_entry.data.get(CONF_ADB_PORT, adb_port),
            )
            if config_entry is not None
            else adb_port
        )
        configured_mac = (
            config_entry.options.get(CONF_MAC) or config_entry.data.get(CONF_MAC) if config_entry is not None else None
        )
        self.mac: str | None = format_mac(configured_mac) or resolve_mac_from_arp(self.host)

        self.adb = adb_client or XgimiAdbClient(self.host, port=self.adb_port, hass=hass)

        self._power_lock = asyncio.Lock()
        self._power_transition_until: float = 0.0
        self._optimistic_awake: bool | None = None
        self._current_input_source: str = INPUT_SOURCE_APPS
        self._hdmi_input_ids: dict[str, str] = dict(DEFAULT_HDMI_INPUT_IDS)
        self._hdmi_discovered: bool = False
        self._model: str = name
        self._sw_version: str = "14"
        self._delayed_refresh_tasks: set[asyncio.Task[Any]] = set()
        self.suppress_next_reload: bool = False

    def default_state(self, *, online: bool = False, awake: bool | None = False) -> dict[str, Any]:
        """Return a structured state dictionary."""
        return {
            "online": online,
            "awake": awake,
            "input_source": self._current_input_source,
            "running_app": None,
            "model": self._model,
            "sw_version": self._sw_version,
            "mac": self.mac,
        }

    _default_state = default_state

    def check_and_reset_suppress_reload(self) -> bool:
        """Check if reload should be suppressed and reset the flag."""
        if self.suppress_next_reload:
            self.suppress_next_reload = False
            return True
        return False

    @property
    def _suppress_next_reload(self) -> bool:
        return self.suppress_next_reload

    @_suppress_next_reload.setter
    def _suppress_next_reload(self, value: bool) -> None:
        self.suppress_next_reload = value

    def _persist_discovered_mac(self, mac: str) -> None:
        """Persist auto-discovered MAC address into the ConfigEntry if not yet saved."""
        formatted = format_mac(mac)
        if not formatted:
            return
        self.mac = formatted
        if self.config_entry is not None and not self.config_entry.data.get(CONF_MAC):
            new_data = {**self.config_entry.data, CONF_MAC: formatted}
            self.suppress_next_reload = True
            updated = False
            with contextlib.suppress(Exception):
                updated = bool(self.hass.config_entries.async_update_entry(self.config_entry, data=new_data))
            if not updated:
                self.suppress_next_reload = False

    async def _send_cast_http_knock(self) -> None:
        """Send best-effort HTTP GET knock to Google Cast port 8010."""
        url = f"http://{format_host(self.host)}:{CAST_WAKE_PORT}/setup/eureka_info"
        with contextlib.suppress(aiohttp.ClientError, TimeoutError, OSError, RuntimeError):
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=1.2)) as resp:
                await resp.read()

    async def _send_wowlan_knocks(self) -> None:
        """Stage 1 Wake: Fire multi-vector WoWLAN stimuli (Magic Packets, mDNS, Cast/Remote knocks)."""
        if not self.mac:
            arp_mac = await self.hass.async_add_executor_job(resolve_mac_from_arp, self.host)
            if arp_mac:
                self._persist_discovered_mac(arp_mac)

        magic_payload = create_magic_packet(self.mac) or (b"\xff" * 102)

        def _sync_udp_burst() -> None:
            _send_udp_knock(self.host, WOL_WAKE_PORT, payload=magic_payload, repeat=3)
            _send_udp_knock(self.host, WOL_DISCARD_PORT, payload=magic_payload, repeat=2)
            _send_udp_knock(self.host, CAST_WAKE_PORT, payload=magic_payload, repeat=2)
            _send_udp_knock(self.host, REMOTE_WAKE_PORT, payload=magic_payload, repeat=2)
            _send_mdns_knock(self.host)

        await self.hass.async_add_executor_job(_sync_udp_burst)
        await asyncio.gather(
            self._send_cast_http_knock(),
            _send_tcp_knock(self.host, CAST_WAKE_PORT),
            _send_tcp_knock(self.host, REMOTE_WAKE_PORT),
            _send_tcp_knock(self.host, self.adb_port),
            return_exceptions=True,
        )

    async def _async_read_wakefulness(self, timeout: float = 3.0) -> bool | None:
        """Query dumpsys power over ADB and return True if Awake, False if Asleep/Dozing."""
        out = await self.adb.async_shell(
            "dumpsys power | grep -E 'mWakefulness=|Display Power'",
            timeout=timeout,
        )
        return parse_wakefulness(out)

    def _schedule_delayed_refreshes(self, delays: tuple[float, ...] = (3.0, 6.0)) -> None:
        """Schedule follow-up coordinator refreshes after power transitions."""
        for delay in delays:
            task = self.hass.async_create_task(self._delayed_refresh(delay))
            self._delayed_refresh_tasks.add(task)
            task.add_done_callback(self._delayed_refresh_tasks.discard)

    async def _delayed_refresh(self, delay: float) -> None:
        """Wait delay seconds and trigger a coordinator refresh."""
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
            await self.async_request_refresh()

    async def async_turn_on(
        self,
        *,
        wake_timeout: float = 24.0,
        poll_interval: float = 1.5,
    ) -> bool:
        """Execute two-stage wake: Stage 1 WoWLAN burst followed by Stage 2 ADB display ignition."""
        async with self._power_lock:
            now = time.monotonic()
            if now < self._power_transition_until and self._optimistic_awake is True:
                return True

            deadline = time.monotonic() + wake_timeout
            attempt = 0

            while True:
                if attempt % 2 == 0:
                    await self._send_wowlan_knocks()
                attempt += 1

                try:
                    awake = await self._async_read_wakefulness(timeout=2.5)
                    if awake is True:
                        self._mark_power_transition(awake=True)
                        return True

                    # Stage 2: SoC is awake and adbd responded; send idempotent KEYCODE_WAKEUP first
                    await self.adb.async_send_key(KEYCODE_WAKEUP, timeout=3.0)
                    await asyncio.sleep(min(0.4, poll_interval / 2))
                    recheck = await self._async_read_wakefulness(timeout=2.5)
                    if recheck is False:
                        await self.adb.async_send_key(KEYCODE_POWER, timeout=3.0)
                    self._mark_power_transition(awake=True)
                    return True
                except (AdbError, TimeoutError, OSError) as err:
                    _LOGGER.debug(
                        "Stage-2 ADB wake poll attempt %d for %s pending: %s",
                        attempt,
                        self.host,
                        err,
                    )

                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(poll_interval)

            _LOGGER.warning("Timed out waiting for XGIMI projector %s to wake", self.host)
            return False

    async def async_turn_off(self) -> bool:
        """Idempotently put the projector into standby after verifying it is currently Awake."""
        async with self._power_lock:
            now = time.monotonic()
            if now < self._power_transition_until and self._optimistic_awake is False:
                return True

            try:
                awake = await self._async_read_wakefulness(timeout=4.0)
            except (AdbError, TimeoutError, OSError) as err:
                _LOGGER.warning("Cannot turn off XGIMI projector %s (ADB unreachable): %s", self.host, err)
                return False

            if awake is False:
                # Already asleep in standby — do NOT inject KEYCODE_POWER (would toggle back on!)
                self._mark_power_transition(awake=False)
                return True

            if awake is not True:
                # Unknown power state — do NOT inject KEYCODE_POWER to prevent waking sleeping projector
                _LOGGER.warning("Cannot turn off XGIMI projector %s (wakefulness state unknown)", self.host)
                return False

            try:
                await self.adb.async_send_key(KEYCODE_POWER, timeout=4.0)
            except (AdbError, TimeoutError, OSError) as err:
                _LOGGER.warning("Failed to send power off keyevent to %s: %s", self.host, err)
                # Ambiguous power toggle failure: reconnect and inspect power state
                with contextlib.suppress(AdbError, TimeoutError, OSError):
                    recheck = await self._async_read_wakefulness(timeout=3.0)
                    if recheck is False:
                        self._mark_power_transition(awake=False)
                        return True
                return False

            self._mark_power_transition(awake=False)
            return True

    def _mark_power_transition(self, *, awake: bool) -> None:
        """Set optimistic state during a 10s hardware transition window and schedule refreshes."""
        self._optimistic_awake = awake
        self._power_transition_until = time.monotonic() + 10.0
        current = dict(self.data) if self.data else self._default_state(online=True, awake=awake)
        current["online"] = True
        current["awake"] = awake
        self.async_set_updated_data(current)
        self._schedule_delayed_refreshes()

    async def _async_ensure_hdmi_inputs(self) -> None:
        """Discover hardware HDMI passthrough input IDs from dumpsys tv_input once per session."""
        if self._hdmi_discovered:
            return
        with contextlib.suppress(AdbError, TimeoutError, OSError):
            raw = await self.adb.async_shell("dumpsys tv_input", timeout=4.0)
            discovered = parse_hdmi_inputs_from_dumpsys(raw)
            if discovered:
                self._hdmi_input_ids.update(discovered)
            self._hdmi_discovered = True

    async def async_set_input_source(self, source: str) -> bool:
        """Switch projector input source between Apps, HDMI 1, and HDMI 2 over ADB."""
        if source not in INPUT_SOURCES:
            return False

        try:
            if source == INPUT_SOURCE_APPS:
                cmd = (
                    "am force-stop com.google.android.tv.inputplayer && "
                    "am start -a android.intent.action.MAIN -c android.intent.category.HOME"
                )
                await self.adb.async_shell(cmd, timeout=5.0)
            else:
                await self._async_ensure_hdmi_inputs()
                input_id = self._hdmi_input_ids.get(source, DEFAULT_HDMI_INPUT_IDS[source])
                encoded_id = quote(input_id, safe="")
                uri = f"content://android.media.tv/passthrough/{encoded_id}"
                cmd = f'am start -a android.intent.action.VIEW -d "{uri}"'
                await self.adb.async_shell(cmd, timeout=5.0)
        except (AdbError, TimeoutError, OSError) as err:
            _LOGGER.warning("Failed to switch input source to %s on %s: %s", source, self.host, err)
            return False

        self._current_input_source = source
        current = dict(self.data) if self.data else self._default_state(online=True, awake=True)
        current["input_source"] = source
        self.async_set_updated_data(current)
        self._schedule_delayed_refreshes(delays=(2.0,))
        return True

    async def async_send_key(self, keycode: int) -> bool:
        """Send an Android keycode over ADB."""
        try:
            await self.adb.async_send_key(keycode, timeout=4.0)
            return True
        except (AdbError, TimeoutError, OSError, ValueError) as err:
            _LOGGER.warning("Failed to send keycode %s to %s: %s", keycode, self.host, err)
            return False

    async def async_reboot(self) -> bool:
        """Reboot the projector via ADB shell."""
        try:
            await self.adb.async_shell("setprop sys.powerctl reboot || svc power reboot", timeout=4.0)
        except (AdbError, TimeoutError, OSError) as err:
            _LOGGER.warning("Failed to reboot projector %s: %s", self.host, err)
            return False

        await self.adb.async_close()
        state = self._default_state(online=False, awake=False)
        self.async_set_updated_data(state)
        return True

    def _parse_compound_poll(self, raw: str) -> dict[str, Any]:
        """Parse the single-command compound ADB poll response."""
        sections: dict[str, list[str]] = {}
        current_key = "HEADER"
        sections[current_key] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if stripped in ("---WAKE---", "---APP1---", "---APP2---", "---META---"):
                current_key = stripped.strip("-")
                sections[current_key] = []
            else:
                sections.setdefault(current_key, []).append(stripped)

        wake_text = "\n".join(sections.get("WAKE", []))
        parsed_awake = parse_wakefulness(wake_text)
        awake = parsed_awake

        # Respect optimistic power transition window if hardware has not yet settled
        now = time.monotonic()
        if now < self._power_transition_until and self._optimistic_awake is not None:
            if awake == self._optimistic_awake:
                self._power_transition_until = 0.0
                self._optimistic_awake = None
            else:
                awake = self._optimistic_awake

        app1 = next((x for x in sections.get("APP1", []) if x and x != "null"), None)
        app2 = next((x for x in sections.get("APP2", []) if x and x != "null"), None)
        running_app = app1 or app2

        if running_app and (
            "com.google.android.tv.inputplayer" in running_app or "com.mediatek.tvinput" in running_app
        ):
            if self._current_input_source not in (INPUT_SOURCE_HDMI1, INPUT_SOURCE_HDMI2):
                self._current_input_source = INPUT_SOURCE_HDMI1
        elif running_app:
            self._current_input_source = INPUT_SOURCE_APPS

        raw_meta = sections.get("META", [])
        if len(raw_meta) >= 1 and raw_meta[0] and not format_mac(raw_meta[0]):
            self._model = raw_meta[0]
        if len(raw_meta) >= 2 and raw_meta[1] and not format_mac(raw_meta[1]):
            self._sw_version = raw_meta[1]
        for line in raw_meta:
            cand_mac = format_mac(line)
            if cand_mac and cand_mac != "00:00:00:00:00:00":
                if not self.mac:
                    self._persist_discovered_mac(cand_mac)
                break

        return {
            "online": True,
            "awake": awake,
            "input_source": self._current_input_source,
            "running_app": running_app,
            "model": self._model,
            "sw_version": self._sw_version,
            "mac": self.mac,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll projector state via a single compound ADB shell command."""
        if not self.mac:
            arp_mac = await self.hass.async_add_executor_job(resolve_mac_from_arp, self.host)
            if arp_mac:
                self._persist_discovered_mac(arp_mac)

        try:
            raw = await self.adb.async_shell(POLL_SHELL_COMMAND, timeout=4.5)
            return self._parse_compound_poll(raw)
        except (AdbError, TimeoutError, OSError) as err:
            _LOGGER.debug("Projector %s offline or in deep sleep: %s", self.host, err)
            now = time.monotonic()
            if now < self._power_transition_until and self._optimistic_awake is True:
                return self._default_state(online=True, awake=True)
            return self._default_state(online=False, awake=False)

    async def async_shutdown(self) -> None:
        """Cancel delayed refresh tasks and close ADB connection on unload."""
        tasks = list(self._delayed_refresh_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._delayed_refresh_tasks.clear()
        await super().async_shutdown()
        await self.adb.async_close()
