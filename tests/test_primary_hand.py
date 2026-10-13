import json

import numpy as np
import pytest

from marker_detection.models import Episode, ScalarSeries, VideoPacket
from marker_detection.primary_hand import ActivityConfig, select_primary_hand


def series(values, *, unit="unknown", timestamps=None):
    values = np.asarray(values, dtype=float)
    if values.ndim == 0:
        values = np.full(101, values)
    if timestamps is None:
        timestamps = np.arange(len(values), dtype=np.int64) * 10_000_000 + 1_790_000_000_000_000_000
    return ScalarSeries(np.asarray(timestamps), values, unit=unit)


def episode(left=102, right=102, *, left_contact=0, right_contact=0):
    return Episode(
        source="fixture",
        episode_id="activity",
        task_id="A_001",
        grippers={"left": series(left), "right": series(right)},
        tactile={
            "left_finger0": series(left_contact),
            "left_finger1": series(0),
            "right_finger0": series(right_contact),
            "right_finger1": series(0),
        },
    )


def test_stable_partial_opening_can_be_sustained_holding_without_tactile():
    # Supplied bottle episode has stable right position ~50 and all-zero touch.
    result = select_primary_hand(episode(right=50))
    assert (result["status"], result["hand"]) == ("single", "right")
    assert "sustained_partial_opening" in result["evidence"]["right"]["activity_reasons"]
    assert result["experimental"]


def test_two_static_open_grippers_do_not_count_as_bimanual_activity():
    result = select_primary_hand(episode())
    assert result["status"] == "unknown"
    assert result["hand"] is None
    assert not result["evidence"]["left"]["activity_supported"]
    assert not result["evidence"]["right"]["activity_supported"]


@pytest.mark.parametrize("missing_touch", [False, True])
def test_closed_static_zero_cannot_resolve_operating_hand(missing_touch):
    data = episode(left=0, right=0)
    if missing_touch:
        data.tactile.clear()
    result = select_primary_hand(data)
    assert result["status"] == "unknown"
    assert result["hand"] is None


def test_missing_touch_is_preserved_and_does_not_negate_opening_motion():
    data = episode(left=np.linspace(101, 0, 101))
    data.tactile.clear()
    result = select_primary_hand(data)
    assert (result["status"], result["hand"]) == ("single", "left")
    assert result["evidence"]["left"]["tactile"]["streams"]["finger0"]["present"] is False


def test_active_hand_with_unresolved_other_hand_is_not_forced_to_single():
    result = select_primary_hand(episode(left=0, right=50, right_contact=10))
    assert result["status"] == "unknown"
    assert result["reasons"] == ["other_hand_activity_unresolved"]


def test_one_responding_finger_is_sufficient_contact_evidence():
    result = select_primary_hand(episode(left=0, left_contact=5))
    assert (result["status"], result["hand"]) == ("single", "left")
    assert result["evidence"]["left"]["tactile"]["contact_supported"]


def test_both_hands_activity_is_rejected_as_bimanual_even_when_not_simultaneous():
    data = episode(left=0, right=0)
    data.tactile["left_finger0"] = series(np.r_[np.ones(30), np.zeros(71)])
    data.tactile["right_finger0"] = series(np.r_[np.zeros(71), np.ones(30)])
    result = select_primary_hand(data)
    assert result["status"] == "bimanual"
    assert result["hand"] is None


def test_carry_task_has_separate_status():
    data = episode(left=50, right=50)
    data.task_id = "A_003"
    result = select_primary_hand(data)
    assert result["status"] == "carry"
    assert result["hand"] is None


def test_documented_metre_conversion_preserves_activity_without_relabeling_raw_units():
    old = episode(left=102, right=50)
    converted = episode(left=102, right=50)
    for values in converted.grippers.values():
        values.values /= 1000
        values.unit = "m"
    old_result = select_primary_hand(old)
    new_result = select_primary_hand(converted)
    assert old_result["hand"] == new_result["hand"] == "right"
    assert old_result["evidence"]["left"]["position"]["unit"] == "unknown"
    assert new_result["evidence"]["left"]["position"]["unit"] == "m"
    assert old_result["evidence"]["right"]["position"]["raw_min"] == 50
    assert new_result["evidence"]["right"]["position"]["raw_min"] == 0.05


def test_unknown_scale_does_not_guess_metres_from_a_small_raw_value():
    result = select_primary_hand(episode(left=0.102, right=0.050))
    assert result["status"] == "unknown"


def test_missing_other_hand_position_is_not_treated_as_open_idle():
    data = episode(right=50)
    del data.grippers["left"]
    result = select_primary_hand(data)
    assert result["status"] == "unknown"
    assert "missing_position" in result["evidence"]["left"]["position"]["issues"]


def test_isolated_contact_spike_and_sensor_gap_do_not_make_sustained_contact():
    data = episode(left=0)
    start = 1_790_000_000_000_000_000
    data.tactile["left_finger0"] = series([10, 10, 10, 10], timestamps=np.array([0, 10, 900, 910]) * 1_000_000 + start)
    result = select_primary_hand(data)
    assert result["status"] == "unknown"
    assert not result["evidence"]["left"]["tactile"]["contact_supported"]


def test_preparation_contact_outside_head_video_window_does_not_choose_hand():
    data = episode(left=0)
    data.tactile["left_finger0"] = series(np.r_[np.ones(30), np.zeros(71)])
    start = int(data.grippers["left"].timestamps_ns[0])
    data.videos["head_left"] = [VideoPacket(start + 500_000_000, b""), VideoPacket(start + 1_000_000_000, b"")]
    result = select_primary_hand(data)
    assert result["status"] == "unknown"


def test_nonfinite_sensor_values_are_not_in_json_evidence():
    data = episode(left=50)
    data.grippers["right"].values[0] = np.nan
    result = select_primary_hand(data)
    assert result["status"] == "unknown"
    json.dumps(result, allow_nan=False)


def test_negative_contact_norm_and_time_reversal_cannot_supply_activity():
    data = episode(left=0)
    data.tactile["left_finger0"] = series(10)
    data.tactile["left_finger0"].timestamps_ns[50] = data.tactile["left_finger0"].timestamps_ns[49]
    data.tactile["left_finger1"] = series(-2)
    result = select_primary_hand(data)
    assert result["status"] == "unknown"


def test_activity_threshold_configuration_rejects_nonfinite_or_inconsistent_values():
    with pytest.raises(ValueError):
        ActivityConfig(contact_threshold=float("nan"))
    with pytest.raises(ValueError):
        ActivityConfig(partial_max_fraction=0.99)
