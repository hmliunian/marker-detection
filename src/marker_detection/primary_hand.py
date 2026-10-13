"""Experimental, evidence-preserving selection of the operating gripper.

Position is gripper opening, not arm position. These rules describe sensor
evidence and do not claim to recover the operator's control commands.
"""

from dataclasses import asdict, dataclass
from math import isfinite

import numpy as np

from .models import Episode, ScalarSeries


@dataclass(frozen=True)
class ActivityConfig:
    """Uncalibrated starting values, saved with every decision.

    Raw opening around 98--103 was visually checked in the supplied dataset;
    102 is a normalization reference, not a claim that the raw unit is mm.
    The metre reference assumes the documented CDR converter's /1000 rule.
    Contact threshold applies to per-finger sum of force-vector norms in the
    original sensor unit, whose physical meaning has not yet been verified.
    """

    open_reference_raw: float = 102.0
    open_reference_m: float = 0.102
    motion_fraction: float = 0.08
    open_fraction: float = 0.93
    partial_min_fraction: float = 0.03
    partial_max_fraction: float = 0.90
    min_position_duration_s: float = 0.20
    contact_threshold: float = 0.5
    min_contact_duration_s: float = 0.10
    max_sensor_gap_s: float = 0.10
    min_idle_coverage: float = 0.80
    carry_task_ids: tuple[str, ...] = ("A_003",)

    def __post_init__(self) -> None:
        numeric = {k: v for k, v in asdict(self).items() if k != "carry_task_ids"}
        if any(not isfinite(v) or v <= 0 for v in numeric.values()):
            raise ValueError("Activity thresholds must be finite and positive")
        if not 0 < self.partial_min_fraction < self.partial_max_fraction < self.open_fraction <= 1:
            raise ValueError("Opening fractions must increase from partial minimum to open")
        if self.motion_fraction > 1 or self.min_idle_coverage > 1:
            raise ValueError("Motion and coverage fractions must not exceed one")


def _series_arrays(series: ScalarSeries) -> tuple[np.ndarray, np.ndarray, list[str]]:
    timestamps = np.asarray(series.timestamps_ns)
    values = np.asarray(series.values, dtype=float)
    issues: list[str] = []
    if timestamps.ndim != 1 or values.ndim != 1 or len(timestamps) != len(values):
        return np.array([], dtype=np.int64), np.array([]), ["invalid_series_shape"]
    if not np.issubdtype(timestamps.dtype, np.integer):
        return np.array([], dtype=np.int64), np.array([]), ["noninteger_sensor_timestamp"]
    timestamps = timestamps.astype(np.int64, copy=False)
    if len(timestamps) > 1 and np.any(np.diff(timestamps) <= 0):
        issues.append("nonincreasing_sensor_timestamp")
    if np.any(~np.isfinite(values)):
        issues.append("nonfinite_sensor_value")
    return timestamps, values, issues


def _longest_run_s(timestamps: np.ndarray, mask: np.ndarray, max_gap_s: float) -> float:
    """Measure sustained evidence without bridging missing or stale samples."""
    longest = run = 0.0
    for index in range(1, len(timestamps)):
        dt = (int(timestamps[index]) - int(timestamps[index - 1])) / 1e9
        if mask[index] and mask[index - 1] and 0 < dt <= max_gap_s:
            run += dt
            longest = max(longest, run)
        else:
            run = 0.0
    return longest


def _coverage_s(timestamps: np.ndarray, mask: np.ndarray, max_gap_s: float) -> float:
    return sum(
        (int(timestamps[i]) - int(timestamps[i - 1])) / 1e9
        for i in range(1, len(timestamps))
        if mask[i] and mask[i - 1] and 0 < int(timestamps[i]) - int(timestamps[i - 1]) <= max_gap_s * 1e9
    )


def _window(episode: Episode) -> tuple[int, int] | None:
    # Restrict evidence to the head recording if available. This avoids using
    # preparation motions outside the actual action clip to infer its hand.
    head = episode.videos.get("head_left", [])
    if head:
        return min(p.timestamp_ns for p in head), max(p.timestamp_ns for p in head)
    limits = []
    for series in [*episode.grippers.values(), *episode.tactile.values()]:
        timestamps, _, issues = _series_arrays(series)
        if len(timestamps) and "nonincreasing_sensor_timestamp" not in issues:
            limits.extend((int(timestamps[0]), int(timestamps[-1])))
    return (min(limits), max(limits)) if limits else None


def _crop(timestamps: np.ndarray, values: np.ndarray, window: tuple[int, int] | None) -> tuple[np.ndarray, np.ndarray]:
    if window is None:
        return timestamps, values
    mask = (timestamps >= window[0]) & (timestamps <= window[1])
    return timestamps[mask], values[mask]


def _opening_evidence(series: ScalarSeries | None, window: tuple[int, int] | None, config: ActivityConfig) -> dict:
    evidence: dict = {
        "present": series is not None,
        "active_reasons": [],
        "idle_supported": False,
        "issues": [],
    }
    if series is None:
        evidence["issues"].append("missing_position")
        return evidence
    timestamps, values, issues = _series_arrays(series)
    timestamps, values = _crop(timestamps, values, window)
    finite = np.isfinite(values)
    evidence.update(
        unit=series.unit,
        source=series.source,
        samples=len(values),
        finite_samples=int(finite.sum()),
        issues=issues,
        raw_min=float(values[finite].min()) if finite.any() else None,
        raw_max=float(values[finite].max()) if finite.any() else None,
    )
    reference = {
        "unknown": config.open_reference_raw,
        "": config.open_reference_raw,
        "m": config.open_reference_m,
    }.get(series.unit)
    if reference is None:
        evidence["issues"].append("unsupported_position_unit")
        return evidence
    evidence["normalization"] = {
        "open_reference": reference,
        "unit": series.unit,
        "experimental": True,
        "basis": "observed_raw_opening_98_to_103"
        if series.unit != "m"
        else "documented_cdr_converter_scale_assumption",
    }
    if not finite.any():
        evidence["issues"].append("no_finite_position")
        return evidence
    normalized = values / reference
    if np.any((normalized[finite] < -0.02) | (normalized[finite] > 1.10)):
        evidence["issues"].append("position_outside_reference_range")
    low, high = np.quantile(normalized[finite], [0.05, 0.95])
    partial = finite & (normalized > config.partial_min_fraction)
    partial &= normalized < config.partial_max_fraction
    coverage_s = _coverage_s(timestamps, finite, config.max_sensor_gap_s)
    duration_s = (window[1] - window[0]) / 1e9 if window else 0.0
    coverage = min(1.0, coverage_s / duration_s) if duration_s > 0 else 0.0
    partial_duration = _longest_run_s(timestamps, partial, config.max_sensor_gap_s)
    evidence.update(
        normalized_p05=float(low),
        normalized_p95=float(high),
        robust_range_fraction=float(high - low),
        observed_duration_s=coverage_s,
        coverage=coverage,
        sustained_partial_opening_s=partial_duration,
    )
    if evidence["issues"]:
        return evidence
    if coverage_s >= config.min_position_duration_s:
        if high - low >= config.motion_fraction and low < config.open_fraction:
            evidence["active_reasons"].append("opening_changed")
        if partial_duration >= config.min_position_duration_s:
            evidence["active_reasons"].append("sustained_partial_opening")
        evidence["idle_supported"] = bool(
            low >= config.open_fraction and high - low < config.motion_fraction and coverage >= config.min_idle_coverage
        )
    if not evidence["active_reasons"] and not evidence["idle_supported"]:
        evidence["issues"].append("opening_does_not_resolve_activity")
    return evidence


def _tactile_evidence(episode: Episode, hand: str, window: tuple[int, int] | None, config: ActivityConfig) -> dict:
    streams: dict = {}
    contact = False
    for finger in ("finger0", "finger1"):
        key = f"{hand}_{finger}"
        series = episode.tactile.get(key)
        if series is None:
            streams[finger] = {"present": False, "issues": ["missing_tactile"]}
            continue
        timestamps, values, issues = _series_arrays(series)
        timestamps, values = _crop(timestamps, values, window)
        finite = np.isfinite(values)
        if finite.any() and np.any(values[finite] < 0):
            issues.append("negative_force_norm")
        response = finite & (values >= config.contact_threshold)
        longest = _longest_run_s(timestamps, response, config.max_sensor_gap_s)
        sustained = longest >= config.min_contact_duration_s and not issues
        contact |= sustained
        streams[finger] = {
            "present": True,
            "source": series.source,
            "unit": series.unit,
            "feature": "sum_of_force_vector_norms",
            "samples": len(values),
            "finite_samples": int(finite.sum()),
            "maximum": float(values[finite].max()) if finite.any() else None,
            "all_zero": bool(np.all(values[finite] == 0)) if finite.any() else None,
            "sustained_response_s": longest,
            "contact_supported": bool(sustained),
            "issues": issues,
        }
    return {"contact_supported": bool(contact), "streams": streams}


def select_primary_hand(episode: Episode, config: ActivityConfig | None = None) -> dict:
    """Return a JSON-safe decision without modifying observations.

    ``single`` identifies one hand with activity evidence and an open idle
    counterpart. ``bimanual`` rejects non-carry episodes with evidence for
    both hands, even if that evidence occurs at different times. ``unknown``
    retains the episode for review. Carry tasks are marked separately.
    """
    config = config or ActivityConfig()
    window = _window(episode)
    evidence = {}
    for hand in ("left", "right"):
        opening = _opening_evidence(episode.grippers.get(hand), window, config)
        tactile = _tactile_evidence(episode, hand, window, config)
        reasons = list(opening["active_reasons"])
        if tactile["contact_supported"]:
            reasons.append("sustained_tactile_response")
        evidence[hand] = {
            "activity_supported": bool(reasons),
            "idle_supported": bool(opening["idle_supported"] and not reasons),
            "activity_reasons": reasons,
            "position": opening,
            "tactile": tactile,
        }
    result = {
        "hand": None,
        "status": "unknown",
        "reasons": [],
        "evidence": evidence,
        "window_timestamp_ns": list(window) if window else None,
        "config": asdict(config),
        "experimental": True,
        "limitations": [
            "Activity thresholds are uncalibrated; partial opening supports possible holding.",
            "Zero or absent tactile response cannot establish inactivity.",
            "Stable near-zero opening alone cannot establish operating-hand identity.",
        ],
    }
    if episode.task_id in config.carry_task_ids:
        result.update(status="carry", reasons=["carry_task_separate"])
        return result
    active = [hand for hand in evidence if evidence[hand]["activity_supported"]]
    if len(active) == 2:
        result.update(status="bimanual", reasons=["both_hands_have_activity_evidence"])
    elif len(active) == 1:
        hand = active[0]
        other = "right" if hand == "left" else "left"
        if evidence[other]["idle_supported"]:
            result.update(hand=hand, status="single", reasons=["one_active_hand_other_open"])
        else:
            result["reasons"] = ["other_hand_activity_unresolved"]
    else:
        result["reasons"] = ["no_reliable_activity_evidence"]
    return result
