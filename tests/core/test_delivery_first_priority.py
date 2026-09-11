"""Delivery-first tradeoffs use the same complete candidate and independent audit."""

from dataclasses import replace

import pytest

from apsgo_scheduler.app.delivery_request import with_delivery_objective
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.app.delivery_report import build_delivery_report
from apsgo_scheduler.app.service import solve_request
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import CoreCandidateSnapshot
from apsgo_scheduler.core.evaluation import evaluate_plan
from apsgo_scheduler.core.final_audit import audit_core_without_search_cache
from apsgo_scheduler.core.model import SearchState
from apsgo_scheduler.core.neighborhoods import SearchContext
from apsgo_scheduler.core.virtual_material import VirtualFactory
from apsgo_scheduler.core.width_optimization import _try_chain_cut
from tests.app.test_input_normalizer import make_spec, read_gqga4_spec
from tests.core.graph.test_bipartite_matching import budget
from tests.core.test_urgent_order_search import audited_case


def first_case():
    request, old, context = audited_case()
    criteria = request.rule_set_spec.quality_spec
    spec = make_spec(rules=request.rule_set_spec.rules,
                     quality_spec=(*criteria[:2], *criteria[4:6], *criteria[2:4], *criteria[6:]),
                     allowed_final_deviation_codes=request.rule_set_spec.allowed_final_deviation_codes)
    request = replace(request, rule_set_spec=spec)
    problem, rules = normalize_input(request), load_rule_set(spec)
    rule_context = context.factory.cache.context
    state = SearchState(old.current_plan, evaluate_plan(old.current_plan, rules, rule_context))
    context = SearchContext(VirtualFactory(RuleEdgeDecisionCache(problem, rules, rule_context),
                                          budget(candidate_check_limit=100)), request.policy)
    return request, state, context


def test_new_order_is_opt_in_with_distinct_identity_and_unchanged_rules():
    seven = read_gqga4_spec("gqga4_rule_set_spec.json")
    old = with_delivery_objective(seven)
    new = with_delivery_objective(seven, delivery_first=True)
    assert new.rules == old.rules and new.allowed_final_deviation_codes == old.allowed_final_deviation_codes
    assert new.quality_spec == (*old.quality_spec[:2], *old.quality_spec[4:6], *old.quality_spec[2:4], *old.quality_spec[6:])
    assert new.fingerprint != old.fingerprint
    assert with_delivery_objective(seven, delivery_first=False) == old
    with pytest.raises(ValueError, match="boolean"):
        with_delivery_objective(seven, delivery_first=1)


def test_delivery_improvement_can_buy_extra_underweight_and_still_audit():
    _, state, context = first_case()
    before = state.current_evaluation.quality_key
    assert _try_chain_cut(state, context, ("width_chain_cut", 0, 1, 1, 0))
    after = state.current_evaluation.quality_key
    assert after[:2] == before[:2] == (0, 0)
    assert after[2] == 0 < before[2]
    assert after[4] > before[4] and after[5] > before[5]
    result = audit_core_without_search_cache(CoreCandidateSnapshot(state.current_plan, state.current_evaluation),
                                            context.factory.cache.problem, context.factory.cache.rule_set, budget())
    assert result.report.passed
    _, old, old_context = audited_case()
    assert not _try_chain_cut(old, old_context, ("width_chain_cut", 0, 1, 1, 0))


def test_extra_underweight_without_delivery_improvement_is_rejected():
    _, state, context = first_case()
    before = state.current_plan
    assert not _try_chain_cut(state, context, ("width_chain_cut", 1, 1, 1, 2))
    assert state.current_plan is before


def test_new_order_does_not_accept_other_deviations_or_overweight():
    from apsgo_scheduler.core.chain_order import refinement_candidate_allowed
    from apsgo_scheduler.core.rules.base import RuleDisposition
    _, state, context = first_case()
    before = state.current_evaluation
    violation = before.violations[0]
    for changed in (replace(violation, reason_code="other"),
                    replace(violation, disposition=RuleDisposition.PROHIBITED, reason_code="chain_weight_above_maximum")):
        assert not refinement_candidate_allowed(before, replace(before, violations=(changed,)), context.factory.cache.rule_set)


def test_new_order_still_validates_delivery_projection_and_order():
    seven = read_gqga4_spec("gqga4_rule_set_spec.json")
    spec = with_delivery_objective(seven, delivery_first=True)
    c = spec.quality_spec
    for criteria in ((*c[:2], c[3], c[2], *c[4:]),
                     (*c[:2], replace(c[2], aggregation="maximum"), *c[3:])):
        with pytest.raises(ValueError):
            load_rule_set(make_spec(rules=spec.rules, quality_spec=criteria))


def test_public_result_audits_delivery_metrics_by_name_in_new_positions():
    request, _, _ = first_case()
    result = solve_request(request)
    assert result.release is not None, result.issues
    assert result.core_audit.passed and result.audit_report.passed
    report = build_delivery_report(request, result)
    assert report["delivery_summary"]["newly_late_original_weight"] == result.release.evaluation.quality_key[2]
