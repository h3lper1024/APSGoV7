"""Read-only comparison of retained runs; report gates independently of publication."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.core.contracts import sum_weights
from tools.profile_solver_search import load_request


def read(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--repeat", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    paths = (args.old, args.new, args.repeat)
    runs = [read(path / "measurement.json") for path in paths]
    reports = [read(path / "delivery_report.json") for path in paths]
    new, repeat = runs[1:]
    deterministic = {
        key: new["final_search"][key] == repeat["final_search"][key]
        for key in ("plan", "evaluation", "trace", "candidate_check_count", "complete_candidate_evaluation_count", "accepted_move_count", "stop_reason")
    }
    deterministic["delivery_report"] = reports[1] == reports[2]
    summaries = []
    for path, run, report in zip(paths, runs, reports):
        result = run["result"]
        release = result["release"]
        nodes = [n for chain in release["plan"]["chains"] for n in chain["nodes"]]
        real = sum_weights(n["weight"] for n in nodes if n["material_role"] != "virtual_sphc")
        virtual = sum_weights(n["weight"] for n in nodes if n["material_role"] == "virtual_sphc")
        quality = release["evaluation"]["quality_key"]
        spec = load_request(path / "prepared_request.json").rule_set_spec
        named_quality = dict(zip((c.metric_key for c in spec.quality_spec), quality, strict=True))
        gate = named_quality["prohibited_violation_count"] == 0 and named_quality["underweight_chain_count"] == 0 and virtual / (real + virtual) <= Decimal("0.05") and result["core_audit"]["passed"] and result["audit_report"]["passed"]
        daily = defaultdict(list)
        for order in report["delivery_orders"]:
            if order["was_backlog_at_start"]:
                daily[order["completion_at"][:10]].append(order["original_weight"])
        summaries.append(dict(
            public_status=result["status"], quality_gate="PASS" if gate else "BEST_EFFORT",
            quality=quality, named_quality=named_quality, rule_set_fingerprint=spec.fingerprint,
            wall_seconds=run["public_call_wall_seconds"], cpu_seconds=run["public_call_cpu_seconds"],
            **{key: run["final_search"][key] for key in ("candidate_check_count", "complete_candidate_evaluation_count", "accepted_move_count", "stop_reason")},
            both_audits_passed=result["core_audit"]["passed"] and result["audit_report"]["passed"],
            real_weight=real, virtual_weight=virtual, virtual_ratio=virtual / (real + virtual),
            stage_seconds=result["run_manifest"]["stage_duration_seconds"],
            first_search_functions=run["first_search_timing"]["functions"],
            accepted_action_counts=dict(Counter(item["action_name"] for item in run["final_search"]["trace"])),
            violations=release["evaluation"]["violations"],
            old_backlog_completed_weight_by_date={day: sum_weights(weights) for day, weights in sorted(daily.items())},
            **report["delivery_summary"],
        ))
    old_orders = {item["source_order_id"]: item for item in reports[0]["delivery_orders"]}
    changes = []
    for item in reports[1]["delivery_orders"]:
        old = old_orders[item["source_order_id"]]
        delta = item["completion_hours"] - old["completion_hours"]
        changes.append(dict(
            source_order_id=item["source_order_id"], due_date=item["due_date"], original_weight=item["original_weight"],
            old_completion_at=old["completion_at"], new_completion_at=item["completion_at"],
            completion_hours_delta=delta, change="earlier" if delta < 0 else "later" if delta > 0 else "unchanged",
            old_classification=old["classification"], new_classification=item["classification"],
        ))
    output = dict(summaries=summaries, deterministic=deterministic,
                  changed_original_order_counts=dict(Counter(item["change"] for item in changes)), order_changes=changes,
                  acceptance="PASS" if summaries[1]["quality_gate"] == "PASS" and all(deterministic.values()) else "BEST_EFFORT")
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(dumps_exact_json(output) + "\n")
    print(dumps_exact_json({key: value for key, value in output.items() if key not in ("summaries", "order_changes")}))


if __name__ == "__main__":
    main()
