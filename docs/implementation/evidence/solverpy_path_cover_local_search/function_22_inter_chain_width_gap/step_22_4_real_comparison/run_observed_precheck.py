"""Observe one selected revision's public precheck without changing its six output files."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

PRECHECK = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_21_complete_acceptance/quality_precheck.py"
)
METRIC = "inter_chain_width_gap"
PREFIX_LIMIT = 16
BOUNDARY_FIELDS = (
    "boundary_index",
    "left_chain_id",
    "right_chain_id",
    "left_period",
    "right_period",
    "left_node_id",
    "right_node_id",
    "left_role",
    "right_role",
    "left_width",
    "right_width",
    "absolute_gap",
    "cross_period",
)


def load_checker(code_root):
    """Select the archive before importing any scheduler or development fixture modules."""
    root = Path(code_root).resolve(strict=True)
    locations = {
        "apsgo_scheduler": root / "src/apsgo_scheduler",
        "tests": root / "tests",
        "capture_solverpy_reference": root / "tools",
    }
    for name, module in tuple(sys.modules.items()):
        for prefix, directory in locations.items():
            if name == prefix or name.startswith(prefix + "."):
                filename = getattr(module, "__file__", None)
                if filename is not None and not Path(filename).resolve().is_relative_to(directory):
                    raise ValueError("run each code root in a separate Python process")
    for directory in (root, root / "src", root / "tools"):
        sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location("observed_quality_precheck", root / PRECHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def counters(context):
    return {
        "candidate_check_count": context.factory.budget.candidate_check_count,
        "complete_candidate_evaluation_count": context.complete_candidate_evaluation_count,
        "accepted_move_count": len(context.accepted_move_traces),
    }


@contextmanager
def observe_precheck(checker):
    """Record existing values only; never call a budget, cancellation token or solver clock."""
    neighborhoods = importlib.import_module("apsgo_scheduler.core.neighborhoods")
    initial = importlib.import_module("apsgo_scheduler.core.initial_solution")
    split = importlib.import_module("apsgo_scheduler.core.controlled_split")
    observed = {
        "stages": [],
        "enumeration_prefix": [],
        "first_group_change": None,
        "core_calls": [],
        "public_results": [],
        "order_plans": [],
    }
    active_round = ["initial"]

    def stage_wrapper(name, original):
        def call(state, context):
            row = {"stage": name, "round": active_round[0], "before": counters(context)}
            observed["stages"].append(row)
            child_start = len(observed["stages"])
            plan_before = state.current_plan
            quality_before = state.current_evaluation.quality_key
            started = perf_counter()
            try:
                return original(state, context)
            finally:
                elapsed = perf_counter() - started
                after = counters(context)
                delta = {key: after[key] - row["before"][key] for key in after}
                children = observed["stages"][child_start:]
                row.update(
                    after=after,
                    inclusive_delta=delta,
                    exclusive_delta={
                        key: value - sum(child["exclusive_delta"][key] for child in children)
                        for key, value in delta.items()
                    },
                    elapsed_seconds=elapsed,
                    exclusive_elapsed_seconds=elapsed
                    - sum(child["exclusive_elapsed_seconds"] for child in children),
                    quality_before=quality_before,
                    quality_after=state.current_evaluation.quality_key,
                    stop_reason=context.factory.budget.stop_reason.value
                    if context.factory.budget.stop_reason
                    else None,
                )
                if name == "improve_chain_order":
                    observed["order_plans"].append((row["round"], plan_before, state.current_plan))

        return call

    original_core, original_public = checker.core.solve, checker.solve_request

    def core_call(problem, rule_set, policy, runtime):
        result = original_core(problem, rule_set, policy, runtime)
        observed["core_calls"].append((problem, rule_set, policy, result))
        return result

    def public_call(request):
        result = original_public(request)
        observed["public_results"].append(result)
        return result

    original_pairs = neighborhoods._candidate_merged_sequences

    def pairs(donor, target, context, virtual_sequence):
        if len(observed["enumeration_prefix"]) < PREFIX_LIMIT:
            observed["enumeration_prefix"].append(
                {
                    "index": len(observed["enumeration_prefix"]) + 1,
                    "round": active_round[0],
                    "donor_chain_id": donor.chain_id,
                    "target_chain_id": target.chain_id,
                    "candidate_check_count_before": context.factory.budget.candidate_check_count,
                }
            )
        return original_pairs(donor, target, context, virtual_sequence)

    original_replay = split.run_local_search

    def replay(state, context):
        previous = active_round[0]
        active_round[0] = "post_split_replay"
        try:
            return original_replay(state, context)
        finally:
            active_round[0] = previous

    with ExitStack() as stack:
        stack.enter_context(patch.object(checker.core, "solve", core_call))
        stack.enter_context(patch.object(checker, "solve_request", public_call))
        stack.enter_context(patch.object(neighborhoods, "_candidate_merged_sequences", pairs))
        stack.enter_context(patch.object(split, "run_local_search", replay))
        for name in (
            "improve_whole_chain",
            "improve_real_node_relocation",
            "improve_virtual_weight_fill",
            "improve_chain_order",
        ):
            if hasattr(neighborhoods, name):
                stack.enter_context(
                    patch.object(
                        neighborhoods, name, stage_wrapper(name, getattr(neighborhoods, name))
                    )
                )
        stack.enter_context(
            patch.object(
                checker.core_solver,
                "run_controlled_order_split",
                stage_wrapper(
                    "controlled_split_and_replay", checker.core_solver.run_controlled_order_split
                ),
            )
        )
        if hasattr(initial, "stable_group_plan"):
            original_group = initial.stable_group_plan

            def group(plan, period_index):
                grouped = original_group(plan, period_index)
                if grouped is not plan and observed["first_group_change"] is None:
                    observed["first_group_change"] = (plan, grouped)
                return grouped

            stack.enter_context(patch.object(initial, "stable_group_plan", group))
        yield observed


def plan_comparison(checker, before, after):
    return {
        "before_plan_fingerprint": checker.fingerprint(before),
        "after_plan_fingerprint": checker.fingerprint(after),
        "before_chain_ids": [chain.chain_id for chain in before.chains],
        "after_chain_ids": [chain.chain_id for chain in after.chains],
        "same_chain_contents": {chain.chain_id: chain for chain in before.chains}
        == {chain.chain_id: chain for chain in after.chains},
    }


def boundary_details(checker, result, problem, rules):
    """Native new-rule contributions, or an explicitly separate old-plan diagnostic."""
    from apsgo_scheduler.core.contracts import sum_decimals
    from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleEvaluationContext
    from apsgo_scheduler.core.rules.concrete import _positive_weight_difference

    if result.release is None:
        return [], {"kind": "unavailable"}
    original = result.release.plan
    rule = next((rule for rule in rules.rules if METRIC in rule.metric_keys()), None)
    period_index = {period: index for index, period in enumerate(problem.period_order)}
    plan = (
        original
        if rule
        else replace(
            original,
            chains=tuple(
                sorted(original.chains, key=lambda chain: period_index[chain.assigned_period])
            ),
        )
    )
    if rule:
        context = RuleEvaluationContext(problem.period_order, period_index, ())
        contributions = rule.boundary_contributions(PlanRuleSubject("plan", plan, None), context)
        gaps = tuple(item.value for _, _, item in contributions)
        if tuple((left, right) for left, right, _ in contributions) != tuple(
            (left.chain_id, right.chain_id) for left, right in zip(plan.chains, plan.chains[1:])
        ):
            raise ValueError("rule boundary identities differ from the released chain order")
    else:
        gaps = tuple(
            _positive_weight_difference(
                max(left.last_node.width, right.first_node.width),
                min(left.last_node.width, right.first_node.width),
            )
            for left, right in zip(plan.chains, plan.chains[1:])
        )
    rows = []
    for index, (left, right, gap) in enumerate(zip(plan.chains, plan.chains[1:], gaps), 1):
        tail, head = left.last_node, right.first_node
        rows.append(
            dict(
                zip(
                    BOUNDARY_FIELDS,
                    (
                        index,
                        left.chain_id,
                        right.chain_id,
                        left.assigned_period,
                        right.assigned_period,
                        tail.node_id,
                        head.node_id,
                        tail.material_role.value,
                        head.material_role.value,
                        str(tail.width),
                        str(head.width),
                        str(gap),
                        left.assigned_period != right.assigned_period,
                    ),
                )
            )
        )
    total = sum_decimals(gaps)
    raw = result.release.evaluation.metrics.get(METRIC) if rule else None
    if rule and len(result.release.evaluation.quality_key) != 7:
        raise ValueError("native width objective requires the formal seven-level quality")
    quality = result.release.evaluation.quality_key[6] if rule else None
    if rule and not (len(result.release.evaluation.quality_key) == 7 and total == raw == quality):
        raise ValueError("native boundary contributions disagree with the seventh quality item")
    return rows, {
        "kind": "native_seven_level" if rule else "historical_stable_grouped_derived",
        "original_plan_fingerprint": checker.fingerprint(original),
        "derived_plan_fingerprint": checker.fingerprint(plan),
        "rule_id": rule.rule_id if rule else None,
        "metric_key": METRIC,
        "raw_metric": str(raw) if rule else None,
        "quality_value": str(quality) if rule else None,
        "absolute_gap_sum": str(total),
        "boundary_count": len(rows),
        "agrees_with_native_metric": True if rule else None,
    }


def write_supplement(checker, observed, output, revision):
    if len(observed["core_calls"]) != 1 or len(observed["public_results"]) != 1:
        raise ValueError("evidence requires exactly one actual core call and public result")
    problem, rules, policy, core = observed["core_calls"][0]
    result = observed["public_results"][0]
    report_path = output / "quality_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if any(
        checker.sha256(output / name) != digest
        for name, digest in report["artifacts_sha256"].items()
    ):
        raise ValueError("original precheck artifact hash mismatch")
    if checker.fingerprint(core.trace) != report["trace_fingerprint"]:
        raise ValueError("observed core trace differs from the original precheck")
    exclusive_totals = {
        name: sum(row["exclusive_delta"][name] for row in observed["stages"])
        for name in (
            "candidate_check_count",
            "complete_candidate_evaluation_count",
            "accepted_move_count",
        )
    }
    if any(total != getattr(core.metrics, name) for name, total in exclusive_totals.items()):
        raise ValueError("exclusive observed stage counts disagree with actual core metrics")
    rows, diagnostic = boundary_details(checker, result, problem, rules)
    first_width = next(
        (
            move
            for move in core.trace
            if len(move.quality_before) == 7
            and move.quality_before[:6] == move.quality_after[:6]
            and move.quality_after[6] < move.quality_before[6]
        ),
        None,
    )
    first_group = observed["first_group_change"]
    observation = {
        "schema_version": 1,
        "code_revision": revision,
        "native_quality_levels": len(rules.quality_spec),
        "policy": asdict(policy),
        "trace_fingerprint": checker.fingerprint(core.trace),
        "stage_observations": observed["stages"],
        "stage_exclusive_totals": exclusive_totals,
        "first_initial_stable_group_change": plan_comparison(checker, *first_group)
        if first_group
        else None,
        "whole_chain_pair_enumeration_prefix": observed["enumeration_prefix"],
        "enumeration_prefix_limit": PREFIX_LIMIT,
        "enumeration_scope": "first pair-generator calls only; a missing prefix difference does not prove identical full enumeration",
        "first_width_only_accepted_move": asdict(first_width) if first_width else None,
        "same_structure_order_comparisons": [
            {"round": round_name, **plan_comparison(checker, before, after)}
            for round_name, before, after in observed["order_plans"]
        ],
        "boundary_diagnostic": diagnostic,
        "instrumentation": "Additional evidence-only stage, initial grouping and first 16 pair-call observations; original objects returned; no budget or cancellation polling. Times include observation overhead and are not paired performance acceptance.",
    }
    checker.write_csv(output / "chain_boundary_detail.csv", BOUNDARY_FIELDS, rows)
    with (output / "observation.json").open("x", encoding="utf-8") as stream:
        json.dump(observation, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.write("\n")
    manifest = {
        "schema_version": 1,
        "code_revision": revision,
        "runner_sha256": checker.sha256(Path(__file__)),
        "quality_report_sha256": checker.sha256(report_path),
        "artifacts_sha256": {
            name: checker.sha256(output / name)
            for name in ("observation.json", "chain_boundary_detail.csv")
        },
    }
    with (output / "observation_manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return observation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--code-repository", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    checker = load_checker(args.code_root)
    with observe_precheck(checker) as observed:
        status = checker.main(
            [
                "--code-repository",
                str(args.code_repository),
                "--code-revision",
                args.code_revision,
                "--output-dir",
                str(args.output_dir),
            ]
        )
    write_supplement(checker, observed, args.output_dir, args.code_revision)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
