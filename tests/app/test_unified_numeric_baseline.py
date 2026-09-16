"""Baseline probes must be bounded, deterministic and observational only."""

import pytest

from apsgo_scheduler.core import _numeric_search as search
from tests.core.test_numeric_construction import budget, construction_case
from tests.core.test_numeric_search import state_for
from tools.verify_unified_numeric_solver import OrderedDigest, WholeFlowTrace


def test_digest_is_order_sensitive_and_retains_only_bounded_examples():
    forward, backward = OrderedDigest(2), OrderedDigest(2)
    for number in range(100):
        forward.add((number, "event"))
        backward.add((99 - number, "event"))
    result = forward.snapshot()
    assert result["count"] == 100
    assert len(result["first"]) == len(result["last"]) == 2
    assert result["first"][0] == [0, "event"]
    assert result["last"][-1] == [99, "event"]
    assert result["sha256"] != backward.snapshot()["sha256"]


def test_trace_preserves_acceptance_quota_and_restores_hooks():
    original = search.improve_numeric_whole_chain

    def run():
        task, rules, quality = construction_case()
        state = state_for(task, rules, quality, (0, 1), (0, 1, 2), (10, 20), (0, 0))
        runtime = budget(candidate_limit=100)
        search.improve_numeric_whole_chain(task, rules, quality, state, runtime,
                                           pair_scan_slack_weight=0)
        return state, runtime

    reference, reference_budget = run()
    first, second = WholeFlowTrace(), WholeFlowTrace()
    with first.install():
        actual, actual_budget = run()
    with second.install():
        run()
    assert search.improve_numeric_whole_chain is original
    assert actual.plan.fingerprint == reference.plan.fingerprint
    assert actual.accepted_moves == reference.accepted_moves
    assert actual_budget.candidate_check_count == reference_budget.candidate_check_count == 2
    assert first.snapshot() == second.snapshot()
    # Formal edit objects are now created only on acceptance. Logical consumed
    # attempts still include the rejected first candidate, without materializing it.
    assert first.streams["edits"].count == 1
    assert first.streams["consumed_attempts"].count == 2
    assert first.streams["accepted"].count == 1
    assert first.streams["quota"].count == 2


def test_hooks_restore_after_failure():
    original = search.NumericCandidateEdit.__post_init__
    with pytest.raises(RuntimeError), WholeFlowTrace().install():
        raise RuntimeError("diagnostic failure")
    assert search.NumericCandidateEdit.__post_init__ is original


def test_public_measurement_with_hooks_matches_uninstrumented_result(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    from tests.core.test_numeric_boundary_audit import numeric_request
    from tools.profile_numeric_solver import _run_once
    from tools.verify_numeric_kernel_migration import compare_runs

    request = numeric_request()
    # Cold native compilation is not a candidate-order difference. Both runs
    # use the same generous diagnostic deadline, rather than the tiny fixture's.
    request = replace(request, policy=replace(request.policy, candidate_check_limit=100,
                      total_time_limit_seconds=Decimal("10009"),
                      finalization_reserve_seconds=Decimal("10")))
    _run_once(request, tmp_path / "reference")
    trace = WholeFlowTrace()
    with trace.install():
        measured = _run_once(request, tmp_path / "traced")
    assert measured["core_audit"]["passed"]
    assert measured["application_audit"]["passed"]
    assert compare_runs(tmp_path / "reference", tmp_path / "traced")["equal"]
    assert set(trace.construction) == {"graph", "cover", "initial"}
    assert trace.phases


def test_baseline_two_material_bridge_keeps_prototype_order():
    from decimal import Decimal
    from apsgo_scheduler.core._numeric_rules import NumericRuleProgram
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram
    from apsgo_scheduler.core._numeric_resources import choose_virtual_bridge
    from tests.app.test_input_normalizer import make_order, make_prototype
    from tests.core.test_numeric_evaluation import numeric_quality_spec
    from tests.core.test_numeric_rules import attributes, build

    rules, task = build(spec=numeric_quality_spec(),
        orders=tuple(make_order(i, width=Decimal(w), rule_attributes=attributes(),
                               min_temperature=Decimal(700 + 100 * i),
                               max_temperature=Decimal(710 + 100 * i))
                     for i, w in enumerate(("1000", "400"))),
        prototypes=tuple(make_prototype(i, width=Decimal(w))
                                 for i, w in enumerate(("800", "600"))))
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    assert choose_virtual_bridge(task, program, quality, 0, 1,
                                 max_nodes=1, first_sequence=1) is None
    result = choose_virtual_bridge(task, program, quality, 0, 1,
                                  max_nodes=2, first_sequence=1)
    assert result is not None and len(result.rows) == 2
    assert result.task.nodes.prototype[list(result.rows)].tolist() == [0, 1]
    assert result.task.node_ids[-2:] == ("virtual-000001", "virtual-000002")
