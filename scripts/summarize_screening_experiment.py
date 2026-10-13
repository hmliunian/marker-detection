"""Audit every saved episode, then build CSV, figures and an offline HTML report."""

import argparse
import base64
import csv
import hashlib
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from marker_detection.io import inspect_mcap

ROOT = Path(__file__).resolve().parents[1]
STATUSES = ["accepted", "rejected", "excluded_carry", "review", "process_error"]
LABELS = dict(zip(STATUSES, ["通过", "拒绝", "搬运单列", "待复核", "无法计算"], strict=True))
COLORS = ["#188875", "#c65a43", "#8398a6", "#d2a13c", "#70579a"]
REASONS = {
    "anomalous_frames_exceed_threshold": "异常帧数超过0",
    "both_hands_have_activity_evidence": "两侧均有操作证据",
    "coverage_below_threshold": "有效覆盖率低于90%",
    "missing_interval_exceeds_threshold": "连续无效超过0.5秒",
    "other_hand_activity_unresolved": "另一侧活动状态无法确定",
    "carry_task_separate": "搬运单独标记",
}
BASELINES = {
    "b6fba8b86c4c5a00e90abe52e87c456c": "real-pickup-v2",
    "37d6a87c621c4fb273bf3ee32e12df33": "real-close-v2",
    "4bef609c55f9136b30f0b77f4d586a2b": "real-zero-touch-v2",
    "e93af50e20c6ecd056b349a0b7402b89": "real-carry-v2",
    "714ea4a2e2c56fdaf8aa1c5ab69fea70": "real-third-714-v2",
    "f0e57dbde5c5d663ebfb3e99ee148373": "real-third-f0-v2",
    "f581f9dd58db586212a2af07b0c960f1": "real-third-f5-v2",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(experiment):
    metadata = read_json(experiment / "experiment.json")
    results = read_json(experiment / "results.json")
    if len(results) != 103 or len({row["episode_id"] for row in results}) != 103:
        raise ValueError("Expected 103 unique completed attempts")
    assert Counter(row["group"] for row in results) == {"main": 100, "third": 3}
    assert not metadata["changed_source_files"]
    for name, expected in metadata["source_sha256"].items():
        assert sha256(ROOT / name) == expected, f"Experiment code changed: {name}"
    assert sha256(Path(metadata["manifest_path"])) == metadata["manifest_sha256"]
    geometry = read_json(experiment / "geometry.json")
    analyses, rows, comparisons, inspections = {}, [], {}, {}
    checked_frames = 0
    for result in results:
        identity = result["episode_id"]
        for name in ("input", "sidecar"):
            if result[name]:
                assert sha256(Path(result[name])) == result[name + "_sha256"], f"Input changed: {identity}/{name}"
        row = {
            key: result[key]
            for key in ("group", "index", "episode_id", "action_id", "action_name", "gpu", "elapsed_s", "output")
        }
        row.update(
            decision=result.get("decision", "process_error"),
            primary_hand=result.get("primary_hand"),
            frames=None,
            valid_frames=None,
            coverage=None,
            longest_gap_s=None,
            anomalous_frames=None,
            max_raw_speed_m_s=None,
            reasons="",
            error=result.get("error", ""),
        )
        if result["state"] != "completed":
            assert result.get("returncode") != 0
            inspection = inspect_mcap(result["input"])
            inspections[identity] = inspection
            write_json(experiment / f"inspect-{identity}.json", inspection)
            row["reasons"] = ";".join(inspection["warnings"])
            rows.append(row)
            continue
        assert result["returncode"] == 0
        directory = Path(result["output"])
        analysis = read_json(directory / "analysis.json")
        frames = read_json(directory / "frames.json")
        with (directory / "actions.csv").open(newline="") as stream:
            actions = list(csv.DictReader(stream))
        assert analysis["episode_id"] == identity
        assert analysis["task_id"] == result["action_id"]
        assert analysis["decision"] == result["decision"]
        assert analysis["config"]["reference"] == geometry
        # JSON round-trips dataclass tuples as lists in both frozen parameter files.
        for part in ("activity", "quality", "pose"):
            assert analysis["config"][part] == metadata["settings"][part], (identity, part)
        assert set(analysis["provenance"]["video_backends"].values()) == {f"cuda:pyav:{result['gpu']}"}
        summary = analysis["summary"]
        assert len(frames) == len(actions) == summary["frames"] == result["frames"]
        if result["manifest_frames"] is not None:
            assert len(frames) == result["manifest_frames"], f"Frame count differs from manifest: {identity}"
        valid_count = 0
        for frame, action in zip(frames, actions, strict=True):
            valid = frame["valid"] and frame["quality"]["valid"]
            valid_count += int(valid)
            assert int(action["frame_valid"]) == int(valid)
            assert int(action["episode_accepted"]) == int(analysis["decision"] == "accepted")
            assert int(action["frame_index"]) == frame["frame_index"]
            assert int(action["timestamp_ns"]) == frame["timestamp_ns"]
            assert (directory / frame["preview"]).is_file()
            if valid:
                assert [float(action[key]) for key in ("x_m", "y_m", "z_m")] == frame["xyz_m"]
            else:
                assert all(action[key] == "" for key in ("x_m", "y_m", "z_m"))
        assert valid_count == summary["valid_frames"] == result["valid_frames"]
        assert abs(summary["coverage"] - valid_count / len(frames)) < 1e-12
        if analysis["decision"] == "accepted":
            assert analysis["primary_hand_evidence"]["status"] == "single"
            assert min(summary["coverage"], summary["time_coverage"]) >= 0.9
            assert summary["longest_gap_s"] <= 0.5 and summary["anomalous_frame_count"] == 0
        if analysis["decision"] == "excluded_carry":
            assert analysis["task_id"] == "A_003" and analysis["primary_hand"] is None
        row.update(
            frames=len(frames),
            valid_frames=valid_count,
            coverage=summary["coverage"],
            longest_gap_s=summary["longest_gap_s"],
            anomalous_frames=summary["anomalous_frame_count"],
            max_raw_speed_m_s=summary["max_raw_speed_m_s"],
            reasons=";".join(analysis["reasons"]),
            primary_status=analysis["primary_hand_evidence"]["status"],
        )
        checked_frames += len(frames)
        analyses[identity] = analysis
        rows.append(row)
        if identity in BASELINES:
            old = read_json(ROOT / "artifacts" / BASELINES[identity] / "analysis.json")
            exact_fields = ("frames", "valid_frames", "anomalous_frame_count", "frame_reasons")
            same = all(analysis[key] == old[key] for key in ("primary_hand", "decision", "reasons"))
            same &= all(summary[key] == old["summary"][key] for key in exact_fields)
            same &= abs(summary["longest_gap_s"] - old["summary"]["longest_gap_s"]) < 1e-9
            comparisons[identity] = {"baseline": BASELINES[identity], "matched": same}
    verification = {
        "unique_inputs": len(rows),
        "completed_analyses": len(analyses),
        "checked_frames_and_csv_rows": checked_frames,
        "source_hashes_unchanged": True,
        "input_hashes_unchanged": True,
        "parameter_and_geometry_match": True,
        "frame_preview_and_csv_consistency": True,
        "main_frame_counts_match_manifest": True,
        "actual_cuda_backends_verified": True,
        "baseline_comparisons": comparisons,
        "failed_input_inspection_ids": list(inspections),
    }
    assert len(comparisons) == 7 and all(value["matched"] for value in comparisons.values())
    write_json(experiment / "audit.json", verification)
    return metadata, rows, analyses, verification


def summarize(rows, analyses):
    totals, by_task = {}, []
    for group in ("main", "third"):
        subset = [row for row in rows if row["group"] == group]
        counts = Counter(row["decision"] for row in subset)
        eligible = len(subset) - counts["excluded_carry"]
        totals[group] = {
            "total": len(subset),
            "counts": {key: counts[key] for key in STATUSES},
            "acceptance_rate": counts["accepted"] / len(subset),
            "noncarry_total": eligible,
            "noncarry_acceptance_rate": counts["accepted"] / eligible,
            "processed_frames": sum(row["frames"] or 0 for row in subset),
            "valid_frames": sum(row["valid_frames"] or 0 for row in subset),
        }
    tasks = defaultdict(list)
    for row in rows:
        if row["group"] == "main":
            tasks[row["action_name"]].append(row)
    for name, subset in tasks.items():
        counts = Counter(row["decision"] for row in subset)
        by_task.append({"action": name, "total": len(subset), **{key: counts[key] for key in STATUSES}})
    main_analyses = [
        analyses[row["episode_id"]] for row in rows if row["group"] == "main" and row["decision"] != "process_error"
    ]
    rejected = [item for item in main_analyses if item["decision"] == "rejected"]
    primary_counts = Counter(item["primary_hand_evidence"]["status"] for item in main_analyses)
    combinations = Counter(tuple(sorted(item["reasons"])) for item in rejected)
    only_anomaly = [item for item in rejected if item["reasons"] == ["anomalous_frames_exceed_threshold"]]
    anomaly_counts = Counter(item["summary"]["anomalous_frame_count"] for item in only_anomaly)
    single = [item for item in main_analyses if item["primary_hand_evidence"]["status"] == "single"]
    bimanual = [item for item in rejected if item["primary_hand_evidence"]["status"] == "bimanual"]
    both_touch = sum(
        all(
            item["primary_hand_evidence"]["evidence"][side]["tactile"]["contact_supported"]
            for side in ("left", "right")
        )
        for item in bimanual
    )
    return {
        "groups": totals,
        "by_action": by_task,
        "main_primary_status": dict(primary_counts),
        "main_rejection_reasons_overlapping": dict(Counter(reason for item in rejected for reason in item["reasons"])),
        "main_rejection_combinations": [{"reasons": list(key), "count": count} for key, count in combinations.items()],
        "main_anomaly_only_histogram": dict(sorted(anomaly_counts.items())),
        "main_bimanual_both_touch": both_touch,
        "main_single_hand": {
            "episodes": len(single),
            "frames": sum(item["summary"]["frames"] for item in single),
            "valid_frames": sum(item["summary"]["valid_frames"] for item in single),
            "hand_counts": dict(Counter(item["primary_hand"] for item in single)),
        },
        "per_episode_runtime_s": {
            "min": min(row["elapsed_s"] for row in rows),
            "median": median(row["elapsed_s"] for row in rows),
            "max": max(row["elapsed_s"] for row in rows),
        },
    }


def write_csv(path, rows):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def plot_summary(experiment, summary, rows):
    font = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if font.exists():
        font_manager.fontManager.addfont(font)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=font).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    figure, axes = plt.subplots(1, 3, figsize=(18, 7), layout="constrained")
    main_counts = summary["groups"]["main"]["counts"]
    bars = axes[0].bar([LABELS[key] for key in STATUSES], [main_counts[key] for key in STATUSES], color=COLORS)
    axes[0].bar_label(bars, padding=3)
    axes[0].set(title="主数据100条：固定阈值筛选", ylabel="episode 数", ylim=(0, max(main_counts.values()) * 1.15))
    tasks = sorted(summary["by_action"], key=lambda row: row["total"])
    left = [0] * len(tasks)
    for key, color in zip(STATUSES, COLORS, strict=True):
        values = [row[key] for row in tasks]
        axes[1].barh([row["action"] for row in tasks], values, left=left, color=color, label=LABELS[key])
        left = [a + b for a, b in zip(left, values, strict=True)]
    axes[1].set(title="按采集任务分布", xlabel="episode 数")
    axes[1].legend(fontsize=8, ncols=2, loc="lower right")
    for status, color in zip(STATUSES[:2], COLORS[:2], strict=True):
        points = [
            row
            for row in rows
            if row["group"] == "main" and row.get("primary_status") == "single" and row["decision"] == status
        ]
        axes[2].scatter(
            [row["coverage"] * 100 for row in points],
            [row["longest_gap_s"] for row in points],
            color=color,
            label=LABELS[status],
            alpha=0.8,
            s=28,
        )
    third = [row for row in rows if row["group"] == "third"]
    axes[2].scatter(
        [row["coverage"] * 100 for row in third],
        [row["longest_gap_s"] for row in third],
        color="#70579a",
        marker="X",
        s=80,
        label="第三方3条",
    )
    for row in third:
        axes[2].annotate(
            row["episode_id"][:3],
            (row["coverage"] * 100, row["longest_gap_s"]),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=8,
        )
    axes[2].axvline(90, color="#6a7982", linestyle="--", linewidth=1)
    axes[2].axhline(0.5, color="#6a7982", linestyle="--", linewidth=1)
    axes[2].set(title="已确认单手片段的覆盖率与缺失", xlabel="有效帧覆盖率 (%)", ylabel="最长连续无效 (s)")
    axes[2].legend(fontsize=8)
    figure.suptitle("UMI marker：100条主数据 + 3条第三方介入（2026-10-13）", fontsize=16)
    figure.savefig(experiment / "overview.png", dpi=160)
    figure.savefig(experiment / "overview.svg")
    plt.close(figure)


def table_html(headers, rows, table_id=None):
    identifier = f' id="{table_id}"' if table_id else ""
    head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table{identifier}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def html_report(experiment, metadata, summary, rows, verification):
    main = summary["groups"]["main"]
    image = base64.b64encode((experiment / "overview.png").read_bytes()).decode()
    detail_rows = []
    for row in rows:
        reason = "; ".join(REASONS.get(code, code) for code in row["reasons"].split(";"))
        detail_rows.append(
            [
                "主数据" if row["group"] == "main" else "第三方",
                row["index"],
                row["episode_id"],
                row["action_name"],
                LABELS[row["decision"]],
                {"left": "左", "right": "右", None: "—"}[row["primary_hand"]],
                f"{row['valid_frames']}/{row['frames']}" if row["frames"] else "—",
                f"{row['coverage']:.2%}" if row["coverage"] is not None else "—",
                f"{row['longest_gap_s']:.3f}" if row["longest_gap_s"] is not None else "—",
                row["anomalous_frames"] if row["anomalous_frames"] is not None else "—",
                reason,
            ]
        )
    counts = table_html(
        ["结论", "主数据100条", "第三方3条"],
        [[LABELS[key], main["counts"][key], summary["groups"]["third"]["counts"][key]] for key in STATUSES],
    )
    reasons = table_html(
        ["拒绝原因组合（不重叠）", "数量"],
        [
            ["；".join(REASONS.get(code, code) for code in row["reasons"]), row["count"]]
            for row in summary["main_rejection_combinations"]
        ],
    )
    details = table_html(
        ["组别", "序号", "episode ID", "任务", "结论", "主手", "有效/总帧", "覆盖率", "最长无效s", "异常帧", "原因"],
        detail_rows,
        "episodes",
    )
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UMI marker 全量筛选实验报告</title><style>
body{{font:15px/1.65 system-ui,sans-serif;color:#203543;background:#f3f6f8;margin:0}}main{{max-width:1400px;margin:auto;padding:28px}}h1{{font-size:28px}}h2{{margin-top:32px}}.card{{background:white;padding:20px;border-radius:10px;margin:15px 0}}img{{width:100%}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:9px 12px;border-bottom:1px solid #dae3e8;text-align:left;white-space:nowrap}}th{{background:#ecf2f5}}input{{padding:10px;width:350px;max-width:90%}}code{{background:#e9eef1;padding:2px 5px}}.lead{{font-size:21px;color:#14745f}}
</style><main><h1>UMI marker 全量筛选实验报告</h1>
<p>2026-10-13 · 固定参数 · 100条主数据与3条第三方样本分别统计</p>
<div class="card"><p class="lead">主数据通过 {main["counts"]["accepted"]}/100（{main["acceptance_rate"]:.1%}）；第三方通过 0/3。</p>
<p>排除7条搬运后：23/93 = {main["noncarry_acceptance_rate"]:.2%}。分母保留待复核和缺标定样本，不能把它们静默去掉。</p>{counts}</div>
<div class="card"><img src="data:image/png;base64,{image}" alt="筛选结果、任务分布和覆盖率图表"></div>
<h2>拒绝原因</h2><div class="card">{reasons}<p>双手判定是整个片段两侧均存在传感器活动证据，并未验证同时动作。22条中21条两侧有持续触觉，另1条为左侧触觉与右侧持续部分开度；需视频复核，不能视为真实双手操作标签。</p>
<p>26条仅因异常帧拒绝，其中8条只有1帧异常、5条只有2帧异常。本次未放宽阈值。</p></div>
<h2>方法、核对与限制</h2><div class="card"><p>固定参考marker尺寸0.0525m；有效覆盖率≥90%、连续无效≤0.5秒、异常帧容忍数0，速度≤1.5m/s、重投影误差≤1px。action为头部左相机坐标系下的米制XYZ，不做平滑或插值。</p>
<p>4张RTX5090各1个进程；CUDA用于解码，AprilTag/PnP仍在CPU运行。批处理耗时{metadata["elapsed_s"]:.3f}秒。102条成功生成分析，1条翻转数据47de444…缺左右相机标定，视频存在；无法计算单列。</p>
<p>逐条核对{verification["checked_frames_and_csv_rows"]:,}帧的JSON/CSV有效性、坐标、时间戳和预览；源数据与源码hash、参数、GPU后端通过核对；原7条参考结果全部一致。</p>
<p>23条通过包含允许范围内的短缺失，只有有效帧可以作为action。通过率是当前规则筛选产出率，不是准确率；没有逐帧身份/三维真值。仅有同ID第三方marker可见时仍无法保证身份。真实新格式未验收。</p>
<p>代码基线：<code>{metadata["git_commit"]}</code>。参数、输入hash和逐条命令见同目录experiment.json、settings.json；完整核对见audit.json。</p></div>
<h2>全部103条明细</h2><p><input id="search" placeholder="输入ID、任务、结论或原因筛选"><span id="visible"></span></p>{details}
<p>查看单条GUI：<code>just gui artifacts/screening-103-20261013/main/完整ID --port 8767</code>；第三方将main换成third。随后转发所用端口。</p>
</main><script>const search=document.getElementById('search');const records=[...document.querySelectorAll('#episodes tbody tr')];function filter(){{const value=search.value.trim().toLowerCase();let shown=0;for(const row of records){{row.hidden=!row.textContent.toLowerCase().includes(value);shown+=!row.hidden;}}document.getElementById('visible').textContent='　'+shown+' / '+records.length;}}search.addEventListener('input',filter);filter();</script></html>"""
    (experiment / "report.html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    experiment = args.experiment.resolve()
    metadata, rows, analyses, verification = audit(experiment)
    summary = summarize(rows, analyses)
    write_json(experiment / "summary.json", summary)
    write_json(experiment / "episode_summary.json", rows)
    write_csv(experiment / "episodes.csv", rows)
    write_csv(experiment / "main-100.csv", [row for row in rows if row["group"] == "main"])
    write_csv(experiment / "third-3.csv", [row for row in rows if row["group"] == "third"])
    write_csv(experiment / "accepted.csv", [row for row in rows if row["decision"] == "accepted"])
    plot_summary(experiment, summary, rows)
    html_report(experiment, metadata, summary, rows, verification)
    print(json.dumps({"groups": summary["groups"], "audit": verification}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
