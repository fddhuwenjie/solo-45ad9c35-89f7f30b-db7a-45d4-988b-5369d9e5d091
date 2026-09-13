"""SQLite persistence for immutable reviews, revisions, and confirmations."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import (
    ActionRecord,
    Confirmation,
    EvidencePackage,
    ReviewRequest,
    RevisionRequest,
    ReviewSummary,
    VersionSummary,
)

UTC = timezone.utc
SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    point_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    review_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER,
    created_at TEXT NOT NULL,
    reviewer TEXT,
    note TEXT,
    status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
    confirmed_at TEXT,
    actions_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    seal_hash TEXT,
    PRIMARY KEY (review_id, version),
    FOREIGN KEY (review_id) REFERENCES reviews(id)
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _loads(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def create_review(self, request: ReviewRequest, result: Any) -> str:
        review_id = request.review_id or f"rev-{uuid.uuid4().hex}"
        created_at = now_iso()
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
            if exists:
                raise KeyError(f"review_id {review_id!r} already exists")
            self._connection.execute(
                "INSERT INTO reviews(id,title,point_id,created_at,request_json) VALUES (?,?,?,?,?)",
                (
                    review_id,
                    request.title,
                    request.site.point_id,
                    created_at,
                    self._dumps(request.model_dump(mode="json")),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO versions(review_id,version,parent_version,created_at,reviewer,note,
                                     status,confirmed_at,actions_json,result_json,seal_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    1,
                    None,
                    created_at,
                    None,
                    None,
                    "draft",
                    None,
                    "[]",
                    self._dumps(result.model_dump(mode="json")),
                    None,
                ),
            )
            self._connection.commit()
        return review_id

    def _require_review(self, review_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM reviews WHERE id=?", (review_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"review {review_id!r} not found")
        return row

    def _version_row(self, review_id: str, version: int) -> sqlite3.Row:
        self._require_review(review_id)
        row = self._connection.execute(
            "SELECT * FROM versions WHERE review_id=? AND version=?", (review_id, version)
        ).fetchone()
        if row is None:
            raise KeyError(f"version {version} not found")
        return row

    def get_request(self, review_id: str) -> ReviewRequest:
        with self._lock:
            row = self._require_review(review_id)
        return ReviewRequest.model_validate(self._loads(row["request_json"]))

    def get_result(self, review_id: str, version: int) -> Any:
        from .models import ComputeResult

        with self._lock:
            row = self._version_row(review_id, version)
        return ComputeResult.model_validate(self._loads(row["result_json"]))

    def get_actions(self, review_id: str, version: int) -> list[ActionRecord]:
        with self._lock:
            row = self._version_row(review_id, version)
        return [ActionRecord.model_validate(item) for item in self._loads(row["actions_json"])]

    def add_revision(
        self,
        review_id: str,
        parent_version: int,
        payload: RevisionRequest,
        result: Any,
        created_at: datetime,
    ) -> int:
        with self._lock:
            parent = self._version_row(review_id, parent_version)
            latest_row = self._connection.execute(
                "SELECT MAX(version) AS version FROM versions WHERE review_id=?",
                (review_id,),
            ).fetchone()
            latest = int(latest_row["version"])
            if parent_version != latest:
                raise ValueError("revisions can only branch from the current latest version")
            parent_actions = [ActionRecord.model_validate(x) for x in self._loads(parent["actions_json"])]
            created_iso = created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            new_action_records = [
                ActionRecord(
                    **action.model_dump(exclude_none=True),
                    reviewer=payload.reviewer,
                    created_at=created_at,
                ).model_dump(mode="json")
                for action in payload.actions
            ]
            all_actions = [a.model_dump(mode="json") for a in parent_actions] + new_action_records
            version = latest + 1
            self._connection.execute(
                """
                INSERT INTO versions(review_id,version,parent_version,created_at,reviewer,note,
                                     status,confirmed_at,actions_json,result_json,seal_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    version,
                    parent_version,
                    created_iso,
                    payload.reviewer,
                    payload.note,
                    "draft",
                    None,
                    self._dumps(all_actions),
                    self._dumps(result.model_dump(mode="json")),
                    None,
                ),
            )
            self._connection.commit()
        return version

    def confirm_version(
        self,
        review_id: str,
        version: int,
        reviewer: str,
        note: Optional[str],
        result: Any,
        seal_hash: str,
        confirmed_at: datetime,
    ) -> None:
        with self._lock:
            row = self._version_row(review_id, version)
            if row["status"] == "confirmed":
                raise ValueError("version is already confirmed; create a revision to correct it")
            confirmed_iso = confirmed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            # Persist recomputed result so seal reflects the exact engine output.
            self._connection.execute(
                """
                UPDATE versions
                   SET status='confirmed', confirmed_at=?, reviewer=?, note=COALESCE(?, note),
                       result_json=?, seal_hash=?
                 WHERE review_id=? AND version=?
                """,
                (
                    confirmed_iso,
                    reviewer,
                    note,
                    self._dumps(result.model_dump(mode="json")),
                    seal_hash,
                    review_id,
                    version,
                ),
            )
            self._connection.commit()

    def build_evidence(
        self,
        review_id: str,
        version: int,
        result: Optional[Any] = None,
    ) -> EvidencePackage:
        from .engine import ENGINE_VERSION

        with self._lock:
            review = self._require_review(review_id)
            row = self._version_row(review_id, version)
        request = ReviewRequest.model_validate(self._loads(review["request_json"]))
        stored_result = self.get_result(review_id, version) if result is None else result
        actions = [ActionRecord.model_validate(x) for x in self._loads(row["actions_json"])]
        confirmation = None
        if row["status"] == "confirmed" and row["seal_hash"]:
            confirmation = Confirmation(
                reviewer=row["reviewer"] or "",
                confirmed_at=datetime.fromisoformat(row["confirmed_at"]),
                rule_hash=stored_result.rule_hash,
                calibration_version=stored_result.calibration_version,
                sample_index=stored_result.sample_index,
                seal_hash=row["seal_hash"],
            )
        return EvidencePackage(
            review_id=review_id,
            title=review["title"],
            point_id=request.site.point_id,
            version=version,
            parent_version=row["parent_version"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sealed_at=datetime.fromisoformat(row["confirmed_at"]) if row["confirmed_at"] else None,
            confirmation=confirmation,
            engine_version=ENGINE_VERSION,
            rules=request.rules.model_dump(mode="json"),
            limits={"day_db": request.limit_day_db, "night_db": request.limit_night_db},
            site=request.site.model_dump(mode="json"),
            actions=actions,
            source_request=request,
            result=stored_result,
        )

    def list_versions(self, review_id: str) -> tuple[ReviewSummary, ReviewRequest]:
        from .models import ReviewSummary

        with self._lock:
            review = self._require_review(review_id)
            rows = self._connection.execute(
                "SELECT * FROM versions WHERE review_id=? ORDER BY version", (review_id,)
            ).fetchall()
        request = ReviewRequest.model_validate(self._loads(review["request_json"]))
        summaries: list[VersionSummary] = []
        confirmed_version = None
        for row in rows:
            result = self.get_result(review_id, row["version"])
            if row["status"] == "confirmed":
                confirmed_version = row["version"]
            summaries.append(
                VersionSummary(
                    version=row["version"],
                    parent_version=row["parent_version"],
                    status=row["status"],
                    confirmed=row["status"] == "confirmed",
                    created_at=datetime.fromisoformat(row["created_at"]),
                    confirmed_at=(
                        datetime.fromisoformat(row["confirmed_at"]) if row["confirmed_at"] else None
                    ),
                    reviewer=row["reviewer"],
                    note=row["note"],
                    compute_status=result.status,
                    seal_hash=row["seal_hash"],
                    action_count=len(self._loads(row["actions_json"])),
                )
            )
        summary = ReviewSummary(
            review_id=review_id,
            title=review["title"],
            point_id=request.site.point_id,
            created_at=datetime.fromisoformat(review["created_at"]),
            latest_version=rows[-1]["version"],
            confirmed_version=confirmed_version,
            versions=summaries,
        )
        return summary, request

    def list_reviews(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT r.id, r.title, r.point_id, r.created_at,
                       MAX(v.version) AS latest_version,
                       SUM(CASE WHEN v.status='confirmed' THEN 1 ELSE 0 END) AS confirmed_count
                  FROM reviews r JOIN versions v ON v.review_id=r.id
                 GROUP BY r.id ORDER BY r.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
