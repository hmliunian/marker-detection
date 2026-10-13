import copy
import csv
import hashlib
import json
from dataclasses import asdict

import numpy as np
import pytest

from marker_detection import cli
from marker_detection.models import CameraCalibration, Episode, VideoPacket
from marker_detection.video import DecodedVideo


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Fixed detections exercise assembly; real quality rules audit the poses."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "episode.mcap"
    source.write_bytes(b"read-only acquisition fixture")
    output = tmp_path / "analysis"
    timestamps = np.arange(6, dtype=np.int64) * 100_000_000 + 1_790_000_000_000_000_123
    camera = CameraCalibration(
        64,
        48,
        np.array([[80.0, 0, 32], [0, 80, 24], [0, 0, 1]]),
        np.zeros(5),
        np.eye(3),
        np.array([[80.0, 0, 32, 0], [0, 80, 24, 0], [0, 0, 1, 0]]),
        frame_id="recorded_head_left",
    )
    episode = Episode(
        source=str(source),
        episode_id="fixture_episode",
        task_id="A_001",
        videos={"head_left": [VideoPacket(int(t), b"encoded") for t in timestamps]},
        cameras={"head_left": camera},
        warnings=["fixture reader warning"],
    )
    decoded = DecodedVideo(
        timestamps,
        [np.full((48, 64), 40 + i, dtype=np.uint8) for i in range(len(timestamps))],
        "cpu:fixture",
        warnings=["fixture decode warning"],
    )
    state = {
        "source": source,
        "output": output,
        "episode": episode,
        "points": [[i * 0.01, 0.0, 0.5] for i in range(len(timestamps))],
        "raw_points": None,
        "primary_status": "single",
        "load_calls": [],
        "decode_calls": [],
        "frame_hands": [],
    }

    def load(path, sidecar=None):
        state["load_calls"].append((path, sidecar))
        return state["episode"]

    def decode(packets, width, height, backend, device):
        state["decode_calls"].append((width, height, backend, device))
        return decoded

    def primary(data, config):
        state["activity_config"] = config
        status = state["primary_status"]
        return {
            "status": status,
            "hand": "right" if status == "single" else None,
            "reasons": [f"fixture_{status}"],
            "evidence": {},
            "experimental": True,
            "config": asdict(config),
        }

    class Estimator:
        stereo_warning = "fixture stereo warning"

        def __init__(self, reference, left_camera, right_camera, config):
            state["pose_config"] = config
            self.index = 0

        @staticmethod
        def detect(gray):
            return ["fixed tag evidence"]

        def frame(self, hand, timestamp_ns, detections, right_detections):
            state["frame_hands"].append(hand)
            point = copy.deepcopy(state["points"][self.index]) if hand else None
            raw = (
                copy.deepcopy(state["raw_points"][self.index])
                if state["raw_points"] is not None
                else copy.deepcopy(point)
            )
            self.index += 1
            return {
                "xyz_m": point,
                "raw_xyz_m": raw,
                "valid": point is not None,
                "reason": "ok" if point is not None else "primary_hand_unresolved",
                "solver": "fixed_observation",
                "detections": [],
            }

    monkeypatch.setattr(cli, "load_episode", load)
    monkeypatch.setattr(cli, "decode_video", decode)
    monkeypatch.setattr(cli, "select_primary_hand", primary)
    monkeypatch.setattr(cli, "PoseEstimator", Estimator)
    return state


def analyze(state, **kwargs):
    return cli.analyze(state["source"], state["output"], progress=False, **kwargs)


def test_accepted_analysis_writes_traceable_actions_previews_and_preserves_source(pipeline):
    source = pipeline["source"]
    original = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)
    report = analyze(pipeline, video_backend="cpu", gpu=2, sidecar="collection.json")
    output = pipeline["output"]
    stored = json.loads((output / "analysis.json").read_text())
    frames = json.loads((output / "frames.json").read_text())
    rows = list(csv.DictReader((output / "actions.csv").open()))
    assert report["decision"] == stored["decision"] == "accepted"
    assert report["primary_hand"] == "right"
    assert len(frames) == len(rows) == len(pipeline["points"])
    assert rows[2]["timestamp_ns"] == str(int(pipeline["episode"].videos["head_left"][2].timestamp_ns))
    assert rows[2]["x_m"] == "0.02"
    assert all(row["frame_valid"] == row["episode_accepted"] == "1" for row in rows)
    assert all((output / frame["preview"]).is_file() for frame in frames)
    assert all((output / frame["preview"]).read_bytes().startswith(b"\xff\xd8") for frame in frames)
    assert report["action"]["recorded_frame_id"] == "recorded_head_left"
    assert report["provenance"]["video_backends"]["head_left"] == "cpu:fixture"
    assert set(report["warnings"]) == {"fixture reader warning", "fixture decode warning", "fixture stereo warning"}
    assert pipeline["load_calls"] == [(source.resolve(), "collection.json")]
    assert pipeline["decode_calls"] == [(64, 48, "cpu", 2)]
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == original
    assert list(source.parent.iterdir()) == [source]


def test_quality_rejected_pose_never_becomes_a_valid_action_in_export(pipeline):
    pipeline["points"][2] = [4.0, 0.0, 0.5]
    report = analyze(pipeline)
    frames = json.loads((pipeline["output"] / "frames.json").read_text())
    rows = list(csv.DictReader((pipeline["output"] / "actions.csv").open()))
    assert report["decision"] == "rejected"
    assert frames[2]["valid"] is True  # Detection evidence remains available.
    assert frames[2]["quality"]["valid"] is False
    assert frames[2]["raw_xyz_m"] == [4.0, 0.0, 0.5]
    assert rows[2]["frame_valid"] == "0"
    assert rows[2]["x_m"] == rows[2]["y_m"] == rows[2]["z_m"] == ""
    assert "speed_exceeded" in rows[2]["reason"]
    assert all(row["episode_accepted"] == "0" for row in rows)


def test_nonfinite_raw_pose_is_rejected_and_saved_as_null_with_its_diagnosis(pipeline):
    pipeline["raw_points"] = copy.deepcopy(pipeline["points"])
    pipeline["raw_points"][2][0] = float("nan")
    report = analyze(pipeline)
    text = (pipeline["output"] / "frames.json").read_text()
    frames = json.loads(text)
    rows = list(csv.DictReader((pipeline["output"] / "actions.csv").open()))
    assert report["decision"] == "rejected"
    assert "raw_nonfinite_xyz" in report["summary"]["reasons"]
    assert "NaN" not in text and "Infinity" not in text
    assert frames[2]["raw_xyz_m"][0] is None
    assert "raw_nonfinite_xyz" in frames[2]["quality"]["issues"]
    assert rows[2]["frame_valid"] == "0"
    assert rows[2]["x_m"] == ""


@pytest.mark.parametrize(
    ("primary", "decision"), [("carry", "excluded_carry"), ("bimanual", "rejected"), ("unknown", "review")]
)
def test_non_single_hand_status_routes_episode_without_producing_actions(pipeline, primary, decision):
    pipeline["primary_status"] = primary
    report = analyze(pipeline)
    rows = list(csv.DictReader((pipeline["output"] / "actions.csv").open()))
    assert report["decision"] == decision
    assert report["reasons"] == [f"fixture_{primary}"]
    assert report["primary_hand"] is None
    assert all(hand is None for hand in pipeline["frame_hands"])
    assert all(row["episode_accepted"] == row["frame_valid"] == "0" for row in rows)
    assert all(row["primary_hand"] == row["x_m"] == "" for row in rows)


@pytest.mark.parametrize("as_file", [False, True])
def test_existing_output_is_never_overwritten_or_processed(pipeline, as_file):
    output = pipeline["output"]
    if as_file:
        output.write_text("existing results")
        sentinel = output
    else:
        output.mkdir()
        sentinel = output / "analysis.json"
        sentinel.write_text("existing results")
    with pytest.raises(ValueError, match="new or empty directory"):
        analyze(pipeline)
    assert sentinel.read_text() == "existing results"
    assert pipeline["load_calls"] == []


def test_source_directory_cannot_be_repurposed_as_output(pipeline):
    with pytest.raises(ValueError, match="new or empty|must not contain"):
        cli.analyze(pipeline["source"], pipeline["source"].parent, progress=False)
    assert pipeline["source"].read_bytes() == b"read-only acquisition fixture"
    assert pipeline["load_calls"] == []


@pytest.mark.parametrize("missing", ["videos", "cameras"])
def test_missing_head_left_input_fails_before_creating_results(pipeline, missing):
    getattr(pipeline["episode"], missing).clear()
    with pytest.raises(ValueError, match="Head-left video and its calibration"):
        analyze(pipeline)
    assert not pipeline["output"].exists()
    assert pipeline["decode_calls"] == []


@pytest.mark.parametrize("field", ["k", "d", "r", "p"])
def test_nonfinite_camera_calibration_is_not_sanitized_into_valid_input(pipeline, field):
    getattr(pipeline["episode"].cameras["head_left"], field).flat[0] = float("nan")
    with pytest.raises(ValueError, match="Camera calibration head_left contains nonfinite"):
        analyze(pipeline)
    assert not pipeline["output"].exists()
    assert pipeline["decode_calls"] == []


@pytest.mark.parametrize("explicit_quality_speed", [None, 0.05])
def test_settings_override_reaches_activity_pose_and_quality_and_is_recorded(
    pipeline, tmp_path, explicit_quality_speed
):
    settings = {
        "activity": {"open_reference_raw": 103},
        "pose": {"max_linear_speed_mps": 9.0},
    }
    if explicit_quality_speed is not None:
        settings["quality"] = {"max_speed_m_s": explicit_quality_speed}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    report = analyze(pipeline, settings=str(path))
    assert pipeline["activity_config"].open_reference_raw == 103
    assert pipeline["pose_config"].max_linear_speed_mps == 9.0
    assert report["config"]["activity"]["open_reference_raw"] == 103
    assert report["config"]["quality"]["max_speed_m_s"] == (9 if explicit_quality_speed is None else 0.05)
    assert report["decision"] == ("accepted" if explicit_quality_speed is None else "rejected")


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"unrecognized": {}}, "Settings must contain only"),
        ([], "Settings must contain only"),
        ({"quality": {"no_such_setting": 1}}, "no_such_setting"),
        ({"quality": {"min_coverage": 2}}, "min_coverage"),
        ({"pose": {"enable_velocity_gate": 1}}, "must be boolean"),
    ],
)
def test_invalid_settings_have_cli_error_without_processing_input(pipeline, tmp_path, capsys, settings, message):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    result = cli.main(
        ["analyze", str(pipeline["source"]), "--output", str(pipeline["output"]), "--settings", str(path)]
    )
    capture = capsys.readouterr()
    assert result == 2
    assert message in capture.err
    assert "Traceback" not in capture.err
    assert not capture.out
    assert not pipeline["output"].exists()
    assert pipeline["load_calls"] == []


def test_cli_analyze_returns_json_summary_and_keeps_progress_on_stderr(pipeline, capsys):
    result = cli.main(
        ["analyze", str(pipeline["source"]), "--output", str(pipeline["output"]), "--video-backend", "cpu"]
    )
    capture = capsys.readouterr()
    summary = json.loads(capture.out)
    assert result == 0
    assert summary["decision"] == "accepted"
    assert summary["frames"] == summary["valid_frames"] == 6
    assert summary["output"] == str(pipeline["output"].resolve())
    assert "fixture_episode: hand=right" in capture.err
