# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.8] - 2026-10-27

### Fixed
- **Hardware Shutdown Cooldown Enforcement**: Track dedicated 35s hardware teardown cooldown (`_shutdown_settle_until`) after `turn_off` to prevent injecting wake keys or WoWLAN knocks while optical engine and fans cool down, protecting MediaTek WoWLAN suspend arming from cold power-off corruption.
- **Wake Budget Post-Cooldown**: Grant full 30s wake timeout budget starting after hardware shutdown cooldown completes when waking during a recent shutdown.
- **Background Poll Lock Contention**: Skip background coordinator polling when `_power_lock` is held, giving `async_turn_on` exclusive access to ADB and eliminating 4.5s socket lock delays.
- **Instant Pre-Check When Already Awake**: Pre-check wakefulness before sending WoWLAN knocks if projector is already known to be `Awake`, returning in ~0.1s without spurious network traffic.

---

## [0.0.7] - 2026-10-27

### Fixed
- **Power Transition & Settle Verification**: Track pending power-on transitions (`_power_on_pending`) and poll a dedicated settling window after `KEYCODE_POWER` to detect `Awake` without double-toggling while hardware transitions are underway.
- **Ignored Command Recovery**: Obtain fresh post-settle `Asleep` evidence before retrying `KEYCODE_POWER`, distinguishing genuinely ignored toggles from delayed transitions.
- **Indeterminate State Safeguards**: Inconclusive state, ADB disconnects, or empty dumpsys outputs after a power toggle no longer trigger duplicate toggles.
- **Accurate Timeout State Reporting**: Preserve actual observed hardware state upon timeout rather than inventing optimistic `Awake` state.
- **Timestamped Wake Diagnostics**: Added elapsed `[+%.2fs]` monotonic timestamps to all wake logs (`WoWLAN`, `KEYCODE_WAKEUP`, `KEYCODE_POWER`, and wakefulness queries) for live hardware diagnostics.

---

## [0.0.6] - 2026-10-27

### Breaking Changes
- **Binary Sensor Renamed**: Renamed entity **Connectivity** to **ADB Connectivity** with unique ID suffix `adb_connectivity` (entity ID: `binary_sensor.<name>_adb_connectivity`) to clearly reflect that it tracks active ADB daemon reachability over TCP 5555 rather than physical network link state.

### Fixed
- **Power Switch & Wake Hardening**: Active wakefulness verification after injecting `KEYCODE_WAKEUP` and `KEYCODE_POWER` until `mWakefulness=Awake` is confirmed, preventing dropped keys during SoC resume from requiring a second power toggle.
- **Optimistic State Verification**: Verify physical hardware wakefulness before trusting optimistic state during power turn-on.
- **Fast Reconnect for Dead Sockets**: Split timeout budgets on cached sockets to fail fast and retry cleanly when Wi-Fi sleep silently drops TCP connections.
- **Subnet-Directed Knock Broadcasts**: Send subnet-directed broadcasts (`x.x.x.255`) alongside limited broadcasts to reliably wake sleeping Wi-Fi interfaces across APs.
- **Power Transition Safeguards**: Guard indeterminate wakefulness states and verify wake key dispatch.
- **Shutdown Resilience**: Preserve pending shutdown state across arbitrary connection drops and transient ADB errors during settling.
- **Deep Sleep Handling**: Reliably recognize sustained deep sleep and remove fragile wake loop offline flag.

### Changed
- **Documentation**: Streamlined README to highlight wake from Optimized (Network Standby) mode and dedicated hardware controls.
- **CI**: Bump `actions/upload-artifact` to v7 for native Node 24 support.

---

## [0.0.5] - 2026-10-26

### Changed
- Release version bump and validation workflow enhancements.

---

## [0.0.4] - 2026-10-26

### Fixed
- Resolve remaining power race conditions, command timeouts, shell error parsing, and HDMI input detection issues.
- Harden retry command safety checks and properly clean up temporary key files.
- Preserve unknown wakefulness state in Power switch and Power State binary sensor.
- Validate Android shell execution to accommodate benign task-to-front warnings.
- Refine active HDMI session parsing and expose unknown state.

---

## [0.0.3] - 2026-10-26

### Fixed
- Restore direct `DeviceClass` imports for Home Assistant compatibility.

---

## [0.0.2] - 2026-10-26

### Fixed
- Resolve Pyright and Pylance strict typing diagnostics.

---

## [0.0.1] - 2026-10-25

### Added
- Initial standalone alpha release for XGIMI Google TV / Android 14 projectors.
- Two-Stage WoWLAN + native async RSA-2048 ADB TCP (`5555`) control without requiring third-party APKs.
