"""Measure the complete fixed reference process, including input and output I/O."""

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from capture_solverpy_reference import load_manifest, resolve_path, solver_argv, verify_manifest
from compare_solver_stages import compare_artifacts


def summarize(seconds: list[float]) -> dict:
    if not seconds or any(not math.isfinite(value) or value < 0 for value in seconds):
        raise ValueError("Expected nonempty finite nonnegative elapsed seconds")
    ordered = sorted(seconds)
    return {
        "sample_count": len(seconds),
        "median_seconds": statistics.median(ordered),
        "p95_seconds": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_seconds": ordered[-1],
        "median_method": "mean of middle two for even sample size",
        "p95_method": "nearest rank: sorted[ceil(0.95 * n) - 1]",
    }


def measure(manifest: dict) -> dict:
    verify_manifest(manifest)
    with tempfile.TemporaryDirectory(prefix="apsgo-reference-benchmark-") as directory:
        destination = Path(directory)
        command = [
            sys.executable,
            str(resolve_path(manifest["script"]["path"])),
            *solver_argv(manifest, destination),
        ]
        started = time.perf_counter()
        timed_out = False
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=600)
            returncode, stdout, stderr = process.returncode, process.stdout, process.stderr
        except subprocess.TimeoutExpired as error:
            timed_out = True
            returncode = None
            stdout, stderr = error.stdout or "", error.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
        elapsed = time.perf_counter() - started
        run = {}
        try:
            verify_manifest(manifest)
            if timed_out:
                comparison = {"status": "fail", "error": "outer process exceeded 600 seconds"}
            else:
                comparison = compare_artifacts(manifest, destination)
                run = json.loads((destination / "run_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            comparison = {"status": "fail", "error": str(error)}
        return {
            "elapsed_seconds": elapsed,
            "command": command,
            "returncode": returncode,
            "timed_out": timed_out,
            "comparison": comparison,
            "candidate_checks": run.get("candidate_checks"),
            "stop_reason": run.get("stop_reason"),
            "quality": run.get("best_quality_vector"),
            "reference_internal_runtime_seconds": run.get("actual_runtime_seconds"),
            "stdout": stdout,
            "stderr": stderr,
        }


def save_report(output: Path, report: dict) -> None:
    # A unique sibling avoids following an unrelated or symlinked <output>.tmp.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output.parent, prefix=output.name + ".", delete=False
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    Path(stream.name).replace(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["reference"])
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.warmup < 0 or args.samples < 1:
        parser.error("warmup must be nonnegative and samples must be positive")
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists; choose a new path to preserve previous evidence")
    manifest = load_manifest(args.reference_manifest)
    verify_manifest(manifest)
    report = {
        "schema_version": 1,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": hashlib.sha256(args.reference_manifest.read_bytes()).hexdigest(),
        "environment": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "parameters": manifest["parameters"],
        "measurement_scope": "Outer process wall time from before launch until exit, including I/O",
        "warmup_requested": args.warmup,
        "samples_requested": args.samples,
        "warmup": [],
        "samples": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    save_report(args.output, report)
    try:
        for kind, count in (("warmup", args.warmup), ("samples", args.samples)):
            for index in range(count):
                sample = measure(manifest)
                sample["index"] = index + 1
                report[kind].append(sample)
                save_report(args.output, report)
                print(
                    json.dumps(
                        {
                            "phase": kind,
                            "index": index + 1,
                            "seconds": sample["elapsed_seconds"],
                            "comparison": sample["comparison"]["status"],
                            "first_difference": sample["comparison"].get("first_difference"),
                        }
                    ),
                    flush=True,
                )
                if sample["returncode"] or sample["comparison"]["status"] != "pass":
                    raise ValueError("Reference replay differs; sample not silently discarded")
        report["statistics"] = summarize([item["elapsed_seconds"] for item in report["samples"]])
        report["status"] = "pass"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["status"] = "fail"
        report["error"] = str(error)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_report(args.output, report)
    print(
        json.dumps({"status": report["status"], "statistics": report.get("statistics")}), flush=True
    )
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
