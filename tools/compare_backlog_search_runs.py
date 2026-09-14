"""Compare frozen runs; policy changes and non-publishable diagnostics require explicit opt-in."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
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
from apsgo_scheduler.core.chain_order import has_backlog_priority, has_second_precision_delivery
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.app.delivery_request import with_delivery_objective
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
from tools.profile_solver_search import load_request
from tools.replay_backlog_search_witnesses import load_case, read_json, report_with_positions
from tools.verify_solver_diagnostics import _sha256, _write_json


def backlog_summary(orders):
    backlog = sorted((row for row in orders if row["was_backlog_at_start"]),
                     key=lambda row: row["completion_hours"])
    weight = sum_weights(row["original_weight"] for row in backlog)
    burden = sum_weights(row.get("raw_delivery_wait_tardiness_tonne_hours", row["delivery_wait_tardiness_tonne_hours"])
                         for row in backlog)
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


def read_run(path, *, allow_diagnostic=False):
    measurement = read_json(path / "measurement.json")
    result, final = measurement["result"], measurement["final_search"]
    published = bool(result["release"])
    both_audits = result["core_audit"]["passed"] and result["audit_report"]["passed"]
    if (published and not both_audits) or (not published and not allow_diagnostic):
        raise ValueError(f"both release audits must pass: {path}")
    candidate = result["release"] if published else result["diagnostic_candidate"]
    if candidate is None:
        raise ValueError(f"comparison requires an actual candidate: {path}")
    state, context = load_case(path)  # Recompute the complete evaluation and verify its frozen identity.
    if tuple(final["quality"]) != state.current_evaluation.quality_key:
        raise ValueError(f"summary quality differs from recomputed evaluation: {path}")
    for key in ("candidate_check_count", "complete_candidate_evaluation_count", "accepted_move_count"):
        if final[key] != result["run_manifest"]["counters"][key]:
            raise ValueError(f"summary and result counters differ: {key}")
    if candidate["plan"] != final["plan"] or candidate["evaluation" if published else "search_evaluation"] != final["evaluation"]:
        raise ValueError(f"returned candidate and final search differ: {path}")
    report = report_with_positions(state.current_plan, context.factory.cache.context.delivery_timing,
                                   include_backlog_clearance=has_backlog_priority(context.factory.cache.rule_set),
                                   second_precision=has_second_precision_delivery(context.factory.cache.rule_set))
    stored = read_json(path / "delivery_report.json")
    expected_kind = "audited_release" if published else "diagnostic_candidate_not_publishable"
    if stored.get("kind") != expected_kind:
        raise ValueError(f"report publication label differs: {path}")
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
        both_audits_passed=both_audits, source_conservation_passed=True,
        quality_gate="NOT_PUBLISHABLE" if not published else "PASS" if quality["prohibited_violation_count"] == quality["underweight_chain_count"] == 0
            and virtual_weight / total <= Decimal("0.05") else "BEST_EFFORT",
        wall_seconds=measurement["public_call_wall_seconds"], cpu_seconds=measurement["public_call_cpu_seconds"],
        stage_seconds=result["run_manifest"]["stage_duration_seconds"],
        counters=result["run_manifest"]["counters"], stop_reason=final["stop_reason"],
        accepted_actions=dict(Counter(row["action_name"] for row in final["trace"])),
        virtual_count=len(virtual), virtual_weight=virtual_weight, virtual_ratio=virtual_weight / total,
        violations=final["evaluation"]["violations"],
        backlog=backlog_summary(report["delivery_orders"]), delivery=report["delivery_summary"],
        due_groups={day: dict(order_count=len(rows), newly_late_count=sum(row["newly_late"] for row in rows),
                    newly_late_weight=sum_weights(row["original_weight"] for row in rows if row["newly_late"]),
                    tardiness_tonne_hours=sum_weights(row.get("raw_delivery_wait_tardiness_tonne_hours", row["delivery_wait_tardiness_tonne_hours"]) for row in rows))
                    for day, rows in sorted(due_groups.items())},
    )
    if not published:
        summary.update(kind=expected_kind, core_audit=result["core_audit"], application_audit=result["audit_report"])
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


def verify_comparison_requests(old, new, *, backlog_priority=False, second_precision=False):
    if second_precision:
        if backlog_priority or replace(new, rule_set_spec=old.rule_set_spec) != old:
            raise ValueError("second-precision comparison may change only its approved rule extension")
        spec = old.rule_set_spec
        seven = replace(spec, version=spec.version.removesuffix("+delivery-backlog-priority-v1"),
                        rules=spec.rules[:-1], quality_spec=(*spec.quality_spec[:2], *spec.quality_spec[4:]))
        seven = replace(seven, fingerprint=fingerprint_rule_set_spec(seven))
        if (with_delivery_objective(seven, include_backlog_clearance=True) != spec
                or with_delivery_objective(seven, include_backlog_clearance=True, second_precision=True) != new.rule_set_spec):
            raise ValueError("comparison requires the exact approved second-precision extension")
        return
    if not backlog_priority:
        if old == new:
            return
        raise ValueError("comparison requires identical typed requests, rules, timing and budgets")
    if replace(new, rule_set_spec=old.rule_set_spec) != old:
        raise ValueError("backlog-priority comparison may change only the rule-set extension")
    # Reuse the production preparation contract instead of accepting arbitrary rule/priority differences.
    spec = old.rule_set_spec
    seven = replace(spec, version=spec.version.removesuffix("+delivery-v1"), rules=spec.rules[:-1],
                    quality_spec=(*spec.quality_spec[:4], *spec.quality_spec[6:]))
    seven = replace(seven, fingerprint=fingerprint_rule_set_spec(seven))
    if with_delivery_objective(seven) != spec or with_delivery_objective(seven, include_backlog_clearance=True) != new.rule_set_spec:
        raise ValueError("comparison requires the exact approved backlog-priority extension")


def compare(old_path, new_path, *, backlog_priority=False, allow_diagnostic=False, second_precision=False):
    old_request, old, old_report, old_summary = read_run(old_path, allow_diagnostic=allow_diagnostic)
    new_request, new, new_report, new_summary = read_run(new_path, allow_diagnostic=allow_diagnostic)
    verify_comparison_requests(old_request, new_request, backlog_priority=backlog_priority, second_precision=second_precision)
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
        identical_request=old_request == new_request, first_search_equal=old["first_search"] == new["first_search"],
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
    if backlog_priority:
        summary["scope"] = "same_input_approved_backlog_priority_extension_not_performance_acceptance"
        summary["initial_plan_equal"] = old["first_search"]["initial"]["plan"] == new["first_search"]["initial"]["plan"]
        for item in summary["runs"]:
            named = {**item["quality"], "old_backlog_last_completion_hours":
                     item["backlog"]["clearance"].get("100", {}).get("completion_hours", Decimal(0))}
            item["backlog_priority_projection_not_original_score"] = {
                criterion.metric_key: named[criterion.metric_key] for criterion in new_request.rule_set_spec.quality_spec}
    if second_precision:
        summary["scope"] = "same_input_approved_second_precision_and_underweight_priority_not_performance_acceptance"
        summary["initial_plan_equal"] = old["first_search"]["initial"]["plan"] == new["first_search"]["initial"]["plan"]
        _, new_context = load_case(new_path)
        for item in summary["runs"]:
            state, _ = load_case(Path(item["path"]))
            evaluation = evaluate_plan(state.current_plan, new_context.factory.cache.rule_set,
                                       new_context.factory.cache.context)
            item["second_precision_projection_not_original_score"] = {
                criterion.metric_key: value for criterion, value in zip(new_request.rule_set_spec.quality_spec, evaluation.quality_key)}
    if backlog_priority or second_precision:
        without_scores = [[{k: v for k, v in row.items() if k not in ("quality_before", "quality_after")}
                           for row in trace] for trace in traces]
        count = 0
        for a, b in zip(*without_scores):
            if a != b:
                break
            count += 1
        summary["common_acceptance_prefix_excluding_scores"] = count
        summary["first_different_acceptance_excluding_scores"] = {
            name: trace[count] if count < len(trace) else None for name, trace in zip(("old", "new"), traces)}
    if allow_diagnostic:
        summary["includes_non_publishable_diagnostic"] = any(not row["both_audits_passed"] for row in summary["runs"])
    return summary, changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--observed-repeat", type=Path)
    parser.add_argument("--backlog-priority", action="store_true", help="Permit only the approved scoring extension")
    parser.add_argument("--second-precision", action="store_true", help="Compare historical backlog priority with the approved second-precision order")
    parser.add_argument("--allow-diagnostic", action="store_true", help="Label non-publishable candidates; never mark their audits passed")
    args = parser.parse_args()
    summary, changes = compare(args.old, args.new, backlog_priority=args.backlog_priority,
                               allow_diagnostic=args.allow_diagnostic, second_precision=args.second_precision)
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
