"""Separate original-entry observations from isolated late-period rule comparisons."""

import json
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import permutations, product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import LateOriginalPeriodMoveRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_late_period_differential")
evaluate_reference = reference["evaluate_plan"]
reference_globals = evaluate_reference.__globals__
original_normalizer = reference_globals["normalize_plan_periods"]
D = Decimal
rule_id = "forbid_late_original_due_period"
metric_key = "late_original_due_period_move_count"
rule = LateOriginalPeriodMoveRule(rule_id, "原订单延后计划期", RuleScope.PLAN, True, "1", {})
disabled_rule = replace(rule, enabled=False)
material_roles = (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION)
period_names = ("z-first", "a-second", "m-third")


def old_rules(periods, enabled=True):
    return reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    {
                        "rule_id": rule_id,
                        "enabled": enabled,
                        "params": {"forbid_late_original_due_period": enabled},
                    }
                ]
            }
        },
        {"allow_consecutive_reverse_width": True, "reverse_width_carrier_grades": ["*"]},
        {"period_order": periods},
    )


def make_case(layout):
    """One real order and one irrelevant virtual node per chain, with unique identities."""
    old_chains, chains = [], []
    for index, (assigned, source_period, role) in enumerate(layout):
        node_id, source_id = f"node-{index}", f"order-{index}"
        node = Node(
            node_id,
            source_id,
            f"resource-{index}",
            source_period,
            D(20),
            None,
            None,
            None,
            None,
            "",
            role,
            {},
        )
        virtual = Node(
            f"virtual-{index}",
            None,
            None,
            None,
            D(1),
            None,
            None,
            None,
            None,
            "",
            MaterialRole.GENERATED_VIRTUAL,
            {},
            VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1),
        )
        old_node = replace(
            reference["_sentinel_node"](),
            node_id=node_id,
            source_order_id=source_id,
            source_period=source_period,
            weight=D(20),
            actual_transition_material=role is MaterialRole.ACTUAL_TRANSITION,
        )
        old_virtual = replace(
            reference["_sentinel_node"](),
            node_id=virtual.node_id,
            weight=D(1),
            node_type="virtual_sphc",
        )
        old_chains.append(reference["Chain"]([old_node, old_virtual], assigned))
        chains.append(Chain(f"chain-{index}", (node, virtual), assigned))
    return old_chains, SchedulePlan(tuple(chains))


def snapshot(chains):
    return tuple((chain.assigned_period, tuple(chain.nodes)) for chain in chains)


def evaluate_isolated(chains, rules):
    # NOT the original complete entry: temporarily isolate its period predicate by
    # disabling only the preceding reassignment, inside this runpy namespace.
    before = snapshot(chains)
    assert reference_globals["normalize_plan_periods"] is original_normalizer
    try:
        reference_globals["normalize_plan_periods"] = lambda items, rules: list(items)
        result = evaluate_reference(chains, rules)
    finally:
        reference_globals["normalize_plan_periods"] = original_normalizer
        assert snapshot(chains) == before
    return result


def target_contribution(plan, periods, enabled=True):
    before = snapshot(plan.chains)
    context = RuleEvaluationContext(
        periods, dict(zip(periods, range(len(periods)))), ("prototype",)
    )
    try:
        # This leaf never reads the resource view; its real implementation belongs to function 7.
        return (rule if enabled else disabled_rule).evaluate(
            PlanRuleSubject("plan", plan, None), context
        )
    finally:
        assert snapshot(plan.chains) == before


def project_reference(result, plan):
    by_source = {
        node.source_order_id: (node, chain.assigned_period)
        for chain in plan.chains
        for node in chain.nodes
        if node.material_role is not MaterialRole.GENERATED_VIRTUAL
    }
    projected = []
    for item in result.violations:
        # No broad filtering: any unrelated failure is a probe failure.
        assert item["rule_id"] == rule_id and item["scope"] == "result_source", item
        assert item["chain_id"] == "" and item["positions"] == []
        assert item["prohibited"] is True and item["severity_value"] == 1.0
        node, assigned = by_source[item["subject_id"]]
        assert item["reason"] == f"source period {node.source_period} assigned later to {assigned}"
        projected.append((node.node_id, D(str(item["severity_value"]))))
    assert result.result_metrics[metric_key] == len(projected)
    return tuple(projected)


def project_target(contribution, enabled=True):
    for item in contribution.violations:
        assert item.rule_id == rule_id and item.scope is RuleScope.PLAN
        assert item.reason_code == "late_original_due_period_move"
        assert item.disposition is RuleDisposition.PROHIBITED and item.severity == D(1)
    assert tuple((item.metric_key, item.value) for item in contribution.metrics) == (
        ((metric_key, len(contribution.violations)),) if enabled else ()
    )
    return tuple((item.subject_id, item.severity) for item in contribution.violations)


# A. Observe the real, unmodified entry. It rewrites assigned periods before testing lateness.
original_observations = []
for assigned, source_index in ((0, 0), (1, 0), (2, 0), (0, 1), (1, 2), (2, 2)):
    layout = ((period_names[assigned], period_names[source_index], material_roles[assigned % 2]),)
    old_chains, plan = make_case(layout)
    before_nodes = tuple(old_chains[0].nodes)
    original = evaluate_reference(old_chains, old_rules(period_names))
    assert original.violations == () and original.result_metrics[metric_key] == 0
    assert old_chains[0].assigned_period == period_names[source_index]
    assert tuple(old_chains[0].nodes) == before_nodes
    target = project_target(target_contribution(plan, period_names))
    assert len(target) == int(assigned > source_index)
    original_observations.append(
        {
            "source_period": period_names[source_index],
            "assigned_before": period_names[assigned],
            "assigned_after_reference": old_chains[0].assigned_period,
            "original_reference_late_count": 0,
            "unchanged_target_plan_late_count": len(target),
        }
    )

# B. Explicitly isolated comparisons, not claims about the original complete entry.
isolated_cases = 0
single_chain_cases = 0
multi_chain_cases = 0
for periods, source_index, assigned, role in product(
    permutations(period_names),
    range(3),
    range(3),
    material_roles,
):
    first = (periods[assigned], periods[source_index], role)
    second = (
        periods[(assigned + 1) % 3],
        periods[(source_index + 1) % 3],
        material_roles[1 - material_roles.index(role)],
    )
    for layout in ((first,), (first, second)):
        old_chains, plan = make_case(layout)
        expected = project_reference(evaluate_isolated(old_chains, old_rules(periods)), plan)
        actual = project_target(target_contribution(plan, periods))
        assert actual == expected, (periods, layout, expected, actual)
        isolated_cases += 1
        single_chain_cases += len(layout) == 1
        multi_chain_cases += len(layout) > 1

# Period count is not fixed to three or to the GQGA4 period identifiers.
variable_period_count_cases = 0
for periods, role in product((("single",), ("z", "b", "y", "a")), material_roles):
    old_chains, plan = make_case(((periods[-1], periods[0], role),))
    expected = project_reference(evaluate_isolated(old_chains, old_rules(periods)), plan)
    assert project_target(target_contribution(plan, periods)) == expected
    isolated_cases += 1
    variable_period_count_cases += 1

# C. Strict disabling and unknown-period protection are deliberately separate differences.
disabled_cases = 0
first_disabled_difference = None
for (source_index, assigned), role, multiple in product(
    ((0, 1), (0, 2), (1, 2)),
    material_roles,
    (False, True),
):
    first = (period_names[assigned], period_names[source_index], role)
    layout = (first, first) if multiple else (first,)
    old_chains, plan = make_case(layout)
    old = project_reference(evaluate_isolated(old_chains, old_rules(period_names, False)), plan)
    new = project_target(target_contribution(plan, period_names, False), enabled=False)
    assert len(old) == len(layout) and new == ()
    if first_disabled_difference is None:
        first_disabled_difference = {
            "source_period": first[1],
            "assigned_period": first[0],
            "isolated_reference": old,
            "target": new,
        }
    disabled_cases += 1

unknown_cases = []
for field, role in product(("assigned_period", "source_period"), material_roles):
    assigned = "unknown" if field == "assigned_period" else period_names[1]
    source_period = "unknown" if field == "source_period" else period_names[0]
    old_chains, plan = make_case(((assigned, source_period, role),))
    try:
        target_contribution(plan, period_names)
    except ValueError as error:
        assert field in str(error)
    else:
        raise AssertionError(("unknown period must be rejected", field))
    try:
        evaluate_isolated(old_chains, old_rules(period_names))
    except KeyError as error:
        assert error.args == ("unknown",)
    else:
        raise AssertionError("isolated reference must fail on an unknown period")
    if field == "assigned_period":
        evaluate_reference(old_chains, old_rules(period_names))
        assert old_chains[0].assigned_period == source_period
        original_behavior = "rewrites unknown assigned period to source period"
    else:
        try:
            evaluate_reference(old_chains, old_rules(period_names))
        except KeyError as error:
            assert error.args == ("unknown",)
        else:
            raise AssertionError("original reference must reject unknown source period")
        original_behavior = "KeyError on unknown source period"
    unknown_cases.append(
        {
            "field": field,
            "material_role": role.value,
            "original_reference": original_behavior,
            "isolated_reference": "KeyError",
            "target": "ValueError with field",
        }
    )

assert reference_globals["normalize_plan_periods"] is original_normalizer
print(
    json.dumps(
        {
            "status": "pass",
            "original_entry_observations": original_observations,
            "isolated_predicate_comparisons": isolated_cases,
            "single_chain_comparisons": single_chain_cases,
            "multi_chain_comparisons": multi_chain_cases,
            "variable_period_count_comparisons": variable_period_count_cases,
            "disabled_correction_cases": disabled_cases,
            "first_disabled_difference": first_disabled_difference,
            "unknown_period_cases": unknown_cases,
            "first_unexpected_difference": None,
            "normalizer_restored": True,
            "target_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "original-entry observations and explicitly isolated predicate; no solver or GQGA4 acceptance",
        },
        ensure_ascii=False,
        default=str,
    )
)
