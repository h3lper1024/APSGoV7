"""Optional width-only reference check, including the existing real-anchor check."""

import json
import math
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec, RuleSetSpec
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    EdgeRuleSubject,
    NumericProjection,
    QualityAggregation,
    QualityCriterion,
    QualityDirection,
    RuleDisposition,
    RuleEvaluationContext,
)
from apsgo_scheduler.core.rules.concrete import WidthTransitionRule
from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_width_differential")
D = Decimal
rule_id = "reverse_width_limit"
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
metric_keys = ("prohibited_violation_count", "prohibited_violation_severity")
real_roles = (MaterialRole.NORMAL_REAL, MaterialRole.ACTUAL_TRANSITION)
virtual_role = MaterialRole.GENERATED_VIRTUAL
configurations = ((D(0), D(0)), (D("0.5"), D(2)), (D(20), D(200)), (D(23), D(350)))


def make_bundle(configuration, enabled=True):
    maximum, tolerance = configuration
    parameters = {"max_reverse_width": maximum, "virtual_width_tolerance": tolerance}
    public_rule = WidthTransitionRule(rule_id, "逆宽幅度", RuleScope.EDGE, enabled, "1", parameters)
    direct = ProcessRuleSet(
        "probe",
        "coating",
        "month",
        "1",
        (public_rule,),
        tuple(
            QualityCriterion(
                key,
                key,
                QualityDirection.MINIMIZE,
                QualityAggregation.NAMED_VALUE,
                NumericProjection.EXACT_DECIMAL,
            )
            for key in metric_keys
        ),
        frozenset(),
        "direct-probe-identity",
    )
    spec = RuleSetSpec(
        "probe",
        "coating",
        "month",
        "1",
        (
            RuleDefinitionSpec(
                rule_id, "WidthTransitionRule", "逆宽幅度", RuleScope.EDGE, enabled, "1", parameters
            ),
        ),
        tuple(
            QualityCriterionSpec(key, key, "minimize", "named_value", "exact_decimal")
            for key in metric_keys
        ),
        frozenset(),
        "pending",
    )
    loaded = load_rule_set(replace(spec, fingerprint=fingerprint_rule_set_spec(spec)))
    old_rules = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    {
                        "rule_id": rule_id,
                        "enabled": enabled,
                        "params": {"max_reverse_width": maximum},
                    }
                ]
            },
            "evaluation": {"virtual_sphc_width_tolerance": tolerance},
        },
        {"allow_consecutive_reverse_width": True, "reverse_width_carrier_grades": ["*"]},
        {"period_order": ["period"]},
    )
    return public_rule, direct, loaded, old_rules


bundles = {configuration: make_bundle(configuration) for configuration in configurations}
default_configuration = (D(20), D(200))
disabled_bundle = make_bundle(default_configuration, enabled=False)


def nodes_for(widths, roles):
    assert len(widths) == len(roles)
    old_nodes, new_nodes = [], []
    for index, (width, role) in enumerate(zip(widths, roles)):
        virtual = role is virtual_role
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=f"n-{index}",
                source_order_id=f"o-{index}",
                source_period="period",
                weight=D(1),
                width=reference["optional_float"](width),
                hot_roll_grade="SPHC",
                actual_transition_material=role is MaterialRole.ACTUAL_TRANSITION,
                node_type="virtual_sphc" if virtual else "real",
            )
        )
        new_nodes.append(
            Node(
                node_id=f"n-{index}",
                source_order_id=None if virtual else f"o-{index}",
                source_resource_id=None if virtual else f"r-{index}",
                source_period=None if virtual else "period",
                weight=D(1),
                width=None if width is None else D(str(width)),
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=role,
                rule_attributes={"hot_roll_grade": "SPHC"},
                virtual_lineage=VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
            )
        )
    return old_nodes, tuple(new_nodes)


def old_projection(failures):
    result = []
    for item in failures:
        assert item["rule_id"] == rule_id and item["prohibited"] is True
        assert item["scope"] in ("edge", "virtual_bridge_anchor"), item
        left, right = item["positions"]
        if item["scope"] == "edge":
            assert item["subject_id"] == f"C:{rule_id}:{left}-{right}"
            assert item["reason"].startswith("width transition ")
            kind, reason = "edge", "width_transition_exceeded"
        else:
            assert item["subject_id"] == f"C:virtual_anchor:{left}-{right}"
            assert item["reason"].startswith(
                "virtual bridge cannot hide real-anchor reverse width "
            )
            kind, reason = "anchor", "virtual_bridge_reverse_width_exceeded"
        result.append((kind, left, right, reason, D(str(item["severity_value"]))))
    return tuple(result)


def new_projection(contribution, scope):
    assert contribution.metrics == ()
    result = []
    for item in contribution.violations:
        assert item.rule_id == rule_id and item.scope is scope
        assert item.disposition is RuleDisposition.PROHIBITED
        kind = "edge" if scope is RuleScope.EDGE else "anchor"
        prefix = f"C:{rule_id}:" + ("virtual_anchor:" if kind == "anchor" else "")
        assert item.subject_id.startswith(prefix)
        left, right = (int(value) for value in item.subject_id[len(prefix) :].split("-"))
        assert left < right and (kind != "edge" or right == left + 1)
        result.append((kind, left, right, item.reason_code, item.severity))
    return tuple(result)


counters = {
    "edge_reference_matches": 0,
    "chain_reference_matches": 0,
    "edge_missing_width_differences": 0,
    "edge_invalid_width_differences": 0,
    "chain_missing_width_differences": 0,
    "chain_invalid_width_differences": 0,
    "disabled_reference_matches": 0,
}
first_safety_differences = {}


def compare_result(old, actual, widths, locations, safety_reason, counter):
    expected = (
        tuple((kind, left, right, safety_reason, D(1)) for kind, left, right in locations)
        if safety_reason
        else old
    )
    assert actual == expected, (widths, old, expected, actual)
    if safety_reason:
        # These isolated safety cases have no separate, ordinary width violations.
        assert old == () and actual
        counters[f"{counter}_{safety_reason}_differences"] += 1
        first_safety_differences.setdefault(
            safety_reason,
            {
                "widths": [None if width is None else str(width) for width in widths],
                "reference_failures": old,
                "target_failures": actual,
            },
        )
    else:
        counters[f"{counter}_reference_matches"] += 1


def compare_edge(
    widths, roles, configuration=default_configuration, safety_reason=None, disabled=False
):
    public_rule, direct, loaded, old_rules = disabled_bundle if disabled else bundles[configuration]
    old_nodes, new_nodes = nodes_for(widths, roles)
    old = old_projection(
        tuple(
            dict(
                item, scope="edge", positions=[0, 1], subject_id=f"C:{rule_id}:0-1", prohibited=True
            )
            for item in reference["edge_rule_failures"](*old_nodes, old_rules)
        )
    )
    subject = EdgeRuleSubject(f"C:{rule_id}:0-1", *new_nodes)
    projections = (
        new_projection(public_rule.evaluate(subject, context), RuleScope.EDGE),
        new_projection(direct.evaluate_edge(subject, context), RuleScope.EDGE),
        new_projection(loaded.evaluate_edge(subject, context), RuleScope.EDGE),
    )
    assert projections[0] == projections[1] == projections[2]
    if disabled:
        assert old == projections[0] == ()
        counters["disabled_reference_matches"] += 1
    else:
        compare_result(old, projections[0], widths, (("edge", 0, 1),), safety_reason, "edge")
    return old


def compare_chain(
    widths,
    roles,
    configuration=default_configuration,
    safety_reason=None,
    locations=(),
    disabled=False,
):
    _, direct, loaded, old_rules = disabled_bundle if disabled else bundles[configuration]
    old_nodes, new_nodes = nodes_for(widths, roles)
    old_evaluation = reference["evaluate_chain"](
        reference["Chain"](old_nodes, "period"), 0, old_rules, "C"
    )
    # Inspect every reference failure. No carrier or unrelated-rule filter may hide a mismatch.
    old = old_projection(old_evaluation.violations)
    assert old_evaluation.summary["reverse_width_limit_ok"] is (not old)
    projections = []
    for rule_set in (direct, loaded):
        actual = []
        for position, (left, right) in enumerate(zip(new_nodes, new_nodes[1:])):
            actual.extend(
                new_projection(
                    rule_set.evaluate_edge(
                        EdgeRuleSubject(f"C:{rule_id}:{position}-{position + 1}", left, right),
                        context,
                    ),
                    RuleScope.EDGE,
                )
            )
        actual.extend(
            new_projection(
                rule_set.evaluate_chain(
                    ChainRuleSubject("C", Chain("C", new_nodes, "period")),
                    context,
                ),
                RuleScope.CHAIN,
            )
        )
        projections.append(tuple(actual))
    assert projections[0] == projections[1]
    if disabled:
        assert old == projections[0] == ()
        counters["disabled_reference_matches"] += 1
    else:
        compare_result(old, projections[0], widths, locations, safety_reason, "chain")
    return old


width_values = tuple(D(value) for value in ("1", "1000", "1020", "1030", "1200", "1201"))
for configuration, roles, widths in product(
    configurations,
    product(tuple(MaterialRole), repeat=2),
    product(width_values, repeat=2),
):
    compare_edge(widths, roles, configuration)

# Probe the actual binary-float boundary at several magnitudes, in both directions.
for configuration, roles, start in product(
    configurations,
    product(tuple(MaterialRole), repeat=2),
    (1.0, 1000.0, 1e16),
):
    limit = configuration[1 if virtual_role in roles else 0]
    boundary = start + float(limit) + 1e-9
    for end in (math.nextafter(boundary, -math.inf), boundary, math.nextafter(boundary, math.inf)):
        compare_edge((start, end), roles, configuration)
        compare_edge((end, start), roles, configuration)

trap = compare_edge((1e16, 1e16 + 24), (real_roles[0],) * 2, (D(23), D(350)))
assert len(trap) == 1 and (1e16 + 24 <= 1e16 + 23 + 1e-9)

# All nine accepted design cases already exist in the frozen reference behavior.
golden_cases = (
    ((1000, 1010, 1020), (1,), 0, 0),
    ((1000, 1010, 1030), (1,), 0, 1),
    ((1000, 1100, 1050), (1,), 0, 1),
    ((1000, 1200, 1020), (1,), 0, 0),
    ((1000, 1201, 1020), (1,), 1, 0),
    ((1000, 1100, 950), (1,), 0, 0),
    ((1000, 1100, 1120, 1050), (1, 2), 0, 1),
    ((1000, 1100, 1120, 1020), (1, 2), 0, 0),
    ((1000, 1010, 1020, 1030, 1040), (1, 3), 0, 0),
)
golden_results = []
for widths, virtuals, expected_edges, expected_anchors in golden_cases:
    real_count = len(widths) - len(virtuals)
    for choices in product(real_roles, repeat=real_count):
        remaining = iter(choices)
        roles = tuple(
            virtual_role if index in virtuals else next(remaining) for index in range(len(widths))
        )
        failures = compare_chain(widths, roles)
        assert sum(item[0] == "edge" for item in failures) == expected_edges
        assert sum(item[0] == "anchor" for item in failures) == expected_anchors
    golden_results.append(
        {
            "widths": widths,
            "virtual_positions": virtuals,
            "edge_count": expected_edges,
            "anchor_count": expected_anchors,
        }
    )

for configuration in configurations:
    for widths, virtuals in (
        ((1000, 1000, 1000), (1,)),
        ((1000, 1000, 1001), (1,)),
        ((1100, 1000, 1030), (0,)),
        ((1000, 1100, 1120), (1, 2)),
        ((1000, 1030), ()),
        ((1300, 1150, 1000), (1,)),
        ((1000, 1100, 1030, 1120, 1090), (1, 3)),
        ((1e16, 1e16 + 100, 1e16 + 24), (1,)),
    ):
        roles = tuple(
            virtual_role if index in virtuals else real_roles[index % len(real_roles)]
            for index in range(len(widths))
        )
        compare_chain(widths, roles, configuration)

# Reference normalization turns an unrepresentable physical float into None. The
# target retains the finite Decimal input but deliberately reports invalid_width.
for safety_reason, bad_width in (("missing_width", None), ("invalid_width", D("1e1000"))):
    for roles, widths in product(
        product(tuple(MaterialRole), repeat=2),
        (
            (bad_width, 1000),
            (1000, bad_width),
            (bad_width, bad_width),
        ),
    ):
        compare_edge(widths, roles, safety_reason=safety_reason)
    for widths, locations in (
        ((bad_width, 1010, 1020), (("edge", 0, 1), ("anchor", 0, 2))),
        ((1000, 1010, bad_width), (("edge", 1, 2), ("anchor", 0, 2))),
        ((1000, bad_width, 1020), (("edge", 0, 1), ("edge", 1, 2))),
        ((bad_width, 1010, bad_width), (("edge", 0, 1), ("edge", 1, 2), ("anchor", 0, 2))),
    ):
        compare_chain(
            widths,
            (real_roles[0], virtual_role, real_roles[1]),
            safety_reason=safety_reason,
            locations=locations,
        )

for widths in ((1000, 1300), (None, 1000), (D("1e1000"), 1000)):
    compare_edge(widths, (real_roles[0], virtual_role), disabled=True)
for widths in ((1000, 1100, 1050), (None, 1100, 1050), (D("1e1000"), 1100, 1050)):
    compare_chain(widths, (real_roles[0], virtual_role, real_roles[1]), disabled=True)

print(
    json.dumps(
        {
            "status": "pass",
            "counts": counters,
            "golden_cases": golden_results,
            "first_unexpected_difference": None,
            "first_expected_safety_differences": first_safety_differences,
            "reference_already_checks_virtual_real_anchors": True,
            "virtual_anchor_limit_is_not_a_reference_difference": True,
            "direct_and_loaded_rule_sets_checked": True,
            "float_subtraction_order_trap_checked": True,
            "reference_sha256": source_hash,
            "scope": "width-only edge/chain dispatch; no full evaluator, search or GQGA4 quality acceptance",
        },
        ensure_ascii=False,
        default=str,
    )
)
