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
- Kalman filter state persisted to `user_state.json` between runs (never re-boots from `start_weight`/`start_impedance`)
- Derived body metrics: BMI, BMR, body fat %, water %, lean mass
- Confidence gating: ambiguous readings tagged as `unassigned`
- InfluxDB 1.x integration (configurable, enabled/disabled)
- Ambiguous reading confirmation via self-hosted ntfy server: tap-action buttons per user, background reply polling, timeout fallback
- Per-user weight-reading notifications via ntfy
- Pending confirmations persisted to JSON sidecar (survives restarts)
- systemd daemon service (auto-start on boot, crash recovery, journal logging)
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

## User Detection

The scale supports auto-detection of multiple users using per-user Kalman filters that track each user's true weight and impedance as slowly-drifting hidden states.

### How it works

1. Each configured user gets a 2D Kalman filter (state = `[weight_kg, impedance_ohm]`)
2. On first run: filters are seeded from `start_weight` and `start_impedance` (or a configurable default)
3. Filter state `(x, P)` is persisted to a JSON sidecar file between runs
4. A finalized reading is scored against every user's filter using Mahalanobis distance
5. A plausibility penalty is applied if the implied weight jump exceeds physiological limits
6. The closest user wins, unless the gap between best and second-best is too small — then the reading is flagged ambiguous

### State persistence (`user_state.json`)

Filter state is persisted to a JSON file (default `~/.cache/miscale/user_state.json`, configurable via `detection.state_file` in `miscale.toml`). This file stores each user's current Kalman filter estimate and last-seen timestamp, so the app doesn't re-bootstrap from `start_weight` on every restart.

**This file should be gitignored** — it contains personal biometric data that drifts over time. Add `user_state.json` to `.gitignore` (or the path you configure).

A typical entry looks like:

```json
{
  "Slartibartfast": {
    "x": [67.08, 600.0],
    "P": [[0.10, 0.0], [0.0, 24.30]],
    "last_seen": "2026-09-06T13:58:14+00:00"
  }
}
```

### Tuning

The `[detection]` section in `miscale.toml` exposes several knobs:

| Parameter | Default | Effect |
|---|---|---|
| `process_var_weight` | 0.02 | How much a user's true weight is allowed to drift between readings |
| `process_var_impedance` | 4.0 | Same for impedance |
| `meas_var_weight` | 0.09 | Expected noise in a single scale weight reading |
| `meas_var_impedance` | 25.0 | Expected noise in a single impedance reading |
| `confidence_gap_threshold` | 1.0 | Minimum separation (Mahalanobis units) between best and second-best user to auto-assign |
| `max_plausible_delta_kg_per_day` | 2.0 | Max day-over-day weight change before a plausibility penalty applies |
| `max_intraday_delta_kg` | 1.5 | Same-day fluctuation budget that is never penalized (prevents ordinary swings from triggering penalties) |
| `max_plausibility_penalty` | 8.0 | Maximum multiplier applied to the Mahalanobis distance for implausible jumps |
| `default_start_impedance` | 500.0 | Fallback impedance guess when not set per-user |

## Derived Body Metrics

When a user has `height`, `age`, and `gender` configured, the scanner computes five derived metrics from published anthropometric formulas. These are logged alongside the detection result and stored in InfluxDB.

| Metric | Formula | Requires |
|---|---|---|
| BMI | `weight_kg / height_m²` | `height` |
| BMR | Mifflin-St Jeor equation (1990) | `height`, `age`, `gender` |
| Body fat % | Deurenberg et al. (1991), based on BMI + age + sex | `height`, `age`, `gender` |
| Water % | Hume & Weyers (1971), anthropometric estimate from height + weight + sex | `height`, `gender` |
| Lean mass | Boer (1984), estimated lean body mass from height + weight + sex | `height`, `gender` |

> **Note:** Body fat %, water %, and lean mass are anthropometric estimates using only height, weight, age, and sex — they do not use impedance. True BIA-based body composition requires proprietary coefficients that are not publicly available. The `_est` suffix on these field names in InfluxDB is a permanent reminder of this distinction.

## Ambiguous Reading Confirmation (ntfy)

When the Kalman filter cannot confidently assign a reading to a specific user, the scanner can send a notification to a self-hosted ntfy server with one tap button per configured user. Tapping a button immediately resolves the reading. Unanswered notifications expire after `pending_timeout_hours` and the reading is written as `unassigned`.

### How it works

1. An ambiguous reading triggers an ntfy notification with tap-action buttons
2. A background thread long-polls a reply topic for the response
3. The reply is a simple `session_id|user_id` string — no JSON parsing needed on the ntfy side
4. On tap: the reading commits to the chosen user's Kalman filter and InfluxDB
5. On "Neither": the reading is written as `unassigned`
6. On timeout: the reading is written as `unassigned`

### Configuration

```toml
[ntfy]
enabled = true
port = 8080
# base_url = "http://192.168.1.50:8080"  # auto-detected by default
topic = "miscale-ambiguous"
reply_topic = "miscale-ambiguous-reply"
pending_timeout_hours = 24
pending_file = "~/.cache/miscale/pending_confirmations.json"
```

The `base_url` is auto-detected as the machine's LAN IP. Set it explicitly only if auto-detection fails (multiple NICs, VPN, Docker networking).

### Per-user weight notifications

Optionally push every successfully-attributed reading to a per-user ntfy topic:

```toml
send_weight_readings = true
weight_reading_topic_template = "miscale_{user}_weight_reading"
```

This sends a notification with weight, impedance, and derived metrics to each user's own topic — e.g. `miscale_Slartibartfast_weight_reading` only notifies Slartibartfast.

## Running as a systemd Service

The scanner runs as a systemd service for automatic startup and crash recovery:

- Create a miscale.service file and replace <USERNAME> with yours and if your miscale project location is different, change that also:

```ini
[Unit]
Description=Mi Scale BLE Monitor
After=bluetooth.target network-online.target
Wants=bluetooth.target network-online.target

[Service]
Type=simple
User=<USERNAME>
Group=<USERNAME>
WorkingDirectory=/home/<USERNAME>/projects/miscale
ExecStart=/usr/bin/python3 /home/<USERNAME>/projects/miscale/miscale.py --config /home/<USERNAME>/projects/miscale/miscale.toml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp miscale.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now miscale.service
```

Key details:
- Runs as your regular user (not root) — no special BLE permissions needed
- Depends on `bluetooth.target` and `network-online.target`
- Auto-restarts on failure after 10 seconds
- Use `journalctl -u miscale -f` to view live logs (includes crash traces that don't appear in `miscale.log`)

## Project Roadmap

| Stage | Status | Description |
|-------|--------|-------------|
| 1 | **Done** | BLE monitor, session tracking, logging |
| 2 | **Done** | InfluxDB writer, Kalman filter user detection, state persistence, ntfy notifications |
| 3 | **Done** | systemd daemon service (auto-start, crash recovery, journal logging) |
| 4 | Planned | GATT config commands (calibration, LED control, etc.) |

## External Resources

- [BLE Protocol Spec](https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md) — wiecosystem
- [Python/MQTT Implementation](https://github.com/lolouk44/xiaomi_mi_scale) — lolouk44
- [Theengs Decoder](https://decoder.theengs.io/devices/XMTZC05HM.html)
- [Rust decode + ESPHome Config](https://gist.github.com/passcod/2132f3ca33232108f00e86f6147e47)

## License

MIT
