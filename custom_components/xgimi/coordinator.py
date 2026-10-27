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
from typing import Any, TypedDict
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
    "echo '---HDMI---'; "
    "dumpsys tv_input | grep -iE 'mCurId|currentInputId|sessionState|mSession|mInputId'; "
    "echo '---META---'; "
    "getprop ro.product.model; "
    "getprop ro.build.version.release; "
    "cat /sys/class/net/wlan0/address 2>/dev/null"
)


CURRENT_INPUT_LINE_RE = re.compile(
    r"\b(?:mCurId|currentInputId|mCurInputId|curInputId|activeInputId|current_input_id|currentInput)\b",
    re.IGNORECASE,
)
ACTIVE_SESSION_LINE_RE = re.compile(
    r"\b(?:sessionState|mSessionState|mSession|mInputId|sessionId)\b",
    re.IGNORECASE,
)


def is_android_shell_error(output: str) -> bool:
    """Check if Android shell output indicates an execution error."""
    if not output:
        return False
    lower = output.lower()

    # Non-zero exit status / return codes take precedence as execution failure
    if re.search(r"\b(?:exit\s*(?:code|status)?|return\s*code|rc)\s*[:=]?\s*([1-9]\d*)\b", lower):
        return True
    if re.search(r"\bexit\s+([1-9]\d*)\b", lower):
        return True

    # Definite fatal failure indicators
    if any(
        err in lower
        for err in (
            "error",
            "failure",
            "failed",
            "exception",
            "permission denied",
            "permission denial",
            "access denied",
            "broken pipe",
            "unable to resolve intent",
            "does not exist",
            "not found",
            "killed",
            "segmentation fault",
            "sigsegv",
            "abort",
        )
    ):
        return True

    # Non-error informational warnings from 'am start' when activity is already running/foregrounded
    if "brought to the front" in lower or "delivered to currently running" in lower:
        return False

    # Unhandled 'activity not started' without benign foregrounding message
    return "activity not started" in lower


def _detect_hdmi_from_text(text: str, hdmi_input_ids: dict[str, str] | None = None) -> set[str]:
    """Detect HDMI input candidates referenced in a text snippet."""
    candidates: set[str] = set()
    if hdmi_input_ids:
        for label, full_id in hdmi_input_ids.items():
            if full_id and full_id in text:
                candidates.add(label)
    if not candidates:
        port_match = re.search(
            r"\b(?:mHdmiPortId|hdmiPortId|portId|port_id|port)\s*[=:]\s*([12])\b",
            text,
            re.IGNORECASE,
        )
        if port_match:
            candidates.add(INPUT_SOURCE_HDMI1 if port_match.group(1) == "1" else INPUT_SOURCE_HDMI2)

    if not candidates:
        lower = text.lower()
        if "hw6" in lower or "hdmi2" in lower or "hdmi_2" in lower:
            candidates.add(INPUT_SOURCE_HDMI2)
        if "hw5" in lower or "hdmi1" in lower or "hdmi_1" in lower:
            candidates.add(INPUT_SOURCE_HDMI1)

    return candidates


def parse_active_hdmi_input(text: str, hdmi_input_ids: dict[str, str] | None = None) -> str | None:
    """Parse active HDMI input source from dumpsys tv_input session output."""
    if not text:
        return None

    # Priority 1: Check explicit current-input lines (e.g. mCurId, currentInputId)
    current_candidates: set[str] = set()
    has_non_hdmi_current = False

    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or not CURRENT_INPUT_LINE_RE.search(trimmed):
            continue
        if re.search(r"\b(?:mCurId|currentInputId|mCurInputId)\s*[=:]\s*(?:null|none)\b", trimmed, re.IGNORECASE):
            continue
        cands = _detect_hdmi_from_text(trimmed, hdmi_input_ids)
        if cands:
            current_candidates.update(cands)
        else:
            # Current input is explicitly set to a non-null, non-HDMI source
            has_non_hdmi_current = True

    if len(current_candidates) == 1 and not has_non_hdmi_current:
        return next(iter(current_candidates))
    if len(current_candidates) > 1 or has_non_hdmi_current:
        return None

    # Priority 2: Check active session lines (SessionState / mSession / mInputId)
    session_candidates: set[str] = set()
    for line in text.splitlines():
        trimmed = line.strip()
        if not trimmed or not ACTIVE_SESSION_LINE_RE.search(trimmed):
            continue
        cands = _detect_hdmi_from_text(trimmed, hdmi_input_ids)
        session_candidates.update(cands)

    if len(session_candidates) == 1:
        return next(iter(session_candidates))

    # Ambiguous data, conflicting sessions, or no active session -> unknown result (None)
    return None


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


def send_udp_knock(host: str, port: int, payload: bytes = b"\x00", repeat: int = 2) -> None:
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
        broadcast_targets = ["255.255.255.255"]
        parts = clean_host.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            subnet_bcast = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
            if subnet_bcast not in (clean_host, "255.255.255.255"):
                broadcast_targets.append(subnet_bcast)
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b_sock:
            b_sock.setblocking(False)
            b_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for bcast_ip in broadcast_targets:
                for _ in range(repeat):
                    with contextlib.suppress(OSError):
                        b_sock.sendto(payload, (bcast_ip, port))


def send_mdns_knock(host: str) -> None:
    """Send mDNS multicast and broadcast queries to wake Android TV Cast WoWLAN."""
    clean_host = host.strip("[]")
    with (
        contextlib.suppress(OSError),
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as m_sock,
    ):
        m_sock.setblocking(False)
        m_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        m_sock.sendto(MDNS_CAST_QUERY, ("224.0.0.251", MDNS_WAKE_PORT))

    send_udp_knock(clean_host, MDNS_WAKE_PORT, payload=MDNS_CAST_QUERY, repeat=1)


async def send_tcp_knock(host: str, port: int, timeout: float = 0.8) -> None:
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


async def async_send_wowlan_burst(
    hass: HomeAssistant,
    host: str,
    mac: str | None,
    adb_port: int = DEFAULT_ADB_PORT,
) -> None:
    """Send Stage-1 WoWLAN wake burst (UDP and TCP knocks)."""
    magic_payload = create_magic_packet(mac) or (b"\xff" * 102)

    def _sync_udp() -> None:
        send_udp_knock(host, WOL_WAKE_PORT, payload=magic_payload, repeat=3)
        send_udp_knock(host, WOL_DISCARD_PORT, payload=magic_payload, repeat=2)
        send_udp_knock(host, CAST_WAKE_PORT, payload=magic_payload, repeat=2)
        send_udp_knock(host, REMOTE_WAKE_PORT, payload=magic_payload, repeat=2)
        send_mdns_knock(host)

    await hass.async_add_executor_job(_sync_udp)
    await asyncio.gather(
        send_tcp_knock(host, CAST_WAKE_PORT),
        send_tcp_knock(host, REMOTE_WAKE_PORT),
        send_tcp_knock(host, adb_port),
        return_exceptions=True,
    )


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


class XgimiCoordinatorData(TypedDict):
    """Structured state dictionary for XGIMI Projector."""

    online: bool
    awake: bool | None
    input_source: str | None
    running_app: str | None
    model: str
    sw_version: str
    mac: str | None


class XgimiDataUpdateCoordinator(DataUpdateCoordinator[XgimiCoordinatorData]):
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
        self.mac: str | None = format_mac(configured_mac)

        self.adb = adb_client or XgimiAdbClient(self.host, port=self.adb_port, hass=hass)

        self._power_lock = asyncio.Lock()
        self._power_transition_until: float = 0.0
        self._optimistic_awake: bool | None = None
        self._power_on_pending: bool = False
        self._current_input_source: str | None = INPUT_SOURCE_APPS
        self._hdmi_input_ids: dict[str, str] = dict(DEFAULT_HDMI_INPUT_IDS)
        self._hdmi_discovered: bool = False
        self._model: str = name
        self._sw_version: str = "14"
        self._delayed_refresh_tasks: set[asyncio.Task[Any]] = set()

    def default_state(self, *, online: bool = False, awake: bool | None = False) -> XgimiCoordinatorData:
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

    def _persist_discovered_mac(self, mac: str) -> None:
        """Persist auto-discovered MAC address into the ConfigEntry if not yet saved."""
        formatted = format_mac(mac)
        if not formatted:
            return
        self.mac = formatted
        if self.config_entry is not None and not self.config_entry.data.get(CONF_MAC):
            new_data = {**self.config_entry.data, CONF_MAC: formatted}
            try:
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            except (ValueError, RuntimeError) as err:
                _LOGGER.debug("Failed to update config entry with discovered MAC: %s", err)

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

        await asyncio.gather(
            self._send_cast_http_knock(),
            async_send_wowlan_burst(self.hass, self.host, self.mac, self.adb_port),
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

    @property
    def _power_off_pending(self) -> bool:
        """Return True if a power-off transition is currently pending."""
        return self._optimistic_awake is False and time.monotonic() < self._power_transition_until

    async def async_turn_on(
        self,
        *,
        wake_timeout: float = 30.0,
        poll_interval: float = 1.5,
        power_on_settle_timeout: float = 8.0,
    ) -> bool:
        """Execute two-stage wake: Stage 1 WoWLAN burst followed by Stage 2 ADB display ignition."""
        async with self._power_lock:
            start_time = time.monotonic()
            deadline = start_time + wake_timeout
            self._power_on_pending = False
            last_observed_wakefulness: bool | None = None
            last_adb_reachable: bool = False

            def _record_wake_failure() -> None:
                base = (
                    self.data
                    if self.data
                    else self.default_state(online=last_adb_reachable, awake=last_observed_wakefulness)
                )
                current: XgimiCoordinatorData = {
                    **base,
                    "online": last_adb_reachable,
                    "awake": last_observed_wakefulness,
                }
                self.async_set_updated_data(current)

            try:
                # 1. Fast check if already awake from a recent confirmed power transition
                now = time.monotonic()
                if now < self._power_transition_until and self._optimistic_awake is True:
                    rem_fast = deadline - time.monotonic()
                    if rem_fast > 0:
                        try:
                            fresh = await self._async_read_wakefulness(timeout=min(2.0, max(0.01, rem_fast)))
                            last_adb_reachable = True
                            last_observed_wakefulness = fresh
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Read wakefulness during active power transition: %s",
                                self.host,
                                time.monotonic() - start_time,
                                "Awake" if fresh is True else ("Asleep" if fresh is False else "None/indeterminate"),
                            )
                            if fresh is True:
                                _LOGGER.info(
                                    "[%s] [+%.2fs] Projector is already awake from recent power transition",
                                    self.host,
                                    time.monotonic() - start_time,
                                )
                                return True
                        except (AdbError, TimeoutError, OSError):
                            last_adb_reachable = False
                            last_observed_wakefulness = None
                    self._optimistic_awake = None
                    self._power_transition_until = 0.0

                # 2. Check if power-off request was recently executed; wait for hardware to settle
                shutdown_pending = self._power_off_pending
                settled_to_standby = False
                settle_deadline = min(self._power_transition_until, deadline)

                if shutdown_pending:
                    settled = False
                    consecutive_errors = 0
                    _LOGGER.info(
                        "[%s] [+%.2fs] Shutdown pending; waiting up to %.2fs for hardware to settle to standby",
                        self.host,
                        time.monotonic() - start_time,
                        max(0.0, settle_deadline - time.monotonic()),
                    )
                    while time.monotonic() < settle_deadline:
                        rem_settle = settle_deadline - time.monotonic()
                        if rem_settle <= 0:
                            break
                        try:
                            settle_state = await self._async_read_wakefulness(timeout=min(2.0, max(0.01, rem_settle)))
                            last_adb_reachable = True
                            last_observed_wakefulness = settle_state
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Standby settle poll wakefulness: %s",
                                self.host,
                                time.monotonic() - start_time,
                                "Awake"
                                if settle_state is True
                                else ("Asleep" if settle_state is False else "None/indeterminate"),
                            )
                            consecutive_errors = 0
                            if settle_state is False:
                                settled = True
                                settled_to_standby = True
                                break
                        except (AdbError, TimeoutError, OSError) as err:
                            last_adb_reachable = False
                            last_observed_wakefulness = None
                            consecutive_errors += 1
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Transient ADB error during standby settle poll (consecutive=%d): %s (%s)",
                                self.host,
                                time.monotonic() - start_time,
                                consecutive_errors,
                                type(err).__name__,
                                err,
                            )
                            if consecutive_errors >= 3:
                                break
                        if time.monotonic() >= settle_deadline:
                            break
                        sleep_time = min(0.2, poll_interval, max(0.001, settle_deadline - time.monotonic()))
                        await asyncio.sleep(sleep_time)

                    if not settled:
                        if time.monotonic() >= deadline:
                            _LOGGER.warning(
                                "[%s] [+%.2fs] Timed out waiting for projector %s to settle into standby before waking",
                                self.host,
                                time.monotonic() - start_time,
                                self.host,
                            )
                            _record_wake_failure()
                            return False
                        if time.monotonic() >= settle_deadline:
                            settled = True

                    if settled_to_standby or time.monotonic() >= settle_deadline:
                        self._optimistic_awake = None
                        self._power_transition_until = 0.0

                attempt = 0
                wake_key_sent = False
                power_toggle_sent = False
                power_toggle_ambiguous = False
                power_toggle_count = 0

                effective_settle_duration = min(
                    power_on_settle_timeout,
                    max(poll_interval * 2, wake_timeout * 0.55),
                )

                while time.monotonic() < deadline:
                    if attempt % 2 == 0:
                        _LOGGER.info(
                            "[%s] [+%.2fs] Sending Stage 1 WoWLAN knock burst (burst %d)",
                            self.host,
                            time.monotonic() - start_time,
                            (attempt // 2) + 1,
                        )
                        await self._send_wowlan_knocks()
                    attempt += 1

                    if time.monotonic() >= deadline:
                        break

                    try:
                        rem_deadline = max(0.01, deadline - time.monotonic())
                        awake = await self._async_read_wakefulness(timeout=min(2.5, rem_deadline))
                        last_adb_reachable = True
                        last_observed_wakefulness = awake
                        _LOGGER.debug(
                            "[%s] [+%.2fs] Stage 2 wake poll wakefulness: %s (attempt %d)",
                            self.host,
                            time.monotonic() - start_time,
                            "Awake" if awake is True else ("Asleep" if awake is False else "None/indeterminate"),
                            attempt,
                        )
                    except (AdbError, TimeoutError, OSError) as err:
                        awake = None
                        last_adb_reachable = False
                        last_observed_wakefulness = None
                        _LOGGER.debug(
                            "[%s] [+%.2fs] Stage 2 wake poll ADB error (attempt %d): %s (%s)",
                            self.host,
                            time.monotonic() - start_time,
                            attempt,
                            type(err).__name__,
                            err,
                        )

                    # Handle lingering shutdown settling if device is still shutting down
                    if shutdown_pending and not settled_to_standby and not wake_key_sent:
                        lingering_settle_deadline = min(
                            settle_deadline,
                            start_time + min(2.0, wake_timeout * 0.6),
                        )
                        if awake is False:
                            settled_to_standby = True
                        elif time.monotonic() < lingering_settle_deadline:
                            if time.monotonic() >= deadline:
                                break
                            sleep_time = min(
                                0.2,
                                poll_interval,
                                max(0.001, min(lingering_settle_deadline, deadline) - time.monotonic()),
                            )
                            await asyncio.sleep(sleep_time)
                            attempt = 1
                            continue

                    # If awake confirmed: success!
                    if awake is True:
                        _LOGGER.info(
                            "[%s] [+%.2fs] Wake confirmed: projector is Awake",
                            self.host,
                            time.monotonic() - start_time,
                        )
                        self._mark_power_transition(awake=True)
                        return True

                    if awake is False:
                        settled_to_standby = True

                    # If a power toggle was already sent:
                    # Only allow a single recovery retry when delivery was clean (not ambiguous),
                    # the settle holdoff window has elapsed, fresh positive Asleep evidence arrived,
                    # and sufficient budget remains before deadline.
                    if power_toggle_sent:
                        if (
                            awake is False
                            and not power_toggle_ambiguous
                            and power_toggle_count < 2
                            and (deadline - time.monotonic()) >= max(poll_interval * 2, 0.04)
                        ):
                            _LOGGER.info(
                                "[%s] [+%.2fs] Projector still Asleep after %.2fs settle window; allowing one recovery retry",
                                self.host,
                                time.monotonic() - start_time,
                                effective_settle_duration,
                            )
                            power_toggle_sent = False
                            self._power_on_pending = False
                        else:
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Power transition pending or state indeterminate (awake=%s, ambiguous=%s, count=%d); skipping KEYCODE_POWER toggle",
                                self.host,
                                time.monotonic() - start_time,
                                "None" if awake is None else str(awake),
                                power_toggle_ambiguous,
                                power_toggle_count,
                            )
                            if time.monotonic() >= deadline:
                                break
                            with contextlib.suppress(AdbError, TimeoutError, OSError):
                                await self.adb.async_send_key(
                                    KEYCODE_WAKEUP,
                                    timeout=min(3.0, max(0.01, deadline - time.monotonic())),
                                )
                            if time.monotonic() >= deadline:
                                break
                            sleep_time = min(poll_interval, max(0.001, deadline - time.monotonic()))
                            await asyncio.sleep(sleep_time)
                            continue

                    # State is confirmed Asleep (awake is False) and no power toggle is pending:
                    if awake is False:
                        if time.monotonic() >= deadline:
                            break
                        # 1. Send idempotent KEYCODE_WAKEUP first
                        try:
                            _LOGGER.info(
                                "[%s] [+%.2fs] Sending idempotent KEYCODE_WAKEUP",
                                self.host,
                                time.monotonic() - start_time,
                            )
                            rem_cmd = max(0.01, deadline - time.monotonic())
                            await self.adb.async_send_key(KEYCODE_WAKEUP, timeout=min(3.0, rem_cmd))
                            wake_key_sent = True
                        except (AdbError, TimeoutError, OSError) as err:
                            _LOGGER.debug(
                                "[%s] [+%.2fs] KEYCODE_WAKEUP send failed: %s (%s)",
                                self.host,
                                time.monotonic() - start_time,
                                type(err).__name__,
                                err,
                            )
                            if time.monotonic() >= deadline:
                                break
                            sleep_time = min(poll_interval, max(0.001, deadline - time.monotonic()))
                            await asyncio.sleep(sleep_time)
                            continue

                        if time.monotonic() >= deadline:
                            break
                        # Check if KEYCODE_WAKEUP woke the device
                        await asyncio.sleep(min(0.6, poll_interval / 2, max(0.001, deadline - time.monotonic())))
                        if time.monotonic() >= deadline:
                            break
                        try:
                            rem_recheck = max(0.01, deadline - time.monotonic())
                            recheck = await self._async_read_wakefulness(timeout=min(2.5, rem_recheck))
                            last_adb_reachable = True
                            last_observed_wakefulness = recheck
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Post-WAKEUP wakefulness read: %s",
                                self.host,
                                time.monotonic() - start_time,
                                "Awake"
                                if recheck is True
                                else ("Asleep" if recheck is False else "None/indeterminate"),
                            )
                            if recheck is True:
                                _LOGGER.info(
                                    "[%s] [+%.2fs] Wake confirmed after KEYCODE_WAKEUP: projector is Awake",
                                    self.host,
                                    time.monotonic() - start_time,
                                )
                                self._mark_power_transition(awake=True)
                                return True
                        except (AdbError, TimeoutError, OSError) as err:
                            recheck = None
                            last_adb_reachable = False
                            last_observed_wakefulness = None
                            _LOGGER.debug(
                                "[%s] [+%.2fs] Post-WAKEUP wakefulness check failed: %s (%s)",
                                self.host,
                                time.monotonic() - start_time,
                                type(err).__name__,
                                err,
                            )

                        # If recheck is confirmed False (Asleep), KEYCODE_WAKEUP was insufficient.
                        # Send KEYCODE_POWER toggle and enter the power-on settle window.
                        if recheck is False:
                            if time.monotonic() >= deadline:
                                break
                            power_toggle_count += 1
                            _LOGGER.info(
                                "[%s] [+%.2fs] Sending KEYCODE_POWER toggle (attempt %d)",
                                self.host,
                                time.monotonic() - start_time,
                                power_toggle_count,
                            )
                            power_toggle_sent = True
                            power_toggle_ambiguous = False
                            self._power_on_pending = True
                            try:
                                rem_pwr = max(0.01, deadline - time.monotonic())
                                await self.adb.async_send_key(KEYCODE_POWER, timeout=min(3.0, rem_pwr))
                            except (AdbError, TimeoutError, OSError) as err:
                                power_toggle_ambiguous = True
                                last_adb_reachable = False
                                _LOGGER.debug(
                                    "[%s] [+%.2fs] KEYCODE_POWER send encountered error (key may have been delivered): %s (%s)",
                                    self.host,
                                    time.monotonic() - start_time,
                                    type(err).__name__,
                                    err,
                                )

                            # Poll across the settling window to detect Awake early
                            toggle_settle_deadline = min(deadline, time.monotonic() + effective_settle_duration)
                            settle_interval = min(0.5, max(0.005, poll_interval / 2))
                            while time.monotonic() < toggle_settle_deadline:
                                sleep_sub = min(settle_interval, max(0.001, toggle_settle_deadline - time.monotonic()))
                                await asyncio.sleep(sleep_sub)
                                if time.monotonic() >= deadline:
                                    break
                                try:
                                    rem_settle_poll = max(0.01, deadline - time.monotonic())
                                    post_power = await self._async_read_wakefulness(timeout=min(2.5, rem_settle_poll))
                                    last_adb_reachable = True
                                    last_observed_wakefulness = post_power
                                    _LOGGER.debug(
                                        "[%s] [+%.2fs] Power toggle settle poll: %s",
                                        self.host,
                                        time.monotonic() - start_time,
                                        "Awake"
                                        if post_power is True
                                        else ("Asleep" if post_power is False else "None/indeterminate"),
                                    )
                                    if post_power is True:
                                        _LOGGER.info(
                                            "[%s] [+%.2fs] Wake confirmed after KEYCODE_POWER: projector is Awake",
                                            self.host,
                                            time.monotonic() - start_time,
                                        )
                                        self._mark_power_transition(awake=True)
                                        return True
                                except (AdbError, TimeoutError, OSError) as err:
                                    last_adb_reachable = False
                                    last_observed_wakefulness = None
                                    _LOGGER.debug(
                                        "[%s] [+%.2fs] Power toggle settle poll error: %s (%s)",
                                        self.host,
                                        time.monotonic() - start_time,
                                        type(err).__name__,
                                        err,
                                    )

                            if time.monotonic() >= deadline:
                                break
                            # Settle window has elapsed; loop immediately to obtain fresh post-settle state evidence
                            continue

                    if time.monotonic() >= deadline:
                        break
                    sleep_time = min(poll_interval, max(0.001, deadline - time.monotonic()))
                    await asyncio.sleep(sleep_time)

                _LOGGER.warning(
                    "[%s] [+%.2fs] Timed out waiting for XGIMI projector %s to wake (final_state=%s)",
                    self.host,
                    time.monotonic() - start_time,
                    self.host,
                    "Awake"
                    if last_observed_wakefulness is True
                    else ("Asleep" if last_observed_wakefulness is False else "None/indeterminate"),
                )
                _record_wake_failure()
                return False

            finally:
                self._power_on_pending = False

    async def async_turn_off(self) -> bool:
        """Idempotently put the projector into standby after verifying it is currently Awake."""
        async with self._power_lock:
            now = time.monotonic()
            if self._power_off_pending:
                return True

            # On request recently executed: wait for hardware to finish waking before turning off
            if self._power_on_pending or (now < self._power_transition_until and self._optimistic_awake is True):
                settle_deadline = min(self._power_transition_until, time.monotonic() + 10.0)
                while time.monotonic() < settle_deadline:
                    rem_settle = max(0.01, settle_deadline - time.monotonic())
                    try:
                        settle_state = await self._async_read_wakefulness(timeout=min(2.0, rem_settle))
                        if settle_state is True:
                            break
                    except (AdbError, TimeoutError, OSError):
                        pass
                    sleep_time = min(0.2, max(0.001, settle_deadline - time.monotonic()))
                    await asyncio.sleep(sleep_time)
                self._optimistic_awake = None
                self._power_transition_until = 0.0

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
        self._power_on_pending = False
        self._optimistic_awake = awake
        self._power_transition_until = time.monotonic() + 10.0
        base = self.data if self.data else self.default_state(online=True, awake=awake)
        current: XgimiCoordinatorData = {
            **base,
            "online": True,
            "awake": awake,
        }
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
                res = await self.adb.async_shell(cmd, timeout=5.0)
            else:
                await self._async_ensure_hdmi_inputs()
                input_id = self._hdmi_input_ids.get(source, DEFAULT_HDMI_INPUT_IDS[source])
                encoded_id = quote(input_id, safe="")
                uri = f"content://android.media.tv/passthrough/{encoded_id}"
                cmd = f'am start -a android.intent.action.VIEW -d "{uri}"'
                res = await self.adb.async_shell(cmd, timeout=5.0)
        except (AdbError, TimeoutError, OSError) as err:
            _LOGGER.warning("Failed to switch input source to %s on %s: %s", source, self.host, err)
            return False

        if is_android_shell_error(res):
            _LOGGER.warning("Android reported failure switching input source to %s on %s: %s", source, self.host, res)
            return False

        self._current_input_source = source
        base = self.data if self.data else self.default_state(online=True, awake=True)
        current: XgimiCoordinatorData = {
            **base,
            "input_source": source,
        }
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
        state = self.default_state(online=False, awake=False)
        self.async_set_updated_data(state)
        return True

    def _parse_compound_poll(self, raw: str) -> XgimiCoordinatorData:
        """Parse the single-command compound ADB poll response."""
        sections: dict[str, list[str]] = {}
        current_key = "HEADER"
        sections[current_key] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if stripped in ("---WAKE---", "---APP1---", "---APP2---", "---HDMI---", "---META---"):
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

        hdmi_text = "\n".join(sections.get("HDMI", []))
        detected_hdmi = parse_active_hdmi_input(hdmi_text, self._hdmi_input_ids)

        if running_app and (
            "com.google.android.tv.inputplayer" in running_app or "com.mediatek.tvinput" in running_app
        ):
            self._current_input_source = detected_hdmi
        elif running_app:
            self._current_input_source = INPUT_SOURCE_APPS
        elif detected_hdmi:
            self._current_input_source = detected_hdmi

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

    async def _async_update_data(self) -> XgimiCoordinatorData:
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
                return self.default_state(online=True, awake=True)
            return self.default_state(online=False, awake=False)

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
