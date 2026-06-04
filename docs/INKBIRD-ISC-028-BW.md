# Inkbird ISC-028-BW — Home Assistant integration

Local control for the **Inkbird ISC-028-BW** WiFi BBQ smoker (`8l5th5vqvszbuvlm`, Tuya protocol **3.5**) in Home Assistant via a maintained branch of [Tuya Local](https://github.com/make-all/tuya-local).

**Repository:** [bjursten/tuya-local](https://github.com/bjursten/tuya-local) · **Branch:** `inkbird-isc028bw` · **Overview:** [README.md](../README.md)

## Problem and solution

**Problem:** Settings are stored in DP **102** (`setting_para`, 92 bytes, CRC-16/Modbus). The smoker almost never returns 102 on read. Standard Tuya Local can show temperatures from DP **101** but masked writes to 102 fail without a cached blob (*Cannot mask unknown current value*). Climate target, unit, sound, and start/stop do not work reliably.

**Solution on this branch:**

| Change | Purpose |
|--------|---------|
| `inkbird_isc028bw_smokercontrol.yaml` | Device config (temps, climate, smoker switch, alarms) |
| `crc16_modbus` in `device_config.py` | Correct CRC on masked DP 102 writes |
| DP 102 cache in `device.py` / `__init__.py` | Seed cache at startup (storage or default), update when 101 is polled, protect 102 on full poll cleanup |

## Install (full integration)

Use this branch when ISC-028-BW is your only Tuya Local device, or you accept one custom integration install:

1. HACS → Integrations → ⋮ → Custom repositories  
   - Repository: `https://github.com/bjursten/tuya-local`  
   - Category: Integration  
2. Install **Inkbird ISC-028-BW (Tuya Local)** from that repository (redownload if you previously used make-all).  
3. Branch **`inkbird-isc028bw`** (HACS branch selection, or checkout under `custom_components/tuya_local`).  
4. Restart Home Assistant.  
5. Add the device with protocol **3.5**.

**One local client only** — disable LocalTuya (or other local Tuya clients) for this device.

## Install (minimal patch, HACS base unchanged)

Keep official [make-all/tuya-local](https://github.com/make-all/tuya-local) from HACS and copy only:

- `custom_components/tuya_local/devices/inkbird_isc028bw_smokercontrol.yaml`
- `custom_components/tuya_local/device.py`
- `custom_components/tuya_local/__init__.py`
- `custom_components/tuya_local/helpers/device_config.py`

Restart Home Assistant after Python changes (reload integration is not enough).

The companion experiment repo includes `scripts/deploy-to-ha.sh` for the same four files plus YAML.

## Entities (summary)

| Entity | Role |
|--------|------|
| **Smoker** (switch) | Start/stop using existing hold target — no need to set temperature in UI |
| **Climate** | Grill temp, hold target, hvac mode |
| **Grill / meat probe** sensors | Temperatures from DP 101 |
| **Meat probe 1–4** (diagnostic) | OK / Problem when probe missing |
| **Grill open** | Door sensor |
| **Problem** | Real faults (probe-lost-only `30720` is ignored) |

## Diagnostics checklist

After restart, **Settings → Devices → ISC-028-BW → Diagnostics**:

- `connected`: `true` when the device is on  
- `cached_state` includes `"102": "..."` (even if the device is off at startup)  
- `cached_state` includes `"101": "..."` when the device is reporting  
- `force_dps`: `[102, 101]`

## DP 103 alarm code `30720`

Bitmap value **30720** = meat probes 1–4 unplugged, grill probe OK, lid closed. Not a fault — **Problem** stays OK. Meat probe diagnostics show **Problem** when a probe is missing.

## Protocol notes

- Tuya protocol **3.5** locally  
- DP 101: live temps + fan PID output (read-only)  
- DP 102: fan on/off (byte 0), °C/°F (byte 1), hold target (bytes 7–8), sound (byte 80)  
- DP 103: alarm bitfield  

Physical changes on the device (dial, fan button, power cycle) update DP 102 on the unit first; HA picks up hold target on poll when 102 is available or after a successful write from HA.

## Relation to upstream (make-all/tuya-local)

ISC-028-BW support is maintained on branch `inkbird-isc028bw` only. [PR #5232](https://github.com/make-all/tuya-local/pull/5232) was closed in June 2026. Use [make-all/tuya-local](https://github.com/make-all/tuya-local) from HACS for all other devices.
