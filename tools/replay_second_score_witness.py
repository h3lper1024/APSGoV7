"""Replay the frozen 47th acceptance; stop before solving the whole historical run."""

import argparse
from decimal import Decimal
from pathlib import Path
import sys
from time import monotonic, perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core import neighborhoods
from apsgo_v7_service.diagnostics import _json_values
from tools.compare_backlog_search_runs import verify_comparison_requests
from tools.replay_backlog_search_witnesses import (
    load_request, read_json, load_rule_set, normalize_input, RuleEvaluationContext,
    evaluate_plan, SearchState, SolveRuntimeBudget, SearchContext, VirtualFactory,
    RuleEdgeDecisionCache, fingerprint,
)
from tools.run_delivery_comparison import prepare
from tools.verify_solver_diagnostics import _write_json


class WitnessReached(BaseException):
    """Explicitly stop the diagnostic, never return it as a completed schedule."""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    root = ROOT / "diagnostics/backlog_priority_search"
    old_path = root / "stage5_backlog_priority_1000000_01"
    old = load_request(old_path / "prepared_request.json")
    historical = read_json(old_path / "measurement.json")["final_search"]["trace"]
    captured = {}
    original = neighborhoods.try_complete_candidate

    def observe(state, context, chains, **kwargs):
        before = state.current_plan
        accepted = original(state, context, chains, **kwargs)
        if accepted and state.accepted_move_count == 47:
            captured.update(before=before, after=state.current_plan, traces=context.accepted_move_traces,
                            virtual_sequence=state.virtual_sequence, kwargs=kwargs)
            raise WitnessReached()
        return accepted

    wall, cpu = perf_counter(), process_time()
    with patch.object(neighborhoods, "try_complete_candidate", observe):
        try:
            solve_request(old)
        except WitnessReached:
            pass
    if not captured or _json_values(captured["traces"]) != historical[:47]:
        raise ValueError("frozen historical acceptance prefix differs")
    _write_json(args.output_dir / "physical_witness.json", _json_values(captured))
    _, new = prepare(root / "input_budget1000000_search9999.json",
                     ROOT / "diagnostics/delivery_objective/timing_source.json",
                     "2026-06-01T00:00:00+08:00", Decimal(100),
                     include_backlog_clearance=True, second_precision=True)
    verify_comparison_requests(old, new, second_precision=True)
    rules = load_rule_set(new.rule_set_spec)
    problem = normalize_input(new, rules)
    rc = RuleEvaluationContext(problem.period_order, {p: i for i, p in enumerate(problem.period_order)},
                               tuple(p.prototype_id for p in problem.virtual_prototypes), problem.delivery_timing)
    before = evaluate_plan(captured["before"], rules, rc)
    after = evaluate_plan(captured["after"], rules, rc)
    if before.quality_key[:5] != after.quality_key[:5] or after.quality_key[5] <= before.quality_key[5]:
        raise ValueError("witness no longer isolates tiny clearance gain with worse burden")
    state = SearchState(captured["before"], before, 46, captured["virtual_sequence"], 0, 0, 0)
    budget = SolveRuntimeBudget.from_policy(new.policy, monotonic())
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, rc), budget), new.policy)
    accepted = original(state, context, captured["after"].chains, **captured["kwargs"])
    if accepted or state.current_plan != captured["before"]:
        raise ValueError("second-resolution comparison incorrectly accepted the witness")
    summary = dict(sequence=47, exact_historical_prefix=47, original_trace=historical[46],
                   new_before=before.quality_key, new_after=after.quality_key,
                   new_complete_candidate_accepted=accepted,
                   witness_wall_seconds=Decimal(str(perf_counter() - wall)),
                   witness_cpu_seconds=Decimal(str(process_time() - cpu)),
                   new_request_fingerprint=fingerprint_public_request(new),
                   new_rule_fingerprint=new.rule_set_spec.fingerprint,
                   before_plan_fingerprint=fingerprint(captured["before"]),
                   after_plan_fingerprint=fingerprint(captured["after"]), intermediate_diagnostic_only=True)
    _write_json(args.output_dir / "summary.json", summary)
    print(summary)


if __name__ == "__main__":
    main()
