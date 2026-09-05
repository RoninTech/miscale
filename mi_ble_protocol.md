# Xiaomi Mi Body Composition Scale 2 (XMTZC05HM) — BLE Protocol Summary

> Note: Xiaomi/Huami has never published an official protocol spec. This is
> community reverse-engineering, primarily from the
> [wiecosystem/Bluetooth](https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md)
> project and the [openScale](https://github.com/oliexdev/openScale) app.
> Treat values (especially derived body-composition metrics) as best-effort,
> not verified science.

## Overview

Most live data comes from **BLE advertisements**, not a persistent GATT
connection — this is how Mi Fit / Zepp Life shows a reading the instant you
step on the scale. The GATT services exist mainly for history sync and
device configuration.

## Advertisement packet

Sent as Service Data (AD type `0x16`), advertised under the standard
**Body Composition** service UUID `0000181b-0000-1000-8000-00805f9b34fb`.
Payload is 13 bytes, multi-byte fields little-endian:

| Bytes | Field |
|-------|-------|
| 0     | Type/unit byte — `0x02` = kg, `0x03` = lbs (not a bitfield, see below) |
| 1     | Status flags (see below) |
| 2–3   | Year |
| 4     | Month |
| 5     | Day |
| 6     | Hour |
| 7     | Minute |
| 8     | Second |
| 9–10  | Impedance (raw) |
| 11–12 | Weight — `×200` for kilograms, `×100` for pounds |

> ⚠️ **Correction (verified against real hardware, see below):** older
> community write-ups (including the first version of this doc) describe
> a 16-bit control-byte layout with bits such as "bit 8 = finished / has
> load" and "bit 10 = weight stabilised." That layout comes from an
> **older/different advertisement format** (the 10-byte, `0000181d`
> "weight-only" service used by first-gen Mi Scales) and does **not**
> match what the XMTZC05HM actually sends on its 13-byte `0000181b`
> payload. Byte 0 is *not* a flag byte on this device — it's a plain
> unit-code value (`0x02`/`0x03`). The real status bits live entirely
> in byte 1.

### Byte 1 status flags (verified)

Source: ESPHome's `xiaomi_miscale` component (`parse_message_v2_`),
which is actively tested against real 13-byte/`0000181b` payloads —
cross-checked here against a live capture sequence and confirmed to
match exactly.

| Bit (in byte 1) | Overall bit # | Meaning |
|---|---|---|
| 1 | 9  | `has_impedance` — impedance bytes (9–10) are valid |
| 5 | 13 | `is_stabilized` — weight (and impedance, once present) has settled |
| 7 | 15 | `load_removed` — person has stepped **off** the scale (opposite polarity from "has load") |

All other bits in byte 1, and all of byte 0's bits below the unit
code, are not used by any tested parser and shouldn't be relied on
(the "partial data" bit some docs list at bit 5 of byte 0 does not
appear to apply to this device/format — treat it as unconfirmed).

**Recommended "accept this reading" condition:**
`is_stabilized == true AND load_removed == false`

### Observed measurement lifecycle (verified against a raw capture)

Below is a full byte-level decode of a real 5-packet capture (raw hex →
flags/impedance/weight/timestamp), confirmed self-consistent with no
contradictions:

| # | Raw hex | flags | has_imp (bit9) | stable (bit13) | load_removed (bit15) | Impedance | Weight |
|---|---|---|---|---|---|---|---|
| 1 | `02a6b207031305051b65020c35` | `0xa602` | 1 | 1 | **1** | 613 Ω | 67.90 kg |
| 2 | `0204b207031305092000008a34` | `0x0402` | 0 | 0 | 0 | 0 | 67.25 kg |
| 3 | `0204b207031305092000000c35` | `0x0402` | 0 | 0 | 0 | 0 | 67.90 kg |
| 4 | `0224b207031305092200000c35` | `0x2402` | 0 | 1 | 0 | 0 | 67.90 kg |
| 5 | `0226b207031305092264020c35` | `0x2602` | 1 | 1 | 0 | 612 Ω | 67.90 kg |

Reading order: load detected, weight unsettled (#2) → weight settles,
impedance still computing (#4) → both settle — **this is the final
reading** (#5) → the "idle" packet (#1) is what's broadcast
afterward.

### Idle packets are not a distinct state

Comparing row 1 (`0xa602`) to row 5 (`0x2602`): the two byte-1 values
differ by exactly `0x80` — i.e. only `load_removed` (bit 15) differs —
and the weight/impedance are the same completed reading (67.90 kg,
~612–613 Ω). **"Idle" is the scale re-advertising its last completed,
stable measurement with `load_removed` set**, so a passive BLE scanner
(no GATT connection needed) can still pick up the final result after
someone steps off. There is no separate "final" flag combination to
wait for beyond `is_stabilized=1 AND load_removed=0`.

### Unload transient: fixed sentinel weight

Separately, packets with `load_removed=1 AND is_stabilized=0` (the
brief moment someone is stepping *off*, as opposed to the idle
rebroadcast above) have been observed reporting the same precise
value — `12.75 kg` — across multiple, unrelated sessions. That
precision-matching recurrence across sessions indicates it's a fixed
firmware placeholder for the unload transient, not a real physical
reading. Recommend filtering out any packet where
`load_removed=1 AND is_stabilized=0` outright, regardless of the
weight value, rather than trusting it as noisy-but-real data.

### Clock note

The date/time fields in the payload (bytes 2–8) may read as an
unsynchronized default (e.g. year ~1970) if the scale's clock has
never been set via the `Current Time` characteristic
(`00002a2b-...`). This doesn't affect the flag/weight/impedance
fields — it's independent of measurement state.

## GATT services

### Body Composition service (`0000181b-0000-1000-8000-00805f9b34fb`)

| Characteristic | UUID | Access | Notes |
|---|---|---|---|
| Current Time | `00002a2b-...` | Read/Write | Format: `year[0], year[1], month, day, hour, min, sec, 0x00, 0x00` |
| Body Composition Feature | `00002a9b-...` | Read | Apparently unused |
| Body Composition Measurement | `00002a9c-...` | Indicate | Mirrors the advertisement payload |
| Body Composition History | `00002a2f-0000-3512-2118-0009af100700` | Write/Notify | Main history-sync characteristic (see below) |

### Huami Configuration service (`00001530-0000-3512-2118-0009af100700`)

| Characteristic | UUID | Access | Notes |
|---|---|---|---|
| DFU Control Point | `00001531-...` | Write/Notify | Firmware update |
| DFU Packet | `00001532-...` | Write (no response) | Firmware update |
| Scale configuration | `00001542-...` | Read/Write/Notify | Unit, calibration, LED, self-test, etc. |
| Battery | `00001543-...` | Read/Write/Notify | Two bytes; both `0x01` = low battery |

## Body Composition History protocol

The device tracks a per-client "device id" (randomly generated on first Mi
Fit pairing) so it only sends new data.

**Get data size:**
1. Send `0x01 [device id]`
2. If no response, response length < 3, or `response[0] != 1` → send `0x03` and abort
3. Data size = `response[1]`, `response[2]`; send `0x03` to end

**Get data:**
1. Register for notifications, send `0x02`
2. Read notifications (same payload format as the advertisement)
3. Send `0x03` to end
4. If you received as many records as indicated by the size check, send
   `0x04 [device id]` to advance the sync position
5. If registering/sending `0x02` failed, still send `0x03`

## Device configuration

> ⚠️ Unlike the advertisement flag bits above, nothing in this section
> has been empirically verified against your hardware yet — it's carried
> over from the wiecosystem documentation. Treat command bytes as a
> starting point to test, not confirmed behavior, until you've checked
> a read-back or observed the effect.

### Setting the clock

Given the ~1970 timestamps seen in your captures, the scale's clock is
almost certainly unset. To set it:

1. **Connect via GATT** (not passive scanning) — writes require an
   active connection.
2. **Write 9 bytes** to the Current Time characteristic
   (`00002a2b-0000-1000-8000-00805f9b34fb`, under the `0000181b-...`
   Body Composition service):

   | Byte | Field |
   |---|---|
   | 0 | Year low byte |
   | 1 | Year high byte (little-endian u16, e.g. 2026 = `0xEA 0x07`) |
   | 2 | Month (1–12) |
   | 3 | Day (1–31) |
   | 4 | Hour (0–23) |
   | 5 | Minute |
   | 6 | Second |
   | 7 | `0x00` |
   | 8 | `0x00` |

   Example for 2026-09-04 14:30:00: `EA 07 09 04 0E 1E 00 00 00`

3. **Write with response**, then **read the characteristic back**
   immediately to confirm the scale accepted it, and check the next
   advertisement's date bytes to see whether it stuck.

```python
import asyncio
from datetime import datetime
from bleak import BleakClient

CURRENT_TIME_CHAR = "00002a2b-0000-1000-8000-00805f9b34fb"
SCALE_MAC = "XX:XX:XX:XX:XX:XX"

async def set_time():
    now = datetime.now()
    payload = bytes([
        now.year & 0xFF, (now.year >> 8) & 0xFF,
        now.month, now.day,
        now.hour, now.minute, now.second,
        0x00, 0x00
    ])
    async with BleakClient(SCALE_MAC) as client:
        await client.write_gatt_char(CURRENT_TIME_CHAR, payload, response=True)
        readback = await client.read_gatt_char(CURRENT_TIME_CHAR)
        print("Scale reports:", readback.hex())

asyncio.run(set_time())
```

**Caveats to test for:**
- Likely **local time, not UTC** — confirm against the next advertisement.
- Some Huami devices require an active connection but not necessarily
  bonding/pairing for this characteristic; if the write silently fails,
  check for an encryption/auth error from the GATT server.

### Other configurable settings (`00001542-...`, Huami Configuration service)

All commands below are writes to the **Scale configuration**
characteristic (`00001542-0000-3512-2118-0009af100700`). Several
expect a notify subscription first to catch the response.

| Setting | Command | Response / notes |
|---|---|---|
| Display unit | `0x06 0x04 0x00 [unit]` | `unit`: `0x00` SI (kg), `0x01` imperial (lbs), `0x02` catty |
| Partial measures on/off | `0x06 0x10 0x00 [!enable]` | Response: `0x16 0x06 0x10 0x00 0x01` |
| Erase stored history | `0x06 0x12 0x00 0x00` | Response: `0x16 0x06 0x12 0x00 0x01` — irreversible |
| LED display on/off | `0x04 0x02` / `0x04 0x03` | — |
| Calibrate | `0x06 0x05 0x00 0x00` | Undocumented what state the scale must be in first |
| Self-test on/off | `0x04 0x01` / `0x04 0x04` | — |
| Sandglass (hourglass icon) mode | `0x06 [mode] 0x00` | `mode` is uint16: `0x000A` or `0x000B` |
| Read sandglass mode | Read characteristic | `0x03 0x00` if a mode is set |
| Start one-foot measure | Subscribe notify, send `0x06 0x0f 0x00 0x00` | Notification: `0x06 0x0f 0x00 [flags] [time]×2` — flags `0x01` measuring / `0x02` finished; time is inverted, ×100 |
| Stop one-foot measure | `0x06 0x11 0x00 0x00` | — |

Given how much the advertisement-side documentation needed correcting
against your real captures, I'd sanity-check any of these you actually
plan to use (especially erase-history and calibrate) with a read-back
or an observed effect before relying on them.


## From raw data to body metrics

Impedance + weight + user age/height/sex feed into Xiaomi/Huami's
proprietary calculation library (originally a native SDK from Holtek, the
scale's MCU vendor) to derive fat %, muscle mass, water %, bone mass,
visceral fat, BMR, "body age," "body type," and an overall body score. Mi
Fit/Zepp doesn't use every value the library provides (e.g. protein % and
"body age" are Xiaomi's own additions), so third-party reimplementations
(openScale, Home Assistant integrations, etc.) may not match the app
exactly.

## Note on the older 10-byte format

Some scanners/logs (e.g. the [passcod gist](https://gist.github.com/passcod/2132f8d1ca33232108f00e86f6147e47))
decode a *different*, 10-byte advertisement under service UUID `0000181d`
(the generic "Weight Scale" service). That's the format used by
weight-only, non-impedance Mi Scales — its flag byte and bit meanings
are unrelated to the 13-byte `0000181b` format documented above. If
you ever see 10-byte payloads from the same MAC, they're a different
advertisement channel, not a firmware variant of the 13-byte one.

## Sources

- https://github.com/wiecosystem/Bluetooth/blob/master/doc/devices/huami.health.scale2.md
  (GATT service/characteristic layout and history-sync protocol — accurate;
  its advertisement control-byte table is the part superseded above)
- https://esphome.io/api/xiaomi__miscale_8cpp_source.html — tested,
  production parser for the 13-byte `0000181b` format; source of the
  verified byte-1 flag bits
- https://github.com/oliexdev/openScale
- https://dev.to/henrylim96/reading-xiaomi-mi-scale-data-with-web-bluetooth-scanning-api-1mb9
- https://gist.github.com/passcod/2132f8d1ca33232108f00e86f6147e47
  (decodes the older 10-byte/`0000181d` format — not directly applicable
  to the 13-byte payload)
