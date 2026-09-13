"""FastAPI application and HTTP routes."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Response, status
from pydantic import ValidationError

from .compare import compare_results
from .database import Database, utc_now
from .engine import ENGINE_VERSION, canonical_json, compute_review, sha256_text
from .models import (
    CalibrationFailure,
    ConfirmRequest,
    EvidencePackage,
    ExcludeAction,
    BackgroundInterference,
    MoveBoundaryAction,
    ReviewRequest,
    RevisionComparison,
    RevisionRequest,
    ReviewSummary,
)
from .visualization import render_svg


DATABASE_PATH = os.getenv("NOISE_REVIEW_DB", str(Path(__file__).resolve().parent / "noise_reviews.db"))
_engine_db: Optional[Database] = None


def get_db() -> Database:
    global _engine_db
    if _engine_db is None:
        _engine_db = Database(DATABASE_PATH)
    return _engine_db


app = FastAPI(
    title="Environmental Noise Evidence Review API",
    version=ENGINE_VERSION,
    description=(
        "Recomputes day/night equipment noise evidence from immutable raw records. "
        "Revisions are append-only; confirmations seal rules, calibration, and sample indexes."
    ),
)


def action_from_record(record: Any) -> ExcludeAction | MoveBoundaryAction:
    data = record.model_dump(exclude={"reviewer", "created_at", "label"}, exclude_none=True)
    try:
        if record.type == "exclude":
            label = record.label
            action = ExcludeAction.model_validate(data)
            action.label = label
            return action
        return MoveBoundaryAction.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(status_code=500, detail=f"Stored review action is invalid: {exc}") from exc


def recompute(db: Database, review_id: str, version: int) -> tuple[Any, Any, list[Any]]:
    request = db.get_request(review_id)
    action_records = db.get_actions(review_id, version)
    actions = [action_from_record(record) for record in action_records]
    result = compute_review(request, actions)
    result.recomputed_from_version = version
    return request, result, action_records


def seal_hash(evidence: EvidencePackage, reviewer: str, confirmed_at: Any) -> str:
    payload = {
        "review_id": evidence.review_id,
        "version": evidence.version,
        "reviewer": reviewer,
        "confirmed_at": confirmed_at.isoformat().replace("+00:00", "Z"),
        "engine_version": evidence.engine_version,
        "rule_hash": evidence.result.rule_hash,
        "calibration_version": evidence.result.calibration_version,
        "sample_index": evidence.result.sample_index.model_dump(mode="json"),
        "segments": [s.model_dump(mode="json") for s in evidence.result.segments],
        "findings": [f.model_dump(mode="json") for f in evidence.result.findings],
        "actions": [a.model_dump(mode="json") for a in evidence.actions],
    }
    return sha256_text(canonical_json(payload))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": ENGINE_VERSION}


@app.post("/reviews", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED)
def create_review(payload: ReviewRequest, db: Database = Depends(get_db)) -> dict[str, Any]:
    result = compute_review(payload)
    try:
        review_id = db.create_review(payload, result)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc).strip('"')) from exc
    return {
        "review_id": review_id,
        "version": 1,
        "status": result.status,
        "result": result.model_dump(mode="json"),
    }


@app.get("/reviews", response_model=list[dict[str, Any]])
def list_reviews(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_reviews()


@app.get("/reviews/{review_id}", response_model=ReviewSummary)
def get_review(review_id: str, db: Database = Depends(get_db)) -> Any:
    try:
        summary, _request = db.list_versions(review_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return summary


@app.get("/reviews/{review_id}/versions/{version:int}", response_model=dict[str, Any])
def get_version(review_id: str, version: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        request, _result, actions = recompute(db, review_id, version)
        stored_result = db.get_result(review_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return {
        "review_id": review_id,
        "version": version,
        "recomputed": True,
        "stored_status_matches_recompute": stored_result.model_dump(mode="json")
        == _result.model_dump(mode="json"),
        "actions": [a.model_dump(mode="json") for a in actions],
        "result": _result.model_dump(mode="json"),
    }


@app.post(
    "/reviews/{review_id}/revisions",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
def create_revision(
    review_id: str,
    payload: RevisionRequest,
    parent_version: Optional[int] = None,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    try:
        summary, _request = db.list_versions(review_id)
        base_version = parent_version or summary.latest_version
        if base_version != summary.latest_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"parent_version {base_version} is not latest version {summary.latest_version}; "
                    "old evidence remains available but corrections branch from the latest revision"
                ),
            )
        parent_actions = db.get_actions(review_id, base_version)
        all_actions = [action_from_record(a) for a in parent_actions] + list(payload.actions)
        result = compute_review(_request, all_actions)
        created = utc_now()
        new_version = db.add_revision(review_id, base_version, payload, result, created)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "review_id": review_id,
        "version": new_version,
        "parent_version": base_version,
        "status": result.status,
        "result": result.model_dump(mode="json"),
    }


@app.post("/reviews/{review_id}/versions/{version:int}/confirm", response_model=EvidencePackage)
def confirm_version(
    review_id: str,
    version: int,
    payload: ConfirmRequest,
    db: Database = Depends(get_db),
) -> Any:
    try:
        request, result, _actions = recompute(db, review_id, version)
        if result.status == "blocked":
            raise HTTPException(
                status_code=409,
                detail="Blocked evidence cannot be confirmed; resolve blockers in a new review or revision.",
            )
        confirmed_at = utc_now()
        preliminary = db.build_evidence(review_id, version, result)
        # The preliminary evidence is draft; sealing uses reviewer and confirmation time.
        seal = seal_hash(preliminary, payload.reviewer, confirmed_at)
        db.confirm_version(
            review_id,
            version,
            payload.reviewer,
            payload.note,
            result,
            seal,
            confirmed_at,
        )
        evidence = db.build_evidence(review_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return evidence


@app.get("/reviews/{review_id}/versions/{version:int}/evidence", response_model=EvidencePackage)
def evidence_package(review_id: str, version: int, db: Database = Depends(get_db)) -> Any:
    try:
        evidence = db.build_evidence(review_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return evidence


@app.get("/reviews/{review_id}/versions/{version:int}/curve.svg")
def curve_svg(review_id: str, version: int, db: Database = Depends(get_db)) -> Response:
    try:
        request, _result, _actions = recompute(db, review_id, version)
        svg = render_svg(request, _result, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return Response(content=svg, media_type="image/svg+xml")


@app.get(
    "/reviews/{review_id}/versions/{version:int}/compare/{other_version:int}",
    response_model=RevisionComparison,
)
def compare_versions(review_id: str, version: int, other_version: int, db: Database = Depends(get_db)) -> Any:
    try:
        _request_1, result_1, actions_1 = recompute(db, review_id, version)
        _request_2, result_2, actions_2 = recompute(db, review_id, other_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    low, high = sorted((version, other_version))
    return compare_results(review_id, low, high, actions_1 if version == low else actions_2,
                           actions_2 if other_version == high else actions_1,
                           result_1 if version == low else result_2,
                           result_2 if other_version == high else result_1)


@app.get(
    "/reviews/{review_id}/versions/{version:int}/background-interference",
    response_model=list[BackgroundInterference],
)
def background_interference(review_id: str, version: int, db: Database = Depends(get_db)) -> Any:
    try:
        _, result, _ = recompute(db, review_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return result.background_interference


@app.get(
    "/reviews/{review_id}/versions/{version:int}/calibration-failures",
    response_model=list[CalibrationFailure],
)
def calibration_failures(review_id: str, version: int, db: Database = Depends(get_db)) -> Any:
    try:
        _, result, _ = recompute(db, review_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return result.calibration_failures
