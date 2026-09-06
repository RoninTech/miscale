# InfluxDB Schema — Mi Scale BLE Monitor

**Database:** `miscale`
**Retention policy:** `autogen` (default, infinite duration — matches `miscale.toml`)
**Measurement:** `weight`

## Fields and tags

| Name | Type | Description |
|---|---|---|
| `user` | tag | `"paul"`, `"helen"`, or `"unassigned"` for ambiguous readings. Low-cardinality, used for filtering/grouping. **Reserved InfluxQL keyword** — must be double-quoted (`"user"`) in hand-written InfluxQL (shell, curl, ad-hoc queries). Not an issue in the Python `influxdb` client, which handles quoting internally. |
| `weight_kg` | field, float | Always true kilograms, converted regardless of the scale's physical unit setting (lbs/catty are converted at parse time). |
| `impedance_ohm` | field, integer | Raw impedance reading from the advertisement. |
| `unit_name` | field, string | `"kg"`, `"lbs"`, or `"catty"` — the scale's unit setting at the time of that reading. Queried via `last("unit_name")` to answer `--get-info`'s "what unit is the scale in" question, falling back to a live BLE scan if InfluxDB is disabled/unreachable/empty. |
| `confidence` | field, float | 0–1 classifier confidence for the `user` assignment. Mathematically derivable from the two `dist_*` fields (it's the normalized gap between them) but stored directly for query convenience. |
| `session_id` | field, string | Links back to the BLE session-tracking log line for that reading (e.g. `0C9541886000E:003`), for debugging a specific stored value against the application log. |
| `dist_paul` | field, float | Penalized Mahalanobis distance from this reading to Paul's Kalman filter state at classification time — the raw score the classifier used to decide the assignment. |
| `dist_helen` | field, float | Same, for Helen. |

## Notes

- **`dist_<user_id>` field names are dynamic** — they're generated from the `[user_info]` table's keys in `miscale.toml`. Renaming a user's config key, or adding a third user, produces a new field going forward rather than renaming/removing the old one. Anything querying this schema by field name should account for that.
- **Dropped from the schema:** `unit_code` (the raw byte, `2`/`3`/`4`) was considered but dropped as redundant once `unit_name` is stored — since `weight_kg` is already unit-normalized, the raw code added no information that `unit_name` didn't already capture.
- **Field types are locked on first write.** InfluxDB infers and locks a field's type (float/integer/string) from its first write to a measurement. If seeding the schema manually via InfluxQL before the app's first real write, use explicit type markers: `0i` for integers, quoted strings for `unit_name`/`session_id`, plain decimals for floats.

## Example write (line protocol)

```
weight,user=paul weight_kg=66.8,impedance_ohm=480i,unit_name="kg",confidence=1.0,session_id="0C9541886000E:003",dist_paul=1.76,dist_helen=5.02
```
