"""Geometry checks with independent projection and real raster tag detection.

Synthetic projections verify implementation geometry, not dataset accuracy.
"""

import copy
import gc

import cv2
import numpy as np
import pytest

from marker_detection.models import CameraCalibration
from marker_detection.pose import Detection, PoseConfig, PoseEstimator, load_reference_config


@pytest.fixture
def calibration():
    k = np.array([[600.0, 0, 320], [0, 600.0, 240], [0, 0, 1]])
    left = CameraCalibration(640, 480, k, np.zeros(5), np.eye(3), np.c_[k, np.zeros(3)])
    right = copy.deepcopy(left)
    right.p[0, 3] = -600.0 * 0.055
    return left, right


def rigid(rotation=(0.0, 0.0, 0.0), xyz=(0.0, 0.0, 0.8)):
    pose = np.eye(4)
    pose[:3, :3] = cv2.Rodrigues(np.asarray(rotation, dtype=float))[0]
    pose[:3, 3] = xyz
    return pose


def marker_pose(rotation=(0.35, 0.15, 0.1), xyz=(0.015, -0.02, 0.8)):
    pose = rigid(rotation, xyz)
    # Marker +Y is printed up and +Z points out of the paper toward the viewer.
    pose[:3, :3] = pose[:3, :3] @ np.diag([1.0, -1.0, -1.0])
    return pose


def project_marker(marker, camera_t_rig, camera, baseline=0.0):
    half = marker["marker_size_m"] / 2
    canonical = np.array([[-half, half, 0, 1], [half, half, 0, 1], [half, -half, 0, 1], [-half, -half, 0, 1]])
    # This projection uses explicit homogeneous transforms, independently of the
    # implementation's corner-conversion and PnP helper functions.
    xyz = (camera_t_rig @ np.asarray(marker["rig_T_marker"]) @ canonical.T).T[:, :3]
    xyz[:, 0] -= baseline
    pixels = cv2.projectPoints(xyz, np.zeros(3), np.zeros(3), camera.k, camera.d)[0].reshape(4, 2)
    return Detection(marker["id"], pixels)


def estimator(calibration, config=None, right=True, reference=None):
    left_camera, right_camera = calibration
    return PoseEstimator(reference or load_reference_config(), left_camera, right_camera if right else None, config)


@pytest.mark.parametrize("hand", ["left", "right"])
def test_single_marker_recovers_target_xyz_and_full_transform(calibration, hand):
    reference = load_reference_config()
    rig = next(item for item in reference["marker_geometry"]["rigs"] if item["name"] == hand)
    rig["rig_T_target"] = rigid((0.03, -0.08, 0), (0.01, -0.015, 0.025)).tolist()
    engine = estimator(calibration, reference=reference)
    marker = rig["markers"][0]
    camera_t_rig = marker_pose() @ np.linalg.inv(marker["rig_T_marker"])
    left = project_marker(marker, camera_t_rig, calibration[0])
    right = project_marker(marker, camera_t_rig, calibration[1], baseline=0.055)
    solved = engine.solve(hand, [left], [right])
    assert solved.reason == "ok"
    np.testing.assert_allclose(solved.pose, camera_t_rig, atol=1e-7)
    assert solved.error_px < 1e-7
    frame = engine.frame(hand, 1_790_000_000_000_000_001, [left], [right])
    expected = camera_t_rig @ rig["rig_T_target"]
    assert frame["valid"]
    np.testing.assert_allclose(frame["xyz_m"], expected[:3, 3], atol=1e-7)


@pytest.mark.parametrize("hand", ["left", "right"])
def test_joint_multimarker_pose_and_other_hand_observations(calibration, hand):
    engine = estimator(calibration)
    markers = engine.rigs[hand]["markers"][:3]
    camera_t_rig = marker_pose((0.45, -0.35, 0.15)) @ np.linalg.inv(markers[0]["rig_T_marker"])
    detections = [project_marker(marker, camera_t_rig, calibration[0]) for marker in markers]
    other_hand = "right" if hand == "left" else "left"
    other_marker = engine.rigs[other_hand]["markers"][0]
    detections.append(project_marker(other_marker, rigid(xyz=(0.7, 0.2, 1.2)), calibration[0]))
    result = engine.solve(hand, detections)
    assert result.reason == "ok"
    assert result.solver == "multi_sqpnp_lm"
    assert result.marker_ids == tuple(marker["id"] for marker in markers)
    np.testing.assert_allclose(result.pose, camera_t_rig, atol=1e-7)


def ambiguous_observations(engine, calibration):
    marker = engine.rigs["left"]["markers"][0]
    truth = marker_pose((0.22, 0.10, 0.0), (0, 0, 1.0)) @ np.linalg.inv(marker["rig_T_marker"])
    left = project_marker(marker, truth, calibration[0])
    right = project_marker(marker, truth, calibration[1], baseline=0.055)
    return truth, left, right


def test_ambiguous_single_tag_uses_stereo_and_recovers_pose(calibration):
    engine = estimator(calibration)
    truth, left, right = ambiguous_observations(engine, calibration)
    estimate = engine.solve("left", [left], [right])
    assert estimate.reason == "ok"
    assert estimate.solver == "single_ippe_stereo_scipy_lm"
    np.testing.assert_allclose(estimate.pose, truth, atol=1e-6)


@pytest.mark.parametrize(
    "has_calibration,reason", [(True, "ambiguous_pose_no_right_marker"), (False, "stereo_calibration_unavailable")]
)
def test_ambiguity_is_not_accepted_without_right_evidence(calibration, has_calibration, reason):
    engine = estimator(calibration, right=has_calibration)
    _, left, _ = ambiguous_observations(engine, calibration)
    estimate = engine.solve("left", [left])
    assert estimate.reason == reason
    assert estimate.pose is None


def test_duplicate_same_id_on_either_image_is_not_selected_by_margin(calibration):
    engine = estimator(calibration)
    _, left, right = ambiguous_observations(engine, calibration)
    strong_duplicate = Detection(left.id, left.corners + 70, decision_margin=999)
    estimate = engine.solve("left", [left, strong_duplicate], [right])
    assert estimate.reason == "marker_identity_ambiguous"
    assert estimate.pose is None
    estimate = engine.solve("left", [left], [right, right])
    assert estimate.reason == "marker_identity_ambiguous"
    assert estimate.pose is None


def test_missing_primary_marker_does_not_switch_to_other_hand(calibration):
    engine = estimator(calibration)
    marker = engine.rigs["right"]["markers"][0]
    observation = project_marker(marker, rigid(), calibration[0])
    result = engine.frame("left", 1_000_000_000, [observation])
    assert result["reason"] == "no_primary_marker"
    assert result["xyz_m"] is None
    assert not result["valid"]


def test_velocity_gate_retains_raw_xyz_and_uses_last_accepted_pose(calibration):
    engine = estimator(calibration)
    markers = engine.rigs["left"]["markers"][:2]
    truth = marker_pose((0.4, -0.3, 0.0)) @ np.linalg.inv(markers[0]["rig_T_marker"])

    def frame(time, x_delta):
        pose = truth.copy()
        pose[0, 3] += x_delta
        left = [project_marker(marker, pose, calibration[0]) for marker in markers]
        return engine.frame("left", time, left)

    assert frame(1_000_000_000, 0)["valid"]
    rejected = frame(1_010_000_000, 0.2)
    assert rejected["reason"] == "velocity_gate"
    assert rejected["xyz_m"] is None
    assert rejected["raw_xyz_m"] is not None
    assert rejected["gate_speed_m_s"] == pytest.approx(20)
    np.testing.assert_allclose(rejected["raw_xyz_m"], truth[:3, 3] + [0.2, 0, 0], atol=1e-6)
    recovered = frame(1_100_000_000, 0.02)
    assert recovered["valid"]
    assert recovered["gate_speed_m_s"] == pytest.approx(0.2)
    duplicate_time = frame(1_100_000_000, 0.02)
    assert duplicate_time["reason"] == "nonincreasing_timestamp"
    assert duplicate_time["raw_xyz_m"] is not None


@pytest.mark.parametrize("quarter_turns", [0, 1, 2, 3])
def test_real_apriltag_detection_corner_order_for_all_rotations(calibration, quarter_turns):
    engine = estimator(calibration)
    tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5), 0, 200)
    image = np.full((480, 640), 255, dtype=np.uint8)
    image[100:300, 200:400] = np.rot90(tag, quarter_turns)
    observations = engine.detect(image)
    assert len(observations) == 1 and observations[0].id == 0
    expected = np.roll(np.array([[200, 100], [400, 100], [400, 300], [200, 300]]), quarter_turns, axis=0)
    np.testing.assert_allclose(observations[0].corners, expected, atol=0.6)


def test_real_rendered_marker_recovers_target_position(calibration):
    # This is deliberately an end-to-end corner convention check using the real
    # detector, independently generated AprilTag pixels and known print size.
    config = PoseConfig(max_left_error_gap_px=0.0, enable_velocity_gate=False)
    engine = estimator(calibration, config=config, right=False)
    marker = engine.rigs["left"]["markers"][0]
    tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5), 0, 200)
    image = np.full((480, 640), 255, dtype=np.uint8)
    image[140:340, 220:420] = tag
    observations = engine.detect(image)
    solved = engine.frame("left", 100_000_000, observations)
    assert solved["valid"], solved
    depth = 600 * marker["marker_size_m"] / 200
    camera_t_marker = np.diag([1.0, -1.0, -1.0, 1.0])
    camera_t_marker[:3, 3] = [0, 0, depth]
    target = camera_t_marker @ np.linalg.inv(marker["rig_T_marker"])
    np.testing.assert_allclose(solved["xyz_m"], target[:3, 3], atol=0.002)


def test_stereo_does_not_assume_rectified_geometry_for_distorted_calibration(calibration):
    left, right = calibration
    left.d[0] = 0.05
    engine = PoseEstimator(load_reference_config(), left, right)
    assert engine.baseline is None
    assert "zero-distortion" in engine.stereo_warning


@pytest.mark.parametrize("run_detection", [False, True])
def test_repeated_estimator_lifecycle_does_not_corrupt_native_memory(calibration, run_detection):
    for _ in range(12):
        engine = estimator(calibration)
        if run_detection:
            engine.detect(np.full((120, 160), 255, dtype=np.uint8))
        del engine
        gc.collect()


@pytest.mark.parametrize("sharpening", [0.25, 0.625])
def test_native_detector_keeps_fractional_sharpening_and_close_is_idempotent(calibration, sharpening):
    reference = load_reference_config()
    reference["marker_geometry"]["decode_sharpening"] = sharpening
    engine = estimator(calibration, reference=reference)
    detector = engine.detector
    assert detector.tag_detector_ptr.contents.decode_sharpening == pytest.approx(sharpening)
    detector.close()
    detector.close()
    assert detector.tag_detector_ptr is None
    assert detector.tag_families == {}
    del detector, engine
    gc.collect()
