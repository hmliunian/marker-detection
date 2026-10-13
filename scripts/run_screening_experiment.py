"""Run the frozen 100 + 3 experiment with one isolated CLI process per episode."""

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from marker_detection.cli import _settings
from marker_detection.pose import load_reference_config

ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def inputs(manifest_path, third_directory):
    manifest = json.loads(manifest_path.read_text())
    samples = manifest["samples"]
    if len(samples) != 100 or manifest["count"] != 100:
        raise ValueError("This experiment requires the requested 100 main samples")
    jobs = []
    for sample in samples:
        directory = Path(sample["source_directory"]).resolve()
        jobs.append(
            {
                "group": "main",
                "episode_id": sample["id"],
                "index": sample["index"],
                "action_id": sample["action_id"],
                "action_name": sample["action_name"],
                "manifest_frames": sample["frame_count"],
                "input": str(directory / "episode.mcap"),
                "sidecar": str(directory / f"{sample['id']}.json"),
            }
        )
    third = sorted(third_directory.glob("*.zip"))
    if len(third) != 3:
        raise ValueError("This experiment requires exactly three third-marker ZIP files")
    for index, path in enumerate(third, 1):
        jobs.append(
            {
                "group": "third",
                "episode_id": path.stem,
                "index": index,
                "action_id": "A_020",
                "action_name": "第三方介入",
                "manifest_frames": None,
                "input": str(path.resolve()),
                "sidecar": None,
            }
        )
    if len({job["episode_id"] for job in jobs}) != 103:
        raise ValueError("Duplicate episode IDs across experiment inputs")
    for job in jobs:
        for name in ("input", "sidecar"):
            if job[name] is None:
                continue
            path = Path(job[name])
            if not path.is_file():
                raise FileNotFoundError(path)
            job[name + "_sha256"] = sha256(path)
            job[name + "_size"] = path.stat().st_size
            job[name + "_mtime_ns"] = path.stat().st_mtime_ns
    return manifest, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--third", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpus", default="0,1,2,3")
    args = parser.parse_args()
    gpus = [int(item) for item in args.gpus.split(",")]
    if not gpus or len(set(gpus)) != len(gpus) or any(gpu < 0 for gpu in gpus):
        parser.error("Specify distinct nonnegative GPU indices")
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a new output directory; previous experiments are never overwritten")
    manifest, jobs = inputs(args.manifest.resolve(), args.third.resolve())
    reference = load_reference_config()
    activity, quality, pose = _settings(None, reference)
    settings = {"activity": asdict(activity), "quality": asdict(quality), "pose": asdict(pose)}
    source_paths = sorted((ROOT / "src/marker_detection").glob("*.py")) + [ROOT / "uv.lock", Path(__file__)]
    source_hashes = {str(path.relative_to(ROOT)): sha256(path) for path in source_paths}
    started = time.monotonic()
    metadata = {
        "started_at": utc_now(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": source_hashes,
        "manifest_path": str(args.manifest.resolve()),
        "manifest_sha256": sha256(args.manifest),
        "manifest_sampling": {key: manifest[key] for key in ("seed", "population_size", "sampling", "count")},
        "gpus": gpus,
        "concurrency": len(gpus),
        "environment_overrides": {name: "1" for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
        "settings": settings,
        "jobs": jobs,
        "status": "running",
    }
    output.mkdir(parents=True)
    (output / "logs").mkdir()
    save_json(output / "manifest.json", manifest)
    save_json(output / "settings.json", settings)
    save_json(output / "geometry.json", reference)
    save_json(output / "experiment.json", metadata)
    pending = queue.Queue()
    for job in jobs:
        pending.put(job)
    results, lock = [], threading.Lock()
    env = dict(os.environ, **metadata["environment_overrides"])

    def worker(gpu):
        while True:
            try:
                job = pending.get_nowait()
            except queue.Empty:
                return
            destination = output / job["group"] / job["episode_id"]
            log_path = output / "logs" / f"{job['group']}-{job['episode_id']}.log"
            command = [
                sys.executable,
                "-m",
                "marker_detection.cli",
                "analyze",
                job["input"],
                "--output",
                str(destination),
                "--geometry",
                str(output / "geometry.json"),
                "--settings",
                str(output / "settings.json"),
                "--video-backend",
                "cuda",
                "--gpu",
                str(gpu),
            ]
            if job["sidecar"]:
                command += ["--sidecar", job["sidecar"]]
            record = dict(
                job, gpu=gpu, output=str(destination), log=str(log_path), command=command, started_at=utc_now()
            )
            timer = time.monotonic()
            try:
                with log_path.open("w", encoding="utf-8") as stream:
                    completed = subprocess.run(command, env=env, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
                record["returncode"] = completed.returncode
                if completed.returncode != 0:
                    raise RuntimeError(f"Analysis CLI exited {completed.returncode}; see {log_path.name}")
                analysis = json.loads((destination / "analysis.json").read_text())
                if analysis["episode_id"] != job["episode_id"]:
                    raise ValueError("Output episode ID differs from frozen input")
                for name in ("input", "sidecar"):
                    if job[name]:
                        stat = Path(job[name]).stat()
                        if stat.st_size != job[name + "_size"] or stat.st_mtime_ns != job[name + "_mtime_ns"]:
                            raise ValueError(f"Input {name} changed while processing")
                record.update(
                    state="completed",
                    decision=analysis["decision"],
                    primary_hand=analysis["primary_hand"],
                    frames=analysis["summary"]["frames"],
                    valid_frames=analysis["summary"]["valid_frames"],
                    reasons=analysis["reasons"],
                )
            except Exception as exc:
                record.update(state="process_error", error=f"{type(exc).__name__}: {exc}")
            record.update(finished_at=utc_now(), elapsed_s=round(time.monotonic() - timer, 3))
            with lock:
                results.append(record)
                with (output / "progress.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                print(
                    f"[{len(results):03d}/103] GPU{gpu} {job['group']} {job['episode_id']} "
                    f"{record.get('decision', record['state'])} "
                    f"{record.get('valid_frames', '-')}/{record.get('frames', '-')} {record['elapsed_s']:.1f}s",
                    flush=True,
                )
            pending.task_done()

    with ThreadPoolExecutor(max_workers=len(gpus)) as pool:
        futures = [pool.submit(worker, gpu) for gpu in gpus]
        for future in futures:
            future.result()
    results.sort(key=lambda result: (result["group"] != "main", result["index"]))
    changed = [name for name, expected in source_hashes.items() if sha256(ROOT / name) != expected]
    metadata.update(
        status="completed" if all(row["state"] == "completed" for row in results) and not changed else "needs_review",
        finished_at=utc_now(),
        elapsed_s=round(time.monotonic() - started, 3),
        changed_source_files=changed,
        state_counts=dict(Counter(row["state"] for row in results)),
    )
    save_json(output / "results.json", results)
    save_json(output / "experiment.json", metadata)
    print(json.dumps({key: metadata[key] for key in ("status", "elapsed_s", "state_counts")}, ensure_ascii=False))
    return 0 if metadata["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
