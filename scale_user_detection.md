# Dual-User Detection for Mi Body Composition Scale 2

This document sketches an approach for classifying each incoming
`(weight, impedance)` BLE reading as belonging to one of two known users,
before writing it to InfluxDB.

It combines:

1. **Session grouping** – collapse a burst of BLE advertisements into one
   stabilized reading.
2. **Per-user Kalman filters** – track each user's true weight & impedance
   as slowly-drifting hidden states, seeded from config-file start values.
3. **Distance-based classification** – assign a reading to whichever
   user's filter best "explains" it, using the filter's own uncertainty
   as the yardstick (Mahalanobis-style scoring).
4. **Plausibility constraints** – reject/deprioritize day-over-day jumps
   that are physiologically implausible.
5. **Confidence gating with active-learning fallback** – when the two
   candidates are too close to call, don't guess silently; ask, or park
   the reading as unassigned.

Language: Python (adjust as needed for your stack).

---

## 1. Data model

```python
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import math


@dataclass
class Reading:
    timestamp: datetime
    weight_kg: float
    impedance_ohm: float


class Assignment(Enum):
    USER_A = "user_a"
    USER_B = "user_b"
    AMBIGUOUS = "ambiguous"


@dataclass
class ClassificationResult:
    assignment: Assignment
    confidence: float          # 0..1, higher = more certain
    dist_a: float               # Mahalanobis-style distance to user A
    dist_b: float               # Mahalanobis-style distance to user B
    reading: Reading
    reason: str = ""            # e.g. "implausible_jump", "clear_winner"
```

---

## 2. Session grouping (debounce BLE bursts)

The scale sends multiple advertisements while the weight settles. Group
readings that arrive close together in time and only classify once
values stop changing (or after a max wait).

```python
class SessionGrouper:
    """
    Feed raw advertisement readings in; get back a single stabilized
    Reading once a measurement session completes.
    """

    def __init__(self, gap_seconds: float = 3.0,
                 stability_window: int = 3,
                 stability_tolerance_kg: float = 0.05,
                 max_session_seconds: float = 20.0):
        self.gap_seconds = gap_seconds
        self.stability_window = stability_window
        self.stability_tolerance_kg = stability_tolerance_kg
        self.max_session_seconds = max_session_seconds
        self._buffer: list[Reading] = []

    def add(self, reading: Reading) -> Reading | None:
        """Returns a finalized Reading when a session is deemed complete,
        otherwise None."""
        if self._buffer and (
            (reading.timestamp - self._buffer[-1].timestamp).total_seconds()
            > self.gap_seconds
        ):
            # Gap too large -> previous session (if any) should already
            # have been flushed; start fresh.
            self._buffer.clear()

        self._buffer.append(reading)

        session_started = self._buffer[0].timestamp
        elapsed = (reading.timestamp - session_started).total_seconds()

        if len(self._buffer) >= self.stability_window:
            recent = self._buffer[-self.stability_window:]
            weights = [r.weight_kg for r in recent]
            if max(weights) - min(weights) <= self.stability_tolerance_kg:
                final = recent[-1]
                self._buffer.clear()
                return final

        if elapsed >= self.max_session_seconds:
            # Didn't stabilize cleanly; fall back to last reading.
            final = self._buffer[-1]
            self._buffer.clear()
            return final

        return None
```

Call `SessionGrouper.add()` for every parsed advertisement; only pass the
result (when not `None`) into the classifier below.

---

## 3. Per-user Kalman filter (weight + impedance)

A simple 2D constant-position Kalman filter (state = "true" weight and
impedance, both assumed to drift slowly between measurements). This
gives you both a smoothed estimate *and* a principled uncertainty
(covariance) to score new readings against.

```python
import numpy as np


class UserKalmanFilter:
    """
    State vector x = [weight_kg, impedance_ohm]
    Simple constant-position model with process noise Q allowing slow drift,
    and measurement noise R reflecting scale reading noise.
    """

    def __init__(self, init_weight: float, init_impedance: float,
                 process_var_weight: float = 0.02,   # kg^2 per reading
                 process_var_impedance: float = 4.0,  # ohm^2 per reading
                 meas_var_weight: float = 0.09,       # kg^2 (~0.3kg stddev)
                 meas_var_impedance: float = 25.0):   # ohm^2 (~5ohm stddev)
        self.x = np.array([init_weight, init_impedance], dtype=float)
        self.P = np.diag([1.0, 100.0])  # initial uncertainty, fairly loose
        self.Q = np.diag([process_var_weight, process_var_impedance])
        self.R = np.diag([meas_var_weight, meas_var_impedance])

    def predict(self):
        # State transition is identity (no external dynamics model),
        # just grow uncertainty to allow drift.
        self.P = self.P + self.Q

    def innovation(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return (innovation y, innovation covariance S) without
        committing an update — used for scoring candidate readings."""
        y = z - self.x
        S = self.P + self.R
        return y, S

    def update(self, z: np.ndarray):
        y, S = self.innovation(z)
        K = self.P @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(2) - K) @ self.P

    def mahalanobis(self, z: np.ndarray) -> float:
        y, S = self.innovation(z)
        return float(np.sqrt(y.T @ np.linalg.inv(S) @ y))
```

`mahalanobis()` is the key output: it expresses "how many standard
deviations away is this reading from what I currently believe this
user's weight/impedance to be" — already normalized, so weight (small
numbers) and impedance (large numbers) are on a comparable scale
without manual weighting.

---

## 4. Bootstrap from config

Seed each `UserKalmanFilter` from the config file's start weight (and a
reasonable default impedance, or a config value if you have one) the
first time the program runs — not just as a one-off cold-start check,
but as the actual initial filter state. As real readings arrive, `update()`
naturally moves the estimate away from the config default.

```python
def bootstrap_filters(config: dict) -> dict[str, UserKalmanFilter]:
    """
    config example:
    {
      "user_a": {"start_weight_kg": 82.0, "start_impedance_ohm": 520.0},
      "user_b": {"start_weight_kg": 78.0, "start_impedance_ohm": 480.0},
    }
    """
    filters = {}
    for user_id, cfg in config.items():
        filters[user_id] = UserKalmanFilter(
            init_weight=cfg["start_weight_kg"],
            init_impedance=cfg.get("start_impedance_ohm", 500.0),
        )
    return filters
```

If you persist filter state (x, P) to InfluxDB or a small sidecar file
after every update, restarts won't need to re-bootstrap from config at
all — only a genuinely empty DB should trigger this path, matching your
original spec.

---

## 5. Plausibility constraint

Reject implausible jumps by inflating the effective distance for a
candidate if the implied change since that user's last reading exceeds
a physiological limit.

```python
MAX_PLAUSIBLE_DELTA_KG_PER_DAY = 2.0  # tune to taste


def plausibility_penalty(candidate_filter: UserKalmanFilter,
                          last_reading_time: datetime | None,
                          new_weight: float,
                          now: datetime) -> float:
    """Returns a multiplicative penalty (>=1.0) applied to the distance
    score. 1.0 = no penalty."""
    if last_reading_time is None:
        return 1.0

    days_elapsed = max((now - last_reading_time).total_seconds() / 86400.0, 1e-6)
    max_allowed = MAX_PLAUSIBLE_DELTA_KG_PER_DAY * days_elapsed
    implied_delta = abs(new_weight - candidate_filter.x[0])

    if implied_delta <= max_allowed:
        return 1.0

    # Scale penalty with how far past the plausible limit we are.
    overshoot_ratio = implied_delta / max_allowed
    return 1.0 + (overshoot_ratio - 1.0) * 2.0  # tune steepness
```

---

## 6. Classifier with confidence gating

```python
CONFIDENCE_GAP_THRESHOLD = 1.0   # min separation (in Mahalanobis units)
                                  # between best and second-best candidate
                                  # to accept automatically


def classify_reading(reading: Reading,
                      filters: dict[str, UserKalmanFilter],
                      last_seen: dict[str, datetime | None]) -> ClassificationResult:
    z = np.array([reading.weight_kg, reading.impedance_ohm])

    scores = {}
    for user_id, kf in filters.items():
        kf.predict()  # allow drift since last update before scoring
        base_dist = kf.mahalanobis(z)
        penalty = plausibility_penalty(
            kf, last_seen.get(user_id), reading.weight_kg, reading.timestamp
        )
        scores[user_id] = base_dist * penalty

    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    (best_user, best_score), (second_user, second_score) = ranked[0], ranked[1]

    gap = second_score - best_score
    dist_a = scores.get("user_a")
    dist_b = scores.get("user_b")

    if gap < CONFIDENCE_GAP_THRESHOLD:
        return ClassificationResult(
            assignment=Assignment.AMBIGUOUS,
            confidence=1.0 - min(gap / CONFIDENCE_GAP_THRESHOLD, 1.0),
            dist_a=dist_a, dist_b=dist_b,
            reading=reading,
            reason="low_confidence_gap",
        )

    confidence = min(gap / (CONFIDENCE_GAP_THRESHOLD * 3), 1.0)
    return ClassificationResult(
        assignment=Assignment(best_user),
        confidence=confidence,
        dist_a=dist_a, dist_b=dist_b,
        reading=reading,
        reason="clear_winner",
    )
```

Note `predict()` is called on *every* filter at classification time
(not just the winner) so uncertainty grows appropriately for whichever
user hasn't been measured in a while — this is what lets the plausibility
window widen gradually rather than staying pinned to a fixed constant.

---

## 7. Putting it together: end-to-end flow

```python
class ScaleUserDetector:
    def __init__(self, config: dict):
        self.filters = bootstrap_filters(config)
        self.last_seen: dict[str, datetime | None] = {
            user_id: None for user_id in config
        }
        self.grouper = SessionGrouper()

    def handle_raw_advertisement(self, reading: Reading) -> ClassificationResult | None:
        finalized = self.grouper.add(reading)
        if finalized is None:
            return None  # session still stabilizing

        result = self.classify_reading_and_maybe_update(finalized)
        return result

    def classify_reading_and_maybe_update(self, reading: Reading) -> ClassificationResult:
        result = classify_reading(reading, self.filters, self.last_seen)

        if result.assignment != Assignment.AMBIGUOUS:
            user_id = result.assignment.value
            z = np.array([reading.weight_kg, reading.impedance_ohm])
            self.filters[user_id].update(z)
            self.last_seen[user_id] = reading.timestamp

        return result
```

Wire this into your pipeline as:

```python
detector = ScaleUserDetector(config)

def on_ble_advertisement(raw):
    reading = parse_reading(raw)          # your existing parser
    result = detector.handle_raw_advertisement(reading)
    if result is None:
        return  # still stabilizing, ignore

    if result.assignment == Assignment.AMBIGUOUS:
        handle_ambiguous(result)          # see §8
    else:
        write_to_influx(result.reading, user=result.assignment.value,
                         confidence=result.confidence)
```

---

## 8. Active-learning fallback for ambiguous readings

When `Assignment.AMBIGUOUS` comes back, don't silently guess. Two
reasonable options, easy to support both:

```python
def handle_ambiguous(result: ClassificationResult):
    # Option 1: push a notification (phone app, MQTT topic, etc.)
    # and let the user tap "Alice" or "Bob". When the answer comes back,
    # call detector.classify_reading_and_maybe_update analog but force
    # the assignment:
    #
    #   filters[confirmed_user].update(z)
    #   last_seen[confirmed_user] = reading.timestamp
    #   write_to_influx(reading, user=confirmed_user, confidence=1.0)
    #
    # Option 2: store as unassigned for later manual reconciliation.
    write_to_influx(result.reading, user="unassigned",
                     confidence=result.confidence,
                     meta={"dist_a": result.dist_a, "dist_b": result.dist_b})
    notify_user_for_confirmation(result)
```

Either path is valuable early on, since your two filters start out
seeded only from config defaults and haven't yet "learned" each user's
real impedance signature — expect more ambiguous cases in the first
few days, tapering off as the filters converge.

---

## 9. Persisting filter state across restarts

Since InfluxDB is your store of record, you can either:

- **Recompute on startup** by replaying recent points through fresh
  filters (simple, self-healing, but costs a bit of startup time), or
- **Persist `(x, P)` per user** as a small JSON sidecar or as tagged
  points in InfluxDB itself, loaded directly into `UserKalmanFilter.x`
  / `.P` on startup, only falling back to `bootstrap_filters(config)`
  if that state doesn't exist yet.

The second option better matches your original "no readings in DB →
use config" rule as a true cold-start-only path.

---

## 10. Tuning notes

- `process_var_weight` / `process_var_impedance` control how quickly the
  filter "forgets" old readings in favor of new ones — raise these if
  users' weights/impedance drift quickly (e.g. active dieting).
- `meas_var_*` should reflect the scale's actual noise; log some
  same-user, same-morning repeated readings to estimate this empirically.
- `CONFIDENCE_GAP_THRESHOLD` and `MAX_PLAUSIBLE_DELTA_KG_PER_DAY` are the
  two knobs most worth revisiting once you have a few weeks of real data
  — start conservative (more ambiguous flags) and loosen once you trust
  the filters.
- As your two users' weights converge, the impedance term does most of
  the discriminating work — worth logging `dist_a`/`dist_b` breakdowns
  by weight-component vs impedance-component individually if you ever
  need to debug misclassifications.
