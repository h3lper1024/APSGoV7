"""Reproduce two independent backlog moves through production acceptance and audit."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys
from time import monotonic, perf_counter, process_time

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.app.delivery_report import delivery_plan_report
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.chain_order import delivery_node_positions
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import ControlledSplitMode, CoreCandidateSnapshot, fingerprint
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import (
    Chain, MaterialRole, Node, SchedulePlan, SearchState, SplitLineage, VirtualLineage, VirtualPurpose,
)
from apsgo_scheduler.core.neighborhoods import SearchContext
from apsgo_scheduler.core.rules.base import RuleEvaluationContext
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import _delivery_node_recipes, _try_segment_edit
from apsgo_v7_service.diagnostics import _json_values
from tools.profile_solver_search import load_request
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def read_plan(raw):
    """Decode diagnostic values using the existing validated domain constructors."""
    chains = []
    for chain in raw["chains"]:
        nodes = []
        for value in chain["nodes"]:
            value = dict(value)
            for key in ("weight", "width", "thickness", "min_temperature", "max_temperature"):
                if value[key] is not None:
                    value[key] = Decimal(value[key])
            value["material_role"] = MaterialRole(value["material_role"])
            if value["virtual_lineage"] is not None:
                lineage = dict(value["virtual_lineage"])
                lineage["purpose"] = VirtualPurpose(lineage["purpose"])
                value["virtual_lineage"] = VirtualLineage(**lineage)
            if value["split_lineage"] is not None:
                lineage = dict(value["split_lineage"])
                lineage["parent_weight"] = Decimal(lineage["parent_weight"])
                lineage["split_mode"] = ControlledSplitMode(lineage["split_mode"])
                value["split_lineage"] = SplitLineage(**lineage)
            nodes.append(Node(**value))
        chains.append(Chain(chain["chain_id"], tuple(nodes), chain["assigned_period"]))
    return SchedulePlan(tuple(chains))


def load_case(run):
    request = load_request(run / "prepared_request.json")
    measurement = read_json(run / "measurement.json")
    snapshot = measurement["final_search"]
    plan = read_plan(snapshot["plan"])
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    rule_context = RuleEvaluationContext(
        problem.period_order, {p: i for i, p in enumerate(problem.period_order)},
        tuple(p.prototype_id for p in problem.virtual_prototypes), problem.delivery_timing,
    )
    evaluation = evaluate_plan(plan, rules, rule_context)
    if fingerprint(plan) != snapshot["plan_fingerprint"] or fingerprint(evaluation) != snapshot["evaluation_fingerprint"]:
        raise ValueError("frozen plan or recomputed evaluation identity differs")
    counters = measurement["result"]["run_manifest"]["counters"]
    sequence = max((n.virtual_lineage.accepted_sequence for c in plan.chains for n in c.nodes if n.virtual_lineage), default=0)
    state = SearchState(plan, evaluation, counters["accepted_move_count"], sequence,
                        counters["accepted_split_count"], counters["accepted_same_period_split_count"],
                        counters["accepted_future_borrow_return_count"])
    budget = SolveRuntimeBudget.from_policy(request.policy, monotonic())
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, rule_context), budget), request.policy)
    return state, context


def report_with_positions(plan, timing):
    report = delivery_plan_report(plan, timing)
    positions = {}
    global_position = 0
    for i, chain in enumerate(plan.chains, 1):
        for j, node in enumerate(chain.nodes, 1):
            global_position += 1
            if node.source_order_id:
                positions.setdefault(node.source_order_id, []).append(dict(
                    node_id=node.node_id, chain_id=chain.chain_id, chain_position=i,
                    node_position=j, global_position=global_position, weight=node.weight,
                ))
    for order in report["delivery_orders"]:
        order["pieces"] = positions[order["source_order_id"]]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    started, cpu = perf_counter(), process_time()
    baseline = read_json(args.baseline_manifest)
    frozen = baseline["runs"][-1]
    run = ROOT / frozen["path"]
    for name, expected in frozen["hashes"].items():
        if _sha256(run / name) != expected:
            raise ValueError(f"baseline hash differs: {name}")
    for name, expected in baseline["protected_and_input_hashes"].items():
        if _sha256(ROOT / name) != expected:
            raise ValueError(f"protected input differs: {name}")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    state, context = load_case(run)
    old_report = report_with_positions(state.current_plan, context.factory.cache.context.delivery_timing)
    _write_json(args.output_dir / "baseline_orders.json", old_report)
    _write_json(args.output_dir / "baseline_plan.json", _json_values(CoreCandidateSnapshot(state.current_plan, state.current_evaluation)))
    witnesses = []
    for order_id, slot in (("0002002009-000020", 38), ("0030117283-000020", 23)):
        state, context = load_case(run)  # Independent proposals, never cumulative.
        timing = context.factory.cache.context.delivery_timing
        i, start = next((i, j) for i, c in enumerate(state.current_plan.chains)
                        for j, n in enumerate(c.nodes) if n.source_order_id == order_id)
        target = next(i for i, c in enumerate(state.current_plan.chains) if c.chain_id == "initial-000003")
        recipe = ("width_node_move", i, target, start, start + 1, slot, slot)
        positions = delivery_node_positions(state.current_plan, timing)
        ordinal = next(k for k, item in enumerate(_delivery_node_recipes(state, positions), 1) if item == recipe)
        before = state.current_evaluation.quality_key
        assert context.factory.budget.consume_candidate_check()
        accepted = _try_segment_edit(state, context, recipe)
        audit = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
                                                context.factory.cache.problem, context.factory.cache.rule_set, context.factory.budget)
        if not accepted or not audit.report.passed:
            raise ValueError(f"witness not accepted and audited: {order_id}")
        report = report_with_positions(state.current_plan, timing)
        row = next(row for row in report["delivery_orders"] if row["source_order_id"] == order_id)
        witness = dict(order_id=order_id, recipe=recipe, original_source_rank=positions.index((i, start)) + 1,
                       original_recipe_ordinal=ordinal, quality_before=before, quality_after=state.current_evaluation.quality_key,
                       accepted=accepted, core_audit_passed=audit.report.passed,
                       candidate_checks=context.factory.budget.candidate_check_count,
                       complete_evaluations=context.complete_candidate_evaluation_count,
                       virtual_sequence=state.virtual_sequence, completion=row,
                       plan_fingerprint=fingerprint(state.current_plan))
        _write_json(args.output_dir / f"{order_id}.json", dict(summary=witness, report=report,
                    candidate=_json_values(CoreCandidateSnapshot(state.current_plan, state.current_evaluation)), audit=_json_values(audit)))
        witnesses.append(witness)
    summary = dict(code=_code_identity(ROOT), baseline=frozen, manifest_sha256=_sha256(args.baseline_manifest),
                   proposal_per_original=4, proposal_per_family=64,
                   scope="independent_single_moves_core_audit_only_not_full_solve_or_application_release",
                   witnesses=witnesses, wall_seconds=Decimal(str(perf_counter() - started)),
                   cpu_seconds=Decimal(str(process_time() - cpu)))
    _write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(dict(witnesses=len(witnesses), accepted_and_core_audited=True,
                         source_ranks=[w["original_source_rank"] for w in witnesses],
                         recipe_ordinals=[w["original_recipe_ordinal"] for w in witnesses]), ensure_ascii=False))


if __name__ == "__main__":
    main()
