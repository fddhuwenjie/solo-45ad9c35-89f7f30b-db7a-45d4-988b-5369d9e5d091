from datetime import datetime, timedelta, timezone

from conftest import make_payload


def test_equipment_run_is_split_at_site_night_boundary(client):
    payload = make_payload()
    # UTC equipment run 21:00-23:00 is 05:00-07:00 in Shanghai, crossing the 06:00 day boundary.
    payload["site"]["time_zone"] = "Asia/Shanghai"
    payload["equipment_periods"][0].update(
        {"start": "2026-09-12T21:00:00Z", "end": "2026-09-12T23:00:00Z"}
    )
    start = datetime(2026, 9, 12, 21, 0, tzinfo=timezone.utc)
    payload["samples"] = [
        {
            "timestamp": (start + timedelta(seconds=60 * index)).isoformat().replace("+00:00", "Z"),
            "level_db": 58.0,
        }
        for index in range(121)
    ]
    payload["calibrations"] = [
        {"timestamp": "2026-09-12T20:55:00Z", "phase": "before", "expected_db": 94.0, "measured_db": 94.1},
        {"timestamp": "2026-09-12T23:05:00Z", "phase": "after", "expected_db": 94.0, "measured_db": 94.0},
    ]
    payload["weather"] = [
        {"timestamp": "2026-09-12T21:00:00Z", "wind_speed_ms": 1.0, "rain_mm_h": 0.0},
        {"timestamp": "2026-09-12T23:00:00Z", "wind_speed_ms": 1.0, "rain_mm_h": 0.0},
    ]
    payload["background_measurements"] = [
        {
            "id": "BG-NIGHT",
            "start": "2026-09-12T20:30:00Z",
            "end": "2026-09-12T20:40:00Z",
            "leq_db": 48.0,
            "l90_db": 48.0,
        },
        {
            "id": "BG-DAY",
            "start": "2026-09-12T23:10:00Z",
            "end": "2026-09-12T23:20:00Z",
            "leq_db": 48.0,
            "l90_db": 48.0,
        },
    ]
    payload["rules"]["weather_interpolation_seconds"] = 7200
    payload["rules"]["background_matching_seconds"] = 7200
    payload["limit_day_db"] = 60
    body = client.post("/reviews", json=payload).json()
    phases = sorted(segment["phase"] for segment in body["result"]["segments"])
    assert phases == ["day", "night"]
    assert body["status"] == "valid"
