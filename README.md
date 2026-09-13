# Environmental Noise Evidence Review API

FastAPI/Pydantic service for reviewing short, transient factory-night noise events without overwriting the original monitoring record. It stores the complete source request in SQLite, recomputes day/night and equipment-run segments, preserves reviewer actions as append-only revisions, seals confirmations, and exports JSON/SVG evidence.

## Audit model

- **Raw record is immutable.** Every original sample remains in `source_request` and the raw sample-chain hash never changes.
- **Revisions create a new version.** Excluding a vehicle or moving an equipment boundary never updates version 1.
- **Review actions require a reason.** Both `exclude` and `move_boundary` actions reject an empty reason through the Pydantic contract.
- **Blocked evidence does not report usable levels.** Calibration failure, insufficient background margin, sampling gaps, time reversal, invalid weather/meteorological support, or point-condition failure returns the affected period and the missing basis rather than a valid Leq.
- **Confirmation seals evidence.** The seal includes the engine version, rule hash, calibration version/hash, raw and effective sample indexes, findings, segments, and review actions.

## Computation

Each operating equipment interval is:

1. Chronologically indexed as an equipment run (`run_index`).
2. Split at the site-local day/night boundaries (`day_start_hour`, `night_start_hour`, IANA `site.time_zone`).
3. Clipped by justified reviewer exclusions and boundary moves.
4. Checked for sample gaps, duration, weather support, point conditions, and calibration bracket validity.
5. Evaluated against the nearest qualifying background measurement for the same phase and equipment.

Acoustic metrics are computed from retained samples:

- **Leq:** energy/time-weighted equivalent level.
- **Lmax:** maximum retained level.
- **L90:** duration-weighted level exceeded for 90% of the retained interval. It is a low/background-side statistic (the 10% cumulative rank), not the 90th percentile.
- **Background level:** background `Leq` is used for regulatory correction; `L90` remains available in source evidence.
- **Corrected Leq:** `Leq + table correction`. Default correction table is `{>=3 dB: -3 dB, >=5 dB: -2 dB, >=10 dB: -1 dB}`. A difference below the request's `min_background_difference_db` blocks the segment and applies no correction.

Sample indexes use `sha256-position-time-level-chain-v1`. The raw root includes all submitted samples in submitted order; the effective root contains only samples included in that version's retained segments.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The SQLite file defaults to `app/noise_reviews.db`; override it with:

```bash
NOISE_REVIEW_DB=/data/noise.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive contract: `http://localhost:8000/docs`.

## Endpoints

| Method/Path | Purpose |
|---|---|
| `POST /reviews` | Submit original samples, site/calibration/weather/equipment/background/rule data and calculate version 1. |
| `GET /reviews` | List reviews. |
| `GET /reviews/{id}` | List versions and confirmation state. |
| `GET /reviews/{id}/versions/{v}` | Recompute an old version from stored source data and its action set. |
| `POST /reviews/{id}/revisions?parent_version=n` | Append exclusions or boundary moves with reviewer and reason. New revisions branch from the latest version. |
| `POST /reviews/{id}/versions/{v}/confirm` | Confirm/seal a version. Already-sealed versions cannot be changed; create a revision instead. |
| `GET /reviews/{id}/versions/{v}/evidence` | Complete JSON evidence package, including the immutable source request. |
| `GET /reviews/{id}/versions/{v}/curve.svg` | Raw/retained level curve with equipment lane and red dashed exclusion bands. |
| `GET /reviews/{id}/versions/{a}/compare/{b}` | Compare actions, segments, findings, and status across revisions. |
| `GET /reviews/{id}/versions/{v}/background-interference` | Background match/margin/correction data, including insufficient-margin cases. |
| `GET /reviews/{id}/versions/{v}/calibration-failures` | Missing, reversed, aged, out-of-bracket, or excessive-drift calibration evidence. |

## Review action shapes

Exclude a non-target event:

```json
{
  "type": "exclude",
  "start": "2026-09-12T22:00:40Z",
  "end": "2026-09-12T22:01:00Z",
  "reason": "Heavy goods vehicle pass-by logged by site observer.",
  "label": "vehicle"
}
```

Move a segment boundary (the target must remain inside the original equipment run and cannot overlap another run):

```json
{
  "type": "move_boundary",
  "equipment_id": "COMP-1",
  "run_index": 0,
  "side": "start",
  "to": "2026-09-12T22:00:10Z",
  "reason": "SCADA load state shows compressor achieved stable operation at 22:00:10Z."
}
```

## Finding codes

- `SAMPLE_TIME_ORDER`, `SAMPLING_GAP`, `SEGMENT_DURATION`
- `CALIBRATION_MISSING`, `CALIBRATION_AMBIGUOUS`, `CALIBRATION_TIME_ORDER`, `CALIBRATION_DRIFT`, `CALIBRATION_BRACKET`, `CALIBRATION_AGE`
- `WEATHER_TIME_ORDER`, `WEATHER_RULE_EXCEEDED`
- `SITE_TIMEZONE_INVALID`, `SITE_CONDITION_MISMATCH`, `RULE_DAY_NIGHT_INVALID`
- `EQUIPMENT_PERIOD_INVALID`, `EQUIPMENT_PERIOD_OVERLAP`, `EQUIPMENT_PERIOD_MISSING`
- `BACKGROUND_MEASUREMENT_INVALID`, `BACKGROUND_DIFFERENCE_INSUFFICIENT`
- `REVIEW_ACTION_INVALID`; `REVIEW_ACTION_OUTSIDE_SEGMENT` is a non-blocking audit warning.

## Tests

```bash
PYTHONPATH=. pytest -q
```

The test suite covers valid night calculations, exclusions and boundary moves, old-version recomputation, revision comparison, sealing/re-seal rejection, SVG annotations, phase splitting, calibration failure, background insufficiency, sample gaps/time reversal, and weather/site blockers.
