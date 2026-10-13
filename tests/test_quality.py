import copy
import json

import pytest

from marker_detection.quality import QualityConfig, evaluate_trajectory

ORIGIN = 1_790_000_000_000_000_000


def trajectory(times_s, positions=None):
    if positions is None:
        positions = [[0.1 * time, 0.0, 0.5] for time in times_s]
    return [
        {
            "frame_index": index,
            "timestamp_ns": ORIGIN + round(time * 1e9),
            "time_s": time,
            "xyz_m": position,
            "raw_xyz_m": copy.deepcopy(position),
            "valid": position is not None,
            "reason": "ok" if position is not None else "no_marker",
        }
        for index, (time, position) in enumerate(zip(times_s, positions, strict=True))
    ]


def relaxed(**overrides):
    return QualityConfig(**{"min_coverage": 0.0, "max_gap_s": 10.0, **overrides})


def test_velocity_uses_integer_nanosecond_differences_at_variable_frame_rates():
    frames = trajectory([0, 0.01, 0.06, 0.10])
    for frame in frames:
        frame["time_s"] = 0  # Display timing cannot replace acquisition timing.
    result = evaluate_trajectory(frames)
    assert result["status"] == "accepted"
    assert result["max_speed_m_s"] == pytest.approx(0.1)
    assert result["max_acceleration_m_s2"] == pytest.approx(0.0, abs=1e-12)
    assert frames[1]["quality"]["dt_s"] == 0.01
    assert frames[0]["quality"]["speed_m_s"] is None


@pytest.mark.parametrize(
    ("missing", "location", "expected_gap"),
    [([0, 1], "leading", 0.2), ([8, 9], "trailing", 0.2), ([4, 5], "internal", 0.2), ([9], "trailing", 0.1)],
)
def test_missing_intervals_include_first_and_last_frames(missing, location, expected_gap):
    frames = trajectory([i / 10 for i in range(10)])
    for index in missing:
        frames[index].update(xyz_m=None, raw_xyz_m=None, valid=False, reason="no_marker")
    result = evaluate_trajectory(frames, relaxed())
    assert result["longest_gap_s"] == pytest.approx(expected_gap)
    assert result["gap_intervals"][0]["location"] == location
    assert result["coverage"] == pytest.approx((10 - len(missing)) / 10)


def test_gap_blocks_ordinary_velocity_and_acceleration_but_records_reappearance():
    frames = trajectory([0, 0.1, 0.2, 0.3, 0.4], [[0, 0, 0.5], [0.01, 0, 0.5], None, [0.03, 0, 0.5], [0.04, 0, 0.5]])
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "accepted"
    quality = frames[3]["quality"]
    assert quality["speed_m_s"] is None
    assert quality["acceleration_m_s2"] is None
    assert quality["reappearance_displacement_m"] == pytest.approx(0.02)
    assert quality["reappearance_elapsed_s"] == pytest.approx(0.2)
    assert frames[4]["quality"]["speed_m_s"] == pytest.approx(0.1)
    assert frames[4]["quality"]["acceleration_m_s2"] is None


def test_missing_longer_than_limit_rejects_whole_episode():
    frames = trajectory([i / 10 for i in range(20)])
    for index in range(5, 12):
        frames[index].update(xyz_m=None, raw_xyz_m=None, valid=False)
    result = evaluate_trajectory(frames, QualityConfig(min_coverage=0.0))
    assert result["status"] == "rejected"
    assert "missing_interval_exceeds_threshold" in result["reasons"]
    assert len(frames) == 20


def test_coverage_checks_elapsed_time_as_well_as_frame_count():
    frames = trajectory([0, 0.1, 0.2, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8])
    frames[2].update(xyz_m=None, raw_xyz_m=None, valid=False)
    result = evaluate_trajectory(frames, QualityConfig(max_gap_s=10))
    assert result["coverage"] == 0.9
    assert result["time_coverage"] < 0.5
    assert result["status"] == "rejected"
    assert "coverage_below_threshold" in result["reasons"]


def test_spatial_jump_rejects_and_preserves_both_original_positions():
    frames = trajectory([0, 0.1, 0.2, 0.3], [[0, 0, 0.5], [0.01, 0, 0.5], [1, 0, 0.5], [0.03, 0, 0.5]])
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert "speed_exceeded" in frames[2]["quality"]["issues"]
    assert frames[2]["xyz_m"] == [1, 0, 0.5]
    assert frames[2]["valid"] is True
    assert frames[2]["quality"]["valid"] is False


def test_upstream_gate_cannot_hide_anomalous_raw_pose_behind_good_coverage():
    frames = trajectory([i / 10 for i in range(40)])
    frames[20].update(valid=False, xyz_m=None, raw_xyz_m=[9, 0, 0.5], reason="velocity_gate")
    result = evaluate_trajectory(frames)
    assert result["coverage"] > 0.9
    assert result["status"] == "rejected"
    assert result["max_raw_speed_m_s"] > 80
    assert "upstream_anomaly" in frames[20]["quality"]["issues"]
    assert "raw_speed_exceeded" in frames[20]["quality"]["issues"]


def test_raw_jump_is_caught_without_a_named_upstream_gate():
    frames = trajectory([0, 0.1, 0.2, 0.3])
    frames[2].update(valid=False, xyz_m=None, raw_xyz_m=[9, 0, 0.5], reason="quality_rejected")
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert result["max_raw_speed_m_s"] > 80


def test_impossible_reappearance_is_diagnosed_without_drawing_a_continuous_segment():
    frames = trajectory([0, 0.1, 0.2, 0.3], [[0, 0, 1], None, [9, 0, 1], [9.01, 0, 1]])
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert frames[2]["quality"]["speed_m_s"] is None
    assert "reappearance_speed_exceeded" in frames[2]["quality"]["issues"]


@pytest.mark.parametrize("times", [[0, 0.1, 0.1, 0.3], [0, 0.2, 0.1, 0.3]])
def test_duplicate_or_reversed_timestamp_explicitly_fails(times):
    frames = trajectory(times)
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert "nonincreasing_timestamp" in result["reasons"]
    assert frames[2]["quality"]["speed_m_s"] is None


@pytest.mark.parametrize("bad_timestamp", [None, float(ORIGIN), True])
def test_missing_or_noninteger_timestamp_is_not_silently_repaired(bad_timestamp):
    frames = trajectory([0, 0.1, 0.2, 0.3])
    frames[1]["timestamp_ns"] = bad_timestamp
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert "invalid_timestamp" in result["reasons"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_raw_coordinate_rejects_even_when_filtered_pose_is_good(coordinate):
    frames = trajectory([0, 0.1, 0.2, 0.3])
    frames[1]["raw_xyz_m"][0] = coordinate
    result = evaluate_trajectory(frames, relaxed())
    assert result["status"] == "rejected"
    assert "raw_nonfinite_xyz" in result["reasons"]
    json.dumps(result, allow_nan=False)
    json.dumps([frame["quality"] for frame in frames], allow_nan=False)


def test_all_missing_video_counts_full_observation_duration():
    frames = trajectory([0, 0.1, 0.2, 0.3], [None] * 4)
    result = evaluate_trajectory(frames)
    assert result["status"] == "rejected"
    assert result["longest_gap_s"] == pytest.approx(0.4)
    assert result["coverage"] == 0
    assert result["max_speed_m_s"] is None


def test_empty_or_single_frame_is_review_not_successful_continuity():
    assert evaluate_trajectory([])["status"] == "review"
    assert evaluate_trajectory(trajectory([0]))["status"] == "review"


def test_strict_json_summary_of_sparse_video_contains_no_nan():
    result = evaluate_trajectory(trajectory([0, 0.1, 0.2], [None, None, None]))
    json.dumps(result, allow_nan=False)


def test_invalid_thresholds_fail_before_running():
    with pytest.raises(ValueError):
        QualityConfig(max_speed_m_s=float("nan"))
    with pytest.raises(ValueError):
        QualityConfig(min_coverage=1.2)
