"""Audit the full frame timeline; preserve both retained and rejected poses."""

from dataclasses import asdict, dataclass
from math import isfinite, sqrt
from numbers import Integral
from statistics import median


@dataclass(frozen=True)
class QualityConfig:
    """Experimental episode thresholds, not a calibrated dataset guarantee.

    Speed follows tinynav's 1.5 m/s camera-relative starting threshold.
    0.5 s missing and 90% coverage are explicit trial values. Every anomaly
    rejects by default; acceleration is measured but has no arbitrary gate.
    """

    max_speed_m_s: float = 1.5
    max_gap_s: float = 0.5
    min_coverage: float = 0.90
    max_anomalous_frames: int = 0
    min_valid_frames: int = 3
    max_acceleration_m_s2: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_speed_m_s", "max_gap_s"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not isfinite(self.min_coverage) or not 0 <= self.min_coverage <= 1:
            raise ValueError("min_coverage must be a fraction in [0, 1]")
        if not isinstance(self.max_anomalous_frames, int) or self.max_anomalous_frames < 0:
            raise ValueError("max_anomalous_frames must be a nonnegative integer")
        if not isinstance(self.min_valid_frames, int) or self.min_valid_frames < 2:
            raise ValueError("min_valid_frames must be at least two")
        acceleration = self.max_acceleration_m_s2
        if acceleration is not None and (not isfinite(acceleration) or acceleration < 0):
            raise ValueError("max_acceleration_m_s2 must be finite and nonnegative")


def _point(value: object) -> tuple[list[float] | None, str | None]:
    if value is None:
        return None, None
    try:
        if len(value) != 3:  # type: ignore[arg-type]
            return None, "invalid_xyz_shape"
        point = [float(x) for x in value]  # type: ignore[union-attr]
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_xyz_value"
    if not all(isfinite(x) for x in point):
        return None, "nonfinite_xyz"
    return point, None


def _norm(vector: list[float]) -> float:
    return sqrt(sum(x * x for x in vector))


def _delta(before: list[float], after: list[float]) -> list[float]:
    return [b - a for a, b in zip(before, after, strict=True)]


def _gap_intervals(frames: list[dict], available: list[bool], durations: list[float]) -> list[dict]:
    intervals = []
    start = None
    for index in range(len(frames) + 1):
        missing = index < len(frames) and not available[index]
        if missing and start is None:
            start = index
        if not missing and start is not None:
            end = index - 1
            first_ns = frames[start].get("timestamp_ns")
            last_ns = frames[end].get("timestamp_ns")
            end_ns = int(last_ns) + round(durations[end] * 1e9) if isinstance(last_ns, Integral) else None
            intervals.append(
                {
                    "start_frame_index": frames[start].get("frame_index", start),
                    "end_frame_index": frames[end].get("frame_index", end),
                    "start_timestamp_ns": int(first_ns) if isinstance(first_ns, Integral) else None,
                    "end_timestamp_ns": end_ns,
                    "duration_s": sum(durations[start:index]),
                    "frames": index - start,
                    "location": "all"
                    if start == 0 and index == len(frames)
                    else "leading"
                    if start == 0
                    else "trailing"
                    if index == len(frames)
                    else "internal",
                }
            )
            start = None
    return intervals


def evaluate_trajectory(frames: list[dict], config: QualityConfig | None = None) -> dict:
    """Return a JSON-safe summary and add ``frame['quality']`` in place.

    Input ``valid``, ``xyz_m`` and ``raw_xyz_m`` are preserved. Consumers must
    combine input validity with ``quality.valid`` before using an action.
    Raw coordinates are audited even for an upstream ``velocity_gate``.
    Ordinary velocity/acceleration never bridge missing observations; a
    separate reappearance average-speed diagnostic uses the elapsed gap.

    Frame missing durations cover [timestamp, next timestamp); the final
    frame is assigned the median positive frame interval. Thus leading and
    trailing gaps, including a single missing last frame, are counted.
    ``coverage`` counts frames; ``time_coverage`` weights their durations.
    Both must pass the coverage threshold. Invalid ordering is rejected,
    never hidden by re-sorting. The time_s display field is not used.
    """
    config = config or QualityConfig()
    timestamps: list[int | None] = []
    filtered: list[list[float] | None] = []
    raw: list[list[float] | None] = []
    hard_failures: set[str] = set()
    anomaly_reasons = {
        "velocity_gate",
        "speed_exceeded",
        "jump",
        "identity_ambiguous",
        "identity_switch",
        "nonfinite_pose",
        "nonfinite_xyz",
        "invalid_timestamp",
        "nonincreasing_timestamp",
    }
    for frame in frames:
        quality: dict = {
            "valid": False,
            "issues": [],
            "dt_s": None,
            "delta_xyz_m": None,
            "displacement_m": None,
            "speed_m_s": None,
            "acceleration_m_s2": None,
            "raw_delta_xyz_m": None,
            "raw_displacement_m": None,
            "raw_speed_m_s": None,
            "raw_acceleration_m_s2": None,
            "reappearance_displacement_m": None,
            "reappearance_elapsed_s": None,
            "reappearance_average_speed_m_s": None,
            "raw_reappearance_displacement_m": None,
            "raw_reappearance_elapsed_s": None,
            "raw_reappearance_average_speed_m_s": None,
        }
        frame["quality"] = quality
        timestamp = frame.get("timestamp_ns")
        if not isinstance(timestamp, Integral) or isinstance(timestamp, bool):
            timestamp = None
            quality["issues"].append("invalid_timestamp")
            hard_failures.add("invalid_timestamp")
        else:
            timestamp = int(timestamp)
        timestamps.append(timestamp)
        xyz, issue = _point(frame.get("xyz_m"))
        raw_xyz, raw_issue = _point(frame.get("raw_xyz_m", frame.get("xyz_m")))
        if issue:
            quality["issues"].append(issue)
            hard_failures.add(issue)
        if raw_issue:
            quality["issues"].append(f"raw_{raw_issue}")
            hard_failures.add(f"raw_{raw_issue}")
        filtered.append(xyz if frame.get("valid", False) else None)
        raw.append(raw_xyz)
        if filtered[-1] is None:
            quality["issues"].append("missing_observation")
        reason = frame.get("reason")
        if frame.get("anomaly") or frame.get("anomalies") or reason in anomaly_reasons:
            quality["issues"].append("upstream_anomaly")

    positive_intervals = [
        (right - left) / 1e9
        for left, right in zip(timestamps[:-1], timestamps[1:], strict=True)
        if left is not None and right is not None and right > left
    ]
    nominal_interval = median(positive_intervals) if positive_intervals else 0.0
    durations = []
    for index, frame in enumerate(frames):
        dt = None
        if index and timestamps[index] is not None and timestamps[index - 1] is not None:
            dt = (timestamps[index] - timestamps[index - 1]) / 1e9
            frame["quality"]["dt_s"] = dt
            if dt <= 0:
                frame["quality"]["issues"].append("nonincreasing_timestamp")
                hard_failures.add("nonincreasing_timestamp")
        next_dt = None
        if index + 1 < len(frames) and timestamps[index] is not None and timestamps[index + 1] is not None:
            next_dt = (timestamps[index + 1] - timestamps[index]) / 1e9
        durations.append(next_dt if next_dt is not None and next_dt > 0 else nominal_interval)

    for points, prefix in ((filtered, ""), (raw, "raw_")):
        last_seen = None
        previous_velocity = None
        previous_dt = None
        for index, point in enumerate(points):
            quality = frames[index]["quality"]
            timestamp = timestamps[index]
            if point is None or timestamp is None:
                previous_velocity = previous_dt = None
                continue
            velocity = None
            dt = quality["dt_s"]
            if index and points[index - 1] is not None and dt is not None and dt > 0:
                change = _delta(points[index - 1], point)
                displacement = _norm(change)
                velocity = [x / dt for x in change]
                speed = _norm(velocity)
                if not isfinite(displacement) or not isfinite(speed):
                    quality["issues"].append(f"{prefix}nonfinite_kinematics")
                    hard_failures.add(f"{prefix}nonfinite_kinematics")
                    velocity = None
                else:
                    quality[f"{prefix}delta_xyz_m"] = change
                    quality[f"{prefix}displacement_m"] = displacement
                    quality[f"{prefix}speed_m_s"] = speed
                    if speed > config.max_speed_m_s:
                        quality["issues"].append(f"{prefix}speed_exceeded")
                    if previous_velocity is not None and previous_dt is not None:
                        acceleration = _norm(_delta(previous_velocity, velocity)) / ((dt + previous_dt) / 2)
                        if isfinite(acceleration):
                            quality[f"{prefix}acceleration_m_s2"] = acceleration
                            limit = config.max_acceleration_m_s2
                            if limit is not None and acceleration > limit:
                                quality["issues"].append(f"{prefix}acceleration_exceeded")
                        else:
                            quality["issues"].append(f"{prefix}nonfinite_kinematics")
                            hard_failures.add(f"{prefix}nonfinite_kinematics")
            elif last_seen is not None and index > last_seen + 1:
                previous_timestamp = timestamps[last_seen]
                gap_dt = (timestamp - previous_timestamp) / 1e9
                if gap_dt > 0:
                    displacement = _norm(_delta(points[last_seen], point))
                    average_speed = displacement / gap_dt
                    if isfinite(displacement) and isfinite(average_speed):
                        quality[f"{prefix}reappearance_displacement_m"] = displacement
                        quality[f"{prefix}reappearance_elapsed_s"] = gap_dt
                        quality[f"{prefix}reappearance_average_speed_m_s"] = average_speed
                        if average_speed > config.max_speed_m_s:
                            quality["issues"].append(f"{prefix}reappearance_speed_exceeded")
                    else:
                        quality["issues"].append(f"{prefix}nonfinite_kinematics")
                        hard_failures.add(f"{prefix}nonfinite_kinematics")
            last_seen = index
            previous_velocity = velocity
            previous_dt = dt if velocity is not None else None

    valid = []
    anomalous_frames = []
    for index, frame in enumerate(frames):
        quality = frame["quality"]
        issues = quality["issues"]
        quality["valid"] = filtered[index] is not None and not issues
        valid.append(quality["valid"])
        if any(issue != "missing_observation" for issue in issues):
            anomalous_frames.append(frame.get("frame_index", index))

    gaps = _gap_intervals(frames, valid, durations)
    raw_gaps = _gap_intervals(frames, [p is not None for p in raw], durations)
    count = len(frames)
    valid_count = sum(valid)
    coverage = valid_count / count if count else 0.0
    duration = sum(durations)
    time_coverage = (
        sum(dt for dt, usable in zip(durations, valid, strict=True) if usable) / duration if duration else coverage
    )
    longest_gap = max((gap["duration_s"] for gap in gaps), default=0.0)
    reasons = sorted(hard_failures)
    if count:
        if coverage < config.min_coverage or time_coverage < config.min_coverage:
            reasons.append("coverage_below_threshold")
        if longest_gap > config.max_gap_s:
            reasons.append("missing_interval_exceeds_threshold")
        if len(anomalous_frames) > config.max_anomalous_frames:
            reasons.append("anomalous_frames_exceed_threshold")
    status = "rejected" if reasons else "accepted"
    if valid_count < config.min_valid_frames:
        reasons.append("insufficient_valid_frames" if count else "empty_trajectory")
        if status != "rejected":
            status = "review"
    summary = {
        "status": status,
        "reasons": reasons,
        "frames": count,
        "valid_frames": valid_count,
        "observed_frames": sum(point is not None for point in filtered),
        "raw_observed_frames": sum(point is not None for point in raw),
        "coverage": coverage,
        "time_coverage": time_coverage,
        "duration_s": duration,
        "nominal_frame_interval_s": nominal_interval,
        "longest_gap_s": longest_gap,
        "gap_intervals": gaps,
        "raw_longest_gap_s": max((gap["duration_s"] for gap in raw_gaps), default=0.0),
        "raw_gap_intervals": raw_gaps,
        "anomalous_frames": anomalous_frames,
        "anomalous_frame_count": len(anomalous_frames),
        "config": asdict(config),
        "experimental": True,
        "frame_id": "head_left_camera_optical_frame",
        "duration_convention": "frame_to_next_timestamp; final_frame_uses_median_positive_interval",
    }
    for key in ("speed_m_s", "raw_speed_m_s", "acceleration_m_s2", "raw_acceleration_m_s2"):
        measurements = [frame["quality"][key] for frame in frames if frame["quality"][key] is not None]
        summary[f"max_{key}"] = max(measurements) if measurements else None
    return summary
