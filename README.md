# Mi Scale BLE Monitor

![Mi Body Composition Scale 2](https://i01.appmifile.com/webfile/globalimg/products/pc/mi-body-composition-scale-2/specs01.png)

Passively monitors a **Xiaomi Mi Body Composition Scale 2 (XMTZC05HM)** over
Bluetooth Low Energy, decodes weight and impedance readings, automatically
figures out *which* of your household's users a reading belongs to, computes
some derived body metrics, and optionally stores everything in InfluxDB — 
with optional push-notification confirmation for readings it isn't confident
about.

Runs continuously as a systemd service; no cloud account, no phone app
required to actually use the scale day-to-day.

## Features

- **BLE advertisement parsing** for the XMTZC05HM's 13-byte payload (service
  UUID `0000181b`) — weight, impedance, timestamp, and display unit, with
  correct unit conversion (lbs/catty are converted to true kilograms, not
  just relabeled)
- **Measurement session tracking** — groups the scale's repeated
  advertisements into one reading per weigh-in, with deduplication and
  intermediate/final reading phases
- **Multi-user auto-detection** — no manual "select a user" step. Each
  configured person gets a 2D Kalman filter tracking their weight and
  impedance as slowly-drifting state; a new reading is scored against every
  user via Mahalanobis distance, with a plausibility penalty for physically
  implausible day-over-day jumps
- **Confidence-gated assignment** — a clear winner gets auto-assigned; a
  genuinely ambiguous reading (e.g. two users converging on similar weights)
  is held rather than guessed
- **Ambiguous-reading confirmation via ntfy** — optionally sends a push
  notification with a tap-button per user (plus "Neither") to a self-hosted
  [ntfy](https://ntfy.sh) server; your reply commits the reading and updates
  that user's filter, same as an automatic assignment would
- **Per-user weight notifications** — optional push to each user's own ntfy
  topic on every successfully-attributed reading
- **Derived body metrics** — BMI and BMR (exact formulas) plus body fat %,
  body water %, and lean mass estimates (published anthropometric formulas —
  see [Derived metrics](#derived-metrics) below for an important caveat)
- **InfluxDB 1.x storage** with bounded startup retries and ongoing
  self-healing reconnection, so a slow-starting Docker container doesn't
  permanently disable writing for the whole run
- **One-shot CLI utilities**: read scale device info, set the scale's clock,
  set its display unit
- **systemd service** for always-on background operation, with automatic
  restart on failure

## Requirements

- Linux with BlueZ (developed and tested on EndeavourOS; should work on any
  systemd + BlueZ distro)
- Python 3.11+ (uses stdlib `tomllib`; s back to the `tomli` package on
  older versions)
- A Mi Body Composition Scale 2 (XMTZC05HM) cifically — this decodes the
  V2 advertisement protocol (13-byte payload on service `0000181b`), which
  differs from older weight-only Mi Scales (10-byte payload on `0000181d`)
- Optional: a running InfluxDB 1.x instance, for storage
- Optional: a self-hosted [ntfy](https://ntfy.sh) server, for ambiguous-reading
  confirmations and per-user push notifications

## Installation

```bash
git clone https://github.com/RoninTech/miscale
cd miscale
pip install -r requirements.txt --break-system-packages   # Arch/EndeavourOS and similar
# or, without --break-system-packages, use a virtualenv:
#   python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt

cp miscale_toml.example miscale.toml
```

Find your scale's MAC address (step on the scale first so it's advertising):

```bash
bluetoothctl scan on
```

Edit `miscale.toml`: set `scan.scale_mac`, fill in `[user_info.*]` for each
person (`start_weight`, `start_impedance`, `height`, `age`, `gender`), and enable/configure
`[influxdb]` and `[ntfy]` as needed. Every setting in
`miscale_toml.example` is commented with what it does and its default.

## Usage

**Run in the foreground** (good for initial testing):

```bash
python3 miscale.py --config miscale.toml
```

**One-shot commands** (each connects via GATT, does one thing, and exits —
independent of whether the scanner is otherwise running):

```bash
python3 miscale.py --config miscale.toml -i   # --get-info: read device info from the scale
python3 miscale.py --config miscale.toml -t   # --set-time: sync the scale's internal clock
python3 miscale.py --config miscale.toml -u kg   # --set-unit: kg | lbs | catty | jin
python3 miscale.py --config miscale.toml -d   # --dump-history: dump stored history records
python3 miscale.py --config miscale.toml -e   # --erase-history: erase all stored history
```

### `--dump-history` (`-d`) — how it works

The scale stores up to ~20 readings internally. `--dump-history` reads them
over BLE GATT using a multi-step protocol:

1. Send `0x01 [device_id]` — asks the scale how many records it has
2. Send `0x03` — closes the size query
3. Send `0x02` — starts data transfer; the scale then sends each record as a
   BLE notification (13 bytes each, same format as live advertisements)
4. Send `0x03` — ends data transfer
5. Send `0x04 [device_id]` — **advances the sync position**, telling the scale
   those records have been read

**Important: after a successful `-d`, the scale's history will be empty.**
Step 5 advances the sync position past the records that were just read, so
they are no longer retrievable via dump. This is by design — it prevents
the same records from being dumped repeatedly across restarts.

If you want to preserve the history, note the output or copy the log file
before running `-d` again. To reset the history to zero, use `-e` (which
requires typing `erase` to confirm).

**Run as a systemd service** (recommended for actual daily use):

```bash
sudo cp systemd/miscale.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/miscale.service   # fill in User/Group and paths
sudo systemctl daemon-reload
sudo systemctl enable --now miscale.service
systemctl status miscale.service
journalctl -u miscale -f   # live logs — also catches uncaught-exception
                            # tracebacks that miscale.log alone won't
```

## How user detection works

Each configured user's weight and impedance are tracked as a 2D Kalman
filter, seeded from `start_weight`/`start_impedance` in config on first run
and persisted to a JSON sidecar file thereafter. A new reading is scored
against every user's filter (Mahalanobis distance, penalized for physically
implausible jumps), and the closest match wins — unless the gap between the
best and second-best candidate is too small (`confidence_gap_threshold`), in
which case the reading is held as ambiguous rather than guessed.

If `[ntfy]` is enabled, an ambiguous reading triggers a push notification
with one tap-button per user plus "Neither"; your answer commits the reading
to that user's filter exactly as an automatic assignment would. Left
unanswered past `pending_timeout_hours`, it's written to InfluxDB tagged
`user="unassigned"` instead.

See `AGENTS.md` for the full protocol reverse-engineering notes, byte-level
advertisement format, and implementation details.

## Derived metrics

| Metric | Basis | Uses impedance? |
|---|---|---|
| `bmi` | [Exact formula](https://en.wikipedia.org/wiki/Body_mass_index) | No |
| `bmr` | [Mifflin-St Jeor equation](https://en.wikipedia.org/wiki/Basal_metabolic_rate) | No |
| `fat_percent_est` | [Deurenberg et al.](https://en.wikipedia.org/wiki/Body_fat_percentage#From_BMI) — BMI + age + sex | **No** |
| `water_percent_est` | [Hume & Weyers (1971)](https://pubmed.ncbi.nlm.nih.gov/5573437/) — height + weight + sex | **No** |
| `lean_mass_kg_est` | [Boer (1984)](https://en.wikipedia.org/wiki/Lean_body_mass#Boer) — height + weight + sex | **No** |

**Important:** despite the scale measuring impedance, none of the three
`_est` fields actually use it. Xiaomi's real impedance-based BIA
coefficients are proprietary and not verified in any public source, so
rather than fabricate them, this project uses published, peer-reviewed
anthropometric formulas instead — genuinely useful for tracking trends, but
not equivalent to true bioelectrical impedance analysis. There's no
`muscle_mass`, `bone_mass`, `protein_percent`, `visceral_fat`, or
`metabolic_age` for the same reason: no verified formula exists for these
that isn't just guessing.

## InfluxDB schema

Measurement `weight`, one point per finalized reading:

- **Tag**: `user` (`user1`, `user2`, or `unassigned` — configured user keys,
  not fixed; note `user` is a reserved InfluxQL keyword and needs
  double-quoting, e.g. `WHERE "user"='user1'`, in hand-written queries)
- **Fields**: `weight_kg`, `impedance_ohm`, `unit_name`, `confidence`,
  `session_id`, `dist_<user_id>` (one per configured user), plus `bmi`,
  `bmr`, `fat_percent_est`, `water_percent_est`, `lean_mass_kg_est` when the
  relevant config fields are available for that user

## Known limitations

- `-i or --get-info`'s "what unit is the scale in" answer only checks InfluxDB's
  most recent reading — if InfluxDB is disabled, unreachable, or has no data
  yet, it simply omits that line. No live-BLE fallback (accepted tradeoff).
- ntfy's `base_url` is auto-detected from your machine's LAN-facing IP by
  default. The one thing that can't be auto-detected is the port your
  self-hosted ntfy server is exposed on — set `ntfy.port` accordingly. If
  auto-detection ever picks the wrong interface (multiple NICs, an active
  VPN), set `ntfy.base_url` explicitly to override it.
- Session IDs (`<mac>:<counter>`) are now randomly seeded (0–999) on each
  process start to reduce collision probability, but they can still
  theoretically collide across restarts. Harmless for InfluxDB storage
  (it's a field, not part of a point's identity), but worth knowing if
  cross-referencing a stored `session_id` back to a specific log line long
  after a restart.
- Tested against exactly one scale model (XMTZC05HM). The advertisement
  format is specific to this V2 protocol.

## Protocol references

This project's BLE advertisement decoding was reverse-engineered against
live hardware captures, cross-checked with:

- [wiecosystem/Bluetooth](https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md) — BLE protocol spec
- [lolouk44/xiaomi_mi_scale](https://github.com/lolouk44/xiaomi_mi_scale) — Python/MQTT implementation
- [Theengs decoder for XMTZC05HM](https://decoder.theengs.io/devices/XMTZC05HM.html)
- [ESPHome `xiaomi_miscale` component](https://gist.github.com/passcod/2132f8d1ca33232108f00e86f6147e47) — the authoritative source for the 13-byte payload layout used here

See `AGENTS.md` for the full protocol notes, including a documented
correction to an earlier (incorrect) byte-layout assumption.

## License
- MIT.  See LICENSE file.
