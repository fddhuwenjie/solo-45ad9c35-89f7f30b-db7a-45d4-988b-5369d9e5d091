import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import app, get_db


@pytest.fixture()
def client():
    db = Database(":memory:")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    db.close()


def make_payload(**overrides):
    levels = [55.0] * 6 + [60.0] * 55
    samples = [
        {
            "timestamp": f"2026-09-12T22:{minute:02d}:{second:02d}Z",
            "level_db": level,
        }
        for index, level in enumerate(levels)
        for minute, second in [(index // 6, (index % 6) * 10)]
    ]
    payload = {
        "title": "Night compressor evidence",
        "site": {
            "point_id": "P-NIGHT-01",
            "time_zone": "UTC",
            "surface": "concrete",
            "microphone_height_m": 1.5,
        },
        "samples": samples,
        "calibrations": [
            {"timestamp": "2026-09-12T21:55:00Z", "phase": "before", "expected_db": 94.0, "measured_db": 94.1},
            {"timestamp": "2026-09-12T22:15:00Z", "phase": "after", "expected_db": 94.0, "measured_db": 94.0},
        ],
        "weather": [
            {"timestamp": "2026-09-12T22:05:00Z", "wind_speed_ms": 1.2, "rain_mm_h": 0.0}
        ],
        "equipment_periods": [
            {
                "equipment_id": "COMP-1",
                "name": "Compressor",
                "start": "2026-09-12T22:00:00Z",
                "end": "2026-09-12T22:10:00Z",
            }
        ],
        "background_measurements": [
            {
                "id": "BG-1",
                "start": "2026-09-12T22:15:00Z",
                "end": "2026-09-12T22:20:00Z",
                "leq_db": 49.0,
                "l90_db": 48.0,
                "target_equipment_id": "COMP-1",
            }
        ],
        "rules": {
            "max_sample_gap_seconds": 60,
            "min_segment_duration_seconds": 30,
            "max_wind_speed_ms": 5,
            "max_rain_mm_h": 0,
            "max_calibration_drift_db": 0.7,
            "background_matching_seconds": 7200,
            "required_surface": "concrete",
        },
        "limit_night_db": 55,
    }
    payload.update(overrides)
    return payload
