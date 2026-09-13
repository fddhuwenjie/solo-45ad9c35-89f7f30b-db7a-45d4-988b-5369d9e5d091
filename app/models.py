"""Pydantic HTTP contracts for the environmental noise evidence review API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def parse_utc(value: Any) -> datetime:
    """Parse ISO-8601 timestamps and normalize them to timezone-aware UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    else:
        raise ValueError("timestamp must be an ISO-8601 string")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, BeforeValidator(parse_utc)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sample(StrictModel):
    timestamp: UTCDateTime
    level_db: float = Field(..., description="Instantaneous or short-interval A-weighted sound level.")


class CalibrationReading(StrictModel):
    timestamp: UTCDateTime
    phase: Literal["before", "after"]
    expected_db: float
    measured_db: float


class WeatherReading(StrictModel):
    timestamp: UTCDateTime
    wind_speed_ms: Optional[float] = None
    rain_mm_h: Optional[float] = None


class EquipmentPeriod(StrictModel):
    equipment_id: str
    start: UTCDateTime
    end: UTCDateTime
    operating: bool = True
    name: Optional[str] = None


class BackgroundMeasurement(StrictModel):
    id: Optional[str] = None
    start: UTCDateTime
    end: UTCDateTime
    leq_db: float
    l90_db: Optional[float] = None
    target_equipment_id: Optional[str] = None
    note: Optional[str] = None


class RuleSet(StrictModel):
    day_start_hour: int = Field(default=6, ge=0, le=23)
    night_start_hour: int = Field(default=22, ge=0, le=23)
    max_sample_gap_seconds: float = Field(default=60.0, gt=0)
    min_segment_duration_seconds: float = Field(default=30.0, ge=0)
    weather_interpolation_seconds: float = Field(default=1800.0, ge=0)
    max_wind_speed_ms: Optional[float] = Field(default=5.0, ge=0)
    max_rain_mm_h: Optional[float] = Field(default=0.0, ge=0)
    max_calibration_drift_db: float = Field(default=0.7, ge=0)
    max_calibration_age_seconds: Optional[float] = Field(default=None, ge=0)
    min_background_difference_db: float = Field(default=3.0, ge=0)
    background_difference_invalid_below_db: float = Field(default=3.0, ge=0)
    required_surface: Optional[str] = None
    forbidden_ground: Optional[str] = None
    min_microphone_height_m: Optional[float] = Field(default=None, ge=0)
    max_microphone_height_m: Optional[float] = Field(default=None, ge=0)
    max_nearby_reflector_distance_m: Optional[float] = Field(default=None, ge=0)
    background_matching_seconds: float = Field(default=7200.0, ge=0)
    background_corrections: dict[float, float] = Field(
        default_factory=lambda: {3.0: -3.0, 5.0: -2.0, 10.0: -1.0}
    )


class SiteConditions(StrictModel):
    point_id: str
    time_zone: str = "UTC"
    surface: Optional[str] = None
    ground_type: Optional[str] = None
    microphone_height_m: Optional[float] = None
    nearest_reflector_distance_m: Optional[float] = None
    note: Optional[str] = None


class ReviewRequest(StrictModel):
    review_id: Optional[str] = Field(default=None, description="Client-supplied stable identifier.")
    title: str = "Factory night noise evidence review"
    site: SiteConditions
    samples: list[Sample] = Field(..., min_length=1)
    calibrations: list[CalibrationReading] = Field(..., min_length=1)
    weather: list[WeatherReading] = Field(default_factory=list)
    equipment_periods: list[EquipmentPeriod] = Field(..., min_length=1)
    background_measurements: list[BackgroundMeasurement] = Field(default_factory=list)
    rules: RuleSet = Field(default_factory=RuleSet)
    limit_day_db: Optional[float] = None
    limit_night_db: Optional[float] = None


class ExcludeAction(StrictModel):
    type: Literal["exclude"]
    start: UTCDateTime
    end: UTCDateTime
    reason: str = Field(..., min_length=1)
    label: str = "non-target event"


class MoveBoundaryAction(StrictModel):
    type: Literal["move_boundary"]
    equipment_id: str
    run_index: int = Field(..., ge=0)
    side: Literal["start", "end"]
    to: UTCDateTime
    reason: str = Field(..., min_length=1)


ReviewAction = Annotated[ExcludeAction | MoveBoundaryAction, Field(discriminator="type")]


class RevisionRequest(StrictModel):
    actions: list[ReviewAction] = Field(..., min_length=1)
    reviewer: str = Field(..., min_length=1)
    note: Optional[str] = None


class ConfirmRequest(StrictModel):
    reviewer: str = Field(..., min_length=1)
    note: Optional[str] = None


class ActionRecord(StrictModel):
    type: Literal["exclude", "move_boundary"]
    start: Optional[UTCDateTime] = None
    end: Optional[UTCDateTime] = None
    equipment_id: Optional[str] = None
    run_index: Optional[int] = None
    side: Optional[Literal["start", "end"]] = None
    to: Optional[UTCDateTime] = None
    reason: str
    label: str = "non-target event"
    reviewer: Optional[str] = None
    created_at: Optional[UTCDateTime] = None


class Finding(StrictModel):
    code: str
    severity: Literal["blocker", "warning"]
    segment_id: Optional[str] = None
    period: Optional[tuple[UTCDateTime, UTCDateTime]] = None
    message: str
    missing_basis: Optional[str] = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class MetricSet(StrictModel):
    leq_db: Optional[float] = None
    lmax_db: Optional[float] = None
    l90_db: Optional[float] = None
    corrected_leq_db: Optional[float] = None
    background_level_db: Optional[float] = None
    background_metric: Optional[Literal["L90", "Leq"]] = None
    background_difference_db: Optional[float] = None
    background_correction_db: Optional[float] = None
    limit_db: Optional[float] = None
    exceeds_limit: Optional[bool] = None


class SegmentResult(StrictModel):
    segment_id: str
    equipment_id: str
    run_index: int
    phase: Literal["day", "night"]
    start: UTCDateTime
    end: UTCDateTime
    duration_seconds: float
    sample_count: int
    status: Literal["valid", "blocked"]
    metrics: MetricSet
    finding_codes: list[str] = Field(default_factory=list)


class SampleIndex(StrictModel):
    algorithm: str
    raw_root: str
    effective_root: str
    raw_count: int
    effective_count: int
    nominal_interval_seconds: Optional[float]
    raw_start: Optional[UTCDateTime]
    raw_end: Optional[UTCDateTime]
    included_sample_positions: list[int]


class CalibrationFailure(StrictModel):
    phase: Optional[Literal["before", "after", "between", "after_end"]]
    timestamp: Optional[UTCDateTime] = None
    period: Optional[tuple[UTCDateTime, UTCDateTime]] = None
    expected_db: Optional[float] = None
    measured_db: Optional[float] = None
    drift_db: Optional[float] = None
    threshold_db: float
    message: str


class BackgroundInterference(StrictModel):
    segment_id: Optional[str] = None
    period: tuple[UTCDateTime, UTCDateTime]
    source_level_db: Optional[float]
    background_level_db: Optional[float]
    background_metric: Optional[Literal["L90", "Leq"]]
    difference_db: Optional[float]
    correction_db: Optional[float]
    sufficient: bool
    message: str
    matched_measurement_id: Optional[str] = None


class ComputeResult(StrictModel):
    status: Literal["valid", "blocked"]
    segments: list[SegmentResult]
    findings: list[Finding]
    excluded_ranges: list[tuple[UTCDateTime, UTCDateTime]]
    sample_index: SampleIndex
    calibration_version: dict[str, Any]
    rule_hash: str
    background_interference: list[BackgroundInterference]
    calibration_failures: list[CalibrationFailure]
    recomputed_from_version: Optional[int] = None


class VersionSummary(StrictModel):
    version: int
    parent_version: Optional[int]
    status: Literal["draft", "confirmed"]
    confirmed: bool
    created_at: UTCDateTime
    confirmed_at: Optional[UTCDateTime]
    reviewer: Optional[str]
    note: Optional[str]
    compute_status: Literal["valid", "blocked"]
    seal_hash: Optional[str]
    action_count: int


class ReviewSummary(StrictModel):
    review_id: str
    title: str
    point_id: str
    created_at: UTCDateTime
    latest_version: int
    confirmed_version: Optional[int]
    versions: list[VersionSummary]


class Confirmation(StrictModel):
    reviewer: str
    confirmed_at: UTCDateTime
    rule_hash: str
    calibration_version: dict[str, Any]
    sample_index: SampleIndex
    seal_hash: str


class EvidencePackage(StrictModel):
    review_id: str
    title: str
    point_id: str
    version: int
    parent_version: Optional[int]
    status: Literal["draft", "confirmed"]
    created_at: UTCDateTime
    sealed_at: Optional[UTCDateTime]
    confirmation: Optional[Confirmation]
    engine_version: str
    rules: dict[str, Any]
    limits: dict[str, Optional[float]]
    site: dict[str, Any]
    actions: list[ActionRecord]
    source_request: "ReviewRequest"
    result: ComputeResult


class RevisionComparison(StrictModel):
    review_id: str
    from_version: int
    to_version: int
    added_actions: list[ActionRecord]
    segments_added: list[SegmentResult]
    segments_removed: list[SegmentResult]
    segments_changed: list[dict[str, Any]]
    findings_added: list[Finding]
    findings_removed: list[Finding]
    status_changed: bool


class ErrorResponse(BaseModel):
    detail: str
