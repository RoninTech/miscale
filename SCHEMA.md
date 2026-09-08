# InfluxDB Schema — Mi Scale BLE Monitor

**Database:** `miscale`
**Retention policy:** `autogen` (default, infinite duration — matches `miscale.toml`)
**Measurement:** `weight`

## Fields and tags

| Name | Type | Description |
|---|---|---|
| `user` | tag | `"user1"`, `"user2"`, or `"unassigned"` for ambiguous readings. Low-cardinality, used for filtering/grouping. **Reserved InfluxQL keyword** — must be double-quoted (`"user"`) in hand-written InfluxQL (shell, curl, ad-hoc queries). Not an issue in the Python `influxdb` client, which handles quoting internally. |
| `weight_kg` | field, float | Always true kilograms, converted regardless of the scale's physical unit setting (lbs/catty are converted at parse time). |
| `impedance_ohm` | field, integer | Raw impedance reading from the advertisement. |
| `unit_name` | field, string | `"kg"`, `"lbs"`, or `"catty"` — the scale's unit setting at the time of that reading. Queried via `last("unit_name")` to answer `--get-info`'s "what unit is the scale in" question. No live-BLE-scan fallback — if InfluxDB is disabled, unreachable, or has no data yet, `--get-info` simply omits the unit line (accepted tradeoff, documented separately). |
| `confidence` | field, float | 0–1 classifier confidence for the `user` assignment. Mathematically derivable from the two `dist_*` fields (it's the normalized gap between them) but stored directly for query convenience. |
| `session_id` | field, string | Links back to the BLE session-tracking log line for that reading (e.g. `0C9541886000E:003`), for debugging a specific stored value against the application log. Format is `<MAC>:<counter>` where the counter is randomly seeded (0–999) on each process start to reduce collisions across restarts. |
| `dist_user1` | field, float | Penalized Mahalanobis distance from this reading to user1's Kalman filter state at classification time — the raw score the classifier used to decide the assignment. |
| `dist_user2` | field, float | Same, for user2. |

## Derived body metrics (optional fields)

Written only on successfully-assigned readings (never on `user="unassigned"`), and only when the corresponding `[user_info.*]` config fields are present for that user. Absent fields are simply not written to the point — InfluxDB has no NULL concept, so aggregate queries (`mean()`, etc.) over these fields silently skip points that lack them.

| Name | Type | Requires (config) | Source |
|---|---|---|---|
| `bmi` | field, float | `height` | Exact formula: `weight_kg / height_m²` |
| `bmr` | field, float | `height`, `age`, `gender` | Mifflin-St Jeor equation (1990) — standard, exact, not reverse-engineered |
| `fat_percent_est` | field, float | `height`, `age`, `gender` | Deurenberg et al. (1991) — anthropometric estimate from BMI + age + sex |
| `water_percent_est` | field, float | `height`, `gender` | Hume & Weyers (1971) — anthropometric estimate from height + weight + sex, expressed as % of body weight |
| `lean_mass_kg_est` | field, float | `height`, `gender` | Boer (1984) — anthropometric estimate from height + weight + sex |

**Important:** `fat_percent_est`, `water_percent_est`, and `lean_mass_kg_est` do **not** use impedance at all, despite the scale providing it — Xiaomi's actual impedance-based BIA coefficients are proprietary and unverified in any public source. The `_est` suffix on these three field names is a deliberate, permanent reminder that they're anthropometric estimates, not true BIA readings. `bmi` and `bmr` carry no such caveat — both are exact, standard formulas.

## Notes

- **`dist_<user_id>` field names are dynamic** — they're generated from the `[user_info]` table's keys in `miscale.toml`. Renaming a user's config key, or adding a third user, produces a new field going forward rather than renaming/removing the old one. Anything querying this schema by field name should account for that.
- **The five derived-metric fields can appear inconsistently** even across a single user's own readings, if their config is incomplete — e.g. a user with `height` but no `gender` gets `bmi` on every reading but never the other four. This isn't a bug; it reflects what's actually computable from their config at write time.
- **Dropped from the schema:** `unit_code` (the raw byte, `2`/`3`/`4`) was considered but dropped as redundant once `unit_name` is stored — since `weight_kg` is already unit-normalized, the raw code added no information that `unit_name` didn't already capture.
- **Field types are locked on first write.** InfluxDB infers and locks a field's type (float/integer/string) from its first write to a measurement. If seeding the schema manually via InfluxQL before the app's first real write, use explicit type markers: `0i` for integers, quoted strings for `unit_name`/`session_id`, plain decimals for floats.

## Example write (line protocol)

```
weight,user=user1 weight_kg=66.8,impedance_ohm=480i,unit_name="kg",confidence=1.0,session_id="0C9541886000E:003",dist_user1=1.76,dist_user2=5.02,bmi=22.35,bmr=1465.2,fat_percent_est=24.0,water_percent_est=59.1,lean_mass_kg_est=54.2
```
