"""Real core execution has stable identities; observations cannot change a result."""

import random
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.core import solver
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.contracts import SearchStopReason, SolveStatus, fingerprint
from tests.core.search.test_controlled_order_split import split_case
from tests.core.test_solver_policy import policy

D = Decimal


def split_inputs():
    _, context = split_case()
    problem = context.factory.cache.problem
    values = {
        item.name: getattr(problem, item.name)
        for item in fields(problem)
        if item.name not in {"problem_id", "input_fingerprint"}
        and not (item.name == "delivery_timing" and problem.delivery_timing is None)
    }
    return (
        replace(problem, input_fingerprint=fingerprint(values)),
        context.factory.cache.rule_set,
        policy(candidate_check_limit=1000),
    )


def run_core(problem, rules, active_policy):
    runtime = SolveRuntimeBudget.from_policy(active_policy, 100.0, clock=lambda: 101.0)
    result = solver.solve(problem, rules, active_policy, runtime)
    assert result.stop_reason is runtime.stop_reason
    assert result.metrics.candidate_check_count == runtime.candidate_check_count
    return result


@pytest.fixture(scope="module")
def repeated_core():
    inputs = split_inputs()
    before = fingerprint(inputs)
    random_before = random.getstate()
    results = tuple(run_core(*inputs) for _ in range(3))
    assert fingerprint(inputs) == before and random.getstate() == random_before
    return inputs, results


def result_payload(result):
    payload = {
        item.name: getattr(result, item.name)
        for item in fields(result)
        if item.name != "core_result_fingerprint"
    }
    payload["metrics"] = {
        item.name: getattr(result.metrics, item.name)
        for item in fields(result.metrics)
        if item.name != "stage_duration_seconds"
    }
    return payload


def test_three_real_runs_keep_plan_trace_counters_stop_and_all_release_identities(repeated_core):
    _, results = repeated_core
    first = results[0]
    assert first.status is SolveStatus.SUCCESS
    assert first.stop_reason is SearchStopReason.LOCAL_SEARCH_COMPLETE
    assert first.release is not None and first.core_audit.passed
    assert len(first.trace) == first.metrics.accepted_move_count == 1
    assert first.trace[0].action_name == "controlled_order_split"
    assert first.metrics.accepted_split_count == first.metrics.accepted_same_period_split_count == 1
    assert first.metrics.accepted_future_borrow_return_count == 0
    assert first.metrics.candidate_check_count == 1
    assert first.metrics.complete_candidate_evaluation_count == 1
    for result in results:
        assert result.diagnostic_candidate == first.diagnostic_candidate
        assert result.release == first.release
        assert result.trace == first.trace and result.core_audit == first.core_audit
        assert result.core_result_fingerprint == first.core_result_fingerprint
        assert result_payload(result) == result_payload(first)
        assert result.metrics.stage_duration_seconds
        assert all(value >= 0 for value in result.metrics.stage_duration_seconds.values())


def test_fingerprint_payload_contains_every_result_field_except_observation_time(repeated_core):
    _, (result, *_) = repeated_core
    payload = result_payload(result)
    assert result.core_result_fingerprint == fingerprint(payload)
    changed = replace(
        result,
        metrics=replace(result.metrics, stage_duration_seconds={"different_observation": D(999)}),
    )
    assert fingerprint(result_payload(changed)) == result.core_result_fingerprint
    for name in payload:
        assert fingerprint(payload | {name: "changed"}) != result.core_result_fingerprint


def test_release_binds_plan_evaluation_resources_and_report_without_rescanning(repeated_core):
    (problem, _, _), (result, *_) = repeated_core
    release = result.release
    assert release.canonical_plan is result.diagnostic_candidate.plan
    assert release.plan_fingerprint == fingerprint(release.canonical_plan)
    assert release.evaluation_fingerprint == fingerprint(release.audited_evaluation)
    assert release.core_audit_fingerprint == result.core_audit.report_fingerprint
    assert release.resource_fingerprint == release.resource_facts.facts_fingerprint
    assert release.resource_fingerprint == result.core_audit.derived_resource_fingerprint
    assert release.release_fingerprint == fingerprint(
        {
            name: getattr(release, name)
            for name in (
                "plan_fingerprint",
                "evaluation_fingerprint",
                "resource_fingerprint",
                "core_audit_fingerprint",
            )
        }
    )
    facts = release.resource_facts
    assert facts.input_real_weight == facts.scheduled_real_weight == problem.nodes[0].weight
    assert facts.generated_virtual_weight == D(10)
    assert len(facts.assignments) == 5 and len(facts.split_partitions) == 1
    assert facts.split_partitions[0].piece_weights == (D(50), D(50), D(20))


def test_request_label_is_not_core_problem_identity_but_policy_seed_is(repeated_core):
    (problem, rules, active_policy), (first, *_) = repeated_core
    relabeled = run_core(replace(problem, problem_id="another-request-label"), rules, active_policy)
    assert relabeled.core_result_fingerprint == first.core_result_fingerprint
    changed = run_core(problem, rules, replace(active_policy, seed=active_policy.seed + 1))
    assert changed.release.canonical_plan == first.release.canonical_plan
    assert changed.policy_fingerprint != first.policy_fingerprint
    assert changed.core_result_fingerprint != first.core_result_fingerprint


def test_core_result_collections_and_fields_are_immutable(repeated_core):
    _, (result, *_) = repeated_core
    with pytest.raises(FrozenInstanceError):
        result.release = None
    with pytest.raises(TypeError):
        result.metrics.stage_duration_seconds["injected"] = D(1)
    copied = replace(result, issues=list(result.issues), trace=list(result.trace))
    assert type(copied.issues) is tuple and type(copied.trace) is tuple


@pytest.mark.parametrize(
    "changes",
    (
        {"status": "success"},
        {"stop_reason": None},
        {"diagnostic_candidate": None},
        {"release": None},
        {"core_audit": object()},
        {"metrics": object()},
        {"issues": [object()]},
        {"trace": [object()]},
        {"problem_fingerprint": ""},
        {"rule_set_fingerprint": ""},
        {"policy_fingerprint": ""},
        {"core_result_fingerprint": ""},
        {"status": SolveStatus.FAILED},
        {"stop_reason": SearchStopReason.USER_CANCELLED},
        {"stop_reason": SearchStopReason.FINALIZATION_TIME_LIMIT_REACHED},
        {"status": SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION},
    ),
)
def test_core_result_rejects_invalid_value_or_publication_combinations(changes, repeated_core):
    _, (result, *_) = repeated_core
    with pytest.raises(ValueError):
        replace(result, **changes)


def test_core_result_preserves_audited_release_binding(repeated_core):
    _, (result, *_) = repeated_core
    for field in ("core_audit_fingerprint", "evaluation_fingerprint"):
        with pytest.raises(ValueError):
            replace(result, release=replace(result.release, **{field: "not-the-audited-identity"}))
