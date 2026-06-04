# Inkbird ISC-028-BW — Home Assistant (Tuya Local)

Local control for the **Inkbird ISC-028-BW** WiFi BBQ smoker in Home Assistant.

**Product ID:** `8l5th5vqvszbuvlm` · **Protocol:** 3.5 · Branch: [`inkbird-isc028bw`](https://github.com/bjursten/tuya-local/tree/inkbird-isc028bw)

This branch extends [Tuya Local](https://github.com/make-all/tuya-local) so the ISC-028-BW is fully usable locally — not only temperatures, but climate hold target, unit, sound, and a **Smoker** switch to start/stop without re-entering target temperature in the UI.

![logo](custom_components/tuya_local/brand/icon.svg)

## Why standard Tuya Local is not enough

| Works without patches | Needs this branch |
|----------------------|-------------------|
| Grill and meat probe **temperatures** (DP 101) | Climate **hold target**, °C/°F, sound (DP 102) |
| Alarm status (DP 103) | **Smoker** on/off and other masked DP 102 writes |

All smoker settings live in DP **102** (`setting_para`: 92-byte blob, **CRC-16/Modbus** on writes). The device **rarely returns DP 102** on poll. Tuya Local must know the current blob to apply bit-masked changes; otherwise writes fail with *Cannot mask unknown current value*.

**This branch adds:**

| Change | Purpose |
|--------|---------|
| `inkbird_isc028bw_smokercontrol.yaml` | Entities: climate, temps, **Smoker** switch, alarms |
| `crc16_modbus` in `device_config.py` | Valid CRC on masked DP 102 writes |
| DP 102 cache in `device.py` / `__init__.py` | Seed at startup, refresh when DP 101 arrives, keep 102 across full polls |

## What you get in Home Assistant

| Feature | Source |
|---------|--------|
| Grill + meat probe temperatures | DP 101 |
| Climate (current + hold target), °C/°F, sound | DP 102 (cached) |
| **Smoker** switch | Start/stop; keeps existing hold target |
| Lid, probe missing, faults | DP 103 (`30720` = probes unplugged, not a fault) |

Integration name in HA: **Inkbird ISC-028-BW (Tuya Local)**.

## Install

### Full integration (this branch)

1. HACS → Integrations → ⋮ → Custom repositories  
   - Repository: `https://github.com/bjursten/tuya-local`  
   - Category: Integration  
2. Install **Inkbird ISC-028-BW (Tuya Local)** (replace a previous make-all install if you only need ISC-028-BW).  
3. Branch **`inkbird-isc028bw`** (HACS branch selector, or checkout that branch under `custom_components/tuya_local`).  
4. Restart Home Assistant.  
5. Add the device with protocol **3.5** (local key + IP as for any Tuya Local setup).

**One local client only** — disable LocalTuya or other local Tuya clients on the same device.

### Minimal patch (keep official HACS for other devices)

Copy four files from this branch into your existing `custom_components/tuya_local/` — see **[docs/INKBIRD-ISC-028-BW.md](docs/INKBIRD-ISC-028-BW.md#install-minimal-patch-hacs-base-unchanged)**. Restart HA after Python changes.

## Diagnostics

**Settings → Devices → ISC-028-BW → Diagnostics** after restart:

- `connected`: `true` when the smoker is on and talking locally  
- `cached_state` includes `"102"` even when the unit was off at startup  
- `"101"` when live temperatures are reporting  
- `force_dps`: `[102, 101]`

## Other Tuya devices

Use **[make-all/tuya-local](https://github.com/make-all/tuya-local)** from HACS. This repo branch tracks upstream for convenience but is **maintained for ISC-028-BW** ([PR #5232](https://github.com/make-all/tuya-local/pull/5232) closed — no active upstream request).

## More documentation

- **[docs/INKBIRD-ISC-028-BW.md](docs/INKBIRD-ISC-028-BW.md)** — entities, DP notes, install details  
- **[README.tuya-local-upstream.md](README.tuya-local-upstream.md)** — general Tuya Local docs (all devices, HACS, config flow)  
- Issues: [bjursten/tuya-local](https://github.com/bjursten/tuya-local/issues)
