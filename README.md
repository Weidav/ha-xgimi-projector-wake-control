# XGIMI Projector Wake & Control — Standalone Home Assistant Integration (HACS)

[![Validate](https://github.com/Weidav/ha-xgimi-projector-wake-control/actions/workflows/validate.yml/badge.svg)](https://github.com/Weidav/ha-xgimi-projector-wake-control/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A standalone Home Assistant integration for **XGIMI Google TV / Android 14 projectors** (designed and verified on the **XGIMI Horizon 20** series, `XR14A` / MediaTek `MT9689`.

This integration operates **natively against stock factory XGIMI firmware** over **ADB TCP (`5555`)** and **MediaTek WoWLAN**—requiring **no third-party APK** installed on the projector.

## Key Features

- **Two-Stage Standby Wake (`WoWLAN` + `ADB`)**:
  - **Stage 1 (SoC / Wi-Fi Radio Wake)**: Broadcasts and unicasts 102-byte Wake-on-LAN Magic Packets (UDP ports `9` & `7`), Google Cast mDNS PTR queries (`224.0.0.251:5353`), Cast HTTP eureka knocks (`8010`), and Android TV Remote knocks (`6465`).
  - **Stage 2 (Display / Laser Ignition)**: Polls ADB (`5555`) up to 24 seconds as the SoC wakes and injects `KEYCODE_WAKEUP` (`224`) / state-guarded `KEYCODE_POWER` (`26`) to ignite the projector laser.
- **True Power vs. Fast Boot Standby Detection**:
  - Queries `dumpsys power` (`mWakefulness=Awake` vs `Asleep`/`Dozing`) over a pooled ADB connection so Network Standby never triggers false `awake=True` states or spurious TCP wake knocks.
- **Idempotent Power Off**:
  - Verifies the projector is `Awake` before sending `KEYCODE_POWER` (`26`), preventing repeated `turn_off` automations from accidentally toggling a sleeping projector back on.
- **Zero External ADB Dependency Conflicts**:
  - Built-in async RSA-2048 ADB wire client (`custom_components/xgimi/adb_client.py`) using Home Assistant's native `cryptography` package, persisting its keypair in `.storage/xgimi_adb_key`.
- **Direct Input Source Switching**:
  - Switches seamlessly between `Apps`, `HDMI 1`, and `HDMI 2` using Android TV passthrough URIs (`content://android.media.tv/passthrough/<inputId>`) and clean `com.google.android.tv.inputplayer` teardown.
- **XGIMI Hardware & Remote Controls**:
  - Dedicated buttons for **Auto Focus** (`140` / `KEYCODE_F10`), **Settings Dashboard** (`176`), D-Pad navigation, Volume, Mute, Google Assistant, Force Wake, and Reboot.

## Projector Prerequisites (One-Time Setup on Projector)

1. **Enable Network Standby (for Wake-on-Wi-Fi)**:
   - On the projector, navigate to **Settings -> System -> Power & Energy -> Energy Modes** and select **Optimized (Network Standby)** so the MediaTek Wi-Fi controller keeps listening for WoWLAN packets during standby.
2. **Enable ADB over TCP (`5555`)**:
   - Go to **Settings -> System -> About** and click **Android TV OS Build** 7 times to unlock **Developer Options**.
   - Open **Settings -> System -> Developer Options** and enable **USB Debugging** (and **Wireless Debugging / Network ADB on port 5555**).

## Installation

### Option 1: HACS (Recommended)

1. Open **HACS** in your Home Assistant sidebar.
2. Click the three dots (`⋮`) in the top-right corner and select **Custom repositories**.
3. Enter the repository URL:
   - **Repository**: `https://github.com/Weidav/ha-xgimi-projector-wake-control`
   - **Category**: `Integration`
4. Click **Add**, find **XGIMI Projector Wake & Control** in HACS, and click **Download**.
5. Restart Home Assistant.

### Option 2: Manual Installation

1. Copy the `custom_components/xgimi` folder into your Home Assistant `<config>/custom_components/xgimi` directory.
2. Restart Home Assistant.

## Configuration & Pairing in Home Assistant

1. Navigate to **Settings -> Devices & Services -> Add Integration**.
2. Search for **XGIMI Projector Wake & Control** and enter:
   - **Host**: Your projector's IP address (e.g., `192.168.1.100`, recommended to assign a static DHCP lease on your router).
   - **Device Name**: Default is `XGIMI Horizon 20 Pro`.
   - **MAC Address** *(optional)*: Auto-discovered via local ARP (`/proc/net/arp`) or over ADB (`wlan0`) if left blank.
3. **One-Time ADB Authorization**:
   - Look at your projector screen. A dialog reading **"Allow USB debugging?"** will appear.
   - Check **"Always allow from this computer"**, select **OK** on the projector remote, and click **Submit** in Home Assistant.

## Entities Provided

The integration creates **20 dedicated entities** for complete local control without requiring any Android bridge APK:

### Power & Connectivity
| Platform        | Entity Name      | Unique ID Suffix | Description                                                                                                                                               |
| :-------------- | :--------------- | :--------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `switch`        | **Power**        | `power`          | Turns the projector on (Two-Stage WoWLAN + ADB wake) and off (idempotent standby). Always stays available in HA so wake can be triggered from deep sleep. |
| `binary_sensor` | **Power State**  | `power_state`    | `on` when the projector display/laser is awake (`mWakefulness=Awake`). Device class: `power`.                                                             |
| `binary_sensor` | **Connectivity** | `connectivity`   | `on` when the projector's ADB daemon is reachable over the network (both when display is awake and in Network Standby). Device class: `connectivity`.     |

### Source Selection
| Platform | Entity Name      | Unique ID Suffix      | Description                                                                                                                                                                 |
| :------- | :--------------- | :-------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `select` | **Input Source** | `select_input_source` | Switches active input source between **`Apps`**, **`HDMI 1`**, and **`HDMI 2`** using native Android TV passthrough intents (`content://android.media.tv/passthrough/...`). |

### Hardware & Remote Buttons
| Platform | Entity Name              | Icon                                | Keycode / Action             | Description                                                                                                                                                                           |
| :------- | :----------------------- | :---------------------------------- | :--------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `button` | **Force Wake Projector** | `mdi:power-on`                      | Wake Sequence                | Explicitly fires the Two-Stage WoWLAN + ADB display wake burst on demand, independent from the power state. (The **Power** switch performs this same sequence when its state is off.) |
| `button` | **Auto Focus**           | `mdi:image-filter-center-focus`     | `140` (`KEYCODE_F10`)        | Triggers XGIMI optical autofocus routine (intercepted by XGIMI `PhoneWindowManager`).                                                                                                 |
| `button` | **Settings Dashboard**   | `mdi:cog`                           | `176` (`KEYCODE_SETTINGS`)   | Opens the XGIMI quick settings overlay.                                                                                                                                               |
| `button` | **Home**                 | `mdi:home`                          | `3` (`KEYCODE_HOME`)         | Returns to the Google TV launcher home screen.                                                                                                                                        |
| `button` | **Back**                 | `mdi:keyboard-backspace`            | `4` (`KEYCODE_BACK`)         | Simulates the remote Back button.                                                                                                                                                     |
| `button` | **Menu**                 | `mdi:menu`                          | `82` (`KEYCODE_MENU`)        | Simulates the remote Menu button.                                                                                                                                                     |
| `button` | **D-Pad Up**             | `mdi:chevron-up`                    | `19` (`KEYCODE_DPAD_UP`)     | Directional Pad navigation Up.                                                                                                                                                        |
| `button` | **D-Pad Down**           | `mdi:chevron-down`                  | `20` (`KEYCODE_DPAD_DOWN`)   | Directional Pad navigation Down.                                                                                                                                                      |
| `button` | **D-Pad Left**           | `mdi:chevron-left`                  | `21` (`KEYCODE_DPAD_LEFT`)   | Directional Pad navigation Left.                                                                                                                                                      |
| `button` | **D-Pad Right**          | `mdi:chevron-right`                 | `22` (`KEYCODE_DPAD_RIGHT`)  | Directional Pad navigation Right.                                                                                                                                                     |
| `button` | **D-Pad OK**             | `mdi:checkbox-blank-circle-outline` | `23` (`KEYCODE_DPAD_CENTER`) | Directional Pad Center / Select button.                                                                                                                                               |
| `button` | **Volume Up**            | `mdi:volume-plus`                   | `24` (`KEYCODE_VOLUME_UP`)   | Increments projector audio volume.                                                                                                                                                    |
| `button` | **Volume Down**          | `mdi:volume-minus`                  | `25` (`KEYCODE_VOLUME_DOWN`) | Decrements projector audio volume.                                                                                                                                                    |
| `button` | **Mute**                 | `mdi:volume-mute`                   | `164` (`KEYCODE_MUTE`)       | Toggles projector audio mute.                                                                                                                                                         |
| `button` | **Assistant**            | `mdi:microphone`                    | `219` (`KEYCODE_ASSISTANT`)  | Activates Google Assistant voice prompt.                                                                                                                                              |
| `button` | **Restart Projector**    | `mdi:restart`                       | Shell Reboot                 | Reboots the projector via ADB shell (`EntityCategory.CONFIG`, device class: `restart`).                                                                                               |
