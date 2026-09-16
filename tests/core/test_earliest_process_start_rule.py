"""One integer clock owns release-time decisions, previews and final audit."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from apsgo_scheduler.app.input_normalizer import InputNormalizationError, normalize_input
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.app.delivery_report import numeric_delivery_plan_report
from apsgo_scheduler.core._numeric_audit import audit_numeric_core_without_search_cache, _same_base
from apsgo_scheduler.core._numeric_boundary import numeric_plan_to_domain, numeric_evaluation_to_domain
from apsgo_scheduler.core._numeric_evaluation import (
    evaluate_numeric_plan, evaluate_numeric_candidate, preview_numeric_chain_order_quality,
)
from apsgo_scheduler.core._numeric_kernel import _clock_view, allocate_result, task_columns, rule_tables, flat_chain_view
from apsgo_scheduler.core._numeric_rules import NumericReason, NumericRuleKind, NumericRuleProgram
from apsgo_scheduler.core._numeric_state import NumericPlan, NumericTask, readonly
from apsgo_scheduler.core._numeric_units import NumericValueError
from apsgo_scheduler.core.contracts import RuleScope
from tests.core.test_numeric_rules import definition
from tests.core.test_numeric_boundary_audit import numeric_request, numeric_state, audit_budget


def early_request(*, enabled=True, lower="2026-06-01T01:00:00+08:00"):
    request = numeric_request()
    rule = replace(definition("earliest_process_start", "EarliestProcessStartRule", RuleScope.PLAN, {}), enabled=enabled)
    spec = replace(request.rule_set_spec, rules=(*request.rule_set_spec.rules, rule))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    timing = replace(request.delivery_timing, orders=tuple(
        replace(item, earliest_start_at=lower if i == 0 else "2026-06-01T00:00:00+08:00")
        for i, item in enumerate(request.delivery_timing.orders)))
    return replace(request, rule_set_spec=spec, delivery_timing=timing)


CASES = json.loads((Path(__file__).parents[1] / "baselines/earliest_process_start/cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_hand_calculated_clock_witnesses(case):
    _, _, state = numeric_state(early_request())
    original_count, count = len(case["earliest"]), len(case["sources"])
    # Pure-clock witnesses deliberately isolate timing from unrelated material rules.
    columns = task_columns(state.task)._replace(
        source=readonly(case["sources"], np.int64), duration=readonly(case["durations"], np.int64),
        earliest_start=readonly(case["earliest"], np.int64),
        has_earliest_start=readonly([True] * original_count, np.bool_),
        original_weight=readonly([100] * original_count, np.int64),
        due=readonly([100000000] * original_count, np.int64),
        backlog=readonly([False] * original_count, np.bool_))
    tables = rule_tables(state.program.rules)
    view = flat_chain_view(readonly(range(count), np.int64), readonly(range(count + 1), np.int64),
                           readonly([0] * count, np.int64))
    out = allocate_result(count, len(state.program.rules), count, original_count, count, 0)
    _clock_view(columns, view, out, tables, True)
    assert out.status[0] == 0
    assert (out.ends - columns.duration).tolist() == case["starts"]
    assert out.scores[-1, :2].tolist() == [sum(x > 0 for x in case["early"]), sum(case["early"]) * 1000]
    assert out.violations[:out.counts[0], 5].tolist() == [x * 1000 for x in case["early"] if x]


def test_full_evaluation_reporting_and_independent_audit_reject_same_node():
    request = early_request()
    problem, rules, state = numeric_state(request)
    assert {rule.kind for rule in state.program.rules} == set(NumericRuleKind)
    early = [v for v in state.evaluation.violations if v.reason is NumericReason.EARLY_START]
    assert len(early) == 1 and early[0].severity == 3600000000
    assert state.evaluation.quality_key[:2].tolist() == [1, 3600000000]
    domain = numeric_plan_to_domain(state.task, state.plan, problem, rules)
    projected = numeric_evaluation_to_domain(state.task, state.program, state.quality, state.plan, state.evaluation, domain)
    assert projected.violations[0].subject_id == problem.nodes[0].node_id
    assert "3600" in projected.violations[0].message
    report = numeric_delivery_plan_report(domain, problem.delivery_timing, request.delivery_timing)
    assert report["delivery_summary"]["early_start_node_count"] == 1
    assert report["delivery_summary"]["early_start_total_seconds"] == 3600
    outcome = audit_numeric_core_without_search_cache(state, problem, rules, request.delivery_timing, audit_budget())
    assert not outcome.report.passed and outcome.report.search_evaluation_matches


def test_chain_reorder_preview_and_reuse_recompute_release_times():
    _, _, state = numeric_state(early_request())
    task, program, quality = state.task, state.program, state.quality
    before = NumericPlan.build(task, range(7), (0, 1, 7), (10, 20), (0, 0))
    current = evaluate_numeric_plan(task, program, quality, before)
    after = NumericPlan.build(task, (1, 2, 3, 4, 5, 6, 0), (0, 6, 7), (20, 10), (0, 0), generation=1)
    complete = evaluate_numeric_plan(task, program, quality, after)
    reused = evaluate_numeric_candidate(task, program, quality, after, task, program, quality, before, current)
    assert reused.quality_key.tolist() == complete.quality_key.tolist()
    assert reused.quality_key[0] == 0
    assert preview_numeric_chain_order_quality(task, program, quality, before, current, (1, 0)) == tuple(complete.quality_key)
    assert len(complete.quality_key) == 9


def test_missing_lower_and_legacy_mode_fail_before_search():
    request = early_request(lower=None)
    with pytest.raises(InputNormalizationError) as error:
        normalize_input(request)
    assert any(i.code == "missing_earliest_start" and i.subject_id == request.orders[0].source_order_id for i in error.value.issues)
    from tests.app.test_input_normalizer import make_request
    request = early_request()
    request = replace(request, policy=replace(request.policy, numeric_semantics_key=make_request().policy.numeric_semantics_key))
    with pytest.raises(InputNormalizationError, match="整数毫秒"):
        normalize_input(request)


def test_disabled_missing_input_has_no_new_violation_or_quality_change():
    _, _, old = numeric_state(numeric_request())
    _, _, disabled = numeric_state(early_request(enabled=False, lower=None))
    assert not disabled.program.for_kind(NumericRuleKind.EARLIEST_START)
    assert disabled.evaluation.quality_key.tolist() == old.evaluation.quality_key.tolist()


def test_low_level_compiler_and_audit_cannot_ignore_missing_or_tampered_lower():
    request = early_request()
    problem, rules, state = numeric_state(request)
    damaged = replace(state.task, originals=replace(state.task.originals,
        has_earliest_start=readonly([False] * 7, np.bool_)))
    with pytest.raises(NumericValueError, match="complete original timing"):
        NumericRuleProgram.compile(damaged, rules)
    assert not _same_base(damaged, NumericTask.build(problem, rules, request.delivery_timing))


def test_severity_overflow_is_reported_not_wrapped():
    _, _, state = numeric_state(early_request())
    task = replace(state.task, originals=replace(state.task.originals,
        earliest_start_ms=readonly([(1 << 63) - 1] * 7, np.int64)))
    with pytest.raises(NumericValueError):
        evaluate_numeric_plan(task, state.program, state.quality, state.plan)


def test_real_transition_order_is_checked_and_equal_start_audits_successfully():
    from apsgo_scheduler.core.model import MaterialRole
    request = early_request(lower="2026-06-01T00:00:00+08:00")
    request = replace(request, orders=(replace(request.orders[0], material_role=MaterialRole.ACTUAL_TRANSITION), *request.orders[1:]))
    problem, rules, state = numeric_state(request)
    assert not any(v.reason is NumericReason.EARLY_START for v in state.evaluation.violations)
    assert audit_numeric_core_without_search_cache(state, problem, rules, request.delivery_timing, audit_budget()).report.passed
    changed = replace(request.delivery_timing.orders[0], earliest_start_at="2026-06-01T00:00:01+08:00")
    request = replace(request, delivery_timing=replace(request.delivery_timing, orders=(changed, *request.delivery_timing.orders[1:])))
    _, _, state = numeric_state(request)
    assert any(v.reason is NumericReason.EARLY_START for v in state.evaluation.violations)


def test_private_batch_and_single_attempt_share_the_new_clock():
    from apsgo_scheduler.core import _numeric_candidate_kernel as candidate
    from apsgo_scheduler.core._numeric_batch import NumericCandidateBatchWorkspace
    from apsgo_scheduler.core._numeric_refinement_scan import description, INTRA
    _, _, state = numeric_state(early_request())
    entries = [(description(INTRA, 10, 10, i, i + 1, 0), candidate.CandidateCheckPolicy(), (0,)) for i in (1, 2)]
    single = NumericCandidateBatchWorkspace(state.task, state.program, state.quality, state.plan, state.evaluation)
    expected = [list(single.attempts(d, p, variants, virtual_sequence=0, split_sequence=0, allows_continue=lambda: True))
                for d, p, variants in entries]
    batch = NumericCandidateBatchWorkspace(state.task, state.program, state.quality, state.plan, state.evaluation, _native_executor="serial")
    actual = batch.prepare_many(entries, virtual_sequence=0, split_sequence=0, allows_continue=lambda: True)
    for left, right in zip(actual, expected):
        assert len(left) == len(right) == 1
        result, reference = left[0][1], right[0][1]
        assert result.status == reference.status == 0
        np.testing.assert_array_equal(result.summary.quality, reference.summary.quality)
        early_index = state.program.for_kind(NumericRuleKind.EARLIEST_START)[0].index
        assert not result.summary.hits[:, early_index].any()
