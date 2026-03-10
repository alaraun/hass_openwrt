# Changelog

All notable changes to this project will be documented in this file.

<!--next-version-placeholder-->

## v0.4.0

### Added

- **5G modem stats integration**: optional OpenWrt-side sidecar service (`openwrt-modem-stats/`) collects AT command data from a Quectel RG502Q-EA modem every 60 s and writes `/tmp/modem-stats.json`. New sensors: connection mode (LTE/5G NSA/5G SA), LTE and NR signal metrics (RSRP/RSRQ/RSSI/SINR), modem temperature, RX/TX traffic counters, and last update timestamp. New binary sensor: modem network registration status.
- **Modem dashboard**: `dashboard-modem.yaml` — a ready-made 5G modem view for Home Assistant.
- **Disk sensors**: root (`/`) and tmp filesystem usage sensors added alongside the existing swap sensor.
- **Dynamic entity tracking**: wireless and mesh interface sensors are added automatically at runtime when new interfaces appear — no restart needed.
- **Reconfigure support**: credentials, polling interval, and other settings can be updated via the integration's **Reconfigure** option without removing and re-adding the device.
- **Board info caching with reboot detection**: `system.board` is cached and refreshed only when a reboot is detected, reducing unnecessary ubus calls.
- **Parallel mesh peer calls**: mesh peer data is now fetched concurrently via `asyncio.gather()`.
- **Public `Ubus.login()` method**: passwords are redacted from debug logs.

### Fixed

- Replaced blocking `requests` + executor with `aiohttp` via `async_get_clientsession` for fully async HTTP.
- All six ubus data sources are now fetched concurrently with `asyncio.gather()`.
- Migrated to `entry.runtime_data` and `entry.data` (HA best practice); removed `hass.data` device registry usage.
- Refactored services to use `hass.config_entries` instead of `hass.data`.
- Added HA shutdown guard (`is_stopping` check) in coordinator to avoid errors during shutdown.
- Fixed `SystemDiskSensor`: class body was commented out, causing a `NameError` at runtime when swap data was present.
- Fixed `MeshSignalSensor`: use `native_value` + `native_unit_of_measurement` + `SensorDeviceClass`.
- Fixed `SystemUptimeSensor`: override `state_class=None` (string value, not numeric).
- Fixed `binary_sensor`: rename `OpenWrtSensor` → `OpenWrtBinarySensor`, use `runtime_data`.
- Fixed `config_flow`: add `try/except` with `cannot_connect` error; use `ubus._login()`.
- Fixed wireless discovery on **OpenWrt 25.12**: UCI sections without an explicit `ifname` now have one generated (the `ucode` wifi-scripts no longer stores `option ifname` in UCI).
- Added `_wireless_via_uci` flag to avoid retrying `network.wireless` every poll after a failure; resets on re-login.
- Downgraded `mwan3` / optional ubus object-not-found (`-32000`) log messages from `ERROR` to `DEBUG` — expected when optional packages are not installed.
- Fixed `auto-setup.sh`: ACL JSON write block was at root level instead of inside the `hass` block; `opkg list-installed` always exits 0 (added grep check); `%b` format used for `MSG_INSTALL_*` to render `\n` escapes; extracted `pkg_installed()` helper; removed dead code.
- Fixed network list index guards in `discover_wireless_uci()`.
- Added `WPS` switch `available` property and guard clauses.

## v0.3.0

**Forked from [kvj/hass_openwrt](https://github.com/kvj/hass_openwrt)**

### New

- Wireless clients counters (per interface/SSID) with client details (signal, IP, name)
- Wireless total clients (aggregate across interfaces)
- Known hosts sensor (lists IP, name and MAC)
- System sensors: uptime, load, and memory (swap/disk usage when available)

### Fixes

- **Wireless discovery fallback**: if `network.wireless` fails at runtime, `discover_wireless_uci()` (UCI `wireless` get) is automatically attempted.
- **Validation of `mwan3` entries**: invalid entries (not dicts, missing fields, non-numeric values) are skipped with a warning log.
- **Ubus improvements**: more detailed debug messages for RPC calls; improved error-to-exception mapping (`PermissionError`, `NameError`, `ConnectionError`).

## v0.0.2

- Add service management using command
- Add service translations to English language
- Added French translation

## v0.0.1

First version.
