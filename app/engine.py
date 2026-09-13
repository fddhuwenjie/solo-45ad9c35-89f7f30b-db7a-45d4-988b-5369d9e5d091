"""Deterministic noise evidence calculations.

The engine is intentionally pure: it accepts the original request plus an
append-only list of review actions and returns structured evidence.  Original
samples are never reordered or overwritten.
"""
from __future__ import annotations

import hashlib
import math
from bisect import bisect_left
from datetime import datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    BackgroundInterference,
    BackgroundMeasurement,
    CalibrationFailure,
    CalibrationReading,
    ComputeResult,
    EquipmentPeriod,
    ExcludeAction,
    Finding,
    MetricSet,
    MoveBoundaryAction,
    ReviewRequest,
    Sample,
    SampleIndex,
    SegmentResult,
)

ENGINE_VERSION = "1.0.0"
UTC = timezone.utc


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rules_hash(rules: Any) -> str:
    data = rules.model_dump(mode="json")
    return sha256_text(canonical_json({"engine": ENGINE_VERSION, "rules": data}))[:16]


def sample_chain_hash(samples: Iterable[tuple[int, datetime, float]]) -> str:
    digest = hashlib.sha256()
    for position, timestamp, level in samples:
        digest.update(f"{position}|{iso(timestamp)}|{level:.6f}\n".encode("utf-8"))
    return digest.hexdigest()


def calibration_hash(readings: list[CalibrationReading]) -> str:
    payload = [
        {
            "timestamp": iso(r.timestamp),
            "phase": r.phase,
            "expected_db": r.expected_db,
            "measured_db": r.measured_db,
        }
        for r in readings
    ]
    return sha256_text(canonical_json(payload))[:16]


def round_db(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 2)


def energy_mean_db(levels: list[float], weights: Optional[list[float]] = None) -> float:
    if not levels:
        raise ValueError("cannot calculate Leq without levels")
    if weights is None:
        weights = [1.0] * len(levels)
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("total acoustic weight must be positive")
    energy = sum(w * 10.0 ** (level / 10.0) for level, w in zip(levels, weights))
    return 10.0 * math.log10(energy / total_weight)


def weighted_percentile_db(levels: list[float], weights: list[float], percentile: float) -> float:
    """Nearest-rank percentile with cumulative weights (L90 uses p=90)."""
    order = sorted(range(len(levels)), key=lambda i: levels[i])
    total = sum(weights)
    target = total * percentile / 100.0
    cumulative = 0.0
    for index in order:
        cumulative += weights[index]
        if cumulative >= target - 1e-12:
            return levels[index]
    return levels[order[-1]]


def get_zone(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_dt(value: datetime, zone: ZoneInfo) -> datetime:
    return value.astimezone(zone)


def phase_for(value: datetime, day_start: int, night_start: int) -> str:
    hour = value.hour
    if day_start <= night_start:
        return "day" if day_start <= hour < night_start else "night"
    return "night" if night_start <= hour < day_start else "day"


def phase_ranges(
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    day_start: int,
    night_start: int,
) -> list[tuple[datetime, datetime, str]]:
    """Split a UTC interval at day/night boundaries in site local time."""
    if end <= start:
        return []
    ls, le = local_dt(start, zone), local_dt(end, zone)
    current = ls
    ranges: list[tuple[datetime, datetime, str]] = []
    # A defensive upper bound avoids an infinite loop on odd rule input.
    for _ in range(64):
        if current >= le:
            break
        phase = phase_for(current, day_start, night_start)
        transition_hour = night_start if phase == "day" else day_start
        transition_local = datetime.combine(current.date(), time(transition_hour), tzinfo=zone)
        if transition_local <= current:
            transition_local += timedelta(days=1)
        next_dt = min(transition_local, le)
        ranges.append((current.astimezone(UTC), next_dt.astimezone(UTC), phase))
        current = next_dt
    return ranges


def period_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)).total_seconds())


def contains_period(
    outer_start: datetime,
    outer_end: datetime,
    inner_start: datetime,
    inner_end: datetime,
) -> bool:
    return outer_start <= inner_start and inner_end <= outer_end


def subtract_ranges(
    start: datetime,
    end: datetime,
    exclusions: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    remaining: list[tuple[datetime, datetime]] = [(start, end)]
    for ex_start, ex_end in exclusions:
        next_ranges: list[tuple[datetime, datetime]] = []
        for r_start, r_end in remaining:
            cut_start, cut_end = max(r_start, ex_start), min(r_end, ex_end)
            if cut_end <= cut_start:
                next_ranges.append((r_start, r_end))
            else:
                if cut_start > r_start:
                    next_ranges.append((r_start, cut_start))
                if r_end > cut_end:
                    next_ranges.append((cut_end, r_end))
        remaining = next_ranges
    return remaining


def total_seconds(ranges: list[tuple[datetime, datetime]]) -> float:
    return sum((end - start).total_seconds() for start, end in ranges)


def make_finding(
    code: str,
    severity: str,
    message: str,
    *,
    segment_id: Optional[str] = None,
    period: Optional[tuple[datetime, datetime]] = None,
    missing_basis: Optional[str] = None,
    evidence: Optional[dict[str, Any]] = None,
) -> Finding:
    return Finding(
        code=code,
        severity=severity,  # type: ignore[arg-type]
        segment_id=segment_id,
        period=period,
        message=message,
        missing_basis=missing_basis,
        evidence=evidence or {},
    )


def normalize_equipment(
    periods: list[EquipmentPeriod],
    actions: list[ExcludeAction | MoveBoundaryAction],
) -> tuple[list[tuple[EquipmentPeriod, int]], list[Finding], dict[int, tuple[datetime, datetime]]]:
    indexed = sorted(enumerate(periods), key=lambda item: (item[1].start, item[1].end))
    # run_index refers to chronological order of the submitted equipment runs.
    order = {original_index: run_index for run_index, (original_index, _period) in enumerate(indexed)}
    adjusted_by_original: dict[int, tuple[datetime, datetime]] = {
        original_index: (period.start, period.end) for original_index, period in indexed
    }
    findings: list[Finding] = []

    for original_index, period in indexed:
        if period.end <= period.start:
            findings.append(
                make_finding(
                    "EQUIPMENT_PERIOD_INVALID",
                    "blocker",
                    f"Equipment period {period.equipment_id} has a non-positive interval.",
                    period=(period.start, period.end),
                    missing_basis="Valid equipment start and end timestamps are required.",
                )
            )
        # A submitted state can explicitly record shutdown; these periods are
        # not target operating evidence and do not become computed segments.

    for action in actions:
        if isinstance(action, MoveBoundaryAction):
            matching = [
                (oi, p)
                for oi, p in enumerate(periods)
                if order[oi] == action.run_index
                and p.equipment_id == action.equipment_id
                and p.operating
                and p.end > p.start
            ]
            if not matching:
                findings.append(
                    make_finding(
                        "REVIEW_ACTION_INVALID",
                        "blocker",
                        f"run_index {action.run_index} does not exist.",
                    )
                )
                continue
            original_index, period = matching[0]
            current_start, current_end = adjusted_by_original[original_index]
            if action.side == "start":
                new_start, new_end = action.to, current_end
            else:
                new_start, new_end = current_start, action.to
            if not (period.start <= new_start < new_end <= period.end):
                findings.append(
                    make_finding(
                        "REVIEW_ACTION_INVALID",
                        "blocker",
                        "Boundary move must remain inside the original equipment run and leave positive duration.",
                        period=(new_start, new_end),
                        evidence={"run_index": action.run_index, "side": action.side},
                    )
                )
                continue
            overlap = False
            for other_index, other_range in adjusted_by_original.items():
                if other_index == original_index:
                    continue
                if period_overlap(new_start, new_end, other_range[0], other_range[1]) > 0:
                    overlap = True
            if overlap:
                findings.append(
                    make_finding(
                        "REVIEW_ACTION_INVALID",
                        "blocker",
                        "Boundary move creates overlapping equipment segments.",
                        period=(new_start, new_end),
                    )
                )
                continue
            adjusted_by_original[original_index] = (new_start, new_end)

    adjusted: list[tuple[EquipmentPeriod, int]] = []
    for original_index, period in indexed:
        start, end = adjusted_by_original[original_index]
        if end > start and period.operating:
            adjusted.append(
                (
                    EquipmentPeriod(
                        equipment_id=period.equipment_id,
                        start=start,
                        end=end,
                        operating=True,
                        name=period.name,
                    ),
                    order[original_index],
                )
            )

    # Detect overlap after all adjustments; this also catches invalid source data.
    for i, (p_i, _) in enumerate(adjusted):
        for p_j, _ in adjusted[i + 1 :]:
            if period_overlap(p_i.start, p_i.end, p_j.start, p_j.end) > 0:
                findings.append(
                    make_finding(
                        "EQUIPMENT_PERIOD_OVERLAP",
                        "blocker",
                        "Equipment operating periods overlap and cannot be uniquely attributed.",
                        period=(max(p_i.start, p_j.start), min(p_i.end, p_j.end)),
                    )
                )
    if not adjusted:
        findings.append(
            make_finding(
                "EQUIPMENT_PERIOD_MISSING",
                "blocker",
                "No valid operating equipment interval is available to reorganize into evidence segments.",
                missing_basis="At least one operating equipment run with start before end is required.",
            )
        )
    return adjusted, findings, adjusted_by_original


def validate_raw_sequence(samples: list[Sample]) -> tuple[list[tuple[datetime, datetime]], bool]:
    bad_ranges: list[tuple[datetime, datetime]] = []
    bad = False
    for previous, current in zip(samples, samples[1:]):
        if current.timestamp <= previous.timestamp:
            bad = True
            bad_ranges.append((previous.timestamp, current.timestamp))
    return bad_ranges, bad


def merge_ranges(ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def nominal_interval(samples: list[Sample]) -> Optional[float]:
    ordered = sorted(samples, key=lambda item: item.timestamp)
    deltas = [
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(ordered, ordered[1:])
        if b.timestamp > a.timestamp
    ]
    positive = [d for d in deltas if d > 0]
    return median(positive) if positive else None


def calibration_analysis(
    readings: list[CalibrationReading],
    sample_start: datetime,
    sample_end: datetime,
    threshold: float,
    max_age: Optional[float],
) -> tuple[list[Finding], list[CalibrationFailure], dict[str, Any], bool]:
    findings: list[Finding] = []
    failures: list[CalibrationFailure] = []
    phases: dict[str, CalibrationReading] = {}
    duplicate = False
    invalid_order = False
    for reading in readings:
        if reading.phase in phases:
            duplicate = True
        phases[reading.phase] = reading

    def add_failure(failure: CalibrationFailure, code: str, missing: Optional[str] = None) -> None:
        failures.append(failure)
        findings.append(
            make_finding(
                code,
                "blocker",
                failure.message,
                period=failure.period,
                missing_basis=missing,
                evidence={
                    "expected_db": failure.expected_db,
                    "measured_db": failure.measured_db,
                    "drift_db": round_db(failure.drift_db),
                },
            )
        )

    for phase in ("before", "after"):
        if phase not in phases:
            message = f"Missing {phase}-measurement calibration bracket."
            failures.append(
                CalibrationFailure(
                    phase=phase,  # type: ignore[arg-type]
                    threshold_db=threshold,
                    message=message,
                )
            )
            findings.append(
                make_finding(
                    "CALIBRATION_MISSING",
                    "blocker",
                    message,
                    missing_basis=f"A {phase}-measurement calibrator reading is required.",
                )
            )

    if duplicate:
        message = "Calibration phase appears more than once; bracket version is ambiguous."
        failures.append(CalibrationFailure(phase=None, threshold_db=threshold, message=message))
        findings.append(make_finding("CALIBRATION_AMBIGUOUS", "blocker", message))

    ordered = sorted(readings, key=lambda r: r.timestamp)
    for left, right in zip(ordered, ordered[1:]):
        if right.timestamp <= left.timestamp:
            invalid_order = True
    if invalid_order:
        message = "Calibration readings are not in chronological order."
        failures.append(CalibrationFailure(phase=None, threshold_db=threshold, message=message))
        findings.append(make_finding("CALIBRATION_TIME_ORDER", "blocker", message))

    for reading in readings:
        drift = reading.measured_db - reading.expected_db
        if abs(drift) > threshold:
            failure = CalibrationFailure(
                phase=reading.phase,
                timestamp=reading.timestamp,
                period=(reading.timestamp, reading.timestamp),
                expected_db=reading.expected_db,
                measured_db=reading.measured_db,
                drift_db=round_db(drift),
                threshold_db=threshold,
                message=f"{reading.phase} calibration drift {drift:.2f} dB exceeds ±{threshold:.2f} dB.",
            )
            add_failure(failure, "CALIBRATION_DRIFT")

    if "before" in phases and "after" in phases:
        before, after = phases["before"], phases["after"]
        if before.timestamp > sample_start:
            failure = CalibrationFailure(
                phase="before",
                timestamp=before.timestamp,
                period=(sample_start, before.timestamp),
                expected_db=before.expected_db,
                measured_db=before.measured_db,
                threshold_db=threshold,
                message="Pre-measurement calibration occurred after sampling began.",
            )
            add_failure(failure, "CALIBRATION_BRACKET")
        if after.timestamp < sample_end:
            failure = CalibrationFailure(
                phase="after",
                timestamp=after.timestamp,
                period=(sample_end, after.timestamp),
                expected_db=after.expected_db,
                measured_db=after.measured_db,
                threshold_db=threshold,
                message="Post-measurement calibration occurred before sampling ended.",
            )
            add_failure(failure, "CALIBRATION_BRACKET")
        if after.timestamp < before.timestamp:
            failure = CalibrationFailure(
                phase="between",
                period=(after.timestamp, before.timestamp),
                threshold_db=threshold,
                message="Post-measurement calibration precedes pre-measurement calibration.",
            )
            add_failure(failure, "CALIBRATION_BRACKET")
        else:
            bracket_drift = after.measured_db - before.measured_db
            if abs(bracket_drift) > threshold:
                failure = CalibrationFailure(
                    phase="between",
                    period=(before.timestamp, after.timestamp),
                    expected_db=before.measured_db,
                    measured_db=after.measured_db,
                    drift_db=round_db(bracket_drift),
                    threshold_db=threshold,
                    message=(
                        f"Calibration changed {bracket_drift:.2f} dB between brackets; "
                        f"limit is ±{threshold:.2f} dB."
                    ),
                )
                add_failure(failure, "CALIBRATION_DRIFT")
            if max_age is not None:
                age = (sample_start - before.timestamp).total_seconds()
                if age < 0 or age > max_age:
                    failure = CalibrationFailure(
                        phase="before",
                        timestamp=before.timestamp,
                        expected_db=before.expected_db,
                        measured_db=before.measured_db,
                        drift_db=round_db(before.measured_db - before.expected_db),
                        threshold_db=threshold,
                        message="Calibration is outside the maximum permitted age.",
                    )
                    add_failure(failure, "CALIBRATION_AGE", "A current pre-measurement calibration is required.")

    valid = not findings
    version = {
        "calibration_hash": calibration_hash(readings),
        "valid": valid,
        "threshold_db": threshold,
        "readings": [r.model_dump(mode="json") for r in ordered],
    }
    return findings, failures, version, valid


def weather_state(
    timestamp: datetime,
    weather: list[dict[str, Any]],
    tolerance_seconds: float,
) -> tuple[Optional[float], Optional[float], bool]:
    times = [item["timestamp"] for item in weather]
    index = bisect_left(times, timestamp)
    candidates = []
    if index < len(weather):
        candidates.append(weather[index])
    if index > 0:
        candidates.append(weather[index - 1])
    usable = [c for c in candidates if abs((timestamp - c["timestamp"]).total_seconds()) <= tolerance_seconds]
    if not usable:
        return None, None, False
    nearest = min(usable, key=lambda c: abs((timestamp - c["timestamp"]).total_seconds()))
    return nearest["wind_speed_ms"], nearest["rain_mm_h"], True


def background_phase_intervals(
    measurement: BackgroundMeasurement,
    zone: ZoneInfo,
    day_start: int,
    night_start: int,
) -> list[dict[str, Any]]:
    intervals = []
    for start, end, phase in phase_ranges(measurement.start, measurement.end, zone, day_start, night_start):
        base_id = measurement.id or f"bg@{iso(measurement.start)}"
        intervals.append(
            {
                "id": base_id if start == measurement.start and end == measurement.end else f"{base_id}:{phase}",
                "start": start,
                "end": end,
                "phase": phase,
                "leq_db": measurement.leq_db,
                "l90_db": measurement.l90_db,
                "target_equipment_id": measurement.target_equipment_id,
                "note": measurement.note,
            }
        )
    return intervals


def choose_background(
    segment: EquipmentPeriod,
    phase: str,
    background: list[BackgroundMeasurement],
    all_equipment: list[EquipmentPeriod],
    rules: Any,
    zone: ZoneInfo,
) -> tuple[Optional[dict[str, Any]], list[tuple[datetime, datetime]]]:
    candidates: list[dict[str, Any]] = []
    blocked_periods: list[tuple[datetime, datetime]] = []
    for bg in background:
        if bg.end <= bg.start:
            blocked_periods.append((bg.start, bg.end))
            continue
        if bg.target_equipment_id and bg.target_equipment_id != segment.equipment_id:
            continue
        for item in background_phase_intervals(bg, zone, rules.day_start_hour, rules.night_start_hour):
            if item["phase"] != phase:
                continue
            # A target-equipment operating interval must not be used as its own background.
            overlaps_target = any(
                p.equipment_id == segment.equipment_id
                and period_overlap(item["start"], item["end"], p.start, p.end) > 0
                for p in all_equipment
            )
            if overlaps_target:
                continue
            distance = max(
                (item["start"] - segment.end).total_seconds(),
                (segment.start - item["end"]).total_seconds(),
                0.0,
            )
            if distance <= rules.background_matching_seconds:
                candidates.append(item)
    if not candidates:
        return None, blocked_periods
    midpoint_segment = segment.start + (segment.end - segment.start) / 2
    candidates.sort(
        key=lambda c: (
            min(
                abs((c["start"] - midpoint_segment).total_seconds()),
                abs((c["end"] - midpoint_segment).total_seconds()),
            ),
            0 if c["l90_db"] is not None else 1,
        )
    )
    return candidates[0], blocked_periods


def correction_for_difference(difference: Optional[float], rules: Any) -> tuple[Optional[float], bool, str]:
    if difference is None:
        return None, False, "No comparable background level."
    if difference < rules.background_difference_invalid_below_db:
        return (
            None,
            False,
            f"Source-to-background difference {difference:.2f} dB is below {rules.background_difference_invalid_below_db:.1f} dB; no valid correction.",
        )
    keys = sorted(rules.background_corrections)
    selected = None
    for key in keys:
        if difference + 1e-9 >= key:
            selected = rules.background_corrections[key]
    if selected is None:
        return None, False, "No background correction table entry applies."
    return selected, True, f"Background correction {selected:+.1f} dB applied."


def site_mismatch(site: Any, rules: Any) -> Optional[str]:
    if rules.required_surface and (site.surface or "").strip().lower() != rules.required_surface.lower():
        return f"surface must be {rules.required_surface}"
    if rules.forbidden_ground and (site.ground_type or "").strip().lower() == rules.forbidden_ground.lower():
        return f"ground type must not be {rules.forbidden_ground}"
    if rules.min_microphone_height_m is not None and (
        site.microphone_height_m is None or site.microphone_height_m < rules.min_microphone_height_m
    ):
        return f"microphone height must be at least {rules.min_microphone_height_m} m"
    if rules.max_microphone_height_m is not None and (
        site.microphone_height_m is None or site.microphone_height_m > rules.max_microphone_height_m
    ):
        return f"microphone height must be at most {rules.max_microphone_height_m} m"
    if rules.max_nearby_reflector_distance_m is not None and (
        site.nearest_reflector_distance_m is None
        or site.nearest_reflector_distance_m > rules.max_nearby_reflector_distance_m
    ):
        return f"nearest reflector must be no farther than {rules.max_nearby_reflector_distance_m} m"
    return None


def compute_metrics(
    samples: list[Sample],
    positions: list[int],
    start: datetime,
    end: datetime,
    exclusions: list[tuple[datetime, datetime]],
    nominal_step: Optional[float],
) -> tuple[MetricSet, list[tuple[int, datetime, float]], float]:
    included: list[tuple[int, datetime, float]] = []
    weights: list[float] = []
    retained_ranges = subtract_ranges(start, end, exclusions)
    for sample, position in zip(samples, positions):
        if not (start <= sample.timestamp <= end):
            continue
        if any(period_overlap(sample.timestamp, sample.timestamp + timedelta(microseconds=1), a, b) > 0 for a, b in exclusions):
            continue
        if nominal_step and nominal_step > 0:
            half = nominal_step / 2.0
            sample_start = max(start, sample.timestamp - timedelta(seconds=half))
            sample_end = min(end, sample.timestamp + timedelta(seconds=half))
            weight = total_seconds(
                subtract_ranges(sample_start, sample_end, exclusions)
            )
        else:
            weight = total_seconds(retained_ranges) / max(1, len(positions))
        if weight > 0:
            included.append((position, sample.timestamp, sample.level_db))
            weights.append(weight)
    if not included:
        return MetricSet(), [], total_seconds(retained_ranges)
    levels = [level for _, _, level in included]
    leq = energy_mean_db(levels, weights)
    l90 = weighted_percentile_db(levels, weights, 90)
    lmax = max(levels)
    duration = total_seconds(retained_ranges)
    return (
        MetricSet(
            leq_db=round_db(leq),
            lmax_db=round_db(lmax),
            l90_db=round_db(l90),
        ),
        included,
        duration,
    )


def compute_review(
    request: ReviewRequest,
    actions: Optional[list[ExcludeAction | MoveBoundaryAction]] = None,
) -> ComputeResult:
    """Recompute a review version from the immutable request and review actions."""
    actions = actions or []
    rules = request.rules
    findings: list[Finding] = []
    raw_samples = list(request.samples)
    raw_bad_ranges, raw_time_bad = validate_raw_sequence(raw_samples)
    for period in merge_ranges(raw_bad_ranges):
        findings.append(
            make_finding(
                "SAMPLE_TIME_ORDER",
                "blocker",
                "Samples are duplicated or not in ascending chronological order.",
                period=period,
                missing_basis="A monotonic, de-duplicated raw sample sequence is required.",
            )
        )

    zone = get_zone(request.site.time_zone)
    if zone is None:
        findings.append(
            make_finding(
                "SITE_TIMEZONE_INVALID",
                "blocker",
                f"Unknown site time zone {request.site.time_zone!r}.",
                missing_basis="An IANA time zone is required to split day and night evidence.",
            )
        )
        zone = UTC
    if rules.night_start_hour <= rules.day_start_hour:
        findings.append(
            make_finding(
                "RULE_DAY_NIGHT_INVALID",
                "blocker",
                "This implementation requires day_start_hour to precede night_start_hour.",
            )
        )

    adjusted_periods, equipment_findings, _adjusted_map = normalize_equipment(
        request.equipment_periods, actions
    )
    findings.extend(equipment_findings)

    excludes = [(a.start, a.end) for a in actions if isinstance(a, ExcludeAction)]
    # Actions are chronologically validated enough to retain them as annotations.
    for action in actions:
        if isinstance(action, ExcludeAction) and action.end <= action.start:
            findings.append(
                make_finding("REVIEW_ACTION_INVALID", "blocker", "Exclusion end must follow start.")
            )
    if any(end <= start for start, end in excludes):
        excludes = [(start, end) for start, end in excludes if end > start]

    # An irrelevant exclusion is preserved for audit but does not alter target
    # operating evidence. It is surfaced as a warning rather than a valid result.
    for ex_start, ex_end in excludes:
        overlap = sum(
            period_overlap(ex_start, ex_end, period.start, period.end)
            for period, _run_index in adjusted_periods
        )
        if overlap <= 0:
            findings.append(
                make_finding(
                    "REVIEW_ACTION_OUTSIDE_SEGMENT",
                    "warning",
                    "Annotated exclusion does not intersect a target equipment operating segment.",
                    period=(ex_start, ex_end),
                )
            )

    sample_start = min((s.timestamp for s in raw_samples), default=datetime.min.replace(tzinfo=UTC))
    sample_end = max((s.timestamp for s in raw_samples), default=datetime.max.replace(tzinfo=UTC))
    calibration_findings, calibration_failures, calibration_version, calibration_valid = calibration_analysis(
        request.calibrations,
        sample_start,
        sample_end,
        rules.max_calibration_drift_db,
        rules.max_calibration_age_seconds,
    )
    findings.extend(calibration_findings)

    raw_nominal = nominal_interval(raw_samples)
    raw_root = sample_chain_hash((i, s.timestamp, s.level_db) for i, s in enumerate(raw_samples))

    weather = sorted(
        [
            {
                "timestamp": w.timestamp,
                "wind_speed_ms": w.wind_speed_ms,
                "rain_mm_h": w.rain_mm_h,
            }
            for w in request.weather
        ],
        key=lambda w: w["timestamp"],
    )
    weather_time_bad = any(
        b["timestamp"] <= a["timestamp"] for a, b in zip(weather, weather[1:])
    )
    if weather_time_bad:
        findings.append(
            make_finding(
                "WEATHER_TIME_ORDER",
                "blocker",
                "Weather observations are duplicated or not chronological.",
                missing_basis="Chronological weather records are needed to justify wind and rain conditions.",
            )
        )

    # Site conditions apply to every segment; retain as both a per-segment and global basis.
    site_reason = site_mismatch(request.site, rules)
    if site_reason:
        findings.append(
            make_finding(
                "SITE_CONDITION_MISMATCH",
                "blocker",
                f"Measurement point does not satisfy rule: {site_reason}.",
                missing_basis="A compliant measurement-point condition record is required.",
            )
        )
    action_invalid = any(f.code == "REVIEW_ACTION_INVALID" for f in findings)

    segments: list[SegmentResult] = []
    background_interference: list[BackgroundInterference] = []
    all_effective_included: list[tuple[int, datetime, float]] = []

    # If raw timing is unusable, do not sort it to manufacture metrics.
    if not raw_time_bad:
        sorted_samples = sorted(raw_samples, key=lambda s: s.timestamp)
        positions_sorted = sorted(range(len(raw_samples)), key=lambda i: raw_samples[i].timestamp)
        for period, run_index in adjusted_periods:
            phase_parts = phase_ranges(
                period.start,
                period.end,
                zone,
                rules.day_start_hour,
                rules.night_start_hour,
            )
            for phase_index, (seg_start, seg_end, phase) in enumerate(phase_parts):
                segment_id = f"{period.equipment_id}-r{run_index}-p{phase_index}-{phase[0]}"
                segment_finding_codes: list[str] = []
                segment_blocked = False

                def segment_finding(*args: Any, **kwargs: Any) -> None:
                    nonlocal segment_blocked
                    finding = make_finding(*args, segment_id=segment_id, **kwargs)
                    segment_finding_codes.append(finding.code)
                    findings.append(finding)
                    if finding.severity == "blocker":
                        segment_blocked = True

                in_segment = [
                    (sample, position)
                    for sample, position in zip(sorted_samples, positions_sorted)
                    if seg_start <= sample.timestamp <= seg_end
                ]
                retained = [
                    (sample, position)
                    for sample, position in in_segment
                    if not any(
                        period_overlap(
                            sample.timestamp,
                            sample.timestamp + timedelta(microseconds=1),
                            ex_start,
                            ex_end,
                        )
                        > 0
                        for ex_start, ex_end in excludes
                    )
                ]

                # Missing/gapped coverage, ignoring gaps fully covered by a justified exclusion.
                if not retained:
                    segment_finding(
                        "SAMPLING_GAP",
                        "blocker",
                        "No retained samples cover this effective segment.",
                        period=(seg_start, seg_end),
                        missing_basis="Non-excluded sound-level samples are required.",
                    )
                else:
                    boundary_left = (retained[0][0].timestamp - seg_start).total_seconds()
                    boundary_right = (seg_end - retained[-1][0].timestamp).total_seconds()
                    if boundary_left > rules.max_sample_gap_seconds:
                        segment_finding(
                            "SAMPLING_GAP",
                            "blocker",
                            "Sampling starts after the permitted gap at segment beginning.",
                            period=(seg_start, retained[0][0].timestamp),
                            missing_basis="Continuous samples covering the segment boundary are required.",
                        )
                    if boundary_right > rules.max_sample_gap_seconds:
                        segment_finding(
                            "SAMPLING_GAP",
                            "blocker",
                            "Sampling ends before the permitted gap at segment end.",
                            period=(retained[-1][0].timestamp, seg_end),
                            missing_basis="Continuous samples covering the segment boundary are required.",
                        )
                    gaps: list[tuple[datetime, datetime]] = []
                    for left, right in zip(retained, retained[1:]):
                        delta = (right[0].timestamp - left[0].timestamp).total_seconds()
                        if delta > rules.max_sample_gap_seconds:
                            gaps.append((left[0].timestamp, right[0].timestamp))
                    for gap_start, gap_end in gaps:
                        if any(contains_period(ex_start, ex_end, gap_start, gap_end) for ex_start, ex_end in excludes):
                            continue
                        segment_finding(
                            "SAMPLING_GAP",
                            "blocker",
                            f"Sample gap is {(gap_end-gap_start).total_seconds():.0f} s; maximum is {rules.max_sample_gap_seconds:.0f} s.",
                            period=(gap_start, gap_end),
                            missing_basis="Continuous monitoring or a fully justified exclusion covering the gap is required.",
                        )

                effective_duration = total_seconds(subtract_ranges(seg_start, seg_end, excludes))
                if effective_duration < rules.min_segment_duration_seconds:
                    segment_finding(
                        "SEGMENT_DURATION",
                        "blocker",
                        f"Retained duration {effective_duration:.0f} s is below minimum {rules.min_segment_duration_seconds:.0f} s.",
                        period=(seg_start, seg_end),
                        missing_basis="A longer valid monitoring duration is required.",
                    )

                if action_invalid:
                    segment_finding_codes.append("REVIEW_ACTION_INVALID")
                    segment_blocked = True

                if site_reason:
                    segment_finding_codes.append("SITE_CONDITION_MISMATCH")
                    segment_blocked = True

                if not weather_time_bad:
                    bad_weather_ranges: list[tuple[datetime, datetime]] = []
                    bad_weather_reasons: list[str] = []
                    for sample, _position in retained:
                        wind, rain, present = weather_state(
                            sample.timestamp, weather, rules.weather_interpolation_seconds
                        )
                        bad = False
                        reason_parts = []
                        if not present:
                            bad, reason = True, "no weather observation within matching window"
                            reason_parts.append(reason)
                        else:
                            if rules.max_wind_speed_ms is not None and (
                                wind is None or wind > rules.max_wind_speed_ms
                            ):
                                bad = True
                                reason_parts.append("wind speed missing or above limit")
                            if rules.max_rain_mm_h is not None and (
                                rain is None or rain > rules.max_rain_mm_h
                            ):
                                bad = True
                                reason_parts.append("rain missing or above limit")
                        if bad:
                            bad_weather_ranges.append(
                                (sample.timestamp, sample.timestamp + timedelta(microseconds=1))
                            )
                            bad_weather_reasons.extend(reason_parts)
                    for bad_start, bad_end in merge_ranges(bad_weather_ranges):
                        reason_text = "; ".join(sorted(set(bad_weather_reasons))) or "wind/rain condition"
                        segment_finding(
                            "WEATHER_RULE_EXCEEDED",
                            "blocker",
                            f"Wind/rain evidence is missing or outside the rule ({reason_text}).",
                            period=(bad_start, bad_end),
                            missing_basis="Valid wind speed and rainfall observations within the matching window are required.",
                        )

                metrics, included, _metric_duration = compute_metrics(
                    sorted_samples,
                    positions_sorted,
                    seg_start,
                    seg_end,
                    excludes,
                    raw_nominal,
                )

                if not calibration_valid:
                    segment_finding_codes.append("CALIBRATION_FAILED")
                    segment_blocked = True

                matched_bg, invalid_bg_periods = choose_background(
                    period,
                    phase,
                    request.background_measurements,
                    [p for p, _ in adjusted_periods],
                    rules,
                    zone,
                )
                for bad_period in invalid_bg_periods:
                    segment_finding(
                        "BACKGROUND_MEASUREMENT_INVALID",
                        "blocker",
                        "A background measurement has a non-positive interval.",
                        period=bad_period,
                    )

                background_level = None
                background_metric = None
                difference = None
                correction = None
                sufficient = False
                bg_message = "No eligible background measurement matched this segment and phase."
                matched_id = None
                if matched_bg is not None:
                    matched_id = matched_bg["id"]
                    if matched_bg["leq_db"] is not None:
                        background_level = matched_bg["leq_db"]
                        background_metric = "Leq"
                    elif matched_bg["l90_db"] is not None:
                        background_level = matched_bg["l90_db"]
                        background_metric = "L90"
                    if metrics.leq_db is not None:
                        difference = metrics.leq_db - background_level
                    correction, sufficient, bg_message = correction_for_difference(difference, rules)
                if not segment_blocked and metrics.leq_db is not None:
                    if not sufficient:
                        segment_finding(
                            "BACKGROUND_DIFFERENCE_INSUFFICIENT",
                            "blocker",
                            bg_message,
                            period=(seg_start, seg_end),
                            missing_basis="A qualifying non-target background measurement and source/background margin are required.",
                        )
                if sufficient and metrics.leq_db is not None and correction is not None:
                    metrics.corrected_leq_db = round_db(metrics.leq_db + correction)
                    metrics.background_correction_db = round_db(correction)
                    limit = request.limit_night_db if phase == "night" else request.limit_day_db
                    metrics.limit_db = limit
                    if limit is not None:
                        metrics.exceeds_limit = metrics.corrected_leq_db > limit
                metrics.background_level_db = round_db(background_level)
                metrics.background_metric = background_metric
                metrics.background_difference_db = round_db(difference)
                background_interference.append(
                    BackgroundInterference(
                        segment_id=segment_id,
                        period=(seg_start, seg_end),
                        source_level_db=None if segment_blocked else metrics.leq_db,
                        background_level_db=round_db(background_level),
                        background_metric=background_metric,
                        difference_db=round_db(difference),
                        correction_db=round_db(correction),
                        sufficient=sufficient,
                        message=bg_message,
                        matched_measurement_id=matched_id,
                    )
                )

                if segment_blocked:
                    # Invalid evidence exposes affected periods and basis, not a usable level result.
                    # Its samples remain in the raw chain but are excluded from this effective index.
                    metrics = MetricSet(
                        background_level_db=round_db(background_level),
                        background_metric=background_metric,
                        background_difference_db=round_db(difference),
                    )
                    included = []
                else:
                    all_effective_included.extend(included)
                segments.append(
                    SegmentResult(
                        segment_id=segment_id,
                        equipment_id=period.equipment_id,
                        run_index=run_index,
                        phase=phase,  # type: ignore[arg-type]
                        start=seg_start,
                        end=seg_end,
                        duration_seconds=round(total_seconds(subtract_ranges(seg_start, seg_end, excludes)), 3),
                        sample_count=len(included),
                        status="blocked" if segment_blocked else "valid",
                        metrics=metrics,
                        finding_codes=sorted(set(segment_finding_codes)),
                    )
                )

    effective_root = sample_chain_hash(
        (position, timestamp, level) for position, timestamp, level in sorted(set(all_effective_included))
    )
    effective_positions = sorted({position for position, _, _ in all_effective_included})
    sample_index = SampleIndex(
        algorithm="sha256-position-time-level-chain-v1",
        raw_root=raw_root,
        effective_root=effective_root,
        raw_count=len(raw_samples),
        effective_count=len(effective_positions),
        nominal_interval_seconds=round_db(raw_nominal),
        raw_start=raw_samples[0].timestamp if raw_samples else None,
        raw_end=raw_samples[-1].timestamp if raw_samples else None,
        included_sample_positions=effective_positions,
    )

    blocker = any(f.severity == "blocker" for f in findings) or any(s.status == "blocked" for s in segments)
    return ComputeResult(
        status="blocked" if blocker else "valid",
        segments=segments,
        findings=findings,
        excluded_ranges=merge_ranges(excludes),
        sample_index=sample_index,
        calibration_version=calibration_version,
        rule_hash=rules_hash(rules),
        background_interference=background_interference,
        calibration_failures=calibration_failures,
    )
