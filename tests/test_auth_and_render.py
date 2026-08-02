from whoop_claude_sync.auth import extract_code
from whoop_claude_sync.render import render_brief, render_workouts


def test_extract_code_from_url():
    url = "https://localhost:8787/callback?code=abc123&state=xyz"
    assert extract_code(url) == "abc123"


def test_extract_raw_code():
    assert extract_code("plain-code") == "plain-code"


def test_render_brief_with_sample_data():
    data = {
        "recovery": [
            {
                "cycle_id": 1,
                "created_at": "2026-08-01T06:00:00.000Z",
                "score": {
                    "recovery_score": 72,
                    "hrv_rmssd_milli": 65.2,
                    "resting_heart_rate": 52,
                },
            }
        ],
        "sleep": [
            {
                "id": "s1",
                "nap": False,
                "start": "2026-07-31T23:00:00.000Z",
                "end": "2026-08-01T07:00:00.000Z",
                "score": {
                    "sleep_performance_percentage": 88,
                    "sleep_efficiency_percentage": 91,
                    "stage_summary": {
                        "total_in_bed_time_milli": 8 * 3600 * 1000,
                        "total_awake_time_milli": 30 * 60 * 1000,
                        "total_light_sleep_time_milli": 4 * 3600 * 1000,
                        "total_slow_wave_sleep_time_milli": 90 * 60 * 1000,
                        "total_rem_sleep_time_milli": 100 * 60 * 1000,
                    },
                },
            }
        ],
        "cycles": [
            {
                "id": 1,
                "start": "2026-08-01T07:00:00.000Z",
                "score": {"strain": 9.4, "average_heart_rate": 70},
            }
        ],
        "workouts": [],
    }
    md = render_brief(data, profile={"first_name": "Luv", "last_name": "S"})
    assert "Recovery:** 72%" in md
    assert "Athlete:** Luv S" in md


def test_render_workouts_empty():
    md = render_workouts([])
    assert "No workouts" in md
