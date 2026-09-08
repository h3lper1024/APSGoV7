"""Freeze the three inputs and compare the existing Python baseline, without solving."""

import argparse
import runpy
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tools")]

from apsgo_scheduler.api.request import fingerprint_public_request  # noqa: E402
from apsgo_v7_service.diagnostics import _json_values  # noqa: E402
from tests.app.test_input_normalizer import gqga4_request, gqga4_spec  # noqa: E402
from tools.compare_solver_stages import first_difference  # noqa: E402
from tools.profile_solver_search import load_request  # noqa: E402
from tools.verify_solver_diagnostics import _read_json, _sha256, _write_json  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "reference", "latest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    checks = runpy.run_path(str(
        ROOT / "docs/implementation/evidence/apsgo_v7_unchanged_chain_evaluation_reuse"
        / "stage_03_comparison/run_pairs.py"
    ))
    baseline, reference = (_read_json(path) for path in (args.baseline, args.reference))
    for sample in (baseline, reference):
        checks["require_fixed_work"](sample)
    difference = first_difference(
        checks["stable_measurement"](reference), checks["stable_measurement"](baseline),
    )
    if difference is not None:
        raise ValueError(f"Python baseline changed: {difference}")
    main_input = args.baseline.parent / "prepared_request.json"
    expected_sha = "11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c"
    if _sha256(main_input) != expected_sha:
        raise ValueError("fixed-work prepared bytes changed")
    request, latest = load_request(main_input), load_request(args.latest)
    if replace(latest, request_id=request.request_id, policy=request.policy) != request:
        raise ValueError("latest input differs beyond request id and policy")
    if replace(latest.policy, total_time_limit_seconds=request.policy.total_time_limit_seconds) != (
        request.policy
    ):
        raise ValueError("latest policy differs beyond total time")
    formal = gqga4_request.__wrapped__(gqga4_spec.__wrapped__())
    formal_payload = {
        "request": _json_values(formal),
        "request_fingerprint": fingerprint_public_request(formal),
        "rule_set_fingerprint": formal.rule_set_spec.fingerprint,
    }
    args.output.mkdir(parents=True, exist_ok=False)
    formal_path = args.output / "formal_prepared_request.json"
    _write_json(formal_path, formal_payload)
    if load_request(formal_path) != formal:
        raise ValueError("formal request did not round trip")
    summary = {
        "status": "passed", "business_first_difference": difference,
        "baseline_sha256": _sha256(args.baseline),
        "reference_sha256": _sha256(args.reference),
        "input_identities": {
            name: {
                "sha256": _sha256(path), "request_fingerprint": fingerprint_public_request(value),
                "policy": _json_values(value.policy),
            }
            for name, path, value in (
                ("fixed_work", main_input, request), ("latest_limited", args.latest, latest),
                ("formal_quality", formal_path, formal),
            )
        },
        "comparison": "existing stable_measurement: exact timing leaves only; cache still compared",
    }
    _write_json(args.output / "verification.json", summary)
    print(f"Baseline and input identity checks passed: {args.output}")


if __name__ == "__main__":
    main()
