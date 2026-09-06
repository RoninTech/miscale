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
import sys
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

# Huami Configuration service (00001530) characteristics
CHAR_BATTERY = "00001543-0000-3512-2118-0009af100700"
CHAR_CONFIG = "00001542-0000-3512-2118-0009af100700"

# Byte 0 unit codes (XMTZC05HM, 13-byte payload on 0000181b service)
UNIT_KG = 0x02
UNIT_LBS = 0x03
UNIT_CATTY = 0x04

# Byte 1 status flag bit positions (LSB-first within byte 1)
# Overall bit = byte1_bit + 8 (since byte 0 provides 8 bits)
FLAG_HAS_IMPEDANCE = 1     # bit 1 of byte1 = overall bit 9
FLAG_STABILIZED = 5        # bit 5 of byte1 = overall bit 13
FLAG_LOAD_REMOVED = 7      # bit 7 of byte1 = overall bit 15

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


async def get_scale_info(mac: str, logger: logging.Logger, config: dict) -> None:
    """Connect to the scale and read Device Information Service characteristics."""
    mac_upper = mac.upper()
    logger.info("Connecting to scale %s to read device information...", mac_upper)
    
    # Query InfluxDB for last weight unit if enabled
    influx_cfg = config.get("influxdb", {})
    influx_writer = InfluxDBWriter(influx_cfg, logger)
    last_unit = influx_writer.get_last_unit()
    if last_unit is not None:
        unit_names = {0x02: "kg (SI)", 0x03: "lbs (Imperial)", 0x04: "catty (jin)"}
        unit_str = unit_names.get(last_unit, f"unknown (0x{last_unit:02x})")
        logger.info("Weight unit (from last reading): %s", unit_str)
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

    Returns a dict with weight_kg, impedance_ohm, timestamp, raw_hex
    or None if the advertisement is not from a valid scale reading.
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
    if unit_code == UNIT_KG:
        weight_divisor = 200.0
    elif unit_code in (UNIT_LBS, UNIT_CATTY):
        weight_divisor = 100.0
    else:
        # Unknown unit code — skip
        return None

    # Byte 1: status flags (LSB-first within byte 1)
    byte1 = payload[1]
    has_impedance = bool(byte1 & (1 << FLAG_HAS_IMPEDANCE))   # bit 1 → overall bit 9
    is_stabilized = bool(byte1 & (1 << FLAG_STABILIZED))       # bit 5 → overall bit 13
    load_removed = bool(byte1 & (1 << FLAG_LOAD_REMOVED))      # bit 7 → overall bit 15

    # Valid reading: weight stabilised AND person still on scale AND impedance present
    # Impedance always arrives last in a session — a stabilized reading without it
    # is incomplete and should be rejected (we need impedance for user detection).
    if not is_stabilized or load_removed or not has_impedance:
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
    weight_kg = weight_raw / weight_divisor

    result = {
        "weight_kg": round(weight_kg, 2),
        "timestamp": ts,
        "raw_hex": data.hex(),
        "unit": unit_code,
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
            self._session_counters[mac] = 0
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

    def _save_persisted_state(self) -> None:
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

    def classify(self, weight_kg: float, impedance_ohm: float,
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
            self._commit(best_user, z, timestamp)
            return DetectionResult(best_user, 1.0, scores, "only_user_configured")

        (best_user, best_score), (_, second_score) = ranked[0], ranked[1]
        gap = second_score - best_score

        if gap < self.confidence_gap_threshold:
            return DetectionResult(
                None,
                1.0 - min(gap / self.confidence_gap_threshold, 1.0),
                scores,
                "low_confidence_gap",
            )

        confidence = min(gap / (self.confidence_gap_threshold * 3), 1.0)
        self._commit(best_user, z, timestamp)
        return DetectionResult(best_user, confidence, scores, "clear_winner")

    def _commit(self, user_id: str, z: np.ndarray, timestamp: datetime) -> None:
        """Kalman-update the winning user's filter and persist state."""
        state = self.states[user_id]
        R = np.diag([self.meas_var_weight, self.meas_var_impedance])
        y = z - state.x
        S = state.P + R
        K = state.P @ np.linalg.inv(S)
        state.x = state.x + K @ y
        state.P = (np.eye(2) - K) @ state.P
        state.last_seen = timestamp
        self._save_persisted_state()

    def name_for(self, user_id: Optional[str]) -> str:
        if user_id is None:
            return "unknown"
        return self.states[user_id].name if user_id in self.states else user_id


# ---------------------------------------------------------------------------
# InfluxDB writer
# ---------------------------------------------------------------------------


class InfluxDBWriter:
    """Writes scale readings to InfluxDB 1.x."""

    def __init__(self, config: dict, logger: logging.Logger):
        self._enabled = config.get("enabled", False)
        self._host = config.get("host", "localhost")
        self._port = config.get("port", 8086)
        self._database = config.get("database", "miscale")
        self._retention_policy = config.get("retention_policy", "autogen")
        self._logger = logger
        self._client = None

        if self._enabled:
            try:
                from influxdb import InfluxDBClient  # type: ignore
                self._client = InfluxDBClient(
                    host=self._host,
                    port=self._port,
                    database=self._database,
                )
                self._client.write_points([])
                self._logger.info(
                    "InfluxDB connected at %s:%d, database=%s",
                    self._host, self._port, self._database,
                )
            except Exception as exc:
                self._client = None
                self._logger.warning(
                    "Failed to connect to InfluxDB at %s:%d: %s — "
                    "measurements will not be stored",
                    self._host, self._port, exc,
                )

    def get_last_weight(self, user: str) -> Optional[float]:
        """Get the most recent weight for a user from InfluxDB."""
        if not self._client or not self._enabled:
            return None

        try:
            query = (
                f"SELECT weight_kg FROM scale_reading "
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
                      reading: dict, confidence: float = 1.0):
        """Write a finalized reading to InfluxDB."""
        if not self._client or not self._enabled:
            return

        weight = reading["weight_kg"]
        impedance = reading.get("impedance_ohm")
        ts = reading["timestamp"]
        unit = reading.get("unit")

        influx_ts = ts.astimezone(timezone.utc)

        fields = {
            "weight_kg": weight,
            "impedance_ohm": impedance if impedance else 0,
            "confidence": confidence,
        }
        if unit is not None:
            fields["unit"] = unit

        json_body = [
            {
                "measurement": "scale_reading",
                "tags": {
                    "session": session_id,
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

    def get_last_unit(self) -> Optional[int]:
        """Get the weight unit from the most recent reading."""
        if not self._client or not self._enabled:
            return None

        try:
            query = (
                f"SELECT unit FROM scale_reading "
                f"ORDER BY time DESC LIMIT 1"
            )
            result = self._client.query(query)  # type: ignore[assignment]
            points = list(result.get_points())  # type: ignore[union-attr]
            if points and "unit" in points[0]:
                return int(points[0]["unit"])
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
# Main scanner
# ---------------------------------------------------------------------------


def _detection_callback(device, advertisement_data) -> None:
    """Internal callback passed to BleakScanner."""
    _pending_readings.append((device, advertisement_data))


# Shared list for callback → main loop communication
_pending_readings: list[tuple] = []


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

    logger.info(
        "Starting Mi Scale BLE monitor (adapter=%s, interval=%ds)",
        hci_device,
        scan_interval,
    )
    if mac:
        logger.info("Monitoring scale MAC: %s", mac.upper())
    else:
        logger.info("Monitoring all devices (no MAC filter)")

    if influx_writer.enabled:
        logger.info("InfluxDB logging enabled at %s:%d/%s",
                     influx_cfg.get("host", "localhost"),
                     influx_cfg.get("port", 8086),
                     influx_cfg.get("database", "miscale"))

    # Use the modern bluez kwarg for adapter selection
    bluez_args = {"adapter": hci_device} if hci_device else None

    async with BleakScanner(
        detection_callback=_detection_callback,
        bluez=bluez_args,  # type: ignore[arg-type]
    ) as scanner:
        logger.info("BLE scanner started, waiting for advertisements…")
        try:
            while True:
                # Process any pending advertisements collected by the callback
                pending = _pending_readings[:]
                _pending_readings.clear()

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
                                    unit_name = {0x02: "kg", 0x03: "lbs", 0x04: "catty"}.get(byte0, f"0x{byte0:02x}")
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

                        result = detector.classify(
                            reading["weight_kg"], imp, reading["timestamp"]
                        )

                        if result.assignment is None:
                            logger.warning(
                                "[%s] Ambiguous reading (weight=%.2f kg, impedance=%d Ω) "
                                "— distances=%s, reason=%s. Not auto-assigned.",
                                session_id, reading["weight_kg"], imp,
                                {uid: round(d, 2) for uid, d in result.distances.items()},
                                result.reason,
                            )
                            influx_writer.write_reading(
                                session_id, "unassigned", reading, result.confidence,
                            )
                        else:
                            logger.info(
                                "[%s] Detected user: %s (confidence %.2f, "
                                "distances=%s)",
                                session_id,
                                detector.name_for(result.assignment),
                                result.confidence,
                                {uid: round(d, 2) for uid, d in result.distances.items()},
                            )
                            influx_writer.write_reading(
                                session_id, result.assignment, reading, result.confidence,
                            )

                await asyncio.sleep(scan_interval)
        except asyncio.CancelledError:
            logger.info("Scanner cancelled")
        finally:
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

    try:
        asyncio.run(run_scanner(config, logger))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")

# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
