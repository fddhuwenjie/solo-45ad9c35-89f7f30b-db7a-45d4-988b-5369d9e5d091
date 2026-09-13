from datetime import datetime, timedelta, timezone

from app.engine import exceeded_level_db
from conftest import make_payload


def test_l90_is_level_exceeded_for_ninety_percent_of_duration(client):
    # Ten equal-duration levels from 10 through 100 dB. The level exceeded
    # for 90% of the time is the lowest level; a P90 implementation returns 90.
    payload = make_payload()
    start = datetime(2026, 9, 12, 22, 0, tzinfo=timezone.utc)
    samples = []
    for group, level in enumerate(range(10, 101, 10)):
        for tick in range(6):
            samples.append(
                {
                    "timestamp": (start + timedelta(seconds=10 * (group * 6 + tick)))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "level_db": float(level),
                }
            )
    payload["samples"] = samples
    payload["equipment_periods"][0].update(
        {"start": start.isoformat().replace("+00:00", "Z"), "end": "2026-09-12T22:09:50Z"}
    )
    payload["background_measurements"][0].update({"leq_db": 0.0, "l90_db": 0.0})
    body = client.post("/reviews", json=payload).json()
    segment = body["result"]["segments"][0]
    assert body["status"] == "valid"
    assert segment["metrics"]["l90_db"] == 10.0
    assert segment["metrics"]["l90_db"] != 90.0
    assert segment["metrics"]["lmax_db"] == 100.0


def test_weighted_l90_helper_uses_exceedance_not_high_percentile():
    levels = [float(value) for value in range(10, 101)]
    # 82 of 91 equal-duration bins are strictly above 18 dB (90.1%); a P90
    # implementation instead returns 90 dB.
    assert exceeded_level_db(levels, [1.0] * len(levels), 0.90) == 18.0
    # Unequal weights must be duration weighted: more than 90% is above 10.
    assert exceeded_level_db([10.0, 50.0], [5.0, 95.0], 0.90) == 10.0
    assert exceeded_level_db([10.0, 50.0], [10.0, 90.0], 0.90) == 10.0


def test_requested_minimum_background_difference_controls_correction(client):
    payload = make_payload()
    # Valid source Leq is about 59.7 dB and the background is 49 dB.
    # A requested 11 dB minimum must block; ignoring it and using the
    # historical default would incorrectly select the >=10 dB correction.
    payload["rules"]["min_background_difference_db"] = 11.0
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    finding = next(
        f for f in body["result"]["findings"]
        if f["code"] == "BACKGROUND_DIFFERENCE_INSUFFICIENT"
    )
    assert finding["period"] == ["2026-09-12T22:00:00Z", "2026-09-12T22:10:00Z"]
    assert finding["missing_basis"]
    assert "minimum 11.0 dB" in finding["message"]

    interference = client.get(
        f"/reviews/{body['review_id']}/versions/1/background-interference"
    ).json()[0]
    assert interference["sufficient"] is False
    assert interference["difference_db"] == 10.72
    assert interference["correction_db"] is None
    segment = body["result"]["segments"][0]
    assert segment["metrics"]["leq_db"] is None
    assert segment["metrics"]["corrected_leq_db"] is None


def test_time_order_findings_always_emit_start_before_end(client):
    payload = make_payload()
    payload["samples"] = list(reversed(payload["samples"]))
    body = client.post("/reviews", json=payload).json()
    assert body["status"] == "blocked"
    periods = [
        f["period"]
        for f in body["result"]["findings"]
        if f["code"] == "SAMPLE_TIME_ORDER"
    ]
    assert periods
    assert all(period[0] <= period[1] for period in periods)
    # Every descending adjacent pair collapses to one chronologically ordered span.
    assert periods == [["2026-09-12T22:00:00Z", "2026-09-12T22:10:00Z"]]
