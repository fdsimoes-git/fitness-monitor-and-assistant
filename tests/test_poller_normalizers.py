"""Tests for the Garmin Connect payload normalizers."""
import sys
import types

# Stub the `garminconnect` import so test collection doesn't require the
# library to be installed (it is heavy and only needed at runtime).
if "garminconnect" not in sys.modules:
    stub = types.ModuleType("garminconnect")

    class _Garmin:  # noqa: D401 — placeholder
        pass

    stub.Garmin = _Garmin
    sys.modules["garminconnect"] = stub

from src.garmin_poller import (  # noqa: E402
    normalize_hrv,
    normalize_sleep,
    normalize_stats,
    normalize_stress,
)


def test_normalize_stats_typical_payload():
    out = normalize_stats(
        {
            "restingHeartRate": 58,
            "maxHeartRate": 178,
            "averageHeartRateInBeatsPerMinute": 72,
            "totalSteps": 8421,
            "bodyBatteryMostRecentValue": 64,
        }
    )
    assert out == {
        "resting_hr": 58,
        "max_hr": 178,
        "avg_hr": 72,
        "steps": 8421,
        "body_battery": 64,
    }


def test_normalize_stats_handles_none_and_missing():
    out = normalize_stats(None)
    assert out["resting_hr"] is None
    assert out["steps"] is None


def test_normalize_sleep_dto_shape():
    out = normalize_sleep({"dailySleepDTO": {"sleepTimeSeconds": 27000}})
    assert out["sleep_seconds"] == 27000


def test_normalize_sleep_top_level_fallback():
    out = normalize_sleep({"sleepTimeSeconds": 21600})
    assert out["sleep_seconds"] == 21600


def test_normalize_sleep_missing():
    assert normalize_sleep(None)["sleep_seconds"] is None
    assert normalize_sleep({})["sleep_seconds"] is None


def test_normalize_stress_picks_avg():
    assert normalize_stress({"avgStressLevel": 33})["stress_avg"] == 33
    # legacy field name
    assert normalize_stress({"averageStressLevel": 41})["stress_avg"] == 41
    assert normalize_stress(None)["stress_avg"] is None


def test_normalize_hrv_summary_shape():
    out = normalize_hrv({"hrvSummary": {"lastNightAvg": 48}})
    assert out["hrv_overnight"] == 48


def test_normalize_hrv_top_level_fallback():
    out = normalize_hrv({"lastNightAvg": 51})
    assert out["hrv_overnight"] == 51


def test_normalize_hrv_missing():
    assert normalize_hrv(None)["hrv_overnight"] is None
