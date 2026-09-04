"""Task-local edge decisions depend on rule semantics, never candidate display identity."""

from dataclasses import replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal, Inexact, Rounded, localcontext

import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import NUMERIC_SEMANTICS_KEY, ControlledSplitMode, RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    EdgeRuleSubject,
    NodeRuleSubject,
    Rule,
    RuleContribution,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.rules.concrete import (
    HighSurfaceRunCountRule,
    SyntheticWidthLimitRule,
    WidthTransitionRule,
)
from tests.app.test_input_normalizer import gqga4_spec, make_request, make_spec

D = Decimal


def setup_cache(spec=None):
    request = make_request(rule_set_spec=spec or gqga4_spec.__wrapped__())
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    context = RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
    )
    return RuleEdgeDecisionCache(problem, rules, context), problem, rules, context


def specification_for(*types):
    return make_spec(
        rules=tuple(item for item in gqga4_spec.__wrapped__().rules if item.rule_type in types)
    )


def virtual(
    node, *, prototype="prototype-0", purpose=VirtualPurpose.EDGE_BRIDGE, partition=None, sequence=1
):
    return replace(
        node,
        source_order_id=None,
        source_resource_id=None,
        source_period=None,
        material_role=MaterialRole.GENERATED_VIRTUAL,
        virtual_lineage=VirtualLineage(prototype, purpose, partition, sequence),
    )


def test_same_semantics_share_decisions_and_rebind_every_violation_to_current_subject():
    cache, problem, rules, context = setup_cache()
    left, original = problem.nodes
    right = replace(
        original, width=D(1100), thickness=D(2), min_temperature=D(850), max_temperature=D(900)
    )
    first = EdgeRuleSubject("candidate-one", left, right)
    expected = rules.evaluate_edge(first, context)
    assert len(expected.violations) > 1
    assert cache.evaluate_edge(first) == expected
    second = EdgeRuleSubject(
        "candidate-two", replace(left, node_id="other-left"), replace(right, node_id="other-right")
    )
    actual = cache.evaluate_edge(second)
    assert actual == rules.evaluate_edge(second, context)
    assert all(item.subject_id == "candidate-two" for item in actual.violations)
    assert all(item.subject_id == "candidate-one" for item in expected.violations)
    assert (cache.miss_count, cache.hit_count, cache.entry_count) == (1, 1, 1)
    assert first.left is left and first.right is right


def test_same_display_identity_with_new_width_misses_and_direction_remains_significant():
    cache, problem, _, _ = setup_cache(specification_for("WidthTransitionRule"))
    left, right = problem.nodes
    assert cache.allows(left, right)
    wider = replace(right, width=D(1100))
    assert wider.node_id == right.node_id
    assert not cache.allows(left, wider)
    assert cache.allows(wider, left)
    assert (cache.miss_count, cache.hit_count, cache.entry_count) == (3, 0, 3)
    assert not cache.allows(left, replace(wider))
    assert cache.hit_count == 1


@pytest.mark.parametrize(
    "change",
    (
        {"width": D(1100)},
        {"thickness": D("1.5")},
        {"min_temperature": D(710)},
        {"max_temperature": D(810)},
        {"material_role": MaterialRole.ACTUAL_TRANSITION},
        {"soft_hard_class": "hard"},
        {"hot_roll_grade": "OTHER"},
    ),
)
def test_every_enabled_edge_business_field_participates_in_semantic_identity(change):
    cache, problem, rules, context = setup_cache()
    left, right = problem.nodes
    updates = dict(change)
    if next(iter(updates)) in ("soft_hard_class", "hot_roll_grade"):
        updates = {"rule_attributes": dict(right.rule_attributes) | updates}
    changed = replace(right, **updates)
    assert cache.semantic_fingerprint(right) != cache.semantic_fingerprint(changed)
    cache.evaluate_edge(EdgeRuleSubject("before", left, right))
    subject = EdgeRuleSubject("after", left, changed)
    assert cache.evaluate_edge(subject) == rules.evaluate_edge(subject, context)
    assert cache.miss_count == 2 and cache.entry_count == 2


def test_optional_soft_class_is_declared_separately_from_mandatory_input_fields():
    cache, problem, rules, _ = setup_cache(specification_for("SoftHardConnectionRule"))
    (rule,) = rules.rules
    assert rule.required_fields() == ("hot_roll_grade",)
    assert set(rule.edge_semantic_fields()) == {
        "material_role",
        "rule_attributes.soft_hard_class",
        "rule_attributes.hot_roll_grade",
    }
    left, right = problem.nodes
    missing = replace(
        right, rule_attributes=dict(right.rule_attributes) | {"soft_hard_class": None}
    )
    assert cache.allows(left, missing)  # The configured same-hot-roll fallback permits this.
    different_hot = replace(
        missing, rule_attributes=dict(missing.rule_attributes) | {"hot_roll_grade": "OTHER"}
    )
    assert not cache.allows(left, different_hot)
    assert cache.entry_count == 2


def test_node_source_identity_weight_and_chain_only_attributes_do_not_fragment_edge_cache():
    cache, problem, _, _ = setup_cache()
    original = problem.nodes[0]
    changed = replace(
        original,
        node_id="temporary",
        source_order_id="other-source",
        source_resource_id="other-resource",
        source_period="P1",
        weight=D(30),
        grade="OTHER",
        rule_attributes=dict(original.rule_attributes)
        | {"surface_grade": "FD", "grade_class": "OTHER", "customer_name": "customer"},
    )
    assert cache.semantic_fingerprint(original) == cache.semantic_fingerprint(changed)


def test_only_enabled_edge_rules_are_queried_for_semantic_fields(monkeypatch):
    def unexpected(self):
        raise AssertionError("inactive or non-edge rule was read")

    monkeypatch.setattr(HighSurfaceRunCountRule, "edge_semantic_fields", unexpected)
    monkeypatch.setattr(WidthTransitionRule, "edge_semantic_fields", unexpected)
    original = gqga4_spec.__wrapped__()
    selected = tuple(
        replace(item, enabled=False) if item.rule_type == "WidthTransitionRule" else item
        for item in original.rules
        if item.rule_type in ("WidthTransitionRule", "HighSurfaceRunCountRule")
    )
    cache, problem, _, _ = setup_cache(make_spec(rules=selected))
    left, right = problem.nodes
    changed = replace(right, width=D(1500), rule_attributes={})
    assert cache.semantic_fingerprint(right) == cache.semantic_fingerprint(changed)
    assert cache.allows(left, changed)


def test_explicit_empty_dependencies_support_an_enabled_constant_edge_rule():
    spec = specification_for("TemperatureOverlapRule")
    (definition,) = spec.rules
    ignored = replace(
        definition, parameters=dict(definition.parameters) | {"ignore_temperature": True}
    )
    cache, problem, rules, _ = setup_cache(make_spec(rules=(ignored,)))
    assert rules.rules[0].enabled and rules.rules[0].edge_semantic_fields() == ()
    left, right = problem.nodes
    changed = replace(right, min_temperature=D(900), max_temperature=D(1000))
    assert cache.semantic_fingerprint(right) == cache.semantic_fingerprint(changed)
    assert cache.allows(left, right) and cache.allows(left, changed)
    assert (cache.miss_count, cache.hit_count, cache.entry_count) == (1, 1, 1)


def test_node_fingerprint_memo_retains_the_object_and_avoids_repeated_encoding(monkeypatch):
    from apsgo_scheduler.core import compatibility

    cache, problem, _, _ = setup_cache()
    node = problem.nodes[0]
    calls = []
    original = compatibility.fingerprint

    def count_encoding(value):
        calls.append(True)
        return original(value)

    monkeypatch.setattr(compatibility, "fingerprint", count_encoding)
    first = cache.semantic_fingerprint(node)
    assert cache.semantic_fingerprint(node) == first
    assert calls == [True]
    # Retaining the object prevents reuse of its address by a later temporary node.
    assert cache._node_fingerprints[id(node)][0] is node


def test_temporary_object_memo_is_bounded_and_eviction_preserves_edge_identity(monkeypatch):
    from apsgo_scheduler.core import compatibility

    cache, problem, rules, context = setup_cache(specification_for("WidthTransitionRule"))
    left, original = problem.nodes
    right = replace(virtual(original), width=D(1500))
    semantic = cache.semantic_fingerprint(right)
    first = cache.evaluate_edge(EdgeRuleSubject("first", left, right))
    assert first.violations
    identity = cache.identity
    limit = compatibility._NODE_FINGERPRINT_MEMO_LIMIT
    for index in range(limit + 1):
        temporary = replace(
            right,
            node_id=f"temporary-{index}",
            virtual_lineage=replace(right.virtual_lineage, accepted_sequence=index + 1),
        )
        assert cache.semantic_fingerprint(temporary) == semantic
        assert len(cache._node_fingerprints) <= limit
    assert id(right) not in cache._node_fingerprints
    calls = []
    original_encoding = compatibility.fingerprint

    def count_encoding(value):
        calls.append(True)
        return original_encoding(value)

    monkeypatch.setattr(compatibility, "fingerprint", count_encoding)
    subject = EdgeRuleSubject("after-eviction", left, right)
    actual = cache.evaluate_edge(subject)
    assert actual == rules.evaluate_edge(subject, context)
    assert all(item.subject_id == "after-eviction" for item in actual.violations)
    assert cache.semantic_fingerprint(right) == semantic
    assert len(calls) == 2
    assert cache.identity == identity
    assert (cache.miss_count, cache.hit_count, cache.entry_count) == (1, 1, 1)
    assert len(cache._node_fingerprints) == limit


def test_object_fingerprint_memo_hit_refreshes_eviction_order():
    from apsgo_scheduler.core import compatibility

    cache, problem, _, _ = setup_cache(make_spec())
    limit = compatibility._NODE_FINGERPRINT_MEMO_LIMIT
    nodes = tuple(replace(problem.nodes[0], node_id=f"memo-{index}") for index in range(limit))
    for node in nodes:
        cache.semantic_fingerprint(node)
    refreshed = nodes[0]
    cache.semantic_fingerprint(refreshed)
    newcomer = replace(refreshed, node_id="newcomer")
    cache.semantic_fingerprint(newcomer)
    assert len(cache._node_fingerprints) == limit
    assert cache._node_fingerprints[id(refreshed)][0] is refreshed
    assert cache._node_fingerprints[id(newcomer)][0] is newcomer
    assert id(nodes[1]) not in cache._node_fingerprints


def test_virtual_business_lineage_is_distinct_but_acceptance_sequence_is_not():
    cache, problem, _, _ = setup_cache()
    original = virtual(problem.nodes[0])
    baseline = cache.semantic_fingerprint(original)
    assert cache.semantic_fingerprint(virtual(problem.nodes[0], sequence=2)) == baseline
    assert (
        cache.semantic_fingerprint(virtual(problem.nodes[0], prototype="prototype-1")) != baseline
    )
    assert (
        cache.semantic_fingerprint(virtual(problem.nodes[0], purpose=VirtualPurpose.WEIGHT_FILL))
        != baseline
    )
    separator = virtual(
        problem.nodes[0], purpose=VirtualPurpose.SPLIT_SEPARATOR, partition="part-a"
    )
    changed = replace(
        separator, virtual_lineage=replace(separator.virtual_lineage, related_partition_id="part-b")
    )
    assert cache.semantic_fingerprint(separator) != cache.semantic_fingerprint(changed)
    assert cache.semantic_fingerprint(separator) != baseline


def test_split_partition_is_business_identity_but_piece_and_acceptance_sequences_are_not():
    cache, problem, _, _ = setup_cache()
    node = problem.nodes[0]
    lineage = SplitLineage(
        "partition",
        "parent",
        node.source_order_id,
        node.source_resource_id,
        "P0",
        "P0",
        ControlledSplitMode.SAME_PERIOD_SPLIT,
        "P0",
        1,
        D(20),
        1,
        2,
        "split-rule",
        "1",
        "decision",
        "authorized",
    )
    piece = replace(node, split_lineage=lineage)
    renamed = replace(
        piece,
        node_id="piece-two",
        split_lineage=replace(lineage, piece_index=2, accepted_source_sequence=2),
    )
    other_partition = replace(piece, split_lineage=replace(lineage, partition_id="other"))
    assert cache.semantic_fingerprint(piece) == cache.semantic_fingerprint(renamed)
    assert cache.semantic_fingerprint(piece) != cache.semantic_fingerprint(other_partition)


def test_exact_decimal_rule_metrics_are_not_coalesced_by_equal_float_projection():
    cache, problem, rules, context = setup_cache(make_spec())
    left, right = problem.nodes
    changed = replace(right, width=D("1000.000000000000000001"))
    assert float(right.width) == float(changed.width)
    assert cache.semantic_fingerprint(right) != cache.semantic_fingerprint(changed)
    first = cache.evaluate_edge(EdgeRuleSubject("first", left, right))
    subject = EdgeRuleSubject("second", left, changed)
    second = cache.evaluate_edge(subject)
    assert first.metrics[0].value == 0
    assert second == rules.evaluate_edge(subject, context)
    assert second.metrics[0].value == D("0.000000000000000001")
    assert cache.entry_count == 2


def test_fresh_and_cached_decimal_rule_results_are_independent_of_caller_context():
    spec = make_spec()
    definition = replace(spec.rules[0], parameters={"maximum_increase": D("0.000000000000000001")})
    cache, problem, rules, context = setup_cache(make_spec(rules=(definition,)))
    left, original = problem.nodes
    subject = EdgeRuleSubject(
        "decimal-context", left, replace(original, width=D("1000.123456789123456789"))
    )
    expected = rules.evaluate_edge(subject, context)
    assert expected.metrics[0].value == D("0.123456789123456789")
    assert expected.violations[0].severity == D("0.123456789123456788")
    assert cache.evaluate_edge(subject) == expected
    for precision, rounding in ((2, ROUND_UP), (5, ROUND_DOWN)):
        with localcontext() as decimal_context:
            decimal_context.prec = precision
            decimal_context.rounding = rounding
            decimal_context.traps[Inexact] = True
            decimal_context.traps[Rounded] = True
            assert rules.evaluate_edge(subject, context) == expected
            assert cache.evaluate_edge(subject) == expected
            fresh = RuleEdgeDecisionCache(problem, rules, context)
            assert fresh.evaluate_edge(subject) == expected
    assert (cache.miss_count, cache.hit_count) == (1, 2)


def test_two_allowed_cached_edges_do_not_bypass_the_virtual_bridge_chain_rule():
    cache, problem, rules, context = setup_cache(specification_for("WidthTransitionRule"))
    left, original = problem.nodes
    bridge = replace(virtual(original), node_id="bridge", width=D(1200))
    right = replace(original, width=D(1050))
    assert cache.allows(left, bridge) and cache.allows(bridge, right)
    subject = ChainRuleSubject("chain", Chain("chain", (left, bridge, right), "P0"))
    contribution = rules.evaluate_complete_chain(subject, context)
    assert [item.reason_code for item in contribution.violations] == [
        "virtual_bridge_reverse_width_exceeded"
    ]
    assert contribution.violations[0].scope is RuleScope.CHAIN


def test_cache_identity_and_entries_are_local_to_the_problem_and_rule_configuration():
    first, problem, rules, context = setup_cache(make_spec())
    assert first.identity == (problem.input_fingerprint, rules.fingerprint, NUMERIC_SEMANTICS_KEY)
    assert first.problem is problem and first.rule_set is rules and first.context is context
    assert first.numeric_semantics_key == NUMERIC_SEMANTICS_KEY
    left, right = problem.nodes
    right = replace(right, width=D(1100))
    assert not first.allows(left, right)
    fresh = RuleEdgeDecisionCache(problem, rules, context)
    assert (fresh.hit_count, fresh.miss_count, fresh.entry_count) == (0, 0, 0)
    spec = make_spec()
    relaxed = make_spec(rules=(replace(spec.rules[0], parameters={"maximum_increase": D(200)}),))
    other, _, _, _ = setup_cache(relaxed)
    assert other.identity != first.identity and other.allows(left, right)
    for field in ("identity", "problem", "rule_set", "context", "numeric_semantics_key"):
        with pytest.raises((AttributeError, TypeError)):
            setattr(first, field, None)


@pytest.mark.parametrize(
    "defect",
    (
        "problem",
        "rules",
        "context",
        "line",
        "process",
        "scenario",
        "periods",
        "prototypes",
        "numeric",
    ),
)
def test_cache_rejects_wrong_task_context_and_contract_identity(defect):
    _, problem, rules, context = setup_cache()
    numeric = NUMERIC_SEMANTICS_KEY
    if defect == "problem":
        problem = None
    elif defect == "rules":
        rules = None
    elif defect == "context":
        context = None
    elif defect in ("line", "process", "scenario"):
        name = {"line": "product_line_code", "process": "process_code", "scenario": "scenario"}[
            defect
        ]
        rules = replace(rules, **{name: "OTHER"})
    elif defect == "periods":
        context = RuleEvaluationContext(
            ("P1", "P0"), {"P1": 0, "P0": 1}, context.virtual_prototype_ids
        )
    elif defect == "prototypes":
        context = RuleEvaluationContext(context.period_order, context.period_index, ())
    else:
        numeric = "other-numeric-contract"
    with pytest.raises(ValueError):
        RuleEdgeDecisionCache(problem, rules, context, numeric)


@pytest.mark.parametrize(
    "declaration", (None, ("node_id",), ("width", "width"), ("unknown_core_field",))
)
def test_missing_or_invalid_edge_dependency_declaration_fails_closed(monkeypatch, declaration):
    monkeypatch.setattr(SyntheticWidthLimitRule, "edge_semantic_fields", lambda self: declaration)
    with pytest.raises(ValueError):
        setup_cache(make_spec())


def test_base_rule_has_no_implicit_required_field_fallback_for_edge_dependencies(monkeypatch):
    monkeypatch.setattr(SyntheticWidthLimitRule, "edge_semantic_fields", Rule.edge_semantic_fields)
    with pytest.raises(ValueError):
        setup_cache(make_spec())


def test_invalid_subject_unknown_virtual_prototype_and_nonfinite_projection_do_not_cache():
    cache, problem, _, _ = setup_cache()
    node = problem.nodes[0]
    with pytest.raises((ValueError, TypeError)):
        cache.evaluate_edge(NodeRuleSubject("node", node))
    with pytest.raises(ValueError):
        cache.semantic_fingerprint(None)
    with pytest.raises(ValueError):
        cache.semantic_fingerprint(virtual(node, prototype="unknown"))
    with pytest.raises(ValueError):
        cache.semantic_fingerprint(replace(node, width=D("1e1000")))
    assert cache.entry_count == 0


def test_wrong_violation_subject_is_rejected_instead_of_cached(monkeypatch):
    cache, problem, _, _ = setup_cache(make_spec())
    original_evaluate = SyntheticWidthLimitRule.evaluate

    def wrong_subject(self, subject, context):
        contribution = original_evaluate(self, subject, context)
        return RuleContribution(
            tuple(replace(item, subject_id="foreign") for item in contribution.violations),
            contribution.metrics,
        )

    monkeypatch.setattr(SyntheticWidthLimitRule, "evaluate", wrong_subject)
    left, right = problem.nodes
    with pytest.raises(ValueError):
        cache.evaluate_edge(EdgeRuleSubject("expected", left, replace(right, width=D(1100))))
    assert cache.entry_count == 0
