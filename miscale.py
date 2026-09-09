#!/usr/bin/env python3
"""Mi Scale BLE Monitor — Stage 1

Scans for Mi Body Composition Scale 2 (XMTZC05HM) advertisements,
decodes weight/impedance readings, and logs them.
"""

import argparse
import asyncio
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

try:
    # Python 3.11+
    import tomllib
except ImportError:
    # Fallback for older Python
    import tomli as tomllib  # type: ignore[import-not-found, no-redef]

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCALE_SERVICE_UUID = "0000181b-0000-1000-8000-00805f9b34fb"
SCALE_SERVICE_UUID_16BIT = "181b"  # 16-bit UUID portion
CURRENT_TIME_CHAR = "00002a2b-0000-1000-8000-00805f9b34fb"
SCALE_MFR_ID_HUAMI = 0x100  # Huami (Amazfit) manufacturer ID

# Device Information Service (0000180A) characteristics
DIS_SERVICE_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
CHAR_SYSTEM_ID = "00002a23-0000-1000-8000-00805f9b34fb"
CHAR_PNP_ID = "00002a50-0000-1000-8000-00805f9b34fb"
CHAR_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"
CHAR_HW_REV = "00002a27-0000-1000-8000-00805f9b34fb"
CHAR_FW_REV = "00002a28-0000-1000-8000-00805f9b34fb"

# Body Composition service (0000181b) characteristics
CHAR_BODY_COMP_HISTORY = "00002a2f-0000-3512-2118-0009af100700"

# Huami Configuration service (00001530) characteristics
CHAR_BATTERY = "00001543-0000-3512-2118-0009af100700"
CHAR_CONFIG = "00001542-0000-3512-2118-0009af100700"

# Byte 0 unit codes (XMTZC05HM, 13-byte payload on 0000181b service)
UNIT_KG = 0x02
UNIT_LBS = 0x03
UNIT_CATTY = 0x04

UNIT_NAMES = {UNIT_KG: "kg", UNIT_LBS: "lbs", UNIT_CATTY: "catty"}

# Unit codes for the scale configuration write command (0x06 0x04 0x00 [unit])
CONFIG_UNIT_KG = 0x00
CONFIG_UNIT_LBS = 0x01
CONFIG_UNIT_CATTY = 0x02

CONFIG_UNIT_MAP = {
    "kg": CONFIG_UNIT_KG,
    "lbs": CONFIG_UNIT_LBS,
    "catty": CONFIG_UNIT_CATTY,
    "jin": CONFIG_UNIT_CATTY,
}

# Conversion factors to true kilograms. "Catty" here is the Chinese
# market catty/jin used by Xiaomi scales (0.5 kg), not the Imperial catty.
LBS_TO_KG = 0.45359237
CATTY_TO_KG = 0.5

# Byte 1 status flag bit positions (LSB-first within byte 1)
# Overall bit = byte1_bit + 8 (since byte 0 provides 8 bits)
FLAG_HAS_IMPEDANCE = 1     # bit 1 of byte1 = overall bit 9
FLAG_STABILIZED = 5        # bit 5 of byte1 = overall bit 13
FLAG_LOAD_REMOVED = 7      # bit 7 of byte1 = overall bit 15


def decode_weight(unit_code: int, weight_raw: int) -> tuple[float, str]:
    """Convert a raw weight value from the scale to kilograms and a unit name.

    Handles KG (0x02), LBS (0x03), and Catty (0x04) unit codes.
    Unknown unit codes fall through with an unconverted raw value.
    """
    if unit_code == UNIT_KG:
        weight_kg = weight_raw / 200.0
        unit_name = "kg"
    elif unit_code == UNIT_LBS:
        weight_kg = (weight_raw / 100.0) * LBS_TO_KG
        unit_name = "lbs"
    elif unit_code == UNIT_CATTY:
        weight_kg = (weight_raw / 100.0) * CATTY_TO_KG
        unit_name = "catty"
    else:
        weight_kg = weight_raw
        unit_name = f"unknown(0x{unit_code:02x})"
    return weight_kg, unit_name


DEFAULT_CONFIG = "miscale.toml"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_file: str, log_level: str) -> logging.Logger:
    """Configure logging to console and optional file."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    logger = logging.getLogger("miscale")
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler (if log_file is set)
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    """Load and return the TOML configuration file."""
    config_path = Path(path).expanduser()
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "rb") as f:
        return tomllib.load(f)


async def set_scale_time(mac: str, logger: logging.Logger) -> None:
    """Connect to the scale and set its internal clock to the current time."""
    from datetime import datetime

    now = datetime.now()
    payload = bytes([
        now.year & 0xFF, (now.year >> 8) & 0xFF,
        now.month, now.day,
        now.hour, now.minute, now.second,
        0x00, 0x00,
    ])
    mac_upper = mac.upper()
    logger.info("Connecting to scale %s to set internal clock...", mac_upper)
    try:
        async with BleakClient(mac, timeout=10.0) as client:
            await client.write_gatt_char(CURRENT_TIME_CHAR, payload, response=True)
            readback = await client.read_gatt_char(CURRENT_TIME_CHAR)
            read_time = datetime(
                int.from_bytes(readback[0:2], "little"),
                readback[2], readback[3],
                readback[4], readback[5], readback[6],
            )
            logger.info(
                "Scale clock set — wrote %s, read back %s",
                now.strftime("%Y-%m-%d %H:%M:%S"),
                read_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
    except Exception as exc:
        logger.warning("Failed to set scale clock: %s", exc)


async def erase_history(mac: str, logger: logging.Logger) -> None:
    """Connect to the scale and erase its internal history.

    Subscribes to notifications on the Huami Configuration characteristic,
    sends 0x06 0x12 0x00 0x00, and waits for the response
    0x10 0x06 0x12 0x00 0x01 (success, firmware V1.0.0.12) or
    0x16 0x06 0x12 0x00 0x01 (older firmwares) within 60 seconds.
    This operation is irreversible.
    """
    payload = bytes([0x06, 0x12, 0x00, 0x00])
    expected_responses = [bytes([0x16, 0x06, 0x12, 0x00, 0x01]), bytes([0x10, 0x06, 0x12, 0x00, 0x01])]
    mac_upper = mac.upper()
    logger.info("Connecting to scale %s to erase history...", mac_upper)

    response_found = asyncio.Event()
    response_data: list = []
    notification_count = 0

    def _notification_handler(_char, data: bytearray) -> None:
        nonlocal notification_count
        notification_count += 1
        logger.info(
            "Erase notification #%d: %s",
            notification_count,
            bytes(data).hex(),
        )
        if bytes(data) in expected_responses:
            response_data.extend(data)
            response_found.set()

    try:
        async with BleakClient(mac, timeout=60.0) as client:
            await client.start_notify(CHAR_CONFIG, _notification_handler)
            await client.write_gatt_char(CHAR_CONFIG, payload, response=False)
            logger.info("History erase command sent to scale %s (this may take 20-30s)...", mac_upper)

            try:
                await asyncio.wait_for(response_found.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.error(
                    "Timed out waiting for erase-history response from scale %s "
                    "(scale may still be processing). Total notifications received: %d",
                    mac_upper,
                    notification_count,
                )
                raise BleakError("No response from scale")

            logger.info(
                "History erase confirmed on scale %s (response: %s)",
                mac_upper,
                bytes(response_data).hex(),
            )
    except Exception as exc:
        logger.warning("Failed to erase history: %s", exc)


async def dump_history(mac: str, logger: logging.Logger) -> None:
    """Connect to the scale and dump its internal history via GATT.

    Protocol (per reverse-engineered spec):
      1. Send 0x01 [device_id] to query record count
      2. Send 0x03 to close the size query
      3. Subscribe to notifications, send 0x02 to start data transfer
      4. Read notification records (same format as BLE advertisements)
      5. Send 0x03 to end, then 0x04 [device_id] to advance sync position
    """
    from datetime import datetime, timezone

    DEVICE_ID = bytes([0xDE, 0xAD, 0xBE, 0xEF])
    mac_upper = mac.upper()
    logger.info("Connecting to scale %s to dump history...", mac_upper)

    records: list = []
    size_response: list = []
    size_received = asyncio.Event()
    phase = "size"  # "size" or "data"

    def _notification_handler(_char, data: bytearray) -> None:
        if phase == "size" and len(size_response) == 0:
            size_response.extend(data)
            size_received.set()
        elif phase == "data":
            if len(data) == 13:
                records.append(bytes(data))
            else:
                logger.debug(
                    "Non-record notification in data phase (%d bytes): %s",
                    len(data), bytes(data).hex(),
                )
        else:
            logger.debug(
                "Notification in phase='%s' (size_response_len=%d): %s",
                phase, len(size_response), bytes(data).hex(),
            )

    try:
        async with BleakClient(mac, timeout=15.0) as client:
            # Subscribe to notifications BEFORE any writes
            await client.start_notify(CHAR_BODY_COMP_HISTORY, _notification_handler)
            await asyncio.sleep(0.2)

            # Step 1: Query data size
            size_cmd = bytes([0x01]) + DEVICE_ID
            await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, size_cmd, response=False)
            logger.info("Querying history size...")

            try:
                await asyncio.wait_for(size_received.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("No response to size query, sending 0x03 and aborting")
                await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, bytes([0x03]), response=False)
                return

            size_resp = bytes(size_response)
            if len(size_resp) < 3 or size_resp[0] != 1:
                logger.warning(
                    "Invalid size response (%s), sending 0x03 and aborting",
                    size_resp.hex(),
                )
                await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, bytes([0x03]), response=False)
                return

            record_count = int.from_bytes(size_resp[1:3], "little")
            logger.info("Scale reports %d history record(s)", record_count)

            # Step 2: Close size query
            await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, bytes([0x03]), response=False)
            await asyncio.sleep(0.5)  # give scale time to process before data transfer

            if record_count == 0:
                logger.info("No history records on scale.")
                return

            # Step 3: Start data transfer
            phase = "data"
            await asyncio.sleep(0.2)  # let notification handler settle
            data_received = asyncio.Event()
            expected_count = record_count

            logger.info("Starting data transfer (%d record(s))...", expected_count)
            await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, bytes([0x02]), response=False)

            async def _wait_for_data():
                # Wait until we have all records or timeout
                while len(records) < expected_count:
                    await asyncio.sleep(0.5)
                    if len(records) > 0 and len(records) % 20 == 0:
                        logger.info("Received %d/%d records", len(records), expected_count)

            try:
                await asyncio.wait_for(
                    _wait_for_data(), timeout=120.0
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout receiving history records (got %d/%d)",
                    len(records), expected_count,
                )

            # Step 4: End data transfer
            try:
                await client.write_gatt_char(CHAR_BODY_COMP_HISTORY, bytes([0x03]), response=False)
                await asyncio.sleep(0.2)

                # Step 5: Advance sync position
                await client.write_gatt_char(
                    CHAR_BODY_COMP_HISTORY, bytes([0x04]) + DEVICE_ID, response=False
                )
                await asyncio.sleep(0.2)
            except BleakError as exc:
                logger.warning("BLE write failed during cleanup: %s (connection may have dropped)", exc)

            # Decode and display records
            logger.info("=" * 60)
            logger.info("HISTORY RECORDS (%d total)", len(records))
            logger.info("=" * 60)

            for i, raw in enumerate(records, 1):
                try:
                    unit_code = raw[0]
                    flags = raw[1]
                    year = int.from_bytes(raw[2:4], "little")
                    month = raw[4]
                    day = raw[5]
                    hour = raw[6]
                    minute = raw[7]
                    second = raw[8]
                    impedance = int.from_bytes(raw[9:11], "little")
                    weight_raw = int.from_bytes(raw[11:13], "little")

                    weight_kg, unit_name = decode_weight(unit_code, weight_raw)

                    has_imp = bool(flags & 0x02)
                    is_stabilized = bool(flags & 0x20)
                    load_removed = bool(flags & 0x80)

                    ts = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)

                    logger.info(
                        "  #%d: %s | %s %s | imp=%s%s Ω | stable=%s load=%s",
                        i,
                        ts.strftime("%Y-%m-%d %H:%M:%S"),
                        weight_kg,
                        unit_name,
                        impedance if has_imp else "N/A",
                        "" if has_imp else " (invalid)",
                        is_stabilized,
                        load_removed,
                    )
                except Exception as exc:
                    logger.warning("  #%d: failed to decode: %s (%s)", i, raw.hex(), exc)

            logger.info("=" * 60)

    except Exception as exc:
        logger.warning("Failed to dump history: %s", exc)


async def set_scale_unit(mac: str, unit: str, logger: logging.Logger) -> None:
    """Connect to the scale and set its display unit (kg / lbs / catty).

    Sends a write command to the Huami Configuration characteristic
    (00001542): 0x06 0x04 0x00 [unit_code].
    """
    unit_lower = unit.lower()
    if unit_lower not in CONFIG_UNIT_MAP:
        logger.error(
            "Unknown display unit '%s'. Supported units: kg, lbs, catty (jin)",
            unit,
        )
        sys.exit(1)

    unit_code = CONFIG_UNIT_MAP[unit_lower]
    payload = bytes([0x06, 0x04, 0x00, unit_code])
    mac_upper = mac.upper()
    logger.info(
        "Connecting to scale %s to set display unit to %s (0x%02x)...",
        mac_upper, unit_lower, unit_code,
    )
    try:
        async with BleakClient(mac, timeout=10.0) as client:
            await client.write_gatt_char(CHAR_CONFIG, payload, response=False)
            logger.info(
                "Display unit set to %s on scale %s", unit_lower, mac_upper,
            )
    except Exception as exc:
        logger.warning("Failed to set scale display unit: %s", exc)


async def get_scale_info(mac: str, logger: logging.Logger, config: dict) -> None:
    """Connect to the scale and read Device Information Service characteristics."""
    mac_upper = mac.upper()
    logger.info("Connecting to scale %s to read device information...", mac_upper)
    
    # Query InfluxDB for last weight unit if enabled
    influx_cfg = config.get("influxdb", {})
    influx_writer = InfluxDBWriter(influx_cfg, logger)
    await influx_writer.initialize()
    last_unit = influx_writer.get_last_unit()
    if last_unit is not None:
        logger.info("Weight unit (from last reading): %s", last_unit)
    influx_writer.close()
    
    chars = [
        (CHAR_SYSTEM_ID, "System ID"),
        (CHAR_PNP_ID, "PnP ID"),
        (CHAR_SERIAL, "Serial Number"),
        (CHAR_HW_REV, "Hardware Revision"),
        (CHAR_FW_REV, "Firmware Version"),
        (CHAR_BATTERY, "Battery"),
        (CURRENT_TIME_CHAR, "Current Time"),
        (CHAR_CONFIG, "Scale Config"),
    ]
    
    try:
        async with BleakClient(mac, timeout=10.0) as client:
            for char_uuid, label in chars:
                try:
                    value = await client.read_gatt_char(char_uuid)
                    # Special handling for battery characteristic
                    if char_uuid == CHAR_BATTERY and len(value) >= 2:
                        # Two bytes; both 0x01 = low battery
                        if value[0] == 0x01 and value[1] == 0x01:
                            logger.info("%s: low (0x%02X%02X)", label, value[0], value[1])
                        else:
                            logger.info("%s: OK (0x%02X%02X)", label, value[0], value[1])
                        continue
                    # Special handling for Current Time characteristic
                    if char_uuid == CURRENT_TIME_CHAR and len(value) >= 7:
                        year = int.from_bytes(value[0:2], "little")
                        month, day, hour, minute, second = value[2], value[3], value[4], value[5], value[6]
                        logger.info("%s: %04d-%02d-%02d %02d:%02d:%02d", label, year, month, day, hour, minute, second)
                        continue
                    # Special handling for Scale Config characteristic
                    if char_uuid == CHAR_CONFIG and len(value) >= 3:
                        logger.info("%s: %s", label, value.hex())
                        continue
                    # Try to decode as UTF-8 string
                    try:
                        decoded = value.decode("utf-8").strip()
                        # Verify it's printable
                        if decoded.isprintable():
                            logger.info("%s: %s", label, decoded)
                            continue
                    except (UnicodeDecodeError, ValueError):
                        pass
                    # Fall back to hex for binary data
                    logger.info("%s: %s", label, value.hex())
                except BleakError as e:
                    logger.warning("%s: not available (%s)", label, e)
                except Exception as e:
                    logger.warning("%s: error (%s: %s)", label, type(e).__name__, e)
    except Exception as exc:
        logger.warning("Failed to read scale information: %s", exc)

# ---------------------------------------------------------------------------
# Advertisement parser
# ---------------------------------------------------------------------------


def _normalise_uuid_key(key) -> str:
    """Normalise a UUID key from bleak to a dash-free lowercase hex string."""
    if isinstance(key, bytes):
        return key.hex()
    return str(key).lower().replace("-", "")


def _is_scale_uuid(key) -> bool:
    """Check if a service_data key matches the scale's service UUID."""
    normalised = _normalise_uuid_key(key)
    # Match full 128-bit UUID
    expected_full = SCALE_SERVICE_UUID.replace("-", "")
    if normalised == expected_full:
        return True
    # Match 16-bit UUID (bleak may use 2-byte form for standard UUIDs)
    expected_16 = SCALE_SERVICE_UUID_16BIT
    if normalised == expected_16:
        return True
    return False


def parse_advertisement(service_data: dict[bytes | str, bytes]) -> Optional[dict]:
    """Parse Mi Scale V2 advertisement data.

    XMTZC05HM sends a 13-byte payload on service UUID 0000181b:
      byte 0  = unit code (0x02=kg, 0x03=lbs, 0x04=catty)
      byte 1  = status flags (has_impedance, is_stabilized, load_removed)
      bytes 2-8 = timestamp (year/month/day/hour/minute/second)
      bytes 9-10 = impedance (little-endian, if has_impedance)
      bytes 11-12 = weight (little-endian, *200 for kg, *100 for lbs)

    Returns a dict with weight_kg (always true kilograms, converted if the
    scale is set to lbs/catty), unit_code, unit_name, impedance_ohm (if
    present), timestamp, raw_hex — or None if the advertisement is not a
    valid scale reading.
    """
    # Look for our service UUID in service_data
    data = None
    for key, val in service_data.items():
        if _is_scale_uuid(key):
            data = val
            break

    if data is None or len(data) < 13:
        return None

    payload = data

    # Byte 0: unit code
    unit_code = payload[0]
    if unit_code not in UNIT_NAMES:
        # Unknown unit code — skip
        return None

    # Byte 1: status flags (LSB-first within byte 1)
    byte1 = payload[1]
    has_impedance = bool(byte1 & (1 << FLAG_HAS_IMPEDANCE))   # bit 1 → overall bit 9
    is_stabilized = bool(byte1 & (1 << FLAG_STABILIZED))       # bit 5 → overall bit 13
    load_removed = bool(byte1 & (1 << FLAG_LOAD_REMOVED))      # bit 7 → overall bit 15

    # Valid reading: weight stabilised AND person still on scale.
    # Impedance typically only arrives once the reading is fully final —
    # readings without it yet are still valid "intermediate" readings and
    # are handled as such by SessionTracker, not rejected here.
    if not is_stabilized or load_removed:
        return None

    # Timestamp from bytes 2-8
    year = int.from_bytes(payload[2:4], "little")
    month = payload[4]
    day = payload[5]
    hour = payload[6]
    minute = payload[7]
    second = payload[8]

    # Build timestamp from scale's internal clock
    try:
        ts = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        # Invalid date from scale — skip
        return None

    weight_raw = int.from_bytes(payload[11:13], "little")
    weight_kg, _ = decode_weight(unit_code, weight_raw)

    result = {
        "weight_kg": round(weight_kg, 2),
        "unit_code": unit_code,
        "unit_name": UNIT_NAMES.get(unit_code, f"unknown (0x{unit_code:02x})"),
        "timestamp": ts,
        "raw_hex": data.hex(),
    }

    if has_impedance:
        impedance = int.from_bytes(payload[9:11], "little")
        if impedance > 0:
            result["impedance_ohm"] = impedance

    return result

# ---------------------------------------------------------------------------
# Measurement session tracking
# ---------------------------------------------------------------------------


class MeasurementSession:
    """Tracks a single measurement lifecycle for one scale."""

    def __init__(self, session_id: str, mac: str):
        self.session_id = session_id
        self.mac = mac
        self.start_time: Optional[datetime] = None
        self._readings: list[dict] = []
        self.final_impedance: Optional[int] = None

    def _add_reading(self, weight_kg: float, timestamp: datetime, impedance: Optional[int]) -> None:
        self._readings.append({
            "weight_kg": round(weight_kg, 2),
            "timestamp": timestamp,
            "impedance": impedance,
        })
        if impedance is not None:
            self.final_impedance = impedance

    def add_reading(self, reading: dict) -> str:
        """Add a reading to this session. Returns phase: 'intermediate' or 'final'."""
        weight = reading["weight_kg"]
        ts = reading["timestamp"]
        impedance = reading.get("impedance_ohm")
        if self.start_time is None:
            self.start_time = ts
        self._add_reading(weight, ts, impedance)
        if impedance is not None:
            return "final"
        return "intermediate"


class SessionTracker:
    """Tracks measurement sessions per MAC address.

    Detects new sessions when:
    - The time gap between readings exceeds session_gap_seconds (default 5s)
    - Weight changes significantly (>0.5 kg)
    - The previous session is older than the timeout window (default 120s)
    """

    def __init__(self, timeout_seconds: int = 120, session_gap_seconds: int = 5):
        self._timeout = timeout_seconds
        self._session_gap = session_gap_seconds
        self._sessions: dict[str, MeasurementSession] = {}
        self._session_counters: dict[str, int] = {}
        self._last_seen: dict[str, datetime] = {}

    def _next_session_id(self, mac: str) -> str:
        if mac not in self._session_counters:
            self._session_counters[mac] = random.randint(0, 999)
        self._session_counters[mac] += 1
        return f"{mac.replace(':', '')}:{self._session_counters[mac]:03d}"

    def process(self, mac: str, reading: dict) -> Optional[tuple]:
        """Process a valid reading.

        Returns (session_id, phase, reading) for logging, or None if
        the reading is a duplicate of the last logged packet.
        """
        weight = reading["weight_kg"]
        ts = reading["timestamp"]

        if mac in self._sessions:
            session = self._sessions[mac]
            last_ts = self._last_seen.get(mac, ts)

            # Check if there's a time gap (new measurement session)
            if session._readings and (ts - last_ts).total_seconds() > self._session_gap:
                self._sessions[mac] = MeasurementSession(
                    self._next_session_id(mac), mac,
                )
                session = self._sessions[mac]
            # Check if this is a stale session (old measurement)
            elif (ts - last_ts).total_seconds() > self._timeout:
                self._sessions[mac] = MeasurementSession(
                    self._next_session_id(mac), mac,
                )
                session = self._sessions[mac]
            # Check if weight changed significantly (new person)
            elif session._readings and abs(weight - session._readings[-1]["weight_kg"]) > 0.5:
                session = MeasurementSession(
                    self._next_session_id(mac), mac,
                )
                self._sessions[mac] = session

        else:
            session = MeasurementSession(
                self._next_session_id(mac), mac,
            )
            self._sessions[mac] = session

        self._last_seen[mac] = ts

        # Check for duplicate advertisement (same weight, same impedance,
        # within 3 seconds — scale re-advertises the same packet many times)
        impedance = reading.get("impedance_ohm")
        if session._readings:
            last = session._readings[-1]
            if (last["weight_kg"] == weight
                    and last.get("impedance") == impedance
                    and (ts - last["timestamp"]).total_seconds() < 3):
                return None

        phase = session.add_reading(reading)
        return (session.session_id, phase, reading)

# ---------------------------------------------------------------------------
# User detection (weight + impedance, per-user Kalman filter)
# ---------------------------------------------------------------------------
#
# Each configured user gets a small 2D Kalman filter tracking their "true"
# (weight_kg, impedance_ohm) as a slowly-drifting hidden state. A finalized
# scale reading is scored against every user's filter using a Mahalanobis
# distance (normalizes weight and impedance onto one comparable scale using
# the filter's own uncertainty), penalized if it implies an implausible
# day-over-day weight jump for that user. The closest user wins, unless the
# two best candidates are too close to call, in which case the reading is
# flagged ambiguous rather than guessed.
#
# Filter state is persisted to a small JSON sidecar file so re-starts don't
# have to re-bootstrap from the config's start_weight — that path is only
# used the first time a user has no prior readings at all.


@dataclass
class UserFilterState:
    user_id: str
    name: str
    x: np.ndarray                      # [weight_kg, impedance_ohm]
    P: np.ndarray                      # 2x2 covariance
    last_seen: Optional[datetime] = None


@dataclass
class DetectionResult:
    assignment: Optional[str]          # user_id, or None if ambiguous
    confidence: float                  # 0..1
    distances: dict                    # user_id -> penalized distance
    reason: str
    gap: float = 0.0                   # second-best - best distance
    threshold: float = 0.0             # confidence_gap_threshold at classification time


class UserDetector:
    """Tracks per-user (weight, impedance) state and classifies finalized
    scale readings against the configured users."""

    def __init__(self, user_info_cfg: dict, detection_cfg: dict,
                 state_file: Path, logger: logging.Logger):
        self.logger = logger
        self.state_file = state_file

        self.process_var_weight = detection_cfg.get("process_var_weight", 0.02)
        self.process_var_impedance = detection_cfg.get("process_var_impedance", 4.0)
        self.meas_var_weight = detection_cfg.get("meas_var_weight", 0.09)
        self.meas_var_impedance = detection_cfg.get("meas_var_impedance", 25.0)
        self.default_start_impedance = detection_cfg.get("default_start_impedance", 500.0)
        self.confidence_gap_threshold = detection_cfg.get("confidence_gap_threshold", 1.0)
        self.max_plausible_delta_kg_per_day = detection_cfg.get(
            "max_plausible_delta_kg_per_day", 2.0
        )
        # Normal same-day fluctuation (food, water, clothing) that should
        # never be penalized regardless of how few hours have passed —
        # without this floor, two readings an hour apart would treat any
        # ordinary swing as an implausible jump.
        self.max_intraday_delta_kg = detection_cfg.get("max_intraday_delta_kg", 1.5)
        self.max_plausibility_penalty = detection_cfg.get("max_plausibility_penalty", 8.0)

        self.states: dict[str, UserFilterState] = self._bootstrap(user_info_cfg)
        self._load_persisted_state()

    # -- setup ---------------------------------------------------------

    def _bootstrap(self, user_info_cfg: dict) -> dict[str, UserFilterState]:
        """Seed one filter per configured user from their start_weight."""
        states = {}
        for user_id, cfg in user_info_cfg.items():
            start_weight = cfg.get("start_weight")
            if start_weight is None:
                self.logger.warning(
                    "user_info.%s has no start_weight — skipping", user_id
                )
                continue
            start_impedance = cfg.get("start_impedance", self.default_start_impedance)
            states[user_id] = UserFilterState(
                user_id=user_id,
                name=cfg.get("name", user_id),
                x=np.array([float(start_weight), float(start_impedance)]),
                P=np.diag([1.0, 100.0]),  # fairly loose initial uncertainty
                last_seen=None,
            )
        return states

    def _load_persisted_state(self) -> None:
        """Overlay any previously-saved filter state on top of the
        config-bootstrapped defaults. Only users present in the file are
        overridden — new users just keep their config bootstrap."""
        if not self.state_file.exists():
            self.logger.info(
                "No persisted user state found at %s — using config start weights",
                self.state_file,
            )
            return

        try:
            with open(self.state_file, "r") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning("Could not read user state file (%s) — using config start weights", exc)
            return

        for user_id, entry in saved.items():
            if user_id not in self.states:
                continue
            state = self.states[user_id]
            state.x = np.array(entry["x"])
            state.P = np.array(entry["P"])
            state.last_seen = (
                datetime.fromisoformat(entry["last_seen"])
                if entry.get("last_seen") else None
            )
        self.logger.info("Loaded persisted user state from %s", self.state_file)

    async def _save_persisted_state(self) -> None:
        def _write() -> None:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                user_id: {
                    "x": state.x.tolist(),
                    "P": state.P.tolist(),
                    "last_seen": state.last_seen.isoformat() if state.last_seen else None,
                }
                for user_id, state in self.states.items()
            }
            tmp_path = self.state_file.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=2)
            tmp_path.replace(self.state_file)
        await asyncio.to_thread(_write)

    # -- classification --------------------------------------------------

    def _predict(self, state: UserFilterState) -> None:
        """Grow uncertainty to allow for drift since the last update."""
        state.P = state.P + np.diag([self.process_var_weight, self.process_var_impedance])

    def _mahalanobis(self, state: UserFilterState, z: np.ndarray) -> float:
        R = np.diag([self.meas_var_weight, self.meas_var_impedance])
        y = z - state.x
        S = state.P + R
        return float(np.sqrt(y.T @ np.linalg.inv(S) @ y))

    def _plausibility_penalty(self, state: UserFilterState, new_weight: float,
                               now: datetime) -> float:
        if state.last_seen is None:
            return 1.0
        days_elapsed = (now - state.last_seen).total_seconds() / 86400.0
        # Same-day comparisons always get at least the intraday budget;
        # the per-day rate only extends the budget for multi-day gaps.
        max_allowed = max(
            self.max_intraday_delta_kg,
            self.max_plausible_delta_kg_per_day * days_elapsed,
        )
        implied_delta = abs(new_weight - state.x[0])
        if implied_delta <= max_allowed:
            return 1.0
        overshoot_ratio = implied_delta / max_allowed
        penalty = 1.0 + (overshoot_ratio - 1.0) * 2.0
        return min(penalty, self.max_plausibility_penalty)

    async def classify(self, weight_kg: float, impedance_ohm: float,
                       timestamp: datetime) -> DetectionResult:
        """Score a finalized reading against every user and either assign
        it or flag it ambiguous. Updates the winning user's filter."""
        z = np.array([weight_kg, float(impedance_ohm)])

        scores: dict[str, float] = {}
        for user_id, state in self.states.items():
            self._predict(state)
            base_dist = self._mahalanobis(state, z)
            penalty = self._plausibility_penalty(state, weight_kg, timestamp)
            scores[user_id] = base_dist * penalty

        if not scores:
            return DetectionResult(None, 0.0, {}, "no_users_configured")

        ranked = sorted(scores.items(), key=lambda kv: kv[1])

        if len(ranked) == 1:
            best_user, best_score = ranked[0]
            await self._commit(best_user, z, timestamp)
            return DetectionResult(best_user, 1.0, scores, "only_user_configured",
                                    gap=0.0, threshold=self.confidence_gap_threshold)

        (best_user, best_score), (_, second_score) = ranked[0], ranked[1]
        gap = second_score - best_score

        if gap < self.confidence_gap_threshold:
            return DetectionResult(
                None,
                1.0 - min(gap / self.confidence_gap_threshold, 1.0),
                scores,
                "low_confidence_gap",
                gap=gap, threshold=self.confidence_gap_threshold,
            )

        confidence = min(gap / (self.confidence_gap_threshold * 3), 1.0)
        await self._commit(best_user, z, timestamp)
        return DetectionResult(best_user, confidence, scores, "clear_winner",
                                gap=gap, threshold=self.confidence_gap_threshold)

    async def _commit(self, user_id: str, z: np.ndarray, timestamp: datetime) -> None:
        """Kalman-update the winning user's filter and persist state."""
        state = self.states[user_id]
        R = np.diag([self.meas_var_weight, self.meas_var_impedance])
        y = z - state.x
        S = state.P + R
        K = state.P @ np.linalg.inv(S)
        state.x = state.x + K @ y
        state.P = (np.eye(2) - K) @ state.P
        state.last_seen = timestamp
        await self._save_persisted_state()

    async def manual_commit(self, user_id: str, weight_kg: float,
                            impedance_ohm: Optional[float], timestamp: datetime) -> bool:
        """Commit a human-confirmed assignment exactly like a clear
        classifier win — updates that user's Kalman filter and persists
        state. Returns False if user_id isn't recognized."""
        if user_id not in self.states:
            self.logger.warning("manual_commit: unknown user_id '%s' — ignoring", user_id)
            return False
        impedance = impedance_ohm if impedance_ohm else self.states[user_id].x[1]
        z = np.array([weight_kg, float(impedance)])
        await self._commit(user_id, z, timestamp)
        return True

    def name_for(self, user_id: Optional[str]) -> str:
        if user_id is None:
            return "unknown"
        return self.states[user_id].name if user_id in self.states else user_id


# ---------------------------------------------------------------------------
# Derived body metrics
# ---------------------------------------------------------------------------
#
# BMI and BMR are exact, standard formulas (no estimation involved).
#
# fat_percent_est / water_percent_est / lean_mass_kg_est are anthropometric
# estimates from published, peer-reviewed formulas (cited below) — the
# "_est" suffix and separate naming is deliberate: these do NOT use
# impedance at all, only weight/height/age/sex. Xiaomi's actual
# impedance-based BIA coefficients are proprietary and unverified in any
# public source, so they aren't implemented here. Treat these as rough,
# clearly-labeled estimates, not a substitute for true BIA readings.


def calculate_bmi(weight_kg: float, height_cm: Optional[float]) -> Optional[float]:
    """Body Mass Index = weight_kg / height_m^2. Exact formula, no
    estimation. Returns None if height isn't configured for this user."""
    if not height_cm:
        return None
    height_m = height_cm / 100.0
    return round(weight_kg / (height_m ** 2), 2)


def calculate_bmr(weight_kg: float, height_cm: Optional[float],
                   age: Optional[float], gender: Optional[str]) -> Optional[float]:
    """Basal Metabolic Rate via the Mifflin-St Jeor equation (1990) —
    the standard, widely-validated formula, not reverse-engineered.
    Returns None if height/age/gender aren't all configured for this user."""
    if not height_cm or age is None or not gender:
        return None
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    g = gender.strip().lower()
    if g in ("male", "m"):
        return round(base + 5.0, 1)
    elif g in ("female", "f"):
        return round(base - 161.0, 1)
    return None


def calculate_body_fat_percent_est(weight_kg: float, height_cm: Optional[float],
                                    age: Optional[float],
                                    gender: Optional[str]) -> Optional[float]:
    """Anthropometric body fat % estimate — Deurenberg et al. (1991),
    "Body mass index as a measure of body fatness: age- and sex-specific
    prediction formulas", Br J Nutr 65(2):105-114. Uses BMI + age + sex
    only; does NOT use impedance."""
    bmi = calculate_bmi(weight_kg, height_cm)
    if bmi is None or age is None or not gender:
        return None
    g = gender.strip().lower()
    if g in ("male", "m"):
        return round((bmi * 1.2) + (age * 0.23) - 16.2, 1)
    elif g in ("female", "f"):
        return round((bmi * 1.2) + (age * 0.23) - 5.4, 1)
    return None


def calculate_body_water_percent_est(weight_kg: float, height_cm: Optional[float],
                                      gender: Optional[str]) -> Optional[float]:
    """Anthropometric body water % estimate — Hume & Weyers (1971),
    "Relationship between total body water and surface area in normal
    and obese subjects", J Clin Pathol 24:234-238. Formula gives total
    body water in litres; converted to % of body weight here (1 L water
    ~= 1 kg). Uses height + weight + sex only; does NOT use impedance."""
    if not height_cm or not gender:
        return None
    g = gender.strip().lower()
    if g in ("male", "m"):
        tbw_l = (0.194786 * height_cm) + (0.296785 * weight_kg) - 14.012934
    elif g in ("female", "f"):
        tbw_l = (0.34454 * height_cm) + (0.183809 * weight_kg) - 35.270121
    else:
        return None
    if tbw_l <= 0:
        return None
    return round((tbw_l / weight_kg) * 100.0, 1)


def calculate_lean_mass_kg_est(weight_kg: float, height_cm: Optional[float],
                                gender: Optional[str]) -> Optional[float]:
    """Anthropometric lean body mass estimate — Boer (1984), "Estimated
    lean body mass as an index for normalization of body fluid volumes
    in humans", Am J Physiol 247(4 Pt 2):F632-6. Uses height + weight +
    sex only; does NOT use impedance."""
    if not height_cm or not gender:
        return None
    g = gender.strip().lower()
    if g in ("male", "m"):
        return round((0.407 * weight_kg) + (0.267 * height_cm) - 19.2, 1)
    elif g in ("female", "f"):
        return round((0.252 * weight_kg) + (0.473 * height_cm) - 48.3, 1)
    return None


def compute_derived_metrics(user_info_cfg: dict, user_id: Optional[str],
                             weight_kg: float) -> dict:
    """Compute whatever derived metrics are possible for this user given
    their configured height/age/gender. Returns {} for unassigned/unknown
    users or users missing the needed config fields."""
    if not user_id or user_id not in user_info_cfg:
        return {}

    cfg = user_info_cfg[user_id]
    height_cm = cfg.get("height")
    age = cfg.get("age")
    gender = cfg.get("gender")

    metrics = {}

    bmi = calculate_bmi(weight_kg, height_cm)
    if bmi is not None:
        metrics["bmi"] = bmi

    bmr = calculate_bmr(weight_kg, height_cm, age, gender)
    if bmr is not None:
        metrics["bmr"] = bmr

    fat_pct = calculate_body_fat_percent_est(weight_kg, height_cm, age, gender)
    if fat_pct is not None:
        metrics["fat_percent_est"] = fat_pct

    water_pct = calculate_body_water_percent_est(weight_kg, height_cm, gender)
    if water_pct is not None:
        metrics["water_percent_est"] = water_pct

    lean_mass = calculate_lean_mass_kg_est(weight_kg, height_cm, gender)
    if lean_mass is not None:
        metrics["lean_mass_kg_est"] = lean_mass

    return metrics


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------


class InfluxDBWriter:
    """Writes scale readings to InfluxDB 1.x.

    Connection handling has two layers, since a one-shot connection
    attempt at startup permanently disables writing for the whole run if
    InfluxDB (e.g. a Docker container) isn't ready yet at boot:
      1. Bounded retries at construction time — handles the common
         "systemd started this before the InfluxDB container finished
         initializing" race, without blocking forever if InfluxDB is
         genuinely misconfigured or intentionally stopped.
      2. ensure_connected(), called periodically from the main loop —
         self-heals if InfluxDB comes up late (past the startup retry
         budget) or bounces mid-run, without needing a restart. Rate
         limited so a persistently-down server isn't hammered.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self._enabled = config.get("enabled", False)
        self._host = config.get("host", "localhost")
        self._port = config.get("port", 8086)
        self._database = config.get("database", "miscale")
        self._retention_policy = config.get("retention_policy", "autogen")
        self._startup_retries = config.get("startup_retries", 6)
        self._startup_retry_delay = config.get("startup_retry_delay_seconds", 5)
        self._reconnect_interval = config.get("reconnect_interval_seconds", 60)
        self._logger = logger
        self._client = None
        self._last_reconnect_attempt: Optional[datetime] = None

    async def initialize(self) -> None:
        """Non-blocking startup connection with retries. Call this from
        an async context before using the writer — handles InfluxDB's
        Docker container not being ready yet without blocking the event loop."""
        if not self._enabled:
            return
        for attempt in range(1, self._startup_retries + 1):
            if self._connect_once():
                return
            if attempt < self._startup_retries:
                self._logger.info(
                    "InfluxDB not reachable yet at %s:%d (attempt %d/%d) — "
                    "retrying in %ds...",
                    self._host, self._port, attempt, self._startup_retries,
                    self._startup_retry_delay,
                )
                await asyncio.sleep(self._startup_retry_delay)

        self._logger.warning(
            "Could not connect to InfluxDB at %s:%d after %d attempts (~%ds) — "
            "proceeding without it. Will keep retrying every %ds in the "
            "background; measurements are logged but not stored until it "
            "connects.",
            self._host, self._port, self._startup_retries,
            self._startup_retries * self._startup_retry_delay,
            self._reconnect_interval,
        )
        self._last_reconnect_attempt = datetime.now(timezone.utc)

    def _connect_once(self) -> bool:
        """Single connection attempt. Returns True on success."""
        try:
            from influxdb import InfluxDBClient  # type: ignore
            client = InfluxDBClient(
                host=self._host,
                port=self._port,
                database=self._database,
                timeout=5,
            )
            client.write_points([])
            self._client = client
            self._logger.info(
                "InfluxDB connected at %s:%d, database=%s",
                self._host, self._port, self._database,
            )
            return True
        except Exception as exc:
            self._client = None
            self._logger.debug("InfluxDB connection attempt failed: %s", exc)
            return False

    def ensure_connected(self) -> None:
        """Call periodically from the main loop. No-op if already
        connected, disabled, or the reconnect interval hasn't elapsed
        yet — rate limits retries against a persistently-down server."""
        if not self._enabled or self._client is not None:
            return
        now = datetime.now(timezone.utc)
        if (self._last_reconnect_attempt is not None
                and (now - self._last_reconnect_attempt).total_seconds() < self._reconnect_interval):
            return
        self._last_reconnect_attempt = now
        if self._connect_once():
            self._logger.info("InfluxDB connection recovered")

    def get_last_weight(self, user: str) -> Optional[float]:
        """Get the most recent weight for a user from InfluxDB."""
        if not self._client or not self._enabled:
            return None

        try:
            query = (
                f'SELECT weight_kg FROM "weight" '
                f"WHERE \"user\" = '{user}' ORDER BY time DESC LIMIT 1"
            )
            result = self._client.query(query)  # type: ignore[assignment]
            points = list(result.get_points())  # type: ignore[union-attr]
            if points:
                return float(points[0]["weight_kg"])
        except Exception as exc:
            self._logger.debug("Failed to query last weight for %s: %s", user, exc)
        return None

    def write_reading(self, session_id: str, user: str,
                      reading: dict, confidence: float = 1.0,
                      distances: Optional[dict] = None,
                      extra_fields: Optional[dict] = None):
        """Write a finalized reading to InfluxDB."""
        if not self._client or not self._enabled:
            return

        weight = reading["weight_kg"]
        impedance = reading.get("impedance_ohm")
        ts = reading["timestamp"]
        unit_name = reading.get("unit_name")

        influx_ts = ts.astimezone(timezone.utc)

        fields = {
            "weight_kg": weight,
            "impedance_ohm": impedance if impedance else 0,
            "confidence": confidence,
            "session_id": session_id,
        }
        if unit_name is not None:
            fields["unit_name"] = unit_name
        for uid, dist in (distances or {}).items():
            fields[f"dist_{uid}"] = float(dist)
        for key, val in (extra_fields or {}).items():
            fields[key] = val

        json_body = [
            {
                "measurement": "weight",
                "tags": {
                    "user": user,
                },
                "fields": fields,
                "time": influx_ts,
            },
        ]

        try:
            self._client.write_points(json_body, retention_policy=self._retention_policy)
            self._logger.debug(
                "Wrote reading to InfluxDB: %.2f kg, user=%s, session=%s, confidence=%.2f",
                weight, user, session_id, confidence,
            )
        except Exception as exc:
            self._logger.warning("Failed to write to InfluxDB: %s", exc)

    def get_last_unit(self) -> Optional[str]:
        """Get the weight unit name from the most recent reading."""
        if not self._client or not self._enabled:
            return None

        try:
            query = (
                'SELECT "unit_name" FROM "weight" '
                "ORDER BY time DESC LIMIT 1"
            )
            result = self._client.query(query)  # type: ignore[assignment]
            points = list(result.get_points())  # type: ignore[union-attr]
            if points and points[0].get("unit_name"):
                return str(points[0]["unit_name"])
        except Exception as exc:
            self._logger.debug("Failed to query last unit: %s", exc)
        return None

    def close(self):
        """Close the InfluxDB connection."""
        if self._client:
            self._client.close()
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None


# ---------------------------------------------------------------------------
# ntfy ambiguous-reading confirmation
# ---------------------------------------------------------------------------
#
# Ambiguous readings are held (not written to InfluxDB) until resolved:
# a notification is sent to a self-hosted ntfy topic with one tap-action
# button per configured user; tapping one POSTs a reply to a second
# ("reply") topic, which a background thread long-polls. A resolved
# reading is committed to that user's Kalman filter exactly like a clear
# classifier win, then written to InfluxDB with manual_override=true.
# Anything left unanswered past pending_timeout_hours is written as
# "unassigned", same as today's default behaviour.
#
# Reply encoding is deliberately a plain "session_id|user_id" string
# rather than JSON, to sidestep ntfy's Actions header needing its
# delimiter characters (commas/semicolons) escaped inside a JSON body.


class PendingConfirmations:
    """Durable (JSON file-backed) queue of ambiguous readings awaiting a
    human confirmation reply. Survives process restarts since a reply
    could arrive minutes, hours, or never."""

    def __init__(self, path: Path, logger: logging.Logger):
        self.path = path
        self.logger = logger
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning(
                "Could not read pending confirmations file (%s) — starting fresh", exc
            )
            return {}

    async def _save(self) -> None:
        def _write() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(self._data, f, indent=2)
            tmp_path.replace(self.path)
        await asyncio.to_thread(_write)

    async def add(self, session_id: str, reading: dict, distances: dict, confidence: float) -> None:
        self._data[session_id] = {
            "weight_kg": reading["weight_kg"],
            "impedance_ohm": reading.get("impedance_ohm"),
            "timestamp": reading["timestamp"].isoformat(),
            "unit_name": reading.get("unit_name"),
            "distances": distances,
            "confidence": confidence,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._save()

    async def pop(self, session_id: str) -> Optional[dict]:
        entry = self._data.pop(session_id, None)
        if entry is not None:
            await self._save()
        return entry

    async def pop_expired(self, timeout_hours: float) -> dict:
        now = datetime.now(timezone.utc)
        expired = {}
        for sid, entry in list(self._data.items()):
            created = datetime.fromisoformat(entry["created_at"])
            if (now - created).total_seconds() > timeout_hours * 3600:
                expired[sid] = self._data.pop(sid)
        if expired:
            await self._save()
        return expired

    @staticmethod
    def entry_to_reading(entry: dict) -> dict:
        """Reconstruct a reading dict (as parse_advertisement would
        produce) from a stored pending entry, for re-use with
        write_reading()/manual_commit()."""
        return {
            "weight_kg": entry["weight_kg"],
            "impedance_ohm": entry.get("impedance_ohm"),
            "timestamp": datetime.fromisoformat(entry["timestamp"]),
            "unit_name": entry.get("unit_name"),
        }


def detect_local_ip() -> Optional[str]:
    """Best-effort detection of this machine's LAN-facing IP address, by
    asking the OS which local interface it would use to reach an external
    address. No packets actually reach 8.8.8.8 — UDP is connectionless,
    so connect() here just consults the routing table to pick a source
    interface/IP, which is normally the real LAN adapter even on a
    machine with multiple interfaces (Docker bridges, VPNs, etc. are
    excluded since they're not the default route). Returns None if no
    route exists at all (e.g. no network connectivity)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def send_ambiguous_notification(base_url: str, topic: str, reply_topic: str,
                                 session_id: str, reading: dict, distances: dict,
                                 user_ids: list, logger: logging.Logger) -> None:
    """Publish an ntfy notification with one tap-action button per user.
    Tapping a button POSTs "<session_id>|<user_id>" to reply_topic."""
    reply_url = f"{base_url.rstrip('/')}/{reply_topic}"

    actions = "; ".join(
        f'http, {uid.capitalize()}, {reply_url}, method=POST, '
        f'body="{session_id}|{uid}", clear=true'
        for uid in user_ids
    )
    actions += f'; http, Neither, {reply_url}, method=POST, body="{session_id}|__skip__", clear=true'

    dist_str = ", ".join(f"{u}={d:.2f}" for u, d in distances.items())
    message = (
        f"Weight: {reading['weight_kg']:.2f} kg"
        + (f", impedance: {reading['impedance_ohm']} \u03a9" if reading.get("impedance_ohm") else "")
        + f"\nDistances: {dist_str}"
        + "\nWho was this for?"
    )

    url = f"{base_url.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": "Ambiguous scale reading",
            "Priority": "default",
            "Actions": actions,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        if status not in (200, 201):
            logger.error(
                "[%s] Notification server rejected request with status code: %d",
                session_id, status,
            )
        else:
            logger.info(
                "[%s] Sent ntfy confirmation request to topic '%s' (HTTP %d)",
                session_id, topic, status,
            )
            logger.debug(
                    "[%s] ntfy %s confirmation contents: %s",
                    session_id, topic, body.strip() or "<empty>"
            )
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "[%s] Failed to send ntfy notification to topic '%s': %s", session_id, topic, exc
        )


def send_weight_reading_notification(base_url: str, topic_template: str, user_id: str,
                                      user_name: str, reading: dict, metrics: dict,
                                      manual_override: bool, logger: logging.Logger) -> None:
    """Publish a plain FYI notification with a user's latest reading to
    their own dedicated ntfy topic, e.g. "miscale_user1_weight_reading".
    No action buttons — just a push, independent of the ambiguous-reading
    confirmation flow (this fires for every successfully-attributed
    reading, auto-classified or manually confirmed)."""
    topic = topic_template.format(user=user_id)

    lines = [f"Weight: {reading['weight_kg']:.2f} kg"]
    if reading.get("impedance_ohm"):
        lines.append(f"Impedance: {reading['impedance_ohm']} \u03a9")
    if "bmi" in metrics:
        lines.append(f"BMI: {metrics['bmi']}")
    if "bmr" in metrics:
        lines.append(f"BMR: {metrics['bmr']} kcal/day")
    if "fat_percent_est" in metrics:
        lines.append(f"Body fat (est): {metrics['fat_percent_est']}%")
    if "water_percent_est" in metrics:
        lines.append(f"Body water (est): {metrics['water_percent_est']}%")
    if "lean_mass_kg_est" in metrics:
        lines.append(f"Lean mass (est): {metrics['lean_mass_kg_est']} kg")
    if manual_override:
        lines.append("(manually confirmed)")
    message = "\n".join(lines)

    url = f"{base_url.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": f"{user_name} - new weight reading",
            "Priority": "low",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        if status not in (200, 201):
            logger.error(
                "[%s] Notification server rejected request with status code: %d",
                user_id, status,
            )
        else:
            logger.info(
                "[%s] Sent weight-reading notification to topic '%s' (HTTP %d)",
                user_id, topic, status,
            )
            logger.debug(
                    "[%s] Sent %s response : %s",
                    user_id, topic, body.strip() or "<empty>",
            )
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "[%s] Failed to send weight-reading notification to topic '%s': %s",
            user_id, topic, exc,
        )


def _parse_ntfy_reply_line(raw_line: bytes) -> Optional[dict]:
    """Parse one line of ntfy's /json subscription stream. Returns
    {"session_id": ..., "user": ...} for a valid reply message, else None."""
    line = raw_line.decode("utf-8").strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return None
    if msg.get("event") != "message":
        return None
    body = msg.get("message", "")
    if "|" not in body:
        return None
    session_id, _, user_id = body.partition("|")
    if not session_id or not user_id:
        return None
    return {"session_id": session_id, "user": user_id}


def _ntfy_listener_thread(base_url: str, reply_topic: str,
                           stop_event: threading.Event,
                           out_queue: "queue.Queue",
                           logger: logging.Logger) -> None:
    """Long-polls the ntfy reply topic in a background thread, pushing
    parsed {session_id, user} dicts onto out_queue as replies arrive.
    Reconnects automatically on any error (network hiccup, LAN-only ntfy
    server briefly unreachable, phone's action POST timing out, etc.)."""
    url = f"{base_url.rstrip('/')}/{reply_topic}/json"
    while not stop_event.is_set():
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=90) as resp:
                logger.info("ntfy listener connected to %s", url)
                while not stop_event.is_set():
                    raw_line = resp.readline()
                    if not raw_line:
                        break  # connection closed by server — reconnect
                    reply = _parse_ntfy_reply_line(raw_line)
                    if reply is not None:
                        logger.info(
                            "ntfy listener received reply: session=%s user=%s",
                            reply["session_id"], reply["user"],
                        )
                        out_queue.put(reply)
        except Exception as exc:
            if not stop_event.is_set():
                logger.warning("ntfy listener lost connection, reconnecting: %s", exc)
                time.sleep(5)


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------


async def run_scanner(config: dict, logger: logging.Logger) -> None:
    """Main scanning loop — runs until interrupted."""
    scan_cfg = config["scan"]
    mac = scan_cfg.get("scale_mac", "")
    hci_device = scan_cfg.get("hci_device", "hci0")
    scan_interval = scan_cfg.get("scan_interval", 5)
    session_gap = scan_cfg.get("session_gap_seconds", 5)

    tracker = SessionTracker(session_gap_seconds=session_gap)

    user_info_cfg = config.get("user_info", {})
    detection_cfg = config.get("detection", {})
    state_file = Path(
        detection_cfg.get("state_file", "~/.cache/miscale/user_state.json")
    ).expanduser()
    detector = UserDetector(user_info_cfg, detection_cfg, state_file, logger)

    # InfluxDB writer (Stage 2)
    influx_cfg = config.get("influxdb", {})
    influx_writer = InfluxDBWriter(influx_cfg, logger)
    await influx_writer.initialize()

    # Ambiguous-reading confirmation via ntfy (optional)
    ntfy_cfg = config.get("ntfy", {})
    ntfy_enabled = ntfy_cfg.get("enabled", False)
    ntfy_send_weight_readings = ntfy_cfg.get("send_weight_readings", False)
    ntfy_topic = ntfy_cfg.get("topic", "")
    ntfy_reply_topic = ntfy_cfg.get("reply_topic", "")
    ntfy_timeout_hours = ntfy_cfg.get("pending_timeout_hours", 24)
    ntfy_weight_topic_template = ntfy_cfg.get(
        "weight_reading_topic_template", "miscale_{user}_weight_reading"
    )
    pending_confirmations_file = Path(
        ntfy_cfg.get("pending_file", "~/.cache/miscale/pending_confirmations.json")
    ).expanduser()
    pending_confirmations = PendingConfirmations(pending_confirmations_file, logger)

    # base_url: use an explicit config value if given (escape hatch for
    # multi-NIC machines, active VPNs, or anything else that trips up
    # auto-detection); otherwise auto-detect this machine's LAN IP, since
    # a manually-typed address is exactly what caused a phone-side "can't
    # connect to localhost" failure previously — the URL baked into the
    # notification's tap-actions has to be reachable from the *phone*,
    # not just from this script.
    ntfy_base_url = ntfy_cfg.get("base_url", "").strip()
    if not ntfy_base_url and (ntfy_enabled or ntfy_send_weight_readings):
        ntfy_port = ntfy_cfg.get("port", 8080)
        detected_ip = detect_local_ip()
        if detected_ip:
            ntfy_base_url = f"http://{detected_ip}:{ntfy_port}"
            logger.info(
                "Auto-detected LAN IP for ntfy: %s -> base_url=%s", detected_ip, ntfy_base_url
            )
        else:
            logger.warning(
                "Could not auto-detect a LAN IP for ntfy (no network route found) — "
                "set ntfy.base_url manually in config. Disabling ntfy features for this run."
            )
            ntfy_enabled = False
            ntfy_send_weight_readings = False

    ntfy_reply_queue: "queue.Queue" = queue.Queue()
    ntfy_stop_event = threading.Event()
    ntfy_thread: Optional[threading.Thread] = None

    if ntfy_enabled:
        if not ntfy_topic or not ntfy_reply_topic:
            logger.warning(
                "ntfy.enabled = true but topic/reply_topic aren't both set — "
                "ambiguous-reading confirmations are disabled for this run."
            )
            ntfy_enabled = False
        else:
            ntfy_thread = threading.Thread(
                target=_ntfy_listener_thread,
                args=(ntfy_base_url, ntfy_reply_topic, ntfy_stop_event,
                      ntfy_reply_queue, logger),
                daemon=True,
            )
            ntfy_thread.start()
            logger.info(
                "ntfy confirmation listener started (%s/%s)", ntfy_base_url, ntfy_reply_topic
            )

    logger.info(
        "Starting Mi Scale BLE monitor (adapter=%s, interval=%ds)",
        hci_device,
        scan_interval,
    )
    if mac:
        logger.info("Monitoring scale MAC: %s", mac.upper())
    else:
        logger.info("Monitoring all devices (no MAC filter)")

    # Log the actual effective values (including defaults applied where the
    # config file omitted a key) so it's obvious whether a config edit and
    # restart actually took effect, without needing to re-read the toml.
    logger.info("Effective configuration:")
    logger.info(
        "  [scan] mac=%s hci_device=%s scan_interval=%ds session_gap_seconds=%ds",
        mac or "<all>", hci_device, scan_interval, session_gap,
    )
    logger.info(
        "  [detection] confidence_gap_threshold=%.3f max_plausible_delta_kg_per_day=%.3f "
        "max_intraday_delta_kg=%.3f max_plausibility_penalty=%.3f",
        detector.confidence_gap_threshold, detector.max_plausible_delta_kg_per_day,
        detector.max_intraday_delta_kg, detector.max_plausibility_penalty,
    )
    logger.info(
        "  [detection] process_var_weight=%.4f process_var_impedance=%.4f "
        "meas_var_weight=%.4f meas_var_impedance=%.4f default_start_impedance=%.1f",
        detector.process_var_weight, detector.process_var_impedance,
        detector.meas_var_weight, detector.meas_var_impedance,
        detector.default_start_impedance,
    )
    logger.info("  [detection] state_file=%s", state_file)
    logger.info(
        "  [influxdb] enabled=%s host=%s port=%s database=%s",
        influx_writer.enabled, influx_cfg.get("host", "localhost"),
        influx_cfg.get("port", 8086), influx_cfg.get("database", "miscale"),
    )
    logger.info(
        "  [influxdb] startup_retries=%s startup_retry_delay_seconds=%s "
        "reconnect_interval_seconds=%s",
        influx_cfg.get("startup_retries", 6),
        influx_cfg.get("startup_retry_delay_seconds", 5),
        influx_cfg.get("reconnect_interval_seconds", 60),
    )
    logger.info(
        "  [ntfy] enabled=%s base_url=%s topic=%s reply_topic=%s "
        "pending_timeout_hours=%s send_weight_readings=%s weight_reading_topic_template=%s",
        ntfy_enabled, ntfy_base_url, ntfy_topic or "<unset>", ntfy_reply_topic or "<unset>",
        ntfy_timeout_hours, ntfy_send_weight_readings, ntfy_weight_topic_template,
    )

    if influx_writer.enabled:
        logger.info("InfluxDB logging enabled at %s:%d/%s",
                     influx_cfg.get("host", "localhost"),
                     influx_cfg.get("port", 8086),
                     influx_cfg.get("database", "miscale"))

    # Thread-safe queue for BLE callback → main loop communication
    pending_queue: asyncio.Queue[tuple] = asyncio.Queue()

    def _detection_callback(device, advertisement_data) -> None:
        """Internal callback passed to BleakScanner — pushes into the queue."""
        try:
            pending_queue.put_nowait((device, advertisement_data))
        except asyncio.QueueFull:
            pass  # dropped advertisement if queue is full

    # Use the modern bluez kwarg for adapter selection
    bluez_args = {"adapter": hci_device} if hci_device else None

    async with BleakScanner(
        detection_callback=_detection_callback,
        bluez=bluez_args,  # type: ignore[arg-type]
    ) as scanner:
        logger.info("BLE scanner started, waiting for advertisements…")
        try:
            while True:
                # Drain all pending advertisements collected by the callback
                pending = []
                while True:
                    try:
                        pending.append(pending_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if pending:
                    logger.debug("Received %d advertisement(s)", len(pending))

                for addr, advertisement_data in pending:
                    addr_upper = str(addr.address).upper()

                    # Filter by MAC if configured
                    if mac and addr_upper != mac.upper():
                        continue

                    logger.debug(
                        "Device: %s | RSSI: %d dBm | %s",
                        addr_upper,
                        advertisement_data.rssi,
                        advertisement_data,
                    )

                    # Parse service data from the scale
                    service_data = getattr(advertisement_data, "service_data", {})
                    logger.debug("Service data keys for %s: %s", addr_upper, list(service_data.keys()))

                    # Log raw payload for scale advertisements
                    if service_data:
                        for key, val in service_data.items():
                            logger.debug("Raw payload from %s (%s): %s", addr_upper, key, val.hex())

                    reading = parse_advertisement(service_data)

                    if reading is None:
                        # Log at debug level why the reading was rejected
                        if service_data:
                            # Try to decode flags for debugging
                            for key, val in service_data.items():
                                if len(val) >= 2:
                                    byte0 = val[0]
                                    byte1 = val[1]
                                    has_imp = bool(byte1 & (1 << 1))
                                    is_stab = bool(byte1 & (1 << 5))
                                    load_rem = bool(byte1 & (1 << 7))
                                    unit_name = UNIT_NAMES.get(byte0, f"0x{byte0:02x}")
                                    logger.debug(
                                        "Rejected %s: unit=%s byte1=%s has_imp=%s stable=%s load_rem=%s",
                                        addr_upper, unit_name, f"0x{byte1:02x}", has_imp, is_stab, load_rem,
                                    )
                            logger.debug("No valid scale reading from %s (service data present but invalid)", addr_upper)
                        else:
                            logger.debug("No valid scale reading from %s (no service data)", addr_upper)
                        continue

                    # Session tracking
                    result = tracker.process(addr_upper, reading)
                    if result is None:
                        logger.debug("Duplicate advertisement ignored for %s", addr_upper)
                        continue

                    session_id, phase, reading = result

                    if phase == "intermediate":
                        logger.info(
                            "[%s] %s — weight: %.2f kg, timestamp: %s",
                            session_id,
                            addr_upper,
                            reading["weight_kg"],
                            reading["timestamp"].isoformat(),
                        )
                    else:  # final
                        imp = reading.get("impedance_ohm")
                        logger.info(
                            "[%s] %s — weight: %.2f kg, impedance: %d Ω, timestamp: %s",
                            session_id,
                            addr_upper,
                            reading["weight_kg"],
                            imp,
                            reading["timestamp"].isoformat(),
                        )

                        result = await detector.classify(
                            reading["weight_kg"], imp, reading["timestamp"]
                        )

                        if result.assignment is None:
                            logger.warning(
                                "[%s] Ambiguous reading (weight=%.2f kg, impedance=%d Ω) "
                                "— distances=%s, gap=%.2f (threshold=%.2f), reason=%s. "
                                "Not auto-assigned.",
                                session_id, reading["weight_kg"], imp,
                                {uid: round(d, 2) for uid, d in result.distances.items()},
                                result.gap, result.threshold,
                                result.reason,
                            )
                            if ntfy_enabled:
                                await pending_confirmations.add(
                                    session_id, reading, result.distances, result.confidence
                                )
                                send_ambiguous_notification(
                                    ntfy_base_url, ntfy_topic, ntfy_reply_topic,
                                    session_id, reading, result.distances,
                                    list(detector.states.keys()), logger,
                                )
                            else:
                                influx_writer.write_reading(
                                    session_id, "unassigned", reading, result.confidence,
                                    result.distances,
                                )
                        else:
                            metrics = compute_derived_metrics(
                                user_info_cfg, result.assignment, reading["weight_kg"]
                            )
                            logger.info(
                                "[%s] Detected user: %s (confidence %.2f, "
                                "distances=%s, gap=%.2f (threshold=%.2f))%s",
                                session_id,
                                detector.name_for(result.assignment),
                                result.confidence,
                                {uid: round(d, 2) for uid, d in result.distances.items()},
                                result.gap, result.threshold,
                                f", metrics={metrics}" if metrics else "",
                            )
                            influx_writer.write_reading(
                                session_id, result.assignment, reading, result.confidence,
                                result.distances, metrics,
                            )
                            if ntfy_send_weight_readings:
                                send_weight_reading_notification(
                                    ntfy_base_url, ntfy_weight_topic_template,
                                    result.assignment, detector.name_for(result.assignment),
                                    reading, metrics, False, logger,
                                )

                # Process any ntfy confirmation replies
                while not ntfy_reply_queue.empty():
                    reply = ntfy_reply_queue.get()
                    sid = reply["session_id"]
                    chosen_user = reply["user"]
                    logger.info(
                        "[%s] Processing ntfy reply: user=%s", sid, chosen_user
                    )

                    entry = await pending_confirmations.pop(sid)
                    if entry is None:
                        logger.debug(
                            "Received confirmation for unknown/already-resolved "
                            "session %s — ignoring", sid,
                        )
                        continue

                    resolved_reading = PendingConfirmations.entry_to_reading(entry)

                    if chosen_user == "__skip__":
                        logger.info("[%s] Marked 'Neither' via ntfy — writing as unassigned", sid)
                        influx_writer.write_reading(
                            sid, "unassigned", resolved_reading,
                            entry.get("confidence", 0.0), entry.get("distances", {}),
                        )
                        continue

                    if not await detector.manual_commit(
                        chosen_user, resolved_reading["weight_kg"],
                        resolved_reading.get("impedance_ohm"), resolved_reading["timestamp"],
                    ):
                        logger.warning(
                            "[%s] Confirmation named unrecognized user '%s' — ignoring",
                            sid, chosen_user,
                        )
                        continue

                    metrics = compute_derived_metrics(
                        user_info_cfg, chosen_user, resolved_reading["weight_kg"]
                    )
                    logger.info(
                        "[%s] Manually confirmed as %s via ntfy",
                        sid, detector.name_for(chosen_user),
                    )
                    influx_writer.write_reading(
                        sid, chosen_user, resolved_reading, 1.0,
                        entry.get("distances", {}), {**metrics, "manual_override": True},
                    )
                    if ntfy_send_weight_readings:
                        send_weight_reading_notification(
                            ntfy_base_url, ntfy_weight_topic_template,
                            chosen_user, detector.name_for(chosen_user),
                            resolved_reading, metrics, True, logger,
                        )

                # Sweep pending confirmations that timed out with no reply
                if ntfy_enabled:
                    expired = await pending_confirmations.pop_expired(ntfy_timeout_hours)
                    for sid, entry in expired.items():
                        logger.warning(
                            "[%s] Ambiguous reading timed out waiting for confirmation "
                            "(%.0fh) — writing as unassigned", sid, ntfy_timeout_hours,
                        )
                        influx_writer.write_reading(
                            sid, "unassigned", PendingConfirmations.entry_to_reading(entry),
                            entry.get("confidence", 0.0), entry.get("distances", {}),
                        )

                # Retry the InfluxDB connection if it's not currently up
                # (rate-limited internally — self-heals a slow-starting
                # Docker container or a mid-run outage without a restart).
                influx_writer.ensure_connected()

                await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            logger.info("Scanner cancelled")
        finally:
            ntfy_stop_event.set()
            influx_writer.close()


def main():
    parser = argparse.ArgumentParser(
        description="Mi Scale BLE Monitor — Stage 1",
    )
    parser.add_argument(
        "-c", "--config",
        default=DEFAULT_CONFIG,
        help="Path to TOML config file (default: miscale.toml)",
    )
    parser.add_argument(
        "-i", "--get-info",
        action="store_true",
        help="Read device information from the scale and exit",
    )
    parser.add_argument(
        "-t", "--set-time",
        action="store_true",
        help="Set the scale's internal clock to the current time and exit",
    )
    parser.add_argument(
        "-u", "--set-unit",
        metavar="UNIT",
        choices=["kg", "lbs", "catty", "jin"],
        help="Set the scale display unit and exit (kg, lbs, catty/jin)",
    )
    parser.add_argument(
        "-e", "--erase-history",
        action="store_true",
        help="Erase the scale's internal history and exit (irreversible)",
    )
    parser.add_argument(
        "-d", "--dump-history",
        action="store_true",
        help="Dump the scale's internal history and exit",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Setup logging
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "")
    logger = setup_logging(log_file, log_cfg.get("log_level", "INFO"))

    logger.info("Mi Scale BLE Monitor starting")
    logger.info("Config file: %s", Path(args.config).expanduser().resolve())
    if log_file:
        logger.info("Log file: %s", Path(log_file).expanduser().resolve())

    # One-shot device info read
    if args.get_info:
        scan_cfg = config.get("scan", {})
        mac = scan_cfg.get("scale_mac", "")
        if not mac:
            logger.error("No scale MAC configured in [scan] section")
            sys.exit(1)
        asyncio.run(get_scale_info(mac, logger, config))
        return

    # One-shot time sync
    if args.set_time:
        scan_cfg = config.get("scan", {})
        mac = scan_cfg.get("scale_mac", "")
        if not mac:
            logger.error("No scale MAC configured in [scan] section")
            sys.exit(1)
        asyncio.run(set_scale_time(mac, logger))
        return

    # One-shot display unit setting
    if args.set_unit:
        scan_cfg = config.get("scan", {})
        mac = scan_cfg.get("scale_mac", "")
        if not mac:
            logger.error("No scale MAC configured in [scan] section")
            sys.exit(1)
        asyncio.run(set_scale_unit(mac, args.set_unit, logger))
        return

    # One-shot erase history (with confirmation)
    if args.erase_history:
        scan_cfg = config.get("scan", {})
        mac = scan_cfg.get("scale_mac", "")
        if not mac:
            logger.error("No scale MAC configured in [scan] section")
            sys.exit(1)
        confirm = input(
            "WARNING: This will irreversibly erase all stored history from "
            "the scale. Type 'erase' to confirm: "
        )
        if confirm.strip() != "erase":
            logger.info("Erase history cancelled.")
            return
        asyncio.run(erase_history(mac, logger))
        return

    # One-shot dump history
    if args.dump_history:
        scan_cfg = config.get("scan", {})
        mac = scan_cfg.get("scale_mac", "")
        if not mac:
            logger.error("No scale MAC configured in [scan] section")
            sys.exit(1)
        asyncio.run(dump_history(mac, logger))
        return

    try:
        asyncio.run(run_scanner(config, logger))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
