# Mi Scale BLE Monitor

![Mi Body Composition Scale 2](https://i01.appmifile.com/webfile/globalimg/products/pc/mi-body-composition-scale-2/specs01.png)

Standalone BLE scanner for the **Mi Body Composition Scale 2** (XMTZC05HM) that decodes weight and impedance readings from Bluetooth Low Energy advertisements, tracks measurement sessions, and logs results.

## Features

- Scans for BLE advertisements from Mi Body Composition Scale 2 (XMTZC05HM)
- Decodes the V2 advertisement protocol (13-byte payload on service `0000181b`)
- Filters for valid, stabilised readings (weight + impedance required)
- Groups advertisements into per-session measurements (intermediate weight → final weight+impedance)
- Deduplicates repeated advertisements within a 3-second window
- Per-user Kalman filter auto-detection (weight + impedance) for multi-user households
- Confidence gating: ambiguous readings tagged as `unassigned`
- InfluxDB 1.x integration (configurable, enabled/disabled)
- GATT-based time sync via `-t` flag to align the scale's internal clock
- One-shot device info read (System ID, serial, firmware version, battery)
- Configurable logging to console and file
- TOML configuration file

## Hardware

| Item | Value |
|------|-------|
| Scale | Mi Body Composition Scale 2 (XMTZC05HM) |
| BLE MAC | *(set in config)* |
| Service UUID | `0000181b-0000-1000-8000-00805f9b34fb` |
| Weight unit | kg (raw / 200) |

All measurements are processed and stored in **kg** regardless of the scale's display unit. The advertisement payload contains a unit code (byte 0) that the parser uses to convert the raw weight to kg.

## Installation

### Requirements

- Python 3.11+ (3.10 with `tomli` fallback)
- BlueZ on Linux (BLE adapter)

### Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and edit the config
cp miscale.toml.example miscale.toml
# Edit miscale.toml with your scale's MAC address
```

## Usage

### Start the scanner

```bash
python miscale.py
```

### Read device information (one-shot)

```bash
python miscale.py -i
```

This connects to the scale and reads the Device Information Service characteristics (System ID, PnP ID, serial number, hardware/firmware revision) and battery status, then exits. It also displays the scale's current internal clock and, if InfluxDB is enabled, the weight unit from the most recent stored reading.

### Set scale internal clock (one-shot)

```bash
python miscale.py -t
```

This connects to the scale and writes the current time to its internal clock, then verifies by reading it back.

### Custom config file

```bash
python miscale.py -c /path/to/miscale.toml
```

## Configuration

See `miscale.toml.example` for the full reference. Key settings:

```toml
[scan]
scale_mac = "AA:BB:CC:DD:EE:FF"   # Your scale's BLE MAC
hci_device = "hci0"                # Bluetooth adapter
scan_interval = 5                  # Seconds between scan cycles
session_gap_seconds = 5            # Gap to detect a new measurement

[logging]
log_file = "${HOME}/projects/miscale/miscale.log"
log_level = "DEBUG"
```

## Protocol Details

The scale advertises a 13-byte payload on service UUID `0000181b`:

| Byte(s) | Field | Notes |
|---------|-------|-------|
| 0 | Unit code | `0x02` = kg, `0x03` = lbs, `0x04` = catty |
| 1 | Status flags | LSB-first bit flags |
| 2–3 | Year | little-endian uint16 |
| 4 | Month | 1–12 |
| 5 | Day | 1–31 |
| 6 | Hour | 0–23 |
| 7 | Minute | 0–59 |
| 8 | Second | 0–59 |
| 9–10 | Impedance | Ohms (if flag set) |
| 11–12 | Weight | little-endian, \*200 for kg, \*100 for lbs/catty |

> **Note:** The parser always converts the raw weight to kg using the unit code from byte 0, then works exclusively in kg.

### Status flags (byte 1, LSB-first)

| Bit | Meaning |
|-----|---------|
| 1 | `has_impedance` — impedance bytes are valid |
| 5 | `is_stabilized` — weight has settled |
| 7 | `load_removed` — person has stepped off |

A reading is accepted when `is_stabilized == true` AND `load_removed == false` AND `has_impedance == true`. Impedance is required for user detection.

## Measurement Lifecycle

1. **Load detected** — scale detects weight, reading unsettled
2. **Weight stabilised** — weight value settles, impedance not yet ready
3. **Final reading** — impedance appears, measurement complete
4. **Idle** — scale re-advertises last reading with `load_removed` flag set

The scanner logs intermediate and final phases separately. After step-off, idle packets are silently ignored.

## Project Roadmap

| Stage | Status | Description |
|-------|--------|-------------|
| 1 | **Done** | BLE monitor, session tracking, logging |
| 2 | **Done** | InfluxDB writer, Kalman filter user detection, state persistence |
| 3 | Planned | systemd daemon service |
| 4 | Planned | GATT config commands (calibration, LED control, etc.) |

## External Resources

- [BLE Protocol Spec](https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md) — wiecosystem
- [Python/MQTT Implementation](https://github.com/lolouk44/xiaomi_mi_scale) — lolouk44
- [Theengs Decoder](https://decoder.theengs.io/devices/XMTZC05HM.html)
- [Rust decode + ESPHome Config](https://gist.github.com/passcod/2132f3ca33232108f00e86f6147e47)

## License

MIT
