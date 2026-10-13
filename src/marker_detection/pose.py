"""Independent Python implementation of the documented TinyNav marker geometry.

AprilTag and OpenCV execute on CPU. The stereo objective matches the reference;
SciPy optimizes a rotation vector instead of Ceres' quaternion parameterization.
"""

import json
from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import cv2
import numpy as np
from pupil_apriltags import Detector
from scipy.optimize import least_squares

from .models import CameraCalibration

MARKER_T_SOLVER = np.diag([1.0, -1.0, -1.0, 1.0])


class AprilTagDetector(Detector):
    """Release decoder tables before their families (pupil 1.0.4 reverses this).

    The upstream wrapper also casts sharpening to int; preserve the reference
    floating-point value. No installed dependency files are modified.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tag_detector_ptr.contents.decode_sharpening = float(kwargs.get("decode_sharpening", 0.25))

    def close(self):
        if getattr(self, "tag_detector_ptr", None) is None:
            return
        self.libc.apriltag_detector_destroy.restype = None
        self.libc.apriltag_detector_destroy(self.tag_detector_ptr)
        self.tag_detector_ptr = None
        for name, family in self.tag_families.items():
            destroy = getattr(self.libc, f"{name}_destroy")
            destroy.restype = None
            destroy(family)
        self.tag_families.clear()

    def __del__(self):
        self.close()


def load_reference_config(path: str | Path | None = None) -> dict:
    if path is None:
        bundled = resources.files("marker_detection").joinpath("data/UMI_v3.json")
        if bundled.is_file():
            return json.loads(bundled.read_text())
        path = Path(__file__).resolve().parents[2] / "configs/UMI_v3.json"
    return json.loads(Path(path).read_text())


@dataclass(frozen=True)
class PoseConfig:
    stereo_sync_slop_sec: float = 0.005
    max_left_error_gap_px: float = 0.25
    min_candidate_rotation_deg: float = 10.0
    max_reprojection_error_px: float = 1.0
    max_linear_speed_mps: float = 1.5
    max_angular_speed_degps: float = 360.0
    enable_outlier_rejection: bool = True
    max_single_marker_deviation_m: float = 0.05
    enable_velocity_gate: bool = True

    def __post_init__(self):
        for key, value in vars(self).items():
            if key.startswith("enable_"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
            elif not np.isfinite(value) or value < 0:
                raise ValueError(f"{key} must be finite and nonnegative")

    @classmethod
    def from_reference(cls, reference: dict):
        settings = reference.get("marker_rig_pose_node", {}).get("ros__parameters", {})
        return cls(**{key: value for key, value in settings.items() if key in cls.__dataclass_fields__})


@dataclass
class Detection:
    id: int
    corners: np.ndarray  # Tag-oriented TL, TR, BR, BL, in original image pixels.
    decision_margin: float = 100.0
    hamming: int = 0

    def as_dict(self, selected: bool = False) -> dict:
        return {
            "id": self.id,
            "corners": self.corners.tolist(),
            "selected": selected,
            "decision_margin": self.decision_margin,
            "hamming": self.hamming,
        }


@dataclass
class Estimate:
    pose: np.ndarray | None = None
    error_px: float | None = None
    marker_ids: tuple[int, ...] = ()
    reason: str = "no_primary_marker"
    solver: str = "none"
    dropped_ids: tuple[int, ...] = ()


def transform(rvec: np.ndarray, tvec: np.ndarray) -> np.ndarray:
    result = np.eye(4)
    result[:3, :3] = cv2.Rodrigues(np.asarray(rvec, dtype=float))[0]
    result[:3, 3] = np.asarray(tvec).reshape(3)
    return result


def rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    cosine = (np.trace(first[:3, :3].T @ second[:3, :3]) - 1.0) / 2.0
    return float(np.rad2deg(np.arccos(np.clip(cosine, -1.0, 1.0))))


def marker_points(marker: dict, solver: bool = False) -> np.ndarray:
    half = marker["marker_size_m"] / 2.0
    points = np.array([[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]])
    if solver:
        return points @ MARKER_T_SOLVER[:3, :3]
    pose = np.asarray(marker["rig_T_marker"], dtype=float)
    return points @ pose[:3, :3].T + pose[:3, 3]


def _project(points: np.ndarray, pose: np.ndarray, camera: CameraCalibration) -> np.ndarray:
    return cv2.projectPoints(points, cv2.Rodrigues(pose[:3, :3])[0], pose[:3, 3], camera.k, camera.d)[0].reshape(-1, 2)


def _error(points: np.ndarray, pixels: np.ndarray, pose: np.ndarray, camera: CameraCalibration) -> float:
    return float(np.sqrt(np.mean(np.sum((_project(points, pose, camera) - pixels) ** 2, axis=1))))


class PoseEstimator:
    def __init__(
        self,
        reference: dict,
        left: CameraCalibration,
        right: CameraCalibration | None = None,
        config: PoseConfig | None = None,
    ):
        self.geometry = reference["marker_geometry"]
        self.config = config or PoseConfig.from_reference(reference)
        self.left, self.right = left, right
        self.rigs = {rig["name"]: rig for rig in self.geometry["rigs"]}
        if set(self.rigs) != {"left", "right"}:
            raise ValueError("Geometry must define exactly left and right rigs")
        ids = []
        for rig in self.rigs.values():
            self._check_transform(rig["rig_T_target"])
            if not rig["markers"]:
                raise ValueError("Rig has no markers")
            for marker in rig["markers"]:
                ids.append(marker["id"])
                self._check_transform(marker["rig_T_marker"])
                if not np.isfinite(marker["marker_size_m"]) or marker["marker_size_m"] <= 0:
                    raise ValueError("Marker size must be positive metres")
        if len(ids) != len(set(ids)):
            raise ValueError("Marker IDs must be unique across rigs")
        for camera in (left, right):
            if camera is None:
                continue
            if camera.distortion_model not in {"", "plumb_bob", "rational_polynomial"}:
                raise ValueError(f"Unsupported distortion model: {camera.distortion_model}")
            if camera.k.shape != (3, 3) or not np.isfinite(camera.k).all() or min(camera.k[0, 0], camera.k[1, 1]) <= 0:
                raise ValueError("Invalid camera intrinsic matrix")
            if not np.isfinite(camera.d).all() or camera.d.size not in {0, 4, 5, 8, 12, 14}:
                raise ValueError("Invalid OpenCV distortion coefficients")
        settings = reference.get("marker_rig_pose_node", {}).get("ros__parameters", {})
        override = settings.get("image_scale_override", -1)
        self.scale = override if override > 0 else self.geometry.get("image_scale", 1.0)
        if not np.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("Image scale must be positive")
        self.detector = AprilTagDetector(
            families=self.geometry["apriltag_family"],
            nthreads=self.geometry.get("nthreads", 1),
            quad_decimate=self.geometry.get("quad_decimate", 1.0),
            quad_sigma=self.geometry.get("quad_sigma", 0.0),
            refine_edges=int(self.geometry.get("refine_edges", True)),
            decode_sharpening=self.geometry.get("decode_sharpening", 0.25),
        )
        self.baseline, self.stereo_warning = self._stereo_model()
        self.last_pose = None
        self.last_timestamp_ns = None

    @staticmethod
    def _check_transform(value):
        pose = np.asarray(value, dtype=float)
        if (
            pose.shape != (4, 4)
            or not np.isfinite(pose).all()
            or not np.allclose(pose[3], [0, 0, 0, 1])
            or not np.allclose(pose[:3, :3].T @ pose[:3, :3], np.eye(3), atol=1e-5)
            or not np.isclose(np.linalg.det(pose[:3, :3]), 1, atol=1e-5)
        ):
            raise ValueError("Geometry contains an invalid rigid transform")

    def _stereo_model(self) -> tuple[float | None, str | None]:
        left, right = self.left, self.right
        if right is None:
            return None, "No right calibration: ambiguous single-tag poses stay missing"
        if (
            left.p.shape != (3, 4)
            or right.p.shape != (3, 4)
            or not np.isfinite(left.p).all()
            or not np.isfinite(right.p).all()
        ):
            return None, "Stereo projection matrices are invalid"
        if (
            not np.allclose(left.d, 0)
            or not np.allclose(right.d, 0)
            or not np.allclose(left.k, right.k)
            or not np.allclose(left.k, left.p[:, :3])
            or not np.allclose(right.k, right.p[:, :3])
            or not np.allclose(left.p[:, 3], 0)
            or not np.allclose(right.p[1:, 3], 0)
        ):
            return None, "Calibration does not match TinyNav's zero-distortion horizontal stereo model"
        baseline = float(-right.p[0, 3] / right.k[0, 0])
        if baseline <= 0:
            return None, "Stereo baseline must be positive"
        return baseline, None

    def detect(self, gray: np.ndarray) -> list[Detection]:
        gray = np.ascontiguousarray(gray, dtype=np.uint8)
        scaled = cv2.resize(gray, None, fx=self.scale, fy=self.scale) if self.scale != 1 else gray
        return [
            Detection(
                int(tag.tag_id), tag.corners[[1, 0, 3, 2]] / self.scale, float(tag.decision_margin), int(tag.hamming)
            )
            for tag in self.detector.detect(scaled)
            if tag.hamming <= self.geometry.get("max_hamming", 1)
            and tag.decision_margin >= self.geometry.get("min_decision_margin", 20.0)
        ]

    def _candidates(self, marker: dict, detection: Detection) -> list[tuple]:
        points = marker_points(marker, solver=True)
        result = cv2.solvePnPGeneric(points, detection.corners, self.left.k, self.left.d, flags=cv2.SOLVEPNP_IPPE)
        candidates = []
        if not result[0]:
            return candidates
        for rvec, tvec in zip(result[1], result[2], strict=True):
            if not np.isfinite(rvec).all() or not np.isfinite(tvec).all() or tvec.reshape(-1)[2] <= 0:
                continue
            solver_pose = transform(rvec, tvec)
            rig_pose = solver_pose @ MARKER_T_SOLVER @ np.linalg.inv(marker["rig_T_marker"])
            candidates.append((_error(points, detection.corners, solver_pose, self.left), rig_pose, rvec, tvec))
        return sorted(candidates, key=lambda item: item[0])

    def _stereo(self, marker: dict, left: Detection, right: Detection, initial: np.ndarray) -> tuple[np.ndarray, float]:
        points = marker_points(marker)
        expected = np.concatenate([left.corners, right.corners])

        def residual(parameters):
            pose = transform(parameters[:3], parameters[3:])
            coords = points @ pose[:3, :3].T + pose[:3, 3]
            other = coords - [self.baseline, 0, 0]
            pixels = np.concatenate([coords @ self.left.k.T, other @ self.left.k.T])
            return (pixels[:, :2] / pixels[:, 2:] - expected).ravel()

        start = np.r_[cv2.Rodrigues(initial[:3, :3])[0].ravel(), initial[:3, 3]]
        fit = least_squares(residual, start, method="lm", max_nfev=80)
        pose = transform(fit.x[:3], fit.x[3:])
        if not np.isfinite(pose).all() or np.any((points @ pose[:3, :3].T + pose[:3, 3])[:, 2] <= 0):
            return pose, float("inf")
        return pose, float(np.sqrt(np.sum(fit.fun**2) / 8))

    def _single(self, marker: dict, observation: Detection, right: list[Detection]) -> Estimate:
        ids = (marker["id"],)
        candidates = self._candidates(marker, observation)
        if not candidates:
            return Estimate(marker_ids=ids, reason="no_ippe_candidate")
        error, pose, rvec, tvec = candidates[0]
        ambiguous = (
            len(candidates) > 1
            and candidates[1][0] - error <= self.config.max_left_error_gap_px
            and rotation_distance_deg(pose, candidates[1][1]) >= self.config.min_candidate_rotation_deg
        )
        solver = "single_ippe_lm"
        if ambiguous:
            matches = [detection for detection in right if detection.id == marker["id"]]
            if len(matches) > 1:
                return Estimate(marker_ids=ids, reason="marker_identity_ambiguous")
            if self.baseline is None:
                return Estimate(marker_ids=ids, reason="stereo_calibration_unavailable")
            if not matches:
                return Estimate(marker_ids=ids, reason="ambiguous_pose_no_right_marker")
            solutions = [self._stereo(marker, observation, matches[0], candidate[1]) for candidate in candidates]
            pose, error = min(solutions, key=lambda item: item[1])
            solver = "single_ippe_stereo_scipy_lm"
        else:
            points = marker_points(marker, solver=True)
            rvec, tvec = cv2.solvePnPRefineLM(points, observation.corners, self.left.k, self.left.d, rvec, tvec)
            solver_pose = transform(rvec, tvec)
            pose = solver_pose @ MARKER_T_SOLVER @ np.linalg.inv(marker["rig_T_marker"])
            error = _error(points, observation.corners, solver_pose, self.left)
        return Estimate(pose, error, ids, "ok", solver)

    def _joint(self, markers: list[dict], observations: dict[int, Detection]) -> Estimate:
        points = np.concatenate([marker_points(marker) for marker in markers])
        pixels = np.concatenate([observations[marker["id"]].corners for marker in markers])
        success, rvec, tvec = cv2.solvePnP(points, pixels, self.left.k, self.left.d, flags=cv2.SOLVEPNP_SQPNP)
        if not success:
            return Estimate(reason="pnp_failed")
        rvec, tvec = cv2.solvePnPRefineLM(points, pixels, self.left.k, self.left.d, rvec, tvec)
        pose = transform(rvec, tvec)
        return Estimate(
            pose, _error(points, pixels, pose, self.left), tuple(m["id"] for m in markers), "ok", "multi_sqpnp_lm"
        )

    def solve(self, hand: str, detections: list[Detection], right: list[Detection] | None = None) -> Estimate:
        rig = self.rigs[hand]
        rig_ids = {marker["id"] for marker in rig["markers"]}
        counts = Counter(detection.id for detection in detections if detection.id in rig_ids)
        if any(count > 1 for count in counts.values()):
            return Estimate(reason="marker_identity_ambiguous", marker_ids=tuple(sorted(counts)))
        visible = [marker for marker in rig["markers"] if marker["id"] in counts]
        if not visible:
            return Estimate()
        observations = {detection.id: detection for detection in detections if detection.id in rig_ids}
        right = right or []
        try:
            estimate = (
                self._single(visible[0], observations[visible[0]["id"]], right)
                if len(visible) == 1
                else self._joint(visible, observations)
            )
            dropped = []
            while self.config.enable_outlier_rejection and len(visible) > 1 and estimate.pose is not None:
                deviations = []
                for marker in visible:
                    candidates = self._candidates(marker, observations[marker["id"]])
                    if candidates:
                        deviations.append(
                            (float(np.linalg.norm(candidates[0][1][:3, 3] - estimate.pose[:3, 3])), marker)
                        )
                if not deviations:
                    break
                deviation, worst = max(deviations, key=lambda item: item[0])
                if deviation <= self.config.max_single_marker_deviation_m:
                    break
                remaining = [marker for marker in visible if marker["id"] != worst["id"]]
                trial = (
                    self._single(remaining[0], observations[remaining[0]["id"]], right)
                    if len(remaining) == 1
                    else self._joint(remaining, observations)
                )
                if trial.pose is None or trial.error_px >= estimate.error_px:
                    break
                dropped.append(worst["id"])
                visible, estimate = remaining, trial
            estimate.dropped_ids = tuple(dropped)
        except cv2.error:
            return Estimate(reason="pnp_failed", marker_ids=tuple(sorted(counts)))
        if estimate.pose is not None:
            camera_points = np.concatenate([marker_points(marker) for marker in visible])
            depths = (camera_points @ estimate.pose[:3, :3].T + estimate.pose[:3, 3])[:, 2]
            if not np.isfinite(estimate.pose).all() or not np.isfinite(estimate.error_px):
                estimate.pose, estimate.error_px, estimate.reason = None, None, "nonfinite_pose"
            elif np.any(depths <= 0):
                estimate.pose, estimate.reason = None, "nonpositive_depth"
            elif estimate.error_px > self.config.max_reprojection_error_px:
                estimate.reason = "reprojection_error"
            elif estimate.dropped_ids:
                # Keep the reference outlier solution for diagnostics, but don't
                # let an inconsistent physical rig silently pass identity QC.
                estimate.reason = "marker_geometry_inconsistent"
        return estimate

    def frame(
        self, hand: str | None, timestamp_ns: int, detections: list[Detection], right: list[Detection] | None = None
    ) -> dict:
        estimate = self.solve(hand, detections, right) if hand else Estimate(reason="primary_hand_unresolved")
        target = estimate.pose @ self.rigs[hand]["rig_T_target"] if estimate.pose is not None else None
        raw_xyz = target[:3, 3].tolist() if target is not None else None
        reason = estimate.reason
        speed = angular_speed = None
        if reason == "ok" and self.last_pose is not None and self.config.enable_velocity_gate:
            dt = (int(timestamp_ns) - self.last_timestamp_ns) / 1e9
            if dt <= 0:
                reason = "nonincreasing_timestamp"
            else:
                speed = float(np.linalg.norm(target[:3, 3] - self.last_pose[:3, 3]) / dt)
                angular_speed = rotation_distance_deg(self.last_pose, target) / dt
                if speed > self.config.max_linear_speed_mps or angular_speed > self.config.max_angular_speed_degps:
                    reason = "velocity_gate"
        valid = reason == "ok"
        if valid:
            self.last_pose, self.last_timestamp_ns = target.copy(), int(timestamp_ns)
        anomalies = {
            "marker_identity_ambiguous",
            "marker_geometry_inconsistent",
            "nonfinite_pose",
            "nonpositive_depth",
            "velocity_gate",
            "nonincreasing_timestamp",
        }
        return {
            "xyz_m": raw_xyz if valid else None,
            "raw_xyz_m": raw_xyz,
            "valid": valid,
            "reason": reason,
            "marker_ids": list(estimate.marker_ids),
            "solver": estimate.solver,
            "reprojection_error_px": estimate.error_px,
            "dropped_marker_ids": list(estimate.dropped_ids),
            "gate_speed_m_s": speed,
            "gate_angular_speed_deg_s": angular_speed,
            "anomaly": reason in anomalies,
            "detections": [d.as_dict(valid and d.id in estimate.marker_ids) for d in detections],
        }
