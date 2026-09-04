"""Frozen GQGA4 configuration mapping and dispatch, without a scheduling run."""

import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import QualityCriterionSpec, RuleDefinitionSpec
from apsgo_scheduler.app.rule_set_loader import (
    RULE_REGISTRY,
    RuleSetLoadError,
    fingerprint_rule_set_spec,
    load_rule_set,
)
from apsgo_scheduler.core.chain_order import chain_order_objective_index
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    ControlledSplitMode,
    RuleScope,
    SolverPolicy,
    fingerprint,
    freeze_rule_parameters,
)
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.resource_facts import EvaluationResourceView
from apsgo_scheduler.core.rules.base import (
    ChainRuleSubject,
    ControlledSplitRuleSubject,
    EdgeRuleSubject,
    NodeRuleSubject,
    PlanRuleSubject,
    RuleDisposition,
    RuleEvaluationContext,
)
from tests.app.test_input_normalizer import gqga4_six_level_spec, read_gqga4_spec

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "tests/baselines/gqga4"
RULE_LAYOUT = (
    ("chain_weight_range", "ChainWeightRangeRule", "chain"),
    ("forbid_consecutive_reverse_width", "ConsecutiveReverseWidthRule", "chain"),
    ("max_reverse_width_count", "ReverseWidthCountRule", "chain"),
    ("max_consecutive_virtual_sphc", "ConsecutiveVirtualMaterialRule", "chain"),
    ("reverse_width_limit", "WidthTransitionRule", "edge"),
    ("thickness_jump_limit", "ThicknessTransitionRule", "edge"),
    ("temperature_overlap_min", "TemperatureOverlapRule", "edge"),
    ("gqga4_soft_hard_connection", "SoftHardConnectionRule", "edge"),
    ("chain_high_surface_run_count_lte", "HighSurfaceRunCountRule", "chain"),
    ("chain_if_narrow_real_weight_lte", "ContinuousNarrowSteelWeightRule", "chain"),
    ("chain_same_spec_real_weight_lte", "SameSpecContinuousRealWeightRule", "chain"),
    ("strategic_customer_priority_objective", "StrategicCustomerPriorityRule", "node"),
    ("forbid_late_original_due_period", "LateOriginalPeriodMoveRule", "plan"),
    ("future_fill_weight_target", "FutureFillWeightTargetRule", "plan"),
    ("virtual_output_weight_ratio_limit", "VirtualOutputRatioRule", "plan"),
    ("controlled_order_split", "ControlledOrderSplitRule", "action_eligibility"),
)


def read_json(relative_path):
    return json.loads((BASE / relative_path).read_text(encoding="utf-8"), parse_float=D)


@pytest.fixture(scope="module")
def specification():
    return gqga4_six_level_spec.__wrapped__()


@pytest.fixture(scope="module")
def gqga4_spec():
    return read_gqga4_spec("gqga4_rule_set_spec.json")


@pytest.fixture(scope="module")
def width_last_spec():
    return read_gqga4_spec("gqga4_rule_set_spec_width_last_historical.json")


@pytest.fixture(scope="module")
def loaded(specification):
    return load_rule_set(specification)


def test_historical_six_level_snapshot_preserves_the_original_file_bytes():
    payload = (BASE / "gqga4_rule_set_spec_six_level_historical.json").read_bytes()
    assert (
        sha256(payload).hexdigest()
        == "7c3f7ae0af00c0e3669657990bce60d505f8eb760820c6a9f47055f82d65bf16"
    )


def test_historical_width_last_snapshot_preserves_original_bytes_and_six_level_prefix(
    width_last_spec, specification
):
    payload = (BASE / "gqga4_rule_set_spec_width_last_historical.json").read_bytes()
    assert (
        sha256(payload).hexdigest()
        == "d0a73350aedbb5c7dff0ed427bae8f90110df092cab32089b3905f6c4c596564"
    )
    assert width_last_spec.rules[:-1] == specification.rules
    assert width_last_spec.quality_spec[:-1] == specification.quality_spec
    assert load_rule_set(width_last_spec).fingerprint == (
        "cd4e21b37e815c9100edd9f72b0dd0b76bb5d315463719c5e1fdf546ce96deef"
    )


def test_formal_seven_level_config_only_swaps_fifth_and_seventh(gqga4_spec, width_last_spec):
    expected = list(width_last_spec.quality_spec)
    expected[4], expected[6] = expected[6], expected[4]
    assert gqga4_spec == replace(
        width_last_spec, quality_spec=tuple(expected), fingerprint=gqga4_spec.fingerprint
    )
    assert tuple(item.metric_key for item in gqga4_spec.quality_spec) == (
        "prohibited_violation_count",
        "prohibited_violation_severity",
        "underweight_chain_count",
        "underweight_total_gap",
        "inter_chain_width_gap",
        "generated_virtual_weight",
        "chain_count",
    )


def test_formal_rules_and_quality_bindings_remain_explicit(gqga4_spec, specification):
    assert gqga4_spec.rules[:-1] == specification.rules
    assert (
        replace(
            gqga4_spec,
            rules=specification.rules,
            quality_spec=specification.quality_spec,
            fingerprint=specification.fingerprint,
        )
        == specification
    )
    assert gqga4_spec.rules[-1] == RuleDefinitionSpec(
        "inter_chain_width_gap_objective",
        "InterChainWidthGapRule",
        "相邻链首尾宽度差",
        RuleScope.PLAN,
        True,
        "1",
        {},
    )
    assert gqga4_spec.quality_spec[4] == QualityCriterionSpec(
        "inter_chain_width_gap", "inter_chain_width_gap", "minimize", "sum", "exact_decimal"
    )
    active = load_rule_set(gqga4_spec)
    assert len(gqga4_spec.rules) == 17
    assert sum(item.enabled for item in gqga4_spec.rules) == len(active.rules) == 16
    assert [item.rule_id for item in gqga4_spec.rules if not item.enabled] == [
        "forbid_consecutive_reverse_width"
    ]
    assert len(active.quality_spec) == 7 and chain_order_objective_index(active) == 4
    assert active.rules_for_scope(RuleScope.PLAN)[-1].rule_id == "inter_chain_width_gap_objective"


def test_formal_rule_identity_is_independent_of_the_six_level_historical_identity(gqga4_spec):
    assert (
        gqga4_spec.fingerprint
        == fingerprint_rule_set_spec(gqga4_spec)
        == load_rule_set(gqga4_spec).fingerprint
        == "d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0"
    )
    altered = replace(gqga4_spec, fingerprint=gqga4_six_level_spec.__wrapped__().fingerprint)
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(altered)
    assert any(issue.code == "rule_set_fingerprint_mismatch" for issue in caught.value.issues)


def test_reordered_quality_cannot_reuse_previous_seven_level_identity(gqga4_spec, width_last_spec):
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(replace(gqga4_spec, fingerprint=width_last_spec.fingerprint))
    assert any(issue.code == "rule_set_fingerprint_mismatch" for issue in caught.value.issues)


def test_formal_quality_cannot_keep_a_disabled_width_gap_producer(gqga4_spec):
    altered = signed(
        gqga4_spec,
        rules=(*gqga4_spec.rules[:-1], replace(gqga4_spec.rules[-1], enabled=False)),
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(altered)
    assert any(
        issue.code == "invalid_rule_set" and "inter_chain_width_gap" in issue.message
        for issue in caught.value.issues
    )


@pytest.fixture(scope="module")
def source_rules():
    return {
        item["rule_id"]: item
        for item in read_json("inputs/resolved_rules.json")["rule_snapshot"]["dsl_rules"]
    }


@pytest.fixture(scope="module")
def mapped_parameters(source_rules):
    params = {key: value["params"] for key, value in source_rules.items()}
    resolved = read_json("inputs/resolved_rules.json")
    context = read_json("inputs/rule_context.json")
    narrow = params["chain_if_narrow_real_weight_lte"]
    high = params["chain_high_surface_run_count_lte"]
    same = params["chain_same_spec_real_weight_lte"]
    customer = params["strategic_customer_priority_objective"]
    soft = params["gqga4_soft_hard_connection"]["policy"]
    returns = read_json("inputs/solver_config.json")["historical_solver_policy"]["repair_policy"][
        "rolling_final_cross_period_repair"
    ]
    assert high["max_run_count"] == int(high["max_run_count"])
    return {
        "chain_weight_range": params["chain_weight_range"],
        "forbid_consecutive_reverse_width": {},
        "max_reverse_width_count": params["max_reverse_width_count"],
        "max_consecutive_virtual_sphc": params["max_consecutive_virtual_sphc"],
        "reverse_width_limit": params["reverse_width_limit"]
        | {
            "virtual_width_tolerance": resolved["evaluation"]["virtual_sphc_width_tolerance"],
        },
        "thickness_jump_limit": params["thickness_jump_limit"]["thickness_rules"]
        | {
            "fallback_tolerance": D("0.1"),
        },
        "temperature_overlap_min": params["temperature_overlap_min"]
        | {
            "virtual_temperature_adaptive": context["virtual_sphc_temperature_adaptive"],
        },
        "gqga4_soft_hard_connection": {
            key: soft[key]
            for key in (
                "virtual_sphc_allows_bridge",
                "transition_material_breaks_soft_hard",
                "missing_grade_policy",
            )
        },
        "chain_high_surface_run_count_lte": {
            "surface_grades": high["filter"]["all"][0]["right"]["const"],
            "max_run_count": int(high["max_run_count"]),
        },
        "chain_if_narrow_real_weight_lte": {
            "grade_class": narrow["filter"]["all"][0]["right"]["const"],
            "width_upper_exclusive": D(narrow["width_upper_exclusive"]),
            "max_real_weight": narrow["max_real_weight"],
        },
        "chain_same_spec_real_weight_lte": {
            "group_by_fields": [field.removeprefix("node.") for field in same["group_by_fields"]],
            "max_real_weight": same["max_real_weight"],
        },
        "strategic_customer_priority_objective": {
            key: customer[key] for key in ("contains_any", "rank", "default_rank")
        },
        "forbid_late_original_due_period": {},
        "future_fill_weight_target": params["future_fill_weight_target"],
        "virtual_output_weight_ratio_limit": {
            "max_ratio": params["virtual_output_weight_ratio_limit"]["max_ratio"]
        },
        "controlled_order_split": {
            "grade_class": narrow["filter"]["all"][0]["right"]["const"],
            "width_upper_exclusive": D(narrow["width_upper_exclusive"]),
            "maximum_piece_weight": narrow["max_real_weight"],
            "minimum_piece_weight": returns["min_transfer_weight"],
            "maximum_accepted_source_count": returns["max_moves"],
            "maximum_separator_node_count": returns["virtual_bridge"]["max_bridge_nodes_per_move"],
            "maximum_separator_weight": returns["virtual_bridge"]["max_bridge_weight_per_move"],
            "allowed_modes": [
                ControlledSplitMode.SAME_PERIOD_SPLIT.value,
                ControlledSplitMode.FUTURE_BORROW_RETURN.value,
            ],
        },
    }


def signed(specification, **changes):
    altered = replace(specification, **changes)
    return replace(altered, fingerprint=fingerprint_rule_set_spec(altered))


def context(periods=None):
    periods = (
        tuple(read_json("inputs/solver_config.json")["period_order"])
        if periods is None
        else periods
    )
    return RuleEvaluationContext(periods, dict(zip(periods, range(len(periods)))), ("prototype",))


def node(index, *, weight="100", width="1000", virtual=False, **changes):
    return Node(
        **(
            {
                "node_id": f"node-{index}",
                "source_order_id": None if virtual else f"order-{index}",
                "source_resource_id": None if virtual else f"resource-{index}",
                "source_period": None if virtual else "BR_00000001",
                "weight": D(weight),
                "width": D(width),
                "thickness": D("1"),
                "min_temperature": D("700"),
                "max_temperature": D("800"),
                "grade": "DC01",
                "material_role": MaterialRole.GENERATED_VIRTUAL
                if virtual
                else MaterialRole.NORMAL_REAL,
                "rule_attributes": {
                    "surface_grade": "FC",
                    "grade_class": "IF钢",
                    "soft_hard_class": "soft",
                    "hot_roll_grade": "SPHC",
                },
                "virtual_lineage": VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "filename",
    (
        "input_orders.csv",
        "optimization_problem.json",
        "resolved_rules.json",
        "rule_context.json",
        "solver_config.json",
    ),
)
def test_original_input_bytes_still_match_frozen_manifest(filename):
    record = read_json("reference_manifest.json")["inputs"][filename]
    path = ROOT / record["path"]
    assert path.parent == BASE / "inputs"
    payload = path.read_bytes()
    assert len(payload) == record["bytes"]
    assert sha256(payload).hexdigest() == record["sha256"]


def test_public_identity_counts_and_order_preserve_only_retained_source_records(
    specification, loaded, source_rules
):
    assert (
        specification.product_line_code,
        specification.process_code,
        specification.scenario,
        specification.version,
    ) == ("GQGA4", "default", "month", "1")
    assert (
        read_json("inputs/optimization_problem.json")["summary"]["rule_domain"]
        == "month/GQGA4/default/rolling_strict"
    )
    assert len(source_rules) == 17
    assert set(source_rules) - {item.rule_id for item in specification.rules} == {
        "future_pool_borrow_limit_ratio",
        "grade_connection_policy",
    }
    assert (
        tuple((item.rule_id, item.rule_type, item.scope.value) for item in specification.rules)
        == RULE_LAYOUT
    )
    assert len(specification.rules) == 16
    assert sum(item.enabled for item in specification.rules) == len(loaded.rules) == 15
    assert [item.rule_id for item in specification.rules if not item.enabled] == [
        "forbid_consecutive_reverse_width"
    ]
    assert all(item.version == "1" and item.name.strip() for item in specification.rules)
    for item in specification.rules[:-1]:
        source = source_rules[item.rule_id]
        assert item.enabled is source["enabled"]
        assert item.scope.value == {"objective": "node", "result": "plan"}.get(
            source["scope"], source["scope"]
        )


@pytest.mark.parametrize("rule_id,rule_type,scope", RULE_LAYOUT)
def test_each_target_rule_has_exact_explicit_source_parameters(
    specification, mapped_parameters, rule_id, rule_type, scope
):
    definition = next(item for item in specification.rules if item.rule_id == rule_id)
    assert definition.parameters == freeze_rule_parameters(mapped_parameters[rule_id])
    assert RULE_REGISTRY[rule_type].supported_scope is RuleScope(scope)


def test_removed_dsl_wrappers_have_the_expected_fixed_meaning(source_rules):
    def condition(field, operation, value):
        return {"left": {"field": field}, "op": operation, "right": {"const": value}}

    exclusions = [
        condition("node.node_type", "ne", "virtual_sphc"),
        condition("node.actual_transition_material", "eq", False),
    ]
    high = source_rules["chain_high_surface_run_count_lte"]["params"]
    narrow = source_rules["chain_if_narrow_real_weight_lte"]["params"]
    same = source_rules["chain_same_spec_real_weight_lte"]["params"]
    assert high["filter"] == {
        "all": [condition("node.surface_grade", "in", ["FC", "FD"]), *exclusions]
    }
    assert narrow["filter"] == {
        "all": [
            condition("node.grade_class", "eq", "IF钢"),
            condition("node.width", "lt", 1400),
            *exclusions,
        ]
    }
    assert narrow["width_upper_exclusive"] == 1400
    assert same["filter"] == {"all": exclusions}
    assert same["group_by_fields"] == ["node.thickness", "node.width", "node.grade"]
    assert high["aggregate"] == "count"
    for params in (narrow, same):
        assert (params["aggregate"], params["field"]) == ("sum", "node.weight")
    assert all(params["metric_type"] == "continuous_segment" for params in (high, narrow, same))
    assert (
        source_rules["strategic_customer_priority_objective"]["params"]["field"]
        == "node.customer_name"
    )
    assert (
        source_rules["virtual_output_weight_ratio_limit"]["params"]["denominator"]
        == "final_output_total_weight"
    )
    assert (
        source_rules["gqga4_soft_hard_connection"]["params"]["policy"]["product_line_code"]
        == "GQGA4"
    )
    assert (
        source_rules["forbid_late_original_due_period"]["params"]["forbid_late_original_due_period"]
        is True
    )
    assert (
        source_rules["forbid_consecutive_reverse_width"]["params"][
            "allow_consecutive_reverse_width"
        ]
        is True
    )
    returns = read_json("inputs/solver_config.json")["historical_solver_policy"]["repair_policy"][
        "rolling_final_cross_period_repair"
    ]
    assert returns["enabled"] and returns["allow_partial_split"]
    assert (
        returns["only_return_future_borrowed_orders"] is True
    )  # The approved target also permits same-period splitting.


@pytest.mark.parametrize(
    "scope,expected",
    [
        (RuleScope.NODE, ("strategic_customer_priority_objective",)),
        (
            RuleScope.EDGE,
            (
                "reverse_width_limit",
                "thickness_jump_limit",
                "temperature_overlap_min",
                "gqga4_soft_hard_connection",
            ),
        ),
        (
            RuleScope.CHAIN,
            (
                "chain_weight_range",
                "max_reverse_width_count",
                "max_consecutive_virtual_sphc",
                "reverse_width_limit",
                "chain_high_surface_run_count_lte",
                "chain_if_narrow_real_weight_lte",
                "chain_same_spec_real_weight_lte",
            ),
        ),
        (
            RuleScope.PLAN,
            (
                "forbid_late_original_due_period",
                "future_fill_weight_target",
                "virtual_output_weight_ratio_limit",
            ),
        ),
        (RuleScope.ACTION_ELIGIBILITY, ("controlled_order_split",)),
    ],
)
def test_scope_indexes_preserve_reference_local_order_including_private_width_check(
    loaded, scope, expected
):
    assert tuple(item.rule_id for item in loaded.rules_for_scope(scope)) == expected
    if scope is RuleScope.CHAIN:
        companion = loaded.rules_for_scope(scope)[3]
        parent = loaded.rules_for_scope(RuleScope.EDGE)[0]
        assert companion not in loaded.rules
        assert companion.rule_id == parent.rule_id
        assert (companion.parameters, companion.version, companion.name) == (
            parent.parameters,
            parent.version,
            parent.name,
        )


def test_six_quality_declarations_and_only_chain_lower_bound_deviation(specification, loaded):
    expected = (
        ("prohibited_violation_count", "named_value", "exact_decimal"),
        ("prohibited_violation_severity", "sum", "reference_float_round_6"),
        ("underweight_chain_count", "sum", "exact_decimal"),
        ("underweight_total_gap", "sum", "underweight_gap_round_2_then_sum"),
        ("chain_count", "named_value", "exact_decimal"),
        ("generated_virtual_weight", "named_value", "reference_float_round_6"),
    )
    assert (
        tuple(
            (item.metric_key, item.aggregation, item.numeric_projection)
            for item in specification.quality_spec
        )
        == expected
    )
    assert all(
        item.criterion_id == item.metric_key and item.direction == "minimize"
        for item in specification.quality_spec
    )
    assert len(loaded.quality_spec) == 6
    assert loaded.allowed_final_deviation_codes == frozenset({"chain_weight_below_minimum"})
    assert "future_pool_borrowed_ratio" not in {
        key for item in loaded.rules for key in item.metric_keys()
    }
    short = ChainRuleSubject("short", Chain("short-chain", (node(0),), "BR_00000001"))
    (violation,) = loaded.evaluate_chain(short, context()).violations
    assert violation.reason_code == "chain_weight_below_minimum"
    assert violation.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION


def test_policy_keeps_reference_seed_and_explicit_formal_budgets():
    policy = SolverPolicy(**read_json("gqga4_solver_policy.json"))
    reference = read_json("reference_manifest.json")["parameters"]
    assert policy.seed == reference["seed"] == 590531
    assert policy.candidate_check_limit == 200000
    assert reference["candidate_check_budget"] == 100000
    assert reference["time_budget_seconds"] == 30
    assert policy.total_time_limit_seconds == D("180")
    assert policy.finalization_reserve_seconds == D(
        "10"
    )  # An initial engineering choice, not a measured result.
    assert policy.construction_order_key == CONSTRUCTION_ORDER_KEY
    assert policy.numeric_semantics_key == NUMERIC_SEMANTICS_KEY
    assert policy.whole_chain_pair_scan_slack_weight == D("40")
    assert policy.maximum_virtual_bridge_nodes == 2
    gate = read_json("performance_gate.json")
    assert (
        gate["new_solver_maximum_median_seconds"] == gate["new_solver_maximum_p95_seconds"] == 180
    )
    assert fingerprint(policy) == "b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba"
    assert (
        fingerprint(replace(policy, candidate_check_limit=reference["candidate_check_budget"]))
        == "6ddfb11e815f79273b0e510e7a882e21a799a540af603fb5d098c7e289e7e14e"
    )


@pytest.mark.parametrize("rule_id,rule_type,scope", RULE_LAYOUT)
def test_each_configured_rule_can_toggle_with_dependency_neutral_quality(
    specification, rule_id, rule_type, scope
):
    definitions = tuple(
        replace(item, enabled=not item.enabled) if item.rule_id == rule_id else item
        for item in specification.rules
    )
    # Chain underweight metrics require their producer; keep this test about the enable switch.
    changed = signed(specification, rules=definitions, quality_spec=specification.quality_spec[:2])
    loaded = load_rule_set(changed)
    expected = next(item for item in definitions if item.rule_id == rule_id)
    assert (rule_id in {item.rule_id for item in loaded.rules}) is expected.enabled
    assert changed.fingerprint != specification.fingerprint
    if rule_type == "WidthTransitionRule":
        assert rule_id not in {item.rule_id for item in loaded.rules_for_scope(RuleScope.CHAIN)}


def test_full_six_quality_rejects_disabling_its_underweight_producer(specification):
    changed = signed(
        specification,
        rules=(replace(specification.rules[0], enabled=False),) + specification.rules[1:],
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(changed)
    assert any(
        issue.code == "invalid_rule_set" and "underweight_chain_count" in issue.message
        for issue in caught.value.issues
    )


@pytest.mark.parametrize("rule_type", ("FutureBorrowRatioRule", "GradeConnectionRule"))
def test_removed_rule_types_are_not_registered_or_silently_enabled(specification, rule_type):
    assert rule_type not in RULE_REGISTRY
    extra = replace(
        specification.rules[-1], rule_id="removed-rule", rule_type=rule_type, parameters={}
    )
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(signed(specification, rules=specification.rules + (extra,)))
    assert any(issue.code == "unknown_enabled_rule" for issue in caught.value.issues)
    disabled = load_rule_set(
        signed(specification, rules=specification.rules + (replace(extra, enabled=False),))
    )
    assert all(item.rule_id != "removed-rule" for item in disabled.rules)


@pytest.mark.parametrize(
    "change",
    ("parameter", "rule_order", "quality_projection", "allowed_deviation", "disabled_entry"),
)
def test_frozen_configuration_fingerprint_and_tamper_detection(specification, change):
    assert (
        specification.fingerprint
        == fingerprint_rule_set_spec(specification)
        == "420cd13d59763c140b23664f0cb0aca0437e0680b51899d2e0fb39563b7f5365"
    )
    if change == "parameter":
        altered = replace(
            specification,
            rules=(
                replace(
                    specification.rules[0],
                    parameters=dict(specification.rules[0].parameters)
                    | {"target_weight": D("1800")},
                ),
            )
            + specification.rules[1:],
        )
    elif change == "rule_order":
        altered = replace(specification, rules=tuple(reversed(specification.rules)))
    elif change == "quality_projection":
        altered = replace(
            specification,
            quality_spec=specification.quality_spec[:3]
            + (replace(specification.quality_spec[3], numeric_projection="exact_decimal"),)
            + specification.quality_spec[4:],
        )
    elif change == "allowed_deviation":
        altered = replace(specification, allowed_final_deviation_codes=frozenset())
    else:
        altered = replace(
            specification,
            rules=(
                specification.rules[0],
                replace(specification.rules[1], name="changed-disabled-rule"),
            )
            + specification.rules[2:],
        )
    assert fingerprint_rule_set_spec(altered) != specification.fingerprint
    with pytest.raises(RuleSetLoadError) as caught:
        load_rule_set(altered)
    assert any(issue.code == "rule_set_fingerprint_mismatch" for issue in caught.value.issues)


def test_loaded_edges_emit_width_thickness_temperature_soft_hard_in_order(loaded):
    left = node(0, thickness=D("0.5"))
    right = node(
        1,
        width="1100",
        min_temperature=D("900"),
        max_temperature=D("950"),
        rule_attributes={"soft_hard_class": "hard"},
    )
    result = loaded.evaluate_edge(EdgeRuleSubject("edge", left, right), context())
    assert [item.rule_id for item in result.violations] == [
        "reverse_width_limit",
        "thickness_jump_limit",
        "temperature_overlap_min",
        "gqga4_soft_hard_connection",
    ]
    assert all(
        item.scope is RuleScope.EDGE and item.subject_id == "edge" for item in result.violations
    )
    assert all(item.disposition is RuleDisposition.PROHIBITED for item in result.violations)
    assert not loaded.evaluate_edge(
        EdgeRuleSubject("valid", node(2), node(3, width="990")), context()
    ).violations


def test_loaded_chain_dispatch_includes_internal_anchor_and_all_continuous_rules(loaded):
    nodes = (
        (node(0, weight="1000"),)
        + tuple(
            node(i, width=width, weight="20", virtual=True)
            for i, width in ((1, "1100"), (2, "1050"), (3, "1040"))
        )
        + tuple(node(i, width="1030", weight="200") for i in range(4, 10))
    )
    subject = ChainRuleSubject("chain-subject", Chain("chain", nodes, "BR_00000001"))
    before = fingerprint(subject)
    result = loaded.evaluate_chain(subject, context())
    assert [item.rule_id for item in result.violations] == [
        "chain_weight_range",
        "max_reverse_width_count",
        "max_consecutive_virtual_sphc",
        "reverse_width_limit",
        "chain_high_surface_run_count_lte",
        "chain_if_narrow_real_weight_lte",
        "chain_if_narrow_real_weight_lte",
        "chain_same_spec_real_weight_lte",
    ]
    assert all(item.disposition is RuleDisposition.PROHIBITED for item in result.violations)
    assert result.violations[3].reason_code == "virtual_bridge_reverse_width_exceeded"
    assert result.violations[3].subject_id.endswith("virtual_anchor:0-4")
    assert fingerprint(subject) == before


def test_loaded_node_and_plan_dispatch_use_configured_metrics_without_a_full_evaluator(loaded):
    customer = node(0, weight="900", rule_attributes={"customer_name": "宝马汽车"})
    assert loaded.construction_priority(customer) == (0,)
    node_result = loaded.evaluate_node(NodeRuleSubject("customer", customer), context())
    assert [(item.metric_key, item.value) for item in node_result.metrics] == [
        ("strategic_customer_rank", 0)
    ]
    virtual = node(1, weight="100", virtual=True)
    plan = SchedulePlan((Chain("chain", (customer, virtual), "BR_00000002"),))
    view = EvaluationResourceView((), (virtual.node_id,), (), D("900"), D("100"), D("0"), D("0"))
    subject = PlanRuleSubject("plan", plan, view)
    before = fingerprint(subject)
    result = loaded.evaluate_plan(subject, context())
    assert [item.rule_id for item in result.violations] == [
        "forbid_late_original_due_period",
        "virtual_output_weight_ratio_limit",
    ]
    assert [(item.metric_key, item.value) for item in result.metrics] == [
        ("late_original_due_period_move_count", 1),
        ("future_fill_total_gap", D("200")),
        ("virtual_output_weight_ratio", D("0.1")),
    ]
    assert fingerprint(subject) == before


@pytest.mark.parametrize("periods", (None, ("Z", "A", "M", "B", "X")))
@pytest.mark.parametrize(
    "mode", (ControlledSplitMode.SAME_PERIOD_SPLIT, ControlledSplitMode.FUTURE_BORROW_RETURN)
)
def test_loaded_split_supports_both_modes_and_explicit_task_period_order(loaded, periods, mode):
    current_context = context(periods)
    origin, source = current_context.period_order[:2]
    if mode is ControlledSplitMode.SAME_PERIOD_SPLIT:
        origin = source
    parent = node(0, weight="570.30", width="1300", source_period=source)
    subject = ControlledSplitRuleSubject("split", parent, origin, source, 0)
    before = fingerprint((subject, current_context))
    decision = loaded.evaluate_controlled_split(subject, current_context)
    assert decision.eligible and decision.mode is mode
    assert decision.target_assigned_period == source
    assert decision.reason_code == mode.value
    assert decision.maximum_piece_weight == D("500")
    assert decision.minimum_piece_weight == D("1")
    assert decision.maximum_accepted_source_count == 10000
    assert decision.maximum_separator_node_count == 2
    assert decision.maximum_separator_weight == D("40")
    assert fingerprint((subject, current_context)) == before
    assert decision == loaded.evaluate_controlled_split(subject, current_context)
