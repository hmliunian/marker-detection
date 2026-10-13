"""Offline MCAP -> operating hand + per-frame camera-relative XYZ + audit GUI."""

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import cv2
import numpy as np

from .io import inspect_mcap, load_episode
from .pose import PoseConfig, PoseEstimator, load_reference_config
from .primary_hand import ActivityConfig, select_primary_hand
from .quality import QualityConfig, evaluate_trajectory
from .video import decode_video


def _json(path: Path, value):
    def serializable(item):
        # Quality evaluation has already rejected nonfinite observations.
        # Keep their diagnostic records writable as strict, portable JSON.
        if isinstance(item, (float, np.floating)):
            return float(item) if np.isfinite(item) else None
        if isinstance(item, np.integer):
            return int(item)
        if isinstance(item, dict):
            return {key: serializable(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [serializable(value) for value in item]
        return item

    path.write_text(json.dumps(serializable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _settings(path: str | None, reference: dict) -> tuple[ActivityConfig, QualityConfig, PoseConfig]:
    settings = json.loads(Path(path).read_text()) if path else {}
    if not isinstance(settings, dict) or set(settings) - {"activity", "quality", "pose"}:
        raise ValueError("Settings must contain only activity, quality and pose objects")
    activity = ActivityConfig(**settings.get("activity", {}))
    pose = replace(PoseConfig.from_reference(reference), **settings.get("pose", {}))
    quality = QualityConfig(**{"max_speed_m_s": pose.max_linear_speed_mps, **settings.get("quality", {})})
    return activity, quality, pose


def _nearest(timestamps: np.ndarray, timestamp_ns: int, slop_s: float) -> int | None:
    index = int(np.searchsorted(timestamps, timestamp_ns))
    candidates = [candidate for candidate in (index - 1, index) if 0 <= candidate < len(timestamps)]
    if not candidates:
        return None
    best = min(candidates, key=lambda candidate: abs(int(timestamps[candidate]) - timestamp_ns))
    return best if abs(int(timestamps[best]) - timestamp_ns) <= round(slop_s * 1e9) else None


def _camera_dict(camera):
    return {
        "width": camera.width,
        "height": camera.height,
        "k": camera.k.tolist(),
        "d": camera.d.tolist(),
        "r": camera.r.tolist(),
        "p": camera.p.tolist(),
        "frame_id": camera.frame_id,
        "distortion_model": camera.distortion_model,
        "source": camera.source,
    }


def analyze(
    input_path: str | Path,
    output: str | Path,
    *,
    sidecar: str | None = None,
    geometry: str | None = None,
    settings: str | None = None,
    video_backend: str = "auto",
    gpu: int = 0,
    progress: bool = True,
) -> dict:
    """Process the complete episode; a nonempty output is never overwritten."""
    output = Path(output).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output must be a new or empty directory: {output}")
    source = Path(input_path).expanduser().resolve()
    if output == source or output in source.parents:
        raise ValueError("Output must not contain the source recording")
    reference = load_reference_config(geometry)
    activity_config, quality_config, pose_config = _settings(settings, reference)
    episode = load_episode(source, sidecar=sidecar)
    primary = select_primary_hand(episode, activity_config)
    if "head_left" not in episode.videos or "head_left" not in episode.cameras:
        raise ValueError("Head-left video and its calibration are required to compute camera-relative XYZ")
    for name in ("head_left", "head_right"):
        if name in episode.cameras:
            calibration = episode.cameras[name]
            if any(not np.isfinite(getattr(calibration, field)).all() for field in ("k", "d", "r", "p")):
                raise ValueError(f"Camera calibration {name} contains nonfinite values")
    left_camera = episode.cameras["head_left"]
    left = decode_video(episode.videos["head_left"], left_camera.width, left_camera.height, video_backend, gpu)
    right = None
    warnings = list(episode.warnings) + left.warnings
    if "head_right" in episode.videos and "head_right" in episode.cameras:
        calibration = episode.cameras["head_right"]
        right = decode_video(episode.videos["head_right"], calibration.width, calibration.height, video_backend, gpu)
        warnings.extend(right.warnings)
        if np.any(np.diff(right.timestamps_ns) <= 0):
            raise ValueError(
                "Right video presentation timestamps must be strictly increasing for stereo synchronization"
            )
    estimator = PoseEstimator(reference, left_camera, episode.cameras.get("head_right"), pose_config)
    if estimator.stereo_warning:
        warnings.append(estimator.stereo_warning)
    output.mkdir(parents=True, exist_ok=True)
    (output / "frames").mkdir(exist_ok=True)
    frames = []
    right_cache = {}
    start_ns = int(left.timestamps_ns[0])
    if progress:
        print(
            f"{episode.episode_id}: hand={primary['hand'] or primary['status']}; "
            f"frames={len(left.images)}; video={left.backend}",
            file=sys.stderr,
            flush=True,
        )
    for index, (timestamp, gray) in enumerate(zip(left.timestamps_ns, left.images, strict=True)):
        timestamp_ns = int(timestamp)
        detections = estimator.detect(gray)
        right_index = _nearest(right.timestamps_ns, timestamp_ns, pose_config.stereo_sync_slop_sec) if right else None
        right_detections = []
        if right_index is not None:
            if right_index not in right_cache:
                # Detection is independent per image; cache only the current pair.
                right_cache = {right_index: estimator.detect(right.images[right_index])}
            right_detections = right_cache[right_index]
        frame = estimator.frame(primary["hand"], timestamp_ns, detections, right_detections)
        preview = f"frames/{index:06d}.jpg"
        if not cv2.imwrite(str(output / preview), gray, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            raise OSError(f"Could not write preview: {output / preview}")
        frame.update(
            {
                "frame_index": index,
                "timestamp_ns": timestamp_ns,
                "time_s": (timestamp_ns - start_ns) / 1e9,
                "preview": preview,
                "right_frame_index": right_index,
                "stereo_delta_ns": int(right.timestamps_ns[right_index]) - timestamp_ns
                if right_index is not None
                else None,
            }
        )
        frames.append(frame)
        if progress and (index + 1) % 100 == 0:
            print(f"  {index + 1}/{len(left.images)} frames", file=sys.stderr, flush=True)
    summary = evaluate_trajectory(frames, quality_config)
    summary["frame_reasons"] = dict(Counter(frame["reason"] for frame in frames))
    summary["solvers"] = dict(Counter(frame["solver"] for frame in frames))
    summary["stereo_pairs"] = sum(frame["right_frame_index"] is not None for frame in frames)
    if primary["status"] == "carry":
        decision, reasons = "excluded_carry", primary["reasons"]
    elif primary["status"] == "bimanual":
        decision, reasons = "rejected", primary["reasons"]
    elif primary["status"] != "single":
        decision, reasons = "review", primary["reasons"]
    else:
        decision, reasons = summary["status"], summary["reasons"]
    report = {
        "schema_version": 1,
        "episode_id": episode.episode_id,
        "source": episode.source,
        "task_id": episode.task_id,
        "primary_hand": primary["hand"],
        "decision": decision,
        "reasons": reasons,
        "primary_hand_evidence": primary,
        "summary": summary,
        "action": {
            "fields": ["x", "y", "z"],
            "unit": "m",
            "coordinate_frame": "head_left_camera_optical_frame",
            "recorded_frame_id": left_camera.frame_id,
            "target": "reference rig_T_target",
            "transform": "camera_T_rig @ rig_T_target",
            "interpolated": False,
        },
        "config": {
            "activity": asdict(activity_config),
            "quality": asdict(quality_config),
            "pose": asdict(pose_config),
            "reference": reference,
            "video_backend_requested": video_backend,
            "gpu": gpu,
        },
        "provenance": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reader": episode.metadata,
            "cameras": {name: _camera_dict(camera) for name, camera in episode.cameras.items()},
            "video_backends": {"head_left": left.backend, "head_right": right.backend if right else None},
            "reference_commit": "954abdc7f42e5f912f201727fcb8ca26a0ac1e2d",
            "geometry_sha256": hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest(),
            "packages": {
                name: version(name)
                for name in (
                    "umi-marker-screening",
                    "av",
                    "numpy",
                    "scipy",
                    "opencv-python-headless",
                    "pupil-apriltags",
                    "mcap",
                )
            },
            "solver_difference": "Stereo: SciPy rotation-vector LM (max_nfev=80), reference: Ceres quaternion, 8 iterations",
            "pixel_convention": "Original decoded Y plane + recorded K/D; R is preserved and not applied again",
            "identity_policy": "Duplicate primary IDs or inconsistent rig geometry invalidate the observation",
        },
        "warnings": warnings,
        "limitations": [
            "Experimental activity/gap/coverage thresholds have not been calibrated on a labelled holdout set.",
            "Reference marker dimensions and mounting geometry are assumed; metric scale depends on them.",
            "An identical-ID third-party marker alone cannot always be distinguished by geometry/continuity.",
            "No smoothing or gap interpolation; frame validity and whole-episode decision must both be checked.",
            "New ROS2 CDR support is contract-tested only; real CDR recordings are deferred.",
        ],
    }
    _json(output / "frames.json", frames)
    with (output / "actions.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "frame_index",
                "timestamp_ns",
                "time_s",
                "primary_hand",
                "x_m",
                "y_m",
                "z_m",
                "frame_valid",
                "episode_accepted",
                "reason",
            ]
        )
        for frame in frames:
            usable = frame["valid"] and frame["quality"]["valid"]
            point = frame["xyz_m"] if usable else [None, None, None]
            reason = frame["reason"] if not frame["valid"] else ";".join(frame["quality"]["issues"]) or "ok"
            writer.writerow(
                [
                    frame["frame_index"],
                    frame["timestamp_ns"],
                    frame["time_s"],
                    primary["hand"],
                    *point,
                    int(usable),
                    int(decision == "accepted"),
                    reason,
                ]
            )
    # This completion marker is written after all per-frame artifacts.
    _json(output / "analysis.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="UMI 主手与逐帧 XYZ 筛选（实验阈值）")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="读取 MCAP/ZIP 的数据格式、通道、标定与缺失信息")
    inspect.add_argument("input")
    run = commands.add_parser("analyze", help="分析完整 episode 并写入 GUI 可读结果")
    run.add_argument("input", help="MCAP 或只含一个 MCAP 的 ZIP")
    run.add_argument("--output", required=True, help="新建或空结果目录")
    run.add_argument("--sidecar", help="显式指定采集 JSON")
    run.add_argument("--geometry", help="参考 UMI 几何 JSON；默认项目自带配置")
    run.add_argument("--config", "--settings", dest="settings", help="activity/quality/pose 配置 JSON")
    run.add_argument("--video-backend", choices=["auto", "cuda", "cpu"], default="auto")
    run.add_argument("--gpu", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(json.dumps(inspect_mcap(args.input), ensure_ascii=False, indent=2, allow_nan=False))
        else:
            report = analyze(
                args.input,
                args.output,
                sidecar=args.sidecar,
                geometry=args.geometry,
                settings=args.settings,
                video_backend=args.video_backend,
                gpu=args.gpu,
            )
            print(
                json.dumps(
                    {
                        "episode_id": report["episode_id"],
                        "primary_hand": report["primary_hand"],
                        "decision": report["decision"],
                        "reasons": report["reasons"],
                        "valid_frames": report["summary"]["valid_frames"],
                        "frames": report["summary"]["frames"],
                        "output": str(Path(args.output).resolve()),
                    },
                    ensure_ascii=False,
                )
            )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
