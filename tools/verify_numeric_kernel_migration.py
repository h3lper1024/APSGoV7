"""Compare complete public runs; retired object-candidate probes live in frozen exports."""
import argparse
import json
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from tools.verify_solver_diagnostics import _write_json


def compare_runs(reference_run, candidate_run):
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    left = read(reference_run / "response.json")
    right = read(candidate_run / "response.json")
    # The complete public result includes all ordered nodes, trace identities,
    # resources and both audits. Only measured durations may differ.
    for result in (left, right):
        result["run_manifest"].pop("stage_duration_seconds")
    if left != right:
        from tools.verify_numeric_search import first_difference
        raise AssertionError(first_difference(left, right))
    for name in ("prepared_request.json", "delivery_report.json"):
        if (reference_run / name).read_bytes() != (candidate_run / name).read_bytes():
            raise AssertionError(f"{name} differs")
    return {"equal": True, "reference": str(reference_run), "candidate": str(candidate_run),
            "reference_measurement": read(reference_run / "measurement.json"),
            "candidate_measurement": read(candidate_run / "measurement.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--reference-run", type=Path)
    parser.add_argument("--compare-run", type=Path, action="append")
    args = parser.parse_args()
    if args.reference_run or args.compare_run:
        if not args.reference_run or not args.compare_run:
            parser.error("both --reference-run and --compare-run are required")
        comparisons = [compare_runs(args.reference_run, run) for run in args.compare_run]
        args.output_dir.mkdir(parents=True, exist_ok=False)
        _write_json(args.output_dir / "comparison.json", comparisons)
        print(f"matched {len(comparisons)} complete runs, excluding measured durations only")
        return
    parser.error("object candidate probing is retired; use verify_unified_numeric_search_prefix.py "
                 "or verify_native_candidate_parallel.py for the current numeric pipeline")


if __name__ == "__main__":
    main()
