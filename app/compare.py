"""Revision comparison helpers."""
from __future__ import annotations

from typing import Any

from .models import (
    Finding,
    RevisionComparison,
    SegmentResult,
)


def finding_key(finding: Finding) -> tuple[Any, ...]:
    return (
        finding.code,
        finding.severity,
        finding.segment_id,
        finding.period[0].isoformat() if finding.period else None,
        finding.period[1].isoformat() if finding.period else None,
        finding.message,
        finding.missing_basis,
    )


def segment_delta(old: SegmentResult, new: SegmentResult) -> dict[str, Any]:
    old_metrics = old.metrics.model_dump()
    new_metrics = new.metrics.model_dump()
    changed = {key: {"from": old_value, "to": new_value}
               for key, (old_value, new_value) in
               ((key, (old_metrics[key], new_metrics[key])) for key in new_metrics)
               if old_value != new_value}
    if old.status != new.status:
        changed["status"] = {"from": old.status, "to": new.status}
    if old.sample_count != new.sample_count:
        changed["sample_count"] = {"from": old.sample_count, "to": new.sample_count}
    if old.duration_seconds != new.duration_seconds:
        changed["duration_seconds"] = {"from": old.duration_seconds, "to": new.duration_seconds}
    if old.finding_codes != new.finding_codes:
        changed["finding_codes"] = {"from": old.finding_codes, "to": new.finding_codes}
    return {
        "segment_id": new.segment_id,
        "equipment_id": new.equipment_id,
        "run_index": new.run_index,
        "phase": new.phase,
        "changes": changed,
    }


def compare_results(
    review_id: str,
    from_version: int,
    to_version: int,
    old_actions: list[Any],
    new_actions: list[Any],
    old_result: Any,
    new_result: Any,
) -> RevisionComparison:
    old_segments = {s.segment_id: s for s in old_result.segments}
    new_segments = {s.segment_id: s for s in new_result.segments}
    added = [s for identifier, s in new_segments.items() if identifier not in old_segments]
    removed = [s for identifier, s in old_segments.items() if identifier not in new_segments]
    changed = [
        segment_delta(old_segments[identifier], new_segments[identifier])
        for identifier in sorted(set(old_segments) & set(new_segments))
        if old_segments[identifier].model_dump() != new_segments[identifier].model_dump()
    ]

    old_keys = {finding_key(f) for f in old_result.findings}
    new_keys = {finding_key(f) for f in new_result.findings}
    findings_added = [f for f in new_result.findings if finding_key(f) not in old_keys]
    findings_removed = [f for f in old_result.findings if finding_key(f) not in new_keys]

    old_action_keys = {a.model_dump_json() for a in old_actions}
    actions_added = [a for a in new_actions if a.model_dump_json() not in old_action_keys]
    return RevisionComparison(
        review_id=review_id,
        from_version=from_version,
        to_version=to_version,
        added_actions=actions_added,
        segments_added=added,
        segments_removed=removed,
        segments_changed=changed,
        findings_added=findings_added,
        findings_removed=findings_removed,
        status_changed=old_result.status != new_result.status,
    )
