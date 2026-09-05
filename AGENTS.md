# Mi Scale BLE Monitor — Project Notes

## Hardware
- **Scale**: Mi Body Composition Scale 2 (XMTZC05HM)
- **MAC Address**: *(set in `miscale.toml`)*
- **BLE Protocol**: Advertisement packets on service UUID `0000181b-0000-1000-8000-00805f9b34fb`
- **Weight unit**: kg (weight raw / 200 = kg)
- **Height unit**: cm

## User Detection

The scale supports auto-detection of multiple users based on weight thresholds. Configure user profiles in `miscale.toml` with height, weight, and other biometric data.

A midpoint weight threshold cleanly separates users. A hysteresis margin can be added in stage 2 to prevent flickering near the boundary. Going forward, previous readings may also be factored in to assign readings to users.

## GAP Service (00001801)

| Characteristic | UUID | Example | Notes |
|----------------|------|---------|-------|
| Device Name | `00002a00` | `MIBFS` | Human-readable device name |
| Appearance | `00002a01` | `800c` | BLE appearance descriptor |
| Conn. Params | `00002a04` | `5000a0000000e803` | Min/max interval 80/160ms, timeout 1000ms |

## Device Information Service (0000180A)

The scale exposes device metadata via standard BLE GATT characteristics.
Read with: `python3 miscale.py -i`

| Characteristic | UUID | Notes |
|----------------|------|-------|
| System ID | `00002a23` | Derived from BLE MAC |
| PnP ID | `00002a50` | Vendor/product info |
| Serial Number | `00002a25` | Scale-specific serial |
| Hardware Revision | `00002a27` | Hardware version string |
| Firmware Version | `00002a28` | Firmware version string |

### Huami Configuration service (00001530)

| Characteristic | UUID | Notes |
|----------------|------|-------|
| Battery | `00001543` | Two bytes; `0101` = low battery, `0100` = OK |
| Scale config | `00001542` | Unit, calibration, LED, self-test, etc. |
| DFU Control | `00001531` | Firmware update |
| DFU Packet | `00001532` | Firmware update (write no response) |
| Conn. Params (unused) | `00002a04` | Duplicate of GAP 2A04, returns 8 null bytes |

#### Scale Configuration Characteristic (`00001542`)

Primary interaction: Write Without Response / Write with Notify subscription.

| Setting / Feature | Command (Write) | Response / Notes |
|---|---|---|
| Display Unit | `0x06 0x04 0x00 [unit]` | `unit`: `0x00` SI (kg), `0x01` Imperial (lbs), `0x02` Catty (jin) |
| Partial Measures | `0x06 0x10 0x00 [!enable]` | `0x16 0x06 0x10 0x00 0x01` — Enable/disable unstable measurements |
| Erase History | `0x06 0x12 0x00 0x00` | `0x16 0x06 0x12 0x00 0x01` — Irreversible |
| LED Display Control | `0x04 0x02` (On)<br>`0x04 0x03` (Off) | Directly toggles LED matrix |
| Tare / Zero Calibration | `0x06 0x05 0x00 0x00` | Scale must be idle on flat surface, zero weight. LED shows `- - - -` |
| Self-Test Diagnostics | `0x04 0x01` (Start)<br>`0x04 0x04` (Exit) | Lights all LED segments, runs BIA circuit tests |
| Sandglass (Hourglass) Mode | `0x06 0x0A 0x00` (init)<br>`0x06 0x0B 0x00` (timer) | Balance Test UI (Zepp Life app feature) |
| Read Sandglass Status | Read Characteristic | `0x03 0x00` = locked in Balance/Hourglass state |
| Start One-Foot Measure | `0x06 0x0f 0x00 0x00` *(subscribe notify)* | Notification: `0x06 0x0f 0x00 [flags] [time_lo] [time_hi]`<br>`flags 0x01` = active, `0x02` = stopped<br>`time` = uint16 LE, 10ms units (÷100 for seconds) |
| Stop One-Foot Measure | `0x06 0x11 0x00 0x00` | Cancels active balance test session |

### Body Composition service (0000181b)

| Characteristic | UUID | Access | Notes |
|----------------|------|--------|-------|
| Current Time | `00002a2b` | Read/Write | Set scale clock (9 bytes) |
| Body Comp Feature | `00002a9b` | Read | Feature flags (`01230000`) |
| Body Comp Measurement | `00002a9c` | Indicate | Mirrors advertisement payload |
| Body Comp History | `00002a2f` | Write/Notify | History sync (see mi_ble_protocol.md) |

## BLE Advertisement Format (V2 scale, service UUID 0000181b)

Service data payload is 13 bytes (excluding the 4-byte UUID). Multi-byte fields are little-endian.

| Byte(s) | Field | Endian | Notes |
|---------|-------|--------|-------|
| 0 | Unit code | — | `0x02` = kg, `0x03` = lbs, `0x04` = catty (斤) |
| 1 | Status flags | — | LSB-first bit flags (see below) |
| 2–3 | Year | little-endian | uint16 |
| 4 | Month | — | 1–12 |
| 5 | Day | — | 1–31 |
| 6 | Hour | — | 0–23 |
| 7 | Minute | — | 0–59 |
| 8 | Second | — | 0–59 |
| 9–10 | Impedance | little-endian | Ohms (Ω), only when `has_impedance` flag is set |
| 11–12 | Weight | little-endian | `×200` for kg, `×100` for lbs |

> **Correction (2026-09-04):** Earlier documentation described bytes 0–1 as a
> 16-bit control flags word with bits 5/7/8/9/10/14. That layout came from an
> older/different format (10-byte payload on `0000181d` service, first-gen Mi
> Scale). The XMTZC05HM uses a **different layout**: byte 0 is a plain unit-code
> value (not a bitfield), and all status flags live in byte 1.
>
> Source: ESPHome `xiaomi_miscale` component, verified against live hardware
> captures. See `mi_ble_protocol.md` for full details.

### Byte 1 status flags (LSB-first)

| Bit (in byte 1) | Overall bit | Meaning |
|---|---|---|
| 1 | 9 | `has_impedance` — impedance bytes (9–10) are valid |
| 5 | 13 | `is_stabilized` — weight (and impedance, once present) has settled |
| 7 | 15 | `load_removed` — person has stepped **off** the scale |

**Recommended "accept this reading" condition:**
`is_stabilized == true AND load_removed == false`

### Measurement lifecycle (verified from raw capture)

| # | Raw hex | flags | has_imp | stable | load_removed | Weight | Notes |
|---|---------|-------|---------|--------|--------------|--------|-------|
| 1 | `02a6…` | `0xa602` | 1 | 1 | **1** | ~68 kg | Idle — re-advertising last reading after step-off |
| 2 | `0204…` | `0x0402` | 0 | 0 | 0 | ~67 kg | Load detected, weight unsettled |
| 3 | `0204…` | `0x0402` | 0 | 0 | 0 | ~68 kg | Weight settles, still not flagged |
| 4 | `0224…` | `0x2402` | 0 | 1 | 0 | ~68 kg | Weight stable, impedance not ready |
| 5 | `0226…` | `0x2602` | 1 | 1 | 0 | ~68 kg | **Final reading** — both weight and impedance stable |

After the final reading, the scale re-advertises with `load_removed=1` (idle
packet). A passive BLE scanner can pick up the completed result even after
someone steps off.

### Older 10-byte format

The 10-byte payload on service UUID `0000181d` ("Weight Scale" service) is
used by weight-only, non-impedance Mi Scales. Its flag byte and bit meanings
are unrelated to the 13-byte `0000181b` format. If both appear from the same
MAC, they come from different advertisement channels.

Note: lolouk44/xiaomi_mi_scale has a buggy parser (wrong byte offsets for
weight). The ESPHome `xiaomi_miscale` component is the authoritative parser
for the 13-byte format. The wiecosystem spec is accurate for GATT services
but its advertisement control-byte table is superseded.

## Data fields decoded by the scale
- weight (kg)
- impedance (Ω)
- timestamp (from advertisement)

## Derived metrics (computed per-user using height, age, sex, weight, impedance)
- BMI, BMR, visceral fat, lean body mass, body fat %, water %, bone mass, muscle mass, protein %, body type, metabolic age

## Stages

### Stage 1 — BLE monitor + logfile (COMPLETE)
- Standalone Python script (`miscale.py`)
- Scans BLE advertisements for the scale using `bleak`
- Decodes V2 advertisement payload, filters for valid (stabilised) readings
- Logs to console + file (configurable) with levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
- TOML config file (`miscale.toml`)
- Optional GATT-based time sync to scale (one-shot at startup)
- Measurement session tracking: groups advertisements into per-session readings
  - Logs intermediate weight-only readings
  - Logs final readings when impedance appears
  - Detects new sessions by time gap (>5s configurable) or weight change (>0.5 kg)
- Advertisement deduplication (same weight+impedance within 3s window)
- Runs in a loop until Ctrl+C

### Stage 2 — InfluxDB + user tracking
- Write readings to InfluxDB (timestamped) 
- Auto-assign user based on weight threshold and last readings
- Compute derived body metrics per user
- Configurable InfluxDB connection settings

### Stage 3 — Daemon
- systemd service unit
- Auto-start on boot
- Log rotation 

### Stage 4 — Advanced Configuration & Diagnostics
- GATT-based device configuration via `00001542` characteristic (Huami Config service)
- Remote zero calibration (`0x06 0x05 0x00 0x00`) — requires idle scale on flat surface
- Factory self-test diagnostics (`0x04 0x01` / `0x04 0x04`) — lights all LEDs, runs BIA circuit tests
- LED display on/off control (`0x04 0x02` / `0x04 0x03`)
- Display unit configuration (`0x06 0x04 0x00 [unit]`) — kg/lbs/jin
- Balance test / one-foot measure mode (`0x06 0x0f 0x00 0x00`)
  - Streams real-time duration data via notify subscription
  - `flags 0x01` = active, `0x02` = stopped
  - `time` = uint16 LE, 10ms units
- Erase history command (`0x06 0x12 0x00 0x00`) — irreversible
- Partial measures enable/disable (`0x06 0x10 0x00 [!enable]`)

## Dependencies (stage 1)
- Python 3.11+ (for `tomllib`)
- `bleak` (BLE scanning library)
- `argparse` (stdlib)
- `logging` (stdlib)
- `tomllib` (stdlib, Python 3.11+)

## Dependencies (stage 2+)
- `influxdb-client` (InfluxDB Python client)
- `Xiaomi_Scale_Body_Metrics` or equivalent (body composition calculations)

## Relevant external resources
- https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md — BLE protocol spec
- https://github.com/lolouk44/xiaomi_mi_scale — Python/MQTT implementation
- https://decoder.theengs.io/devices/XMTZC05HM.html — Theengs decoder
- https://gist.github.com/passcod/2132f8d1ca33232108f00e86f6147e47 — Rust decode + ESPHome config

## Current Status

### Parser state (as of 2026-09-04, updated after expert review)
- **Protocol corrected:** Byte 0 is a unit code (not flags), byte 1 holds all status bits
- **Valid reading:** `is_stabilized == true AND load_removed == false`
- Previously rejected all readings because the parser used the wrong flag layout (16-bit word from bytes 0–1 instead of byte-0 unit code + byte-1 flags)
- Verified against 5-packet raw capture: packets 4 and 5 correctly identified as valid readings

### Measurement session tracking (as of 2026-09-04)
- **Session tracking implemented:** `SessionTracker` class groups advertisements into per-session readings
- Detects new sessions by time gap (>5s configurable) or weight change (>0.5 kg)
- Logs intermediate weight-only readings and final weight+impedance readings
- Advertisement deduplication (same weight+impedance within 3s window)
- Optional GATT time sync via `00002a2b` characteristic (enabled/disabled in config)

### Raw capture data
Clean raw hex capture:
```
02a6b207031305051b65020c35
0204b207031305092000008a34
0204b207031305092000000c35
0224b207031305092200000c35
0226b207031305092264020c35
```

## TODO
- [x] Stage 1: Create TOML config file
- [x] Stage 1: Create BLE scanner with bleak (detection callback pattern)
- [x] Stage 1: Implement advertisement parser for V2 protocol
- [x] Stage 1: Add logging (console + file, configurable levels)
- [x] Stage 1: Deduplicate readings (same weight within 30s window)
- [x] Resolve protocol questions with expert → update parser accordingly
- [x] Stage 1: Add optional GATT time sync
- [x] Stage 1: Implement measurement session tracking
- [x] Stage 1: Add `-i` flag for device info (DIS + battery)
- [ ] Stage 2: Add InfluxDB writer
- [ ] Stage 2: Implement user auto-detection with hysteresis
- [ ] Stage 2: Compute derived body metrics
- [ ] Stage 3: Create systemd service unit
- [ ] Stage 3: Test daemon lifecycle
- [ ] Stage 4: Implement GATT config commands via `00001542`
- [ ] Stage 4: Add zero calibration command
- [ ] Stage 4: Add LED display on/off control
- [ ] Stage 4: Add display unit configuration
- [ ] Stage 4: Implement balance test / one-foot measure mode
- [ ] Stage 4: Add erase history command (with confirmation)
