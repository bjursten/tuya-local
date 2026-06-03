# Inkbird ISC-028-BW — special fork branch

> **This is branch `inkbird-isc028bw` on [bjursten/tuya-local](https://github.com/bjursten/tuya-local).**  
> It is **not** official [make-all/tuya-local](https://github.com/make-all/tuya-local).  
> Use it **only** for the Inkbird ISC-028-BW smoker (product `8l5th5vqvszbuvlm`).  
> For all other devices, use upstream Tuya Local from HACS.

Maintained fork branch for the **Inkbird ISC-028-BW** WiFi smoker (`8l5th5vqvszbuvlm`).

## What this branch adds

| Change | Purpose |
|--------|---------|
| `inkbird_isc028bw_smokercontrol.yaml` | Device config (temps, climate, smoker switch, alarms) |
| `crc16_modbus` in `device_config.py` | Correct CRC on masked DP 102 writes |
| DP 102 cache in `device.py` / `__init__.py` | Seed cache at startup (storage or default), fill on poll 101, one controlled write per session |

Without the Python patches, entities may show temperatures from DP 101 but climate target, unit, sound, and start/stop fail with *Cannot mask unknown current value*.

## Install (full integration)

Use this branch **instead of** the standard HACS Tuya Local package for the ISC-028-BW device:

1. HACS → Integrations → ⋮ → Custom repositories  
   - Repository: `https://github.com/bjursten/tuya-local`  
   - Category: Integration  
2. Install **Tuya Local** from that repository (or redownload if already installed from make-all).  
3. Select branch **`inkbird-isc028bw`** if HACS allows branch selection; otherwise clone/checkout that branch under `custom_components/tuya_local`.  
4. Restart Home Assistant.  
5. Add the device in Tuya Local with protocol **3.5** (same local key / IP as any Tuya Local setup).

**One local client only** — disable LocalTuya (or other local Tuya clients) for this device.

## Install (minimal patch, HACS base unchanged)

If you keep official Tuya Local from HACS, copy only the patched files from the companion repo or this branch:

- `custom_components/tuya_local/devices/inkbird_isc028bw_smokercontrol.yaml`
- `custom_components/tuya_local/device.py`
- `custom_components/tuya_local/__init__.py`
- `custom_components/tuya_local/helpers/device_config.py`

Restart Home Assistant after Python changes (reload integration is not enough).

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

## Relation to upstream

Upstream contribution (YAML + `crc16_modbus`) may land in make-all/tuya-local separately. The **DP 102 cache layer** in this branch is device-specific and stays here unless upstream adopts a generic equivalent.
