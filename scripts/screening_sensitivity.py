"""Replay only episode acceptance thresholds; never rewrite saved trajectories."""

import argparse
import copy
import csv
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from marker_detection.quality import QualityConfig, evaluate_trajectory

SCENARIOS = [
    ("baseline", 0, 0.90, 0.5),
    ("anomaly_1", 1, 0.90, 0.5),
    ("anomaly_2", 2, 0.90, 0.5),
    ("anomaly_2_coverage_85", 2, 0.85, 0.5),
    ("anomaly_3", 3, 0.90, 0.5),
    ("anomaly_5", 5, 0.90, 0.5),
    ("anomaly_10", 10, 0.90, 0.5),
    ("coverage_85", 0, 0.85, 0.5),
    ("coverage_80", 0, 0.80, 0.5),
    ("gap_1", 0, 0.90, 1.0),
    ("coverage_85_gap_1", 0, 0.85, 1.0),
    ("coverage_80_gap_1", 0, 0.80, 1.0),
    ("anomaly_2_coverage_85_gap_1", 2, 0.85, 1.0),
    ("anomaly_3_coverage_85_gap_1", 3, 0.85, 1.0),
    ("anomaly_5_coverage_80_gap_1", 5, 0.80, 1.0),
]


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    records = read(experiment / "results.json")
    config = QualityConfig(**read(experiment / "settings.json")["quality"])
    loaded, anomaly_only, low_coverage = {}, [], []
    for row in records:
        if row["state"] != "completed":
            continue
        directory = Path(row["output"])
        analysis = read(directory / "analysis.json")
        if analysis["primary_hand_evidence"]["status"] != "single":
            continue
        frames = read(directory / "frames.json")
        identity = row["episode_id"]
        loaded[identity] = (analysis, frames)
        anomalies = set(analysis["summary"]["anomalous_frames"])
        anomaly_rows = [frame for frame in frames if frame["frame_index"] in anomalies]
        detail = {
            "episode_id": identity,
            "action": row["action_name"],
            "primary_hand": analysis["primary_hand"],
            "frames": len(frames),
            "valid_frames": analysis["summary"]["valid_frames"],
            "coverage": analysis["summary"]["coverage"],
            "time_coverage": analysis["summary"]["time_coverage"],
            "longest_gap_s": analysis["summary"]["longest_gap_s"],
            "anomalous_frame_count": len(anomalies),
            "anomalous_frames": sorted(anomalies),
            "anomalous_pose_reasons": dict(Counter(frame["reason"] for frame in anomaly_rows)),
            "anomalous_quality_issues": dict(
                Counter(issue for frame in anomaly_rows for issue in frame["quality"]["issues"])
            ),
            "max_raw_speed_m_s": analysis["summary"]["max_raw_speed_m_s"],
            "max_reprojection_error_px": max(
                (
                    frame["reprojection_error_px"]
                    for frame in anomaly_rows
                    if frame.get("reprojection_error_px") is not None
                ),
                default=None,
            ),
            "gap_intervals": analysis["summary"]["gap_intervals"],
            "pose_frame_reasons": analysis["summary"]["frame_reasons"],
            "reasons": analysis["reasons"],
        }
        if row["group"] == "main":
            if analysis["reasons"] == ["anomalous_frames_exceed_threshold"]:
                anomaly_only.append(detail)
            if "coverage_below_threshold" in analysis["reasons"]:
                low_coverage.append(detail)
    assert len(anomaly_only) == 26 and len(low_coverage) == 19
    baseline_accepted = {row["episode_id"] for row in records if row.get("decision") == "accepted"}
    scenarios = []
    for name, limit, coverage, gap in SCENARIOS:
        candidate = replace(config, max_anomalous_frames=limit, min_coverage=coverage, max_gap_s=gap)
        decisions = {}
        for record in records:
            identity = record["episode_id"]
            if identity not in loaded:
                decisions[identity] = record.get("decision", "process_error")
                continue
            original, frames = loaded[identity]
            replay = copy.deepcopy(frames)
            summary = evaluate_trajectory(replay, candidate)
            assert [frame["quality"]["valid"] for frame in replay] == [frame["quality"]["valid"] for frame in frames]
            assert summary["anomalous_frames"] == original["summary"]["anomalous_frames"]
            assert summary["coverage"] == original["summary"]["coverage"]
            assert summary["longest_gap_s"] == original["summary"]["longest_gap_s"]
            if name == "baseline":
                assert summary["status"] == original["decision"]
                assert summary["reasons"] == original["reasons"]
            decisions[identity] = summary["status"]
        accepted = {identity for identity, decision in decisions.items() if decision == "accepted"}
        assert baseline_accepted <= accepted
        promoted = sorted(accepted - baseline_accepted)
        groups = {
            group: Counter(decisions[row["episode_id"]] for row in records if row["group"] == group)
            for group in ("main", "third")
        }
        scenarios.append(
            {
                "scenario": name,
                "max_anomalous_frames": limit,
                "min_coverage": coverage,
                "max_gap_s": gap,
                "main_accepted": groups["main"]["accepted"],
                "main_total": 100,
                "third_accepted": groups["third"]["accepted"],
                "third_total": 3,
                "main_gain": groups["main"]["accepted"] - len(baseline_accepted),
                "promoted_anomaly_only": sum(row["episode_id"] in promoted for row in anomaly_only),
                "promoted_low_coverage": sum(row["episode_id"] in promoted for row in low_coverage),
                "group_counts": groups,
                "promoted_ids": promoted,
            }
        )
    result = {
        "method": "Existing evaluate_trajectory replay on deep-copied frozen frames; operating-hand, pose and speed gates unchanged; no saved trajectories rewritten.",
        "frame_validity_identical_for_all_scenarios": True,
        "anomaly_only": sorted(anomaly_only, key=lambda row: (row["anomalous_frame_count"], row["episode_id"])),
        "low_coverage": sorted(low_coverage, key=lambda row: -row["coverage"]),
        "scenarios": scenarios,
    }
    (experiment / "sensitivity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    columns = [key for key in scenarios[0] if key not in {"group_counts", "promoted_ids"}]
    with (experiment / "sensitivity.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row[key] for key in columns} for row in scenarios)
    print(
        json.dumps(
            {
                "scenarios": [{key: row[key] for key in columns} for row in scenarios],
                "low_coverage": [
                    {
                        key: row[key]
                        for key in ("episode_id", "action", "coverage", "longest_gap_s", "anomalous_frame_count")
                    }
                    for row in result["low_coverage"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
