from conftest import make_payload


def test_valid_night_review_metrics_and_evidence(client):
    response = client.post("/reviews", json=make_payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "valid"
    segment = body["result"]["segments"][0]
    assert segment["phase"] == "night"
    assert segment["sample_count"] == 61
    assert segment["metrics"]["l90_db"] == 60.0
    assert segment["metrics"]["lmax_db"] == 60.0
    assert segment["metrics"]["background_level_db"] == 49.0
    assert segment["metrics"]["background_metric"] == "Leq"
    # Leq 59.72 - Leq background 49 = 10.72 dB -> -1 dB table correction.
    assert segment["metrics"]["background_correction_db"] == -1.0
    assert segment["metrics"]["corrected_leq_db"] > 55
    assert segment["metrics"]["exceeds_limit"] is True
    assert body["result"]["sample_index"]["raw_root"]
    assert body["result"]["calibration_version"]["valid"] is True


def test_revision_excludes_vehicle_and_is_compared_with_version_one(client):
    create = client.post("/reviews", json=make_payload()).json()
    review_id = create["review_id"]
    revision = client.post(
        f"/reviews/{review_id}/revisions",
        params={"parent_version": 1},
        json={
            "reviewer": "reviewer-a",
            "note": "remove passing lorry",
            "actions": [
                {
                    "type": "exclude",
                    "start": "2026-09-12T22:00:40Z",
                    "end": "2026-09-12T22:01:00Z",
                    "reason": "Heavy vehicle pass-by visible in field log and waveform.",
                    "label": "vehicle",
                }
            ],
        },
    )
    assert revision.status_code == 201, revision.text
    revised = revision.json()
    assert revised["version"] == 2
    assert revised["result"]["excluded_ranges"] == [
        ["2026-09-12T22:00:40Z", "2026-09-12T22:01:00Z"]
    ]
    # The raw count remains unchanged; only effective indexes shrink.
    index = revised["result"]["sample_index"]
    assert index["raw_count"] == 61
    assert index["effective_count"] == 59

    compare = client.get(f"/reviews/{review_id}/versions/1/compare/2").json()
    assert compare["added_actions"][0]["reason"].startswith("Heavy vehicle")
    assert compare["segments_changed"][0]["changes"]["sample_count"] == {"from": 61, "to": 59}

    evidence = client.get(f"/reviews/{review_id}/versions/2/evidence").json()
    assert evidence["actions"][0]["reviewer"] == "reviewer-a"
    assert evidence["confirmation"] is None
    assert evidence["result"]["sample_index"]["raw_count"] == 61


def test_confirmation_seals_version_and_blocks_reconfirmation(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    confirm = client.post(
        f"/reviews/{review_id}/versions/1/confirm",
        json={"reviewer": "witness-1", "note": "Sealed night segment."},
    )
    assert confirm.status_code == 200, confirm.text
    sealed = confirm.json()
    assert sealed["status"] == "confirmed"
    assert sealed["confirmation"]["seal_hash"]
    assert sealed["confirmation"]["sample_index"]["effective_root"]

    duplicate = client.post(
        f"/reviews/{review_id}/versions/1/confirm",
        json={"reviewer": "witness-2"},
    )
    assert duplicate.status_code == 409


def test_svg_curve_contains_excluded_band_and_equipment_annotation(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    client.post(
        f"/reviews/{review_id}/revisions",
        json={
            "reviewer": "reviewer-a",
            "actions": [
                {
                    "type": "exclude",
                    "start": "2026-09-12T22:00:40Z",
                    "end": "2026-09-12T22:01:00Z",
                    "reason": "passing vehicle",
                }
            ],
        },
    )
    svg_response = client.get(f"/reviews/{review_id}/versions/2/curve.svg")
    assert svg_response.status_code == 200
    assert svg_response.headers["content-type"] == "image/svg+xml"
    assert b"COMP-1" in svg_response.content
    assert b"#ffcccc" in svg_response.content


def test_boundary_move_requires_reason_and_must_stay_inside_original_run(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    response = client.post(
        f"/reviews/{review_id}/revisions",
        json={
            "reviewer": "reviewer-a",
            "actions": [
                {
                    "type": "move_boundary",
                    "equipment_id": "COMP-1",
                    "run_index": 0,
                    "side": "start",
                    "to": "2026-09-12T21:59:00Z",
                    "reason": "Attempt to extend before original interval.",
                }
            ],
        },
    )
    assert response.status_code == 201
    result = response.json()["result"]
    assert result["status"] == "blocked"
    assert "REVIEW_ACTION_INVALID" in {f["code"] for f in result["findings"]}
    # Original segment/evidence is still present for attribution.
    assert result["segments"][0]["status"] == "blocked"


def test_recompute_old_version_remains_available_after_new_revision(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    client.post(
        f"/reviews/{review_id}/revisions",
        json={
            "reviewer": "reviewer-a",
            "actions": [
                {
                    "type": "exclude",
                    "start": "2026-09-12T22:00:40Z",
                    "end": "2026-09-12T22:01:00Z",
                    "reason": "vehicle",
                }
            ],
        },
    )
    old = client.get(f"/reviews/{review_id}/versions/1").json()
    new = client.get(f"/reviews/{review_id}/versions/2").json()
    assert old["result"]["sample_index"]["effective_count"] == 61
    assert new["result"]["sample_index"]["effective_count"] == 59


def test_evidence_package_contains_immutable_source_request(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    evidence = client.get(f"/reviews/{review_id}/versions/1/evidence").json()
    assert evidence["source_request"]["samples"][0]["level_db"] == 55.0
    assert evidence["source_request"]["equipment_periods"][0]["equipment_id"] == "COMP-1"
    assert evidence["result"]["rule_hash"]
