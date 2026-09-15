"""Compare ordered search evidence, not just the final nine score values."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.replay_backlog_search_witnesses import load_case, read_json, report_with_positions
from tools.profile_solver_search import load_request
from tools.verify_solver_diagnostics import _write_json, _sha256
from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.core.contracts import fingerprint


def signature(run, *, allow_evaluation_count_change=False):
    request = load_request(run / "prepared_request.json")
    data = read_json(run / "measurement.json")
    state, context = load_case(run, require_virtual_sequence=True)
    snapshots = {"first": data["first_search"]["final"],
                 "pre_refinement": data["pre_refinement"], "final": data["final_search"]}
    result = {"request": fingerprint_public_request(request)}
    for name, snapshot in snapshots.items():
        result[name] = {key: snapshot[key] for key in (
            "plan_fingerprint", "evaluation_fingerprint", "trace_fingerprint",
            "candidate_check_count", "complete_candidate_evaluation_count",
            "accepted_move_count", "virtual_sequence", "stop_reason")}
        # Also check raw carriers; a stale stored fingerprint must not hide a difference.
        result[name]["raw"] = fingerprint({key: snapshot[key] for key in ("plan", "evaluation", "trace")})
        if allow_evaluation_count_change:
            del result[name]["complete_candidate_evaluation_count"]
    result["split_counts"] = {key: value for key, value in data["result"]["run_manifest"]["counters"].items()
                              if "split" in key or "borrow_return" in key}
    result["audits"] = {key: data["result"][key]["passed"] for key in ("core_audit", "audit_report")}
    if not all(result["audits"].values()):
        raise ValueError("both final audits must pass")
    result["delivery"] = fingerprint(report_with_positions(
        state.current_plan, context.factory.cache.context.delivery_timing,
        include_backlog_clearance=True, second_precision=True))
    return result


def first_difference(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                return f"{path}/{key}: missing"
            difference = first_difference(left[key], right[key], f"{path}/{key}")
            if difference:
                return difference
    elif left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-evaluation-count-change", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    reference = signature(args.reference, allow_evaluation_count_change=args.allow_evaluation_count_change)
    report = {"reference": str(args.reference), "signature": reference,
              "reference_sha256": _sha256(args.reference / "measurement.json")}
    if args.candidate:
        candidate = signature(args.candidate, allow_evaluation_count_change=args.allow_evaluation_count_change)
        report.update(candidate=str(args.candidate), candidate_signature=candidate,
                      first_difference=first_difference(reference, candidate))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(args.output, report)
    return int(bool(report.get("first_difference")))


if __name__ == "__main__":
    raise SystemExit(main())
