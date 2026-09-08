"""Numeric preparation only: binding, exact inputs and bounded, zero-cost stops."""

from dataclasses import fields, replace
from decimal import Decimal
from itertools import product

import numpy as np
import pytest

from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core import _bridge_numeric as numeric
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from apsgo_scheduler.core.compatibility import RuleEdgeDecisionCache
from apsgo_scheduler.core.contracts import RuleScope, SearchStopReason, fingerprint
from apsgo_scheduler.core.model import MaterialRole, Node, VirtualMaterialPrototype, VirtualPurpose
from apsgo_scheduler.core.rules.base import Rule, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import (
    SoftHardConnectionRule,
    TemperatureOverlapRule,
    ThicknessTransitionRule,
    WidthTransitionRule,
)
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
from apsgo_scheduler.core.virtual_material import VirtualFactory, virtual_smoothness
from tests.app.test_input_normalizer import gqga4_request, gqga4_spec
from tests.core.graph.test_bipartite_matching import budget
from tests.core.graph.test_construction_order import node
from tests.core.search.test_virtual_material_factory import make_factory, prototype
from tests.core.test_model_contracts import lineage

D = Decimal


def rule(kind, *, enabled=True, rule_id=None, **changes):
    values = {
        WidthTransitionRule: {"max_reverse_width": D(20), "virtual_width_tolerance": D(200)},
        ThicknessTransitionRule: {
            "basis": "thinner", "ranges": (), "fallback_tolerance": D("0.125"),
        },
        TemperatureOverlapRule: {
            "min_overlap": D(10), "ignore_temperature": False,
            "virtual_temperature_adaptive": True,
        },
        SoftHardConnectionRule: {
            "virtual_sphc_allows_bridge": True, "transition_material_breaks_soft_hard": False,
            "missing_grade_policy": "fallback_same_hot_roll_grade",
        },
    }
    parameters = values[kind] | changes
    name = kind.__name__ if rule_id is None else rule_id
    return kind(name, name, RuleScope.EDGE, enabled, "1", parameters)


def assert_array(array, dtype, shape):
    assert isinstance(array, np.ndarray) and array.dtype == np.dtype(dtype)
    assert array.shape == shape and array.flags.c_contiguous and not array.flags.writeable
    if array.size:
        with pytest.raises(ValueError, match="read-only"):
            array.flat[0] = array.flat[0]


def clone_as(kind, value):
    return kind(**{item.name: getattr(value, item.name) for item in fields(value) if item.init})


def test_formal_gqga4_rules_and_prototypes_qualify_without_running_search():
    request = gqga4_request.__wrapped__(gqga4_spec.__wrapped__())
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    context = RuleEvaluationContext(
        problem.period_order,
        {period: index for index, period in enumerate(problem.period_order)},
        tuple(item.prototype_id for item in problem.virtual_prototypes),
    )
    factory = VirtualFactory(RuleEdgeDecisionCache(problem, rules, context), budget())
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None and catalog.matches(factory.cache)
    assert len(problem.nodes) == 531
    assert_array(catalog.geometry, np.float64, (27, 2))
    assert catalog.rule_kinds.tolist() == [0, 1, 2, 3]
    assert catalog.parameters.tolist() == [200.0, 0.1, 10.0]
    assert catalog.switches.tolist() == [False, False, True, True]
    assert_array(catalog.bands, np.float64, (4, 3))
    assert catalog.geometry.tolist() == [
        [float(item.width), float(item.thickness)] for item in problem.virtual_prototypes
    ]
    assert not catalog.missing.any()
    assert factory.budget.candidate_check_count == factory.cache.entry_count == 0


def test_catalog_layout_uses_rule_order_and_explicit_parameter_flags():
    bands = (
        {"min": D("0.5"), "max": D(1), "include_min": True, "include_max": False,
         "tolerance": D("0.125"), "calculation_mode": "absolute"},
        {"min": None, "max": None, "include_min": False, "include_max": True,
         "tolerance": D("0.25"), "calculation_mode": "relative"},
    )
    factory = make_factory(
        (prototype("second-name", width="1200", thickness="0.75"), prototype("first-name")),
        rule_items=(
            rule(TemperatureOverlapRule, ignore_temperature=True, virtual_temperature_adaptive=False),
            rule(SoftHardConnectionRule, virtual_sphc_allows_bridge=False),
            rule(WidthTransitionRule),
            rule(ThicknessTransitionRule, basis=" THICKER ", ranges=bands),
        ),
    )
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    for name, dtype, shape in (
        ("geometry", np.float64, (2, 2)), ("missing", np.bool_, (2, 2)),
        ("rule_kinds", np.int64, (4,)), ("parameters", np.float64, (3,)),
        ("switches", np.bool_, (4,)), ("bands", np.float64, (2, 3)),
        ("band_flags", np.bool_, (2, 5)),
    ):
        assert_array(getattr(catalog, name), dtype, shape)
    assert catalog.geometry.tolist() == [[1200.0, 0.75], [1000.0, 1.0]]
    assert not catalog.missing.any()
    assert catalog.rule_kinds.tolist() == [2, 3, 0, 1]
    assert catalog.parameters.tolist() == [200.0, 0.125, 10.0]
    assert catalog.switches.tolist() == [True, True, False, False]
    assert catalog.bands[0].tolist() == [0.5, 1.0, 0.125]
    assert catalog.bands[1, 2] == 0.25
    assert catalog.band_flags.tolist() == [
        [True, True, True, False, False], [False, False, False, True, True],
    ]
    assert catalog.problem is factory.cache.problem
    assert catalog.rule_set is factory.cache.rule_set
    assert catalog.context is factory.cache.context
    assert factory.budget.candidate_check_count == factory.cache.entry_count == 0


@pytest.mark.parametrize("enabled", tuple(product((False, True), repeat=4)))
def test_only_enabled_builtin_rules_enter_ordered_catalog(enabled):
    kinds = (WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule, SoftHardConnectionRule)
    factory = make_factory((prototype("p"),), rule_items=tuple(
        rule(kind, enabled=active) for kind, active in zip(kinds, enabled)
    ))
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    assert catalog.rule_kinds.tolist() == [index for index, active in enumerate(enabled) if active]


@pytest.mark.parametrize("size", (0, 1, 27, 65, 129))
def test_catalog_size_is_not_fixed_and_empty_rules_and_bands_are_supported(size):
    factory = make_factory(tuple(prototype(f"p-{index}", width=str(1000 + index)) for index in range(size)))
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    assert_array(catalog.geometry, np.float64, (size, 2))
    assert_array(catalog.missing, np.bool_, (size, 2))
    assert_array(catalog.rule_kinds, np.int64, (0,))
    assert_array(catalog.bands, np.float64, (0, 3))
    assert_array(catalog.band_flags, np.bool_, (0, 5))
    assert catalog.geometry[:, 0].tolist() == [float(1000 + index) for index in range(size)]
    result = numeric.prepare_bridge(catalog, *factory.cache.problem.nodes, factory.budget)
    assert result is not None
    assert_array(result[0], np.float64, (size + 2, 4))
    assert_array(result[1], np.bool_, (size + 2, 4))
    assert factory.budget.candidate_check_count == 0 and factory.budget.stop_reason is None


def test_missing_and_underflow_zero_are_distinct_in_both_catalog_and_anchors():
    factory = make_factory((prototype("missing", width=None, thickness=None),
                            prototype("tiny", width="1e-10000", thickness="1e-10000")))
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    assert catalog.missing.tolist() == [[True, True], [False, False]]
    assert catalog.geometry[1].tolist() == [0.0, 0.0]
    left = replace(factory.cache.problem.nodes[0], width=None, thickness=D("1e-10000"))
    values, missing = numeric.prepare_bridge(catalog, left, factory.cache.problem.nodes[1], factory.budget)
    assert missing[0, :2].tolist() == [True, False]
    assert values[0, 1] == 0.0
    assert missing[2:, :2].tolist() == catalog.missing.tolist()


def test_derived_temperature_uses_original_decimal_extrema_not_prototype_temperature():
    factory = make_factory((
        prototype("unused-temperature", min_temperature=D("1e10000"), max_temperature=D("2e10000")),
        prototype("p"),
    ))
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    left = replace(factory.cache.problem.nodes[0], min_temperature=D("700.00000000000000001"),
                   max_temperature=None)
    right = replace(factory.cache.problem.nodes[1], min_temperature=None,
                    max_temperature=D("900.00000000000000001"))
    values, missing = numeric.prepare_bridge(catalog, left, right, factory.budget)
    assert missing[:2, 2:].tolist() == [[False, True], [True, False]]
    assert values[2:, 2:].tolist() == [[700.0, 900.0], [700.0, 900.0]]
    assert not missing[2:, 2:].any()


def test_float_equal_but_decimal_reversed_derived_temperature_returns_fallback():
    factory = make_factory((prototype("p"),))
    catalog = numeric.prepare_catalog(factory)
    left = replace(factory.cache.problem.nodes[0], min_temperature=D("1.00000000000000001"),
                   max_temperature=None)
    right = replace(factory.cache.problem.nodes[1], min_temperature=None, max_temperature=D(1))
    assert float(left.min_temperature) == float(right.max_temperature)
    assert numeric.prepare_bridge(catalog, left, right, factory.budget) is None
    assert factory.budget.stop_reason is None and factory.budget.candidate_check_count == 0


@pytest.mark.parametrize("minimum,maximum,expected_missing", (
    (None, None, [True, True]), (None, "900", [True, False]),
    ("700", None, [False, True]), ("-1e-10000", "1e-10000", [False, False]),
))
def test_derived_missing_temperature_bounds_keep_their_own_masks(minimum, maximum, expected_missing):
    anchors = tuple(replace(node(name),
                            min_temperature=None if minimum is None else D(minimum),
                            max_temperature=None if maximum is None else D(maximum))
                    for name in ("left", "right"))
    factory = make_factory((prototype("p"),), nodes=anchors)
    catalog = numeric.prepare_catalog(factory)
    values, missing = numeric.prepare_bridge(catalog, *anchors, factory.budget)
    assert missing[:, 2:].tolist() == [expected_missing] * 3
    assert np.isfinite(values).all()
    if minimum == "-1e-10000":
        assert values[2, 2] == values[2, 3] == 0.0
        assert np.signbit(values[2, 2]) and not np.signbit(values[2, 3])


@pytest.mark.parametrize("role", ("ordinary", "transition", "virtual", "split"))
def test_anchor_roles_keep_geometry_and_derived_temperature(role):
    factory = make_factory((prototype("p"),), rule_items=(rule(SoftHardConnectionRule),))
    catalog = numeric.prepare_catalog(factory)
    left, right = factory.cache.problem.nodes
    if role == "transition":
        left = replace(left, material_role=MaterialRole.ACTUAL_TRANSITION)
    elif role == "virtual":
        left = factory.materialize(factory.cache.problem.virtual_prototypes[0], left, right,
                                   purpose=VirtualPurpose.EDGE_BRIDGE, sequence=7)
    elif role == "split":
        split = lineage(parent_node_id=left.node_id, parent_source_order_id=left.source_order_id,
                        source_resource_id=left.source_resource_id, source_period="period",
                        origin_assigned_period="period", target_assigned_period="period",
                        parent_weight=left.weight)
        left = replace(left, node_id="piece", weight=D(10), split_lineage=split)
    values, missing = numeric.prepare_bridge(catalog, left, right, factory.budget)
    assert values.tolist() == [[1000.0, 1.0, 700.0, 900.0]] * 3
    assert not missing.any()
    assert catalog.switches[3]


@pytest.mark.parametrize("field", ("width", "thickness"))
def test_overflowing_unselected_prototype_returns_fallback_without_partial_catalog(field):
    item = replace(prototype("unselected"), **{field: D("1e10000")})
    factory = make_factory((prototype("ordinary"), item))
    before = fingerprint(factory.cache.problem)
    assert numeric.prepare_catalog(factory) is None
    assert fingerprint(factory.cache.problem) == before
    assert factory.budget.stop_reason is None and factory.cache.entry_count == 0


@pytest.mark.parametrize("attribute", ("width", "node_id", "virtual_lineage"))
def test_prototype_attributes_that_materialize_would_reject_use_python_fallback(attribute):
    factory = make_factory((prototype("p", rule_attributes={attribute: "reserved"}),))
    assert numeric.prepare_catalog(factory) is None
    with pytest.raises(ValueError, match="shadow core node fields"):
        factory.materialize(factory.cache.problem.virtual_prototypes[0], *factory.cache.problem.nodes,
                            purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)


def test_unused_virtual_grade_attributes_do_not_become_new_validation_requirements():
    factory = make_factory((prototype("p", rule_attributes={
        "soft_hard_class": 123, "hot_roll_grade": 456,
    }),), rule_items=(rule(SoftHardConnectionRule),))
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None and catalog.switches[3]
    virtual = factory.materialize(factory.cache.problem.virtual_prototypes[0],
                                  *factory.cache.problem.nodes,
                                  purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)
    assert factory.cache.allows(factory.cache.problem.nodes[0], virtual)


@pytest.mark.parametrize("field", ("width", "thickness", "min_temperature", "max_temperature"))
def test_unsafe_anchor_projection_is_fallback_not_a_new_exception(field):
    factory = make_factory((prototype("p"),))
    catalog = numeric.prepare_catalog(factory)
    left, right = factory.cache.problem.nodes
    changes = {field: D("1e10000")}
    if field == "min_temperature":
        changes["max_temperature"] = None
    left = replace(left, **changes)
    assert numeric.prepare_bridge(catalog, left, right, factory.budget) is None
    assert factory.budget.stop_reason is None


@pytest.mark.parametrize("binding", ("problem", "rule_set", "context"))
def test_catalog_matches_bound_objects_not_equal_values_or_cached_edge_storage(binding):
    factory = make_factory((prototype("p"),), rule_items=(rule(WidthTransitionRule),))
    catalog = numeric.prepare_catalog(factory)
    assert catalog.matches(factory.cache)
    assert catalog.matches(replace(factory.cache))
    copied = replace(getattr(factory.cache, binding))
    assert copied == getattr(factory.cache, binding) and copied is not getattr(factory.cache, binding)
    assert not catalog.matches(replace(factory.cache, **{binding: copied}))


def test_repeated_binding_preparation_does_not_reuse_other_rule_or_anchor_values():
    first = make_factory((prototype("p"),), rule_items=(rule(WidthTransitionRule),))
    second = make_factory((prototype("p", width="1200"),), rule_items=(
        rule(WidthTransitionRule, virtual_width_tolerance=D(25)),
    ))
    old, new = numeric.prepare_catalog(first), numeric.prepare_catalog(second)
    assert old.parameters[0] == 200.0 and new.parameters[0] == 25.0
    assert old.geometry[0, 0] == 1000.0 and new.geometry[0, 0] == 1200.0
    assert not old.matches(second.cache) and not new.matches(first.cache)
    left, right = first.cache.problem.nodes
    original, _ = numeric.prepare_bridge(old, left, right, first.budget)
    changed, _ = numeric.prepare_bridge(old, replace(left, width=D(1234)), right, first.budget)
    assert original[0, 0] == 1000.0 and changed[0, 0] == 1234.0
    assert not np.shares_memory(original, changed)


def test_unknown_and_duplicate_edge_rules_are_not_silently_dropped():
    unknown = make_factory((prototype("p"),), allowed_edges={("left", "right")})
    duplicate = make_factory((prototype("p"),), rule_items=(
        rule(WidthTransitionRule, rule_id="one"), rule(WidthTransitionRule, rule_id="two"),
    ))
    assert numeric.prepare_catalog(unknown) is None
    assert numeric.prepare_catalog(duplicate) is None


@pytest.mark.parametrize("target", ("factory", "cache", "rule_set"))
def test_subclassed_dispatch_objects_are_not_assumed_to_match_builtin_behavior(target):
    factory = make_factory((prototype("p"),), rule_items=(rule(WidthTransitionRule),))
    base = {"factory": VirtualFactory, "cache": RuleEdgeDecisionCache, "rule_set": ProcessRuleSet}[target]
    subclass = type("CustomDispatch", (base,), {})
    if target == "factory":
        factory = clone_as(subclass, factory)
    elif target == "cache":
        factory = replace(factory, cache=clone_as(subclass, factory.cache))
    else:
        factory = replace(factory, cache=replace(factory.cache,
                          rule_set=clone_as(subclass, factory.cache.rule_set)))
    assert numeric.prepare_catalog(factory) is None


def test_builtin_rule_subclass_and_prototype_subclass_use_fallback():
    # RuleSet deliberately permits direct Rule inheritance alongside the concrete parent.
    class CustomWidth(WidthTransitionRule, Rule):
        pass

    class CustomPrototype(VirtualMaterialPrototype):
        pass

    changed_rule = make_factory((prototype("p"),), rule_items=(
        clone_as(CustomWidth, rule(WidthTransitionRule)),
    ))
    changed_prototype = make_factory((clone_as(CustomPrototype, prototype("p")),))
    assert numeric.prepare_catalog(changed_rule) is None
    assert numeric.prepare_catalog(changed_prototype) is None


@pytest.mark.parametrize("change", ("subclass", "source_period", "prototype_id"))
def test_unknown_anchor_identity_or_behavior_cannot_enter_numeric_data(change):
    factory = make_factory((prototype("p"),))
    catalog = numeric.prepare_catalog(factory)
    left, right = factory.cache.problem.nodes
    if change == "subclass":
        class CustomNode(Node):
            pass

        left = clone_as(CustomNode, left)
    elif change == "source_period":
        left = replace(left, source_period="unknown-period")
    else:
        left = factory.materialize(factory.cache.problem.virtual_prototypes[0], left, right,
                                   purpose=VirtualPurpose.EDGE_BRIDGE, sequence=1)
        left = replace(left, virtual_lineage=replace(left.virtual_lineage, prototype_id="unknown"))
    assert numeric.prepare_bridge(catalog, left, right, factory.budget) is None
    assert factory.budget.stop_reason is None


class Signal:
    active = False

    def is_cancelled(self):
        return self.active

    def clock(self):
        return 100.0 if self.active else 1.0


@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_catalog_stops_within_64_projected_prototypes_without_consuming_budget(monkeypatch, kind):
    signal = Signal()
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    factory = make_factory(tuple(prototype(f"p-{index}") for index in range(129)), runtime=runtime)
    before = fingerprint(factory.cache.problem)
    projected = 0
    checked_at = []
    original_project = numeric._project_row
    original_check = SolveRuntimeBudget.allows_search

    def project(*args, **kwargs):
        nonlocal projected
        result = original_project(*args, **kwargs)
        if args[0].shape == (2,) and args[1] is not None:
            projected += 1
            if projected == 1:
                signal.active = True
        return result

    def check(self):
        checked_at.append(projected)
        return original_check(self)

    monkeypatch.setattr(numeric, "_project_row", project)
    monkeypatch.setattr(SolveRuntimeBudget, "allows_search", check)
    result = numeric.prepare_catalog(factory)
    assert result is None and signal.active
    assert 1 <= projected <= 64
    assert max(right - left for left, right in zip(checked_at, checked_at[1:])) <= 64
    assert runtime.stop_reason is (SearchStopReason.USER_CANCELLED if kind == "cancel" else
                                   SearchStopReason.SEARCH_TIME_LIMIT_REACHED)
    assert runtime.candidate_check_count == factory.cache.entry_count == 0
    assert fingerprint(factory.cache.problem) == before


@pytest.mark.parametrize("kind", ("cancel", "time"))
def test_bridge_preparation_checks_each_block_and_discards_partial_output(monkeypatch, kind):
    signal = Signal()
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    checks = 0
    stop_after = None
    original_check = SolveRuntimeBudget.allows_search

    def check(self):
        nonlocal checks
        checks += 1
        if checks == stop_after:
            signal.active = True
        return original_check(self)

    monkeypatch.setattr(SolveRuntimeBudget, "allows_search", check)
    observed = {}
    for size in (0, 64, 65, 129):
        factory = make_factory(tuple(prototype(f"p-{index}") for index in range(size)), runtime=runtime)
        catalog = numeric.prepare_catalog(factory)
        checks = 0
        assert numeric.prepare_bridge(catalog, *factory.cache.problem.nodes, runtime) is not None
        observed[size] = checks
    assert observed[64] >= observed[0] + 2
    assert observed[65] >= observed[64] + 2
    assert observed[129] >= observed[65] + 2
    # Stop near the final block, after earlier blocks have already been assembled.
    stop_after = observed[129] - 2
    checks = 0
    before = fingerprint(factory.cache.problem)
    assert numeric.prepare_bridge(catalog, *factory.cache.problem.nodes, runtime) is None
    assert checks == stop_after and signal.active
    assert runtime.stop_reason is (SearchStopReason.USER_CANCELLED if kind == "cancel" else
                                   SearchStopReason.SEARCH_TIME_LIMIT_REACHED)
    assert runtime.candidate_check_count == factory.cache.entry_count == 0
    assert fingerprint(factory.cache.problem) == before


def test_stage_one_does_not_connect_the_numeric_preparer_to_production_bridge(monkeypatch):
    factory = make_factory((prototype("a", width="1100"), prototype("b", width="1200")),
                           nodes=(node("left", width="1000"), node("right", width="1300")),
                           rule_items=(rule(WidthTransitionRule),))

    def forbidden(*args, **kwargs):
        raise AssertionError("stage one must not invoke numeric preparation from bridge")

    monkeypatch.setattr(numeric, "prepare_catalog", forbidden)
    selected = factory.bridge(*factory.cache.problem.nodes, max_nodes=2, first_sequence=5)
    assert len(selected) == 1 and selected[0].virtual_lineage.prototype_id == "a"
    assert factory.cache.miss_count > 0 and factory.budget.candidate_check_count == 0


def prepared(factory):
    catalog = numeric.prepare_catalog(factory)
    assert catalog is not None
    arrays = numeric.prepare_bridge(catalog, *factory.cache.problem.nodes, factory.budget)
    assert arrays is not None
    return catalog, *arrays


def selected_nodes(factory, indices):
    if indices is None:
        return None
    return tuple(
        factory.materialize(
            factory.cache.problem.virtual_prototypes[index], *factory.cache.problem.nodes,
            purpose=VirtualPurpose.EDGE_BRIDGE, sequence=7 + position,
        )
        for position, index in enumerate(indices)
    )


def public_scan_oracle(factory, length):
    """Small exhaustive comparison using public object rules and the original score."""
    left, right = factory.cache.problem.nodes
    valid = []
    for indices in product(range(len(factory.cache.problem.virtual_prototypes)), repeat=length):
        middle = selected_nodes(factory, indices)
        path = (left, *middle, right)
        if all(factory.cache.allows(a, b) for a, b in zip(path, path[1:])):
            score = sum(virtual_smoothness(*path[index:index + 3]) for index in range(length))
            valid.append((score, indices))
    return min(valid, key=lambda item: item[0])[1] if valid else None


def native_rule_args(catalog):
    return (catalog.rule_kinds, catalog.parameters, catalog.switches,
            catalog.bands, catalog.band_flags)


@pytest.mark.parametrize("enabled", tuple(product((False, True), repeat=4)))
def test_native_scans_match_all_builtin_subsets_and_original_complete_bridge(enabled):
    kinds = (WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule, SoftHardConnectionRule)
    factory = make_factory(
        tuple(prototype(str(index), width=str(width))
              for index, width in enumerate((1100, 1300, 1150, 1250))),
        nodes=(node("left", width="1000"), node("right", width="1400")),
        rule_items=tuple(rule(kind, enabled=active) for kind, active in zip(kinds, enabled)),
    )
    catalog, values, missing = prepared(factory)
    left, right = factory.cache.problem.nodes
    results = []
    for length, scan in ((1, numeric.scan_single), (2, numeric.scan_double)):
        safe, indices = scan(catalog, values, missing, factory.budget)
        assert safe is True
        assert indices == public_scan_oracle(factory, length)
        results.append(indices)
    chosen = () if factory.cache.allows(left, right) else (
        results[0] if results[0] is not None else results[1]
    )
    assert selected_nodes(factory, chosen) == factory.bridge(
        left, right, max_nodes=2, first_sequence=7,
    )
    assert numeric._scan_block.nopython_signatures
    assert factory.budget.candidate_check_count == 0


@pytest.mark.parametrize("gap,expected", (("0.0000000005", (0,)), ("0.0000000015", None)))
@pytest.mark.parametrize("kind", (WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule))
def test_native_rule_epsilon_matches_public_edge_decision(kind, gap, expected):
    left, right = node("left"), node("right")
    item = prototype("p")
    config = rule(kind)
    if kind is WidthTransitionRule:
        item = replace(item, width=D(1200) + D(gap))
    elif kind is ThicknessTransitionRule:
        item = replace(item, thickness=D("1.125") + D(gap))
    else:
        left = replace(left, max_temperature=D(710) - D(gap))
        config = rule(kind, virtual_temperature_adaptive=False)
    factory = make_factory((item,), nodes=(left, right), rule_items=(config,))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, expected)
    assert public_scan_oracle(factory, 1) == expected
    virtual = selected_nodes(factory, (0,))[0]
    assert numeric._edge(0, 2, values, missing, *native_rule_args(catalog)) == int(
        factory.cache.allows(left, virtual)
    )


@pytest.mark.parametrize("basis", ("thinner", "thicker"))
@pytest.mark.parametrize("include_lower,include_upper", tuple(product((False, True), repeat=2)))
@pytest.mark.parametrize("mode", ("absolute", "relative"))
def test_native_thickness_preserves_first_matching_order_and_exact_range_boundaries(
    basis, include_lower, include_upper, mode,
):
    boundary = D(1) if basis == "thinner" else D(2)
    bands = (
        {"min": boundary, "max": boundary, "include_min": include_lower,
         "include_max": include_upper, "tolerance": D("0.6"), "calculation_mode": mode},
        {"min": None, "max": None, "include_min": True, "include_max": True,
         "tolerance": D("1.25"), "calculation_mode": "absolute"},
    )
    factory = make_factory((prototype("p", thickness="2"),), rule_items=(
        rule(ThicknessTransitionRule, basis=basis, ranges=bands, fallback_tolerance=D(0)),
    ))
    catalog, values, missing = prepared(factory)
    safe, selected = numeric.scan_single(catalog, values, missing, factory.budget)
    assert safe and selected == public_scan_oracle(factory, 1)
    first_matches = include_lower and include_upper
    first_allows = mode == "relative" and basis == "thicker"
    assert selected == ((0,) if not first_matches or first_allows else None)


@pytest.mark.parametrize("kind", (WidthTransitionRule, ThicknessTransitionRule, TemperatureOverlapRule))
def test_native_missing_values_follow_rule_specific_behavior(kind):
    item = prototype("p")
    left, right = node("left"), node("right")
    if kind is WidthTransitionRule:
        item = replace(item, width=None)
    elif kind is ThicknessTransitionRule:
        item = replace(item, thickness=None)
    else:
        left = replace(left, min_temperature=None)
    factory = make_factory((item,), nodes=(left, right), rule_items=(
        rule(kind, **({"virtual_temperature_adaptive": False} if kind is TemperatureOverlapRule else {})),
    ))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (
        True, None if kind is WidthTransitionRule else (0,),
    )
    assert public_scan_oracle(factory, 1) == (None if kind is WidthTransitionRule else (0,))


def test_double_bridge_counts_middle_edge_twice_and_preserves_complete_nodes():
    factory = make_factory(
        tuple(prototype(str(index), width=str(width))
              for index, width in enumerate((1100, 1300, 1150, 1250))),
        nodes=(node("left", width="1000"), node("right", width="1400")),
        rule_items=(rule(WidthTransitionRule),),
    )
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, None)
    assert numeric.scan_double(catalog, values, missing, factory.budget) == (True, (2, 3))
    assert selected_nodes(factory, (2, 3)) == factory.bridge(
        *factory.cache.problem.nodes, max_nodes=2, first_sequence=7,
    )


@pytest.mark.parametrize("widths,expected", (((1100, 1300, 1100, 1300), (0, 1)), ((1200,), (0, 0))))
def test_double_scan_keeps_first_pair_ties_and_allows_same_prototype_twice(widths, expected):
    factory = make_factory(
        tuple(prototype(str(index), width=str(width)) for index, width in enumerate(widths)),
        nodes=(node("left", width="1000"), node("right", width="1400")),
        rule_items=(rule(WidthTransitionRule),),
    )
    catalog, values, missing = prepared(factory)
    assert numeric.scan_double(catalog, values, missing, factory.budget) == (True, expected)
    assert public_scan_oracle(factory, 2) == expected
    if len(widths) == 1:
        # The wrapper would select the already valid single; test the double primitive itself.
        assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, (0,))


@pytest.mark.parametrize("error_on_first_edge", (True, False))
def test_denied_rule_does_not_hide_same_edge_error_but_denied_edge_skips_next(error_on_first_edge):
    left, right = node("left"), node("right")
    wide = {"min_temperature": D("-1e308"), "max_temperature": D("1e308")}
    if error_on_first_edge:
        left = replace(left, **wide)
    else:
        right = replace(right, **wide)
    factory = make_factory((prototype("p", width="1400"),), nodes=(left, right), rule_items=(
        rule(WidthTransitionRule), rule(TemperatureOverlapRule, virtual_temperature_adaptive=False),
    ))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (
        not error_on_first_edge, None,
    )
    virtual = selected_nodes(factory, (0,))[0]
    if error_on_first_edge:
        with pytest.raises(ValueError, match="temperature overlap must be finite"):
            factory._connected(left, virtual, right)
    else:
        assert not factory._connected(left, virtual, right)
        with pytest.raises(ValueError, match="temperature overlap must be finite"):
            factory.cache.allows(virtual, right)


def test_denied_soft_rule_does_not_hide_later_thickness_severity_overflow():
    factory = make_factory((prototype("p", thickness="1e308"),), rule_items=(
        rule(SoftHardConnectionRule, virtual_sphc_allows_bridge=False),
        rule(ThicknessTransitionRule, fallback_tolerance=D(0)),
    ))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (False, None)
    virtual = selected_nodes(factory, (0,))[0]
    with pytest.raises(ValueError, match="thickness severity must be finite"):
        factory.cache.allows(factory.cache.problem.nodes[0], virtual)


def test_native_relative_tolerance_overflow_is_unsafe_not_no_bridge():
    band = {"min": None, "max": None, "include_min": True, "include_max": True,
            "tolerance": D("1e308"), "calculation_mode": "relative"}
    factory = make_factory(
        (prototype("p", thickness="1e308"),),
        nodes=(node("left", thickness="1e308"), node("right", thickness="1e308")),
        rule_items=(rule(ThicknessTransitionRule, ranges=(band,)),),
    )
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (False, None)
    with pytest.raises(ValueError, match="thickness calculation must be finite"):
        public_scan_oracle(factory, 1)


def test_late_unsafe_score_discards_earlier_single_winner():
    factory = make_factory((prototype("good"), prototype("late", thickness="1e308")))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (False, None)
    with pytest.raises(ValueError, match="virtual smoothness must be finite"):
        public_scan_oracle(factory, 1)
    assert factory.budget.candidate_check_count == 0 and factory.budget.stop_reason is None


def test_native_double_total_score_overflow_checks_sum_of_finite_scores():
    factory = make_factory((prototype("first"), prototype("second", thickness="6e305")))
    catalog, values, missing = prepared(factory)
    safe_first, first = numeric._score(0, 2, 3, values, missing)
    safe_second, second = numeric._score(2, 3, 1, values, missing)
    assert safe_first and safe_second and first + second == float("inf")
    left, right = factory.cache.problem.nodes
    first_node, second_node = selected_nodes(factory, (0, 1))
    assert first == virtual_smoothness(left, first_node, second_node)
    assert second == virtual_smoothness(first_node, second_node, right)
    assert numeric.scan_double(catalog, values, missing, factory.budget) == (False, None)


@pytest.mark.parametrize("length,size", ((1, 0), (1, 65), (1, 129), (2, 0), (2, 65)))
def test_native_scan_blocks_complete_full_order_without_spending_candidate_budget(length, size):
    factory = make_factory(tuple(prototype(f"p-{index}") for index in range(size)))
    catalog, values, missing = prepared(factory)
    scan = numeric.scan_single if length == 1 else numeric.scan_double
    before = values.copy(), missing.copy()
    assert scan(catalog, values, missing, factory.budget) == (
        True, (0,) * length if size else None,
    )
    np.testing.assert_array_equal(values, before[0])
    np.testing.assert_array_equal(missing, before[1])
    assert factory.budget.candidate_check_count == factory.cache.entry_count == 0
    if size:
        assert numeric._scan_block.nopython_signatures


@pytest.mark.parametrize("kind", ("cancel", "time"))
@pytest.mark.parametrize("length", (1, 2))
def test_native_block_stop_discards_existing_best_and_keeps_candidate_counters(monkeypatch, kind, length):
    signal = Signal()
    runtime = budget(cancellation=signal) if kind == "cancel" else budget(clock=signal.clock)
    factory = make_factory(tuple(prototype(f"p-{index}") for index in range(129)), runtime=runtime)
    catalog, values, missing = prepared(factory)
    scan = numeric.scan_single if length == 1 else numeric.scan_double
    native = numeric._scan_block
    calls = []

    def stop_after_first_block(*args):
        result = native(*args)
        calls.append(args[:4])
        assert result[0] and result[2] >= 0
        signal.active = True
        return result

    monkeypatch.setattr(numeric, "_scan_block", stop_after_first_block)
    assert scan(catalog, values, missing, runtime) == (False, None)
    assert len(calls) == 1 and calls[0][3] - calls[0][2] <= 64
    assert native.nopython_signatures
    assert runtime.stop_reason is (SearchStopReason.USER_CANCELLED if kind == "cancel" else
                                   SearchStopReason.SEARCH_TIME_LIMIT_REACHED)
    assert runtime.candidate_check_count == factory.cache.entry_count == 0


@pytest.mark.parametrize("length", (1, 2))
@pytest.mark.parametrize("change", ("row_count", "column_count", "dtype", "writable", "noncontiguous"))
def test_native_scan_invalid_array_contract_raises_instead_of_falling_back(length, change):
    factory = make_factory((prototype("p"),))
    catalog, values, missing = prepared(factory)
    if change == "row_count":
        values = values[:-1]
    elif change == "column_count":
        values = values[:, :3].copy()
        values.setflags(write=False)
    elif change == "dtype":
        values = values.astype(np.float32)
        values.setflags(write=False)
    elif change == "writable":
        values = values.copy()
    else:
        values = values[:, ::-1]
    scan = numeric.scan_single if length == 1 else numeric.scan_double
    with pytest.raises((TypeError, ValueError)):
        scan(catalog, values, missing, factory.budget)


@pytest.mark.parametrize("mode,first,start,stop", ((2, -1, 0, 1), (0, -1, -1, 1),
                                                (0, -1, 0, 66), (1, 65, 0, 1)))
def test_native_block_invalid_mode_or_indices_are_errors(mode, first, start, stop):
    factory = make_factory(tuple(prototype(f"p-{index}") for index in range(65)))
    catalog, values, missing = prepared(factory)
    with pytest.raises((ValueError, IndexError)):
        numeric._scan_block(mode, first, start, stop, values, missing, *native_rule_args(catalog),
                            -1, -1, 0.0)


def test_native_dispatchers_are_nopython_with_strict_compilation_flags():
    factory = make_factory((prototype("p"),), rule_items=(rule(WidthTransitionRule),))
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, (0,))
    assert numeric._edge(0, 2, values, missing, *native_rule_args(catalog)) == 1
    assert numeric._score(0, 2, 1, values, missing)[0]
    for dispatcher in (numeric._scan_block, numeric._edge, numeric._score):
        assert dispatcher.nopython_signatures
        assert dispatcher.targetoptions["nopython"]
        assert dispatcher.targetoptions["boundscheck"]
        assert not dispatcher.targetoptions["fastmath"]
        assert not dispatcher.targetoptions["parallel"]


@pytest.mark.parametrize("length", (1, 2))
def test_native_winner_and_ties_are_preserved_across_block_and_row_boundaries(length):
    if length == 1:
        prototypes = tuple(prototype(str(index), thickness="1" if index in (64, 128) else "2")
                           for index in range(129))
        factory = make_factory(prototypes)
        expected = (64,)
    else:
        widths = {1: 1300, 64: 1150, 65: 1250, 128: 1150}
        prototypes = tuple(prototype(str(index), width=str(widths.get(index, 1100)))
                           for index in range(129))
        factory = make_factory(prototypes, rule_items=(rule(WidthTransitionRule),),
                               nodes=(node("left", width="1000"), node("right", width="1400")))
        expected = (64, 65)
    catalog, values, missing = prepared(factory)
    scan = numeric.scan_single if length == 1 else numeric.scan_double
    assert scan(catalog, values, missing, factory.budget) == (True, expected)
    if length == 2:
        assert selected_nodes(factory, expected) == factory.bridge(
            *factory.cache.problem.nodes, max_nodes=2, first_sequence=7,
        )


def test_native_score_uses_strict_improvement_without_an_epsilon_tie():
    factory = make_factory(
        (prototype("slightly-worse", width="999.9999999999"), prototype("better", width="1000")),
        nodes=(node("left", width="1000"), node("right", width="1400")),
        rule_items=(rule(WidthTransitionRule, virtual_width_tolerance=D(500)),),
    )
    catalog, values, missing = prepared(factory)
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, (1,))
    assert selected_nodes(factory, (1,)) == factory.bridge(
        *factory.cache.problem.nodes, max_nodes=2, first_sequence=7,
    )


def test_native_temperature_predicate_preserves_severity_overflow_for_valid_nodes():
    factory = make_factory((prototype("p"),), rule_items=(
        rule(TemperatureOverlapRule, min_overlap=D(0), virtual_temperature_adaptive=False),
    ))
    catalog, values, missing = prepared(factory)
    # Predicate-only case: a valid virtual node with a disjoint temperature interval.
    # Normal bridge preparation derives a union interval and cannot generate this case.
    virtual = replace(selected_nodes(factory, (0,))[0],
                      min_temperature=D("-1e308"), max_temperature=D("-1e308"))
    values = values.copy()
    values[2, 2:] = float(virtual.min_temperature), float(virtual.max_temperature)
    values.setflags(write=False)
    assert numeric._edge(0, 2, values, missing, *native_rule_args(catalog)) == -1
    with pytest.raises(ValueError, match="temperature severity must be finite"):
        factory.cache.allows(factory.cache.problem.nodes[0], virtual)


def test_empty_catalog_does_not_call_the_native_dispatcher(monkeypatch):
    factory = make_factory()
    catalog, values, missing = prepared(factory)

    def forbidden(*args):
        raise AssertionError("empty catalog must not trigger compilation or a scan block")

    monkeypatch.setattr(numeric, "_scan_block", forbidden)
    for scan in (numeric.scan_single, numeric.scan_double):
        assert scan(catalog, values, missing, factory.budget) == (True, None)


def test_disabled_virtual_soft_bridge_denies_without_a_later_numeric_error():
    factory = make_factory((prototype("p"),), rule_items=(
        rule(SoftHardConnectionRule, virtual_sphc_allows_bridge=False),
    ))
    catalog, values, missing = prepared(factory)
    for length, scan in ((1, numeric.scan_single), (2, numeric.scan_double)):
        assert scan(catalog, values, missing, factory.budget) == (True, None)
        assert public_scan_oracle(factory, length) is None


@pytest.mark.parametrize("ignore,adaptive", tuple(product((False, True), repeat=2)))
def test_temperature_switches_control_actual_numeric_edges(ignore, adaptive):
    factory = make_factory((prototype("p"),), rule_items=(
        rule(TemperatureOverlapRule, ignore_temperature=ignore,
             virtual_temperature_adaptive=adaptive),
    ), nodes=(replace(node("left"), max_temperature=D(705)), node("right")))
    catalog, values, missing = prepared(factory)
    expected = (0,) if ignore or adaptive else None
    assert numeric.scan_single(catalog, values, missing, factory.budget) == (True, expected)
    assert public_scan_oracle(factory, 1) == expected


def test_unknown_numeric_rule_kind_is_an_implementation_error():
    factory = make_factory((prototype("p"),), rule_items=(rule(WidthTransitionRule),))
    catalog, values, missing = prepared(factory)
    corrupted = catalog.rule_kinds.copy()
    corrupted[0] = 99
    corrupted.setflags(write=False)
    with pytest.raises(ValueError, match="unknown numeric bridge rule kind"):
        numeric.scan_single(replace(catalog, rule_kinds=corrupted), values, missing, factory.budget)
