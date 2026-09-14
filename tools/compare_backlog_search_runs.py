"""Compare two audited runs with identical requests; never run or change a solver."""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from itertools import groupby
from pathlib import Path
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.core.contracts import sum_weights
from apsgo_scheduler.core.chain_order import has_backlog_priority
from tools.profile_solver_search import load_request
from tools.replay_backlog_search_witnesses import load_case, read_json, report_with_positions
from tools.verify_solver_diagnostics import _sha256, _write_json


def backlog_summary(orders):
    backlog = sorted((row for row in orders if row["was_backlog_at_start"]),
                     key=lambda row: row["completion_hours"])
    weight = sum_weights(row["original_weight"] for row in backlog)
    burden = sum_weights(row["delivery_wait_tardiness_tonne_hours"] for row in backlog)
    milestones, daily = {}, defaultdict(list)
    completed = Decimal(0)
    for hours, group in groupby(backlog, key=lambda row: row["completion_hours"]):
        rows = list(group)
        completed += sum_weights(row["original_weight"] for row in rows)
        for percent in (50, 90, 100):
            if str(percent) not in milestones and completed * 100 >= weight * percent:
                milestones[str(percent)] = dict(completion_hours=hours, completion_at=rows[0]["completion_at"],
                                               completed_original_weight=completed)
        for row in rows:
            daily[row["completion_at"][:10]].append(row["original_weight"])
    # Include ties at the tail cutoff, including every last-finishing original.
    cutoff = backlog[max(0, len(backlog) - 10)]["completion_hours"] if backlog else None
    return dict(order_count=len(backlog), original_weight=weight, burden_tonne_hours=burden,
                weighted_mean_wait_hours=burden / weight if weight else None,
                clearance=milestones,
                completed_original_weight_by_date={day: sum_weights(values) for day, values in daily.items()},
                tail=[row for row in backlog if row["completion_hours"] >= cutoff])


def read_run(path):
    measurement = read_json(path / "measurement.json")
    result, final = measurement["result"], measurement["final_search"]
    if not result["release"] or not result["core_audit"]["passed"] or not result["audit_report"]["passed"]:
        raise ValueError(f"both release audits must pass: {path}")
    state, context = load_case(path)  # Recompute the complete evaluation and verify its frozen identity.
    if tuple(final["quality"]) != state.current_evaluation.quality_key:
        raise ValueError(f"summary quality differs from recomputed evaluation: {path}")
    for key in ("candidate_check_count", "complete_candidate_evaluation_count", "accepted_move_count"):
        if final[key] != result["run_manifest"]["counters"][key]:
            raise ValueError(f"summary and result counters differ: {key}")
    if result["release"]["plan"] != final["plan"] or result["release"]["evaluation"] != final["evaluation"]:
        raise ValueError(f"release and final search differ: {path}")
    report = report_with_positions(state.current_plan, context.factory.cache.context.delivery_timing,
                                   include_backlog_clearance=has_backlog_priority(context.factory.cache.rule_set))
    stored = read_json(path / "delivery_report.json")
    bare_orders = [{key: value for key, value in row.items() if key != "pieces"} for row in report["delivery_orders"]]
    if (bare_orders != stored["delivery_orders"] or report["delivery_summary"] != stored["delivery_summary"]
            or report["delivery_nodes"] != stored["delivery_nodes"]):
        raise ValueError(f"recomputed delivery report differs: {path}")
    request = load_request(path / "prepared_request.json")
    actual = defaultdict(list)
    nodes = [node for chain in state.current_plan.chains for node in chain.nodes]
    for node in nodes:
        if node.source_order_id is not None:
            actual[node.source_order_id].append(node.weight)
    if {key: sum_weights(weights) for key, weights in actual.items()} != {o.source_order_id: o.weight for o in request.orders}:
        raise ValueError(f"original source coverage/weight mismatch: {path}")
    virtual = [node for node in nodes if node.source_order_id is None]
    total = sum_weights(node.weight for node in nodes)
    virtual_weight = sum_weights(node.weight for node in virtual)
    quality = {metric.metric_key: value for metric, value in zip(request.rule_set_spec.quality_spec, final["quality"])}
    due_groups = defaultdict(list)
    for row in report["delivery_orders"]:
        if not row["was_backlog_at_start"]:
            due_groups[row["due_date"]].append(row)
    summary = dict(
        path=str(path), hashes={name: _sha256(path / name) for name in (
            "input_binding.json", "prepared_request.json", "execution.log", "measurement.json", "delivery_report.json")},
        code=read_json(path / "input_binding.json")["code"], status=result["status"], quality=quality,
        both_audits_passed=True, source_conservation_passed=True,
        quality_gate="PASS" if quality["prohibited_violation_count"] == quality["underweight_chain_count"] == 0
            and virtual_weight / total <= Decimal("0.05") else "BEST_EFFORT",
        wall_seconds=measurement["public_call_wall_seconds"], cpu_seconds=measurement["public_call_cpu_seconds"],
        stage_seconds=result["run_manifest"]["stage_duration_seconds"],
        counters=result["run_manifest"]["counters"], stop_reason=final["stop_reason"],
        accepted_actions=dict(Counter(row["action_name"] for row in final["trace"])),
        virtual_count=len(virtual), virtual_weight=virtual_weight, virtual_ratio=virtual_weight / total,
        violations=result["release"]["evaluation"]["violations"],
        backlog=backlog_summary(report["delivery_orders"]), delivery=report["delivery_summary"],
        due_groups={day: dict(order_count=len(rows), newly_late_count=sum(row["newly_late"] for row in rows),
                    newly_late_weight=sum_weights(row["original_weight"] for row in rows if row["newly_late"]),
                    tardiness_tonne_hours=sum_weights(row["delivery_wait_tardiness_tonne_hours"] for row in rows))
                    for day, rows in sorted(due_groups.items())},
    )
    if (path / "search_opportunities.json").exists():
        opportunities = read_json(path / "search_opportunities.json")
        start_line = next(line for line in (path / "execution.log").read_text().splitlines()
                          if "solver_stage_started stage=width_optimization " in line)
        for recorded, field in (("candidate_checks", "candidate_check_count"),
                                ("complete_evaluations", "complete_candidate_evaluation_count"),
                                ("accepted", "accepted_move_count")):
            start_count = int(re.search(rf"\b{field}=(\d+)", start_line).group(1))
            if sum(item[recorded] for item in opportunities["actions"].values()) != final[field] - start_count:
                raise ValueError(f"post-refinement observation counters do not close: {field}")
        summary["search_opportunities"] = opportunities
        summary["hashes"]["search_opportunities.json"] = _sha256(path / "search_opportunities.json")
    return request, measurement, report, summary


def compare(old_path, new_path):
    old_request, old, old_report, old_summary = read_run(old_path)
    new_request, new, new_report, new_summary = read_run(new_path)
    if old_request != new_request:
        raise ValueError("comparison requires identical typed requests, rules, timing and budgets")
    before = {row["source_order_id"]: row for row in old_report["delivery_orders"]}
    changes = []
    for row in new_report["delivery_orders"]:
        previous = before[row["source_order_id"]]
        delta = row["completion_hours"] - previous["completion_hours"]
        changes.append(dict(source_order_id=row["source_order_id"], old=previous, new=row,
                            completion_hours_delta=delta,
                            change="earlier" if delta < 0 else "later" if delta > 0 else "unchanged"))
    traces = [run["final_search"]["trace"] for run in (old, new)]
    common = 0
    for before_event, after_event in zip(*traces):
        if before_event != after_event:
            break
        common += 1
    summary = dict(scope="same_request_search_only_comparison_not_new_scoring_or_performance_acceptance",
        identical_request=True, first_search_equal=old["first_search"] == new["first_search"],
        final_search_equal=old["final_search"] == new["final_search"],
        delivery_report_equal=old_report == new_report,
        common_accepted_prefix=common,
        first_different_acceptance={key: trace[common] if common < len(trace) else None
                                   for key, trace in zip(("old", "new"), traces)},
        runs=[old_summary, new_summary],
        completion_changes={group: dict(Counter(row["change"] for row in changes
                            if row["new"]["was_backlog_at_start"] == backlog))
                            for group, backlog in (("backlog", True), ("not_backlog", False))},
        rescued_order_ids=[row["source_order_id"] for row in changes if row["old"]["newly_late"] and not row["new"]["newly_late"]],
        regressed_order_ids=[row["source_order_id"] for row in changes if not row["old"]["newly_late"] and row["new"]["newly_late"]],
    )
    return summary, changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--observed-repeat", type=Path)
    args = parser.parse_args()
    summary, changes = compare(args.old, args.new)
    if args.observed_repeat is not None:
        repeated, _ = compare(args.new, args.observed_repeat)
        checks = {key: repeated[key] for key in ("first_search_equal", "final_search_equal", "delivery_report_equal")}
        if not all(checks.values()) or "search_opportunities" not in repeated["runs"][1]:
            raise ValueError("observed repeat must retain identical business results and provide closed counters")
        summary["observed_repeat"] = dict(checks=checks, run=repeated["runs"][1])
    args.output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(args.output_dir / "order_changes.json", changes)
    summary["order_changes_sha256"] = _sha256(args.output_dir / "order_changes.json")
    _write_json(args.output_dir / "summary.json", summary)
    print(f"Compared {len(changes)} original orders; {args.output_dir}")


if __name__ == "__main__":
    main()
