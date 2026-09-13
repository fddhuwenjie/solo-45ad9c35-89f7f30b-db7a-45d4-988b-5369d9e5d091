from conftest import make_payload


def test_calibration_drift_failure_endpoint(client):
    payload = make_payload()
    payload["calibrations"][1]["measured_db"] = 95.2
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    failures = client.get(f"/reviews/{body['review_id']}/versions/1/calibration-failures").json()
    codes = {f["phase"] for f in failures}
    assert "after" in codes
    assert "between" in codes
    assert all(abs(f["drift_db"]) > 0.7 for f in failures if f["drift_db"] is not None)
    # No usable metrics are emitted for calibration-invalid evidence.
    assert all(segment["metrics"]["leq_db"] is None for segment in body["result"]["segments"])


def test_insufficient_background_margin_blocks_and_reports_basis(client):
    payload = make_payload()
    payload["background_measurements"][0]["leq_db"] = 58.0
    payload["background_measurements"][0]["l90_db"] = 58.0
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    interference = client.get(
        f"/reviews/{body['review_id']}/versions/1/background-interference"
    ).json()
    assert interference[0]["sufficient"] is False
    assert interference[0]["difference_db"] < 3
    assert any(
        f["code"] == "BACKGROUND_DIFFERENCE_INSUFFICIENT"
        and f["missing_basis"]
        for f in body["result"]["findings"]
    )


def test_sample_gap_returns_affected_period_without_inventing_full_metrics(client):
    payload = make_payload()
    payload["samples"] = [sample for sample in payload["samples"] if sample["timestamp"]
                          not in {"2026-09-12T22:05:00Z", "2026-09-12T22:05:10Z",
                                  "2026-09-12T22:05:20Z", "2026-09-12T22:05:30Z",
                                  "2026-09-12T22:05:40Z", "2026-09-12T22:05:50Z"}]
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    gap = next(f for f in body["result"]["findings"] if f["code"] == "SAMPLING_GAP")
    assert gap["period"] == ["2026-09-12T22:04:50Z", "2026-09-12T22:06:00Z"]
    assert gap["missing_basis"]


def test_wind_exceedance_and_site_mismatch_return_periods(client):
    payload = make_payload()
    payload["weather"][0]["wind_speed_ms"] = 8.0
    payload["site"]["surface"] = "grass"
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    codes = {f["code"] for f in body["result"]["findings"]}
    assert "WEATHER_RULE_EXCEEDED" in codes
    assert "SITE_CONDITION_MISMATCH" in codes
    weather_finding = next(f for f in body["result"]["findings"] if f["code"] == "WEATHER_RULE_EXCEEDED")
    assert weather_finding["period"][0] == "2026-09-12T22:00:00Z"


def test_time_reversal_is_blocker_and_preserves_raw_root(client):
    payload = make_payload()
    payload["samples"][10], payload["samples"][11] = payload["samples"][11], payload["samples"][10]
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    assert any(f["code"] == "SAMPLE_TIME_ORDER" for f in body["result"]["findings"])
    assert body["result"]["sample_index"]["raw_count"] == 61
    assert body["result"]["sample_index"]["effective_count"] == 0


def test_blocked_result_cannot_be_confirmed(client):
    payload = make_payload()
    payload["calibrations"][1]["measured_db"] = 95.2
    body = client.post("/reviews", json=payload).json()
    response = client.post(
        f"/reviews/{body['review_id']}/versions/1/confirm",
        json={"reviewer": "witness"},
    )
    assert response.status_code == 409


def test_rejection_requires_review_reason(client):
    review_id = client.post("/reviews", json=make_payload()).json()["review_id"]
    response = client.post(
        f"/reviews/{review_id}/revisions",
        json={
            "reviewer": "reviewer-a",
            "actions": [
                {
                    "type": "exclude",
                    "start": "2026-09-12T22:00:40Z",
                    "end": "2026-09-12T22:01:00Z",
                    "reason": "",
                }
            ],
        },
    )
    assert response.status_code == 422
