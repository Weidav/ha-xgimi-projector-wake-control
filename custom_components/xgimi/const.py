"""Constants for the standalone XGIMI Projector Wake & Control integration."""

from __future__ import annotations

DOMAIN = "xgimi"

CONF_HOST = "host"
CONF_MAC = "mac"
CONF_ADB_PORT = "adb_port"

DEFAULT_HOST = "192.168.1.100"
DEFAULT_NAME = "XGIMI Horizon 20 Pro"
DEFAULT_ADB_PORT = 5555
ADB_PORT = 5555

# WoWLAN Stage-1 Wake Ports
WOL_WAKE_PORT = 9
WOL_DISCARD_PORT = 7
CAST_WAKE_PORT = 8010
REMOTE_WAKE_PORT = 6465
MDNS_WAKE_PORT = 5353

# Persistent RSA-2048 ADB key storage filename inside Home Assistant .storage
ADB_KEY_STORAGE_FILE = "xgimi_adb_key"

# Input sources
INPUT_SOURCE_APPS = "Apps"
INPUT_SOURCE_HDMI1 = "HDMI 1"
INPUT_SOURCE_HDMI2 = "HDMI 2"

INPUT_SOURCES: list[str] = [
    INPUT_SOURCE_APPS,
    INPUT_SOURCE_HDMI1,
    INPUT_SOURCE_HDMI2,
]

# Default fallback TV input passthrough IDs on MediaTek MT9689 / XGIMI Google TV
DEFAULT_HDMI_INPUT_IDS: dict[str, str] = {
    INPUT_SOURCE_HDMI1: "com.mediatek.tvinput/.hdmi.HDMIInputService/HW5",
    INPUT_SOURCE_HDMI2: "com.mediatek.tvinput/.hdmi.HDMIInputService/HW6",
}

# Android & XGIMI Custom Keycodes
KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_DPAD_UP = 19
KEYCODE_DPAD_DOWN = 20
KEYCODE_DPAD_LEFT = 21
KEYCODE_DPAD_RIGHT = 22
KEYCODE_DPAD_CENTER = 23
KEYCODE_VOLUME_UP = 24
KEYCODE_VOLUME_DOWN = 25
KEYCODE_POWER = 26
KEYCODE_MENU = 82
KEYCODE_AUTOFOCUS = 140  # KEYCODE_F10 intercepted by Xgimi PhoneWindowManager for Auto Focus
KEYCODE_MUTE = 164
KEYCODE_SETTINGS = 176
KEYCODE_ASSISTANT = 219
KEYCODE_WAKEUP = 224

# Button entity definitions: suffix -> (keycode, name, icon)
KEYCODES: dict[str, tuple[int, str, str]] = {
    "autofocus": (KEYCODE_AUTOFOCUS, "Auto Focus", "mdi:image-filter-center-focus"),
    "settings": (KEYCODE_SETTINGS, "Settings Dashboard", "mdi:cog"),
    "home": (KEYCODE_HOME, "Home", "mdi:home"),
    "back": (KEYCODE_BACK, "Back", "mdi:keyboard-backspace"),
    "menu": (KEYCODE_MENU, "Menu", "mdi:menu"),
    "dpad_up": (KEYCODE_DPAD_UP, "D-Pad Up", "mdi:chevron-up"),
    "dpad_down": (KEYCODE_DPAD_DOWN, "D-Pad Down", "mdi:chevron-down"),
    "dpad_left": (KEYCODE_DPAD_LEFT, "D-Pad Left", "mdi:chevron-left"),
    "dpad_right": (KEYCODE_DPAD_RIGHT, "D-Pad Right", "mdi:chevron-right"),
    "dpad_center": (KEYCODE_DPAD_CENTER, "D-Pad OK", "mdi:checkbox-blank-circle-outline"),
    "volume_up": (KEYCODE_VOLUME_UP, "Volume Up", "mdi:volume-plus"),
    "volume_down": (KEYCODE_VOLUME_DOWN, "Volume Down", "mdi:volume-minus"),
    "mute": (KEYCODE_MUTE, "Mute", "mdi:volume-mute"),
    "assistant": (KEYCODE_ASSISTANT, "Assistant", "mdi:microphone"),
}
