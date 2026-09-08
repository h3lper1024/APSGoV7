"""Run the approved five alternating first-search pairs; retain every attempt."""

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def stable_phases(measurement):
    return [
        {key: value for key, value in item.items() if key not in {"wall_seconds", "cpu_seconds"}}
        for item in measurement["first_search_timing"]["functions"]
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-root", "new-root", "baseline-run", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    for name in ("old_root", "new_root", "baseline_run", "output"):
        setattr(args, name, getattr(args, name).absolute())
    sys.path[:0] = [str(args.new_root), str(args.new_root / "src")]
    from tools.verify_solver_diagnostics import _code_identity, _read_json, _sha256, _write_json

    prepared = args.baseline_run / "prepared_request.json"
    reference = _read_json(args.baseline_run / "measurement.json")
    reference_identity = _read_json(args.baseline_run / "input_identity.json")
    roots = {"old": args.old_root, "new": args.new_root}
    sources = {version: _code_identity(root) for version, root in roots.items()}
    if sources["old"]["production_source_sha256"] != reference_identity["source"]["production_source_sha256"]:
        raise ValueError("old source differs from frozen reference")
    old_files, new_files = (sources[version]["production_source_files"] for version in roots)
    changed = {name for name in old_files.keys() | new_files.keys()
               if old_files.get(name) != new_files.get(name)}
    if changed != {"src/apsgo_scheduler/core/virtual_material.py"}:
        raise ValueError("source difference exceeds approved bridge reuse")
    for root in roots.values():
        if _sha256(root / "tools/profile_solver_search.py") != reference_identity["tool_sha256"]:
            raise ValueError("measurement tool changed")
    args.output.mkdir(parents=True, exist_ok=False)
    _write_json(args.output / "expected_sources.json", sources)
    samples = []
    for pair in range(1, 6):
        for version in (("old", "new") if pair % 2 else ("new", "old")):
            name = f"pair_{pair:02d}_{version}"
            command = [
                sys.executable, str(roots[version] / "tools/profile_solver_search.py"),
                "--prepared-request", str(prepared),
                "--output-dir", str(args.output / name), "--scope", "first",
            ]
            record = {"pair": pair, "version": version, "command": command,
                      "started_at": datetime.now(timezone.utc).isoformat()}
            print(f"Starting {name} at {record['started_at']}", flush=True)
            with (args.output / (name + ".log")).open("x", encoding="utf-8") as stream:
                process = subprocess.run(
                    command, cwd=roots[version], stdout=stream, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
                )
            record.update(returncode=process.returncode,
                          finished_at=datetime.now(timezone.utc).isoformat())
            if process.returncode == 0:
                measurement = _read_json(args.output / name / "measurement.json")
                identity = _read_json(args.output / name / "input_identity.json")
                record.update(
                    first_search_equal=measurement["first_search"] == reference["first_search"],
                    phase_counters_equal=stable_phases(measurement) == stable_phases(reference),
                    input_equal=all(identity[key] == reference_identity[key] for key in (
                        "prepared_request_sha256", "original_request_fingerprint",
                        "measured_request_fingerprint", "original_policy", "measured_policy",
                    )),
                    wall_seconds=measurement["first_search_timing"]["wall_seconds"],
                    cpu_seconds=measurement["first_search_timing"]["cpu_seconds"],
                    source_sha256=identity["source"]["production_source_sha256"],
                    source_equal=identity["source"]["production_source_sha256"]
                    == sources[version]["production_source_sha256"],
                )
            _write_json(args.output / (name + "_summary.json"), record)
            samples.append(record)
            if process.returncode or not all(record[key] for key in (
                "first_search_equal", "phase_counters_equal", "input_equal", "source_equal",
            )):
                raise RuntimeError(f"pair failed; retained evidence: {name}")
            print(f"Completed {name}: wall={record['wall_seconds']} cpu={record['cpu_seconds']}",
                  flush=True)
    medians = {
        version: {key: statistics.median(item[key] for item in samples if item["version"] == version)
                  for key in ("wall_seconds", "cpu_seconds")}
        for version in roots
    }
    report = {"samples": samples, "medians": medians, "status": "pass",
              "wall_reduction_percent": (Decimal(1) - medians["new"]["wall_seconds"]
                                         / medians["old"]["wall_seconds"]) * 100}
    _write_json(args.output / "summary.json", report)
    print(json.dumps({"status": "pass", "medians": medians}, default=str), flush=True)


if __name__ == "__main__":
    main()
