"""Production rule template compilation, exact persistence and editor diagnostics."""

import json
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import (
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    VirtualPrototypeInput,
)
from apsgo_scheduler.api.rule_management import EditableRuleInput
from apsgo_scheduler.app.rule_set_compiler import (
    RuleSetCompilationError,
    compile_rule_set,
    dumps_rule_set_spec,
    load_compiled_rule_set_json,
    normalize_rule_set_snapshot,
)
from apsgo_scheduler.app.rule_set_loader import RuleSetLoadError, load_rule_set
from apsgo_scheduler.core.contracts import RuleScope, fingerprint
from apsgo_v7_service.gqga4 import (
    GQGA4_ALLOWED_FINAL_DEVIATIONS,
    GQGA4_INITIAL_RULES,
    GQGA4_INITIAL_VIRTUAL_PROTOTYPES,
    GQGA4_QUALITY_SPEC,
    GQGA4_RULE_SET_TEMPLATE,
    compile_gqga4_rule_set,
    normalize_gqga4_rule_snapshot,
)

BASE = Path(__file__).resolve().parents[1] / "baselines/gqga4"


def _read_baseline_spec() -> RuleSetSpec:
    raw = json.loads((BASE / "gqga4_rule_set_spec.json").read_text(), parse_float=Decimal)
    return RuleSetSpec(
        **(
            raw
            | {
                "rules": tuple(
                    RuleDefinitionSpec(**(item | {"scope": RuleScope(item["scope"])}))
                    for item in raw["rules"]
                ),
                "quality_spec": tuple(QualityCriterionSpec(**item) for item in raw["quality_spec"]),
            }
        )
    )


def _decode_fixture(value):
    if isinstance(value, list):
        return tuple(_decode_fixture(item) for item in value)
    if not isinstance(value, dict):
        return value
    kind = value["$type"]
    if kind == "dataclass":
        return {key: _decode_fixture(item) for key, item in value["fields"]}
    if kind == "tuple":
        return tuple(_decode_fixture(item) for item in value["items"])
    if kind == "decimal":
        return Decimal(value["value"])
    if kind in ("enum", "datetime"):
        return value["value"]
    raise AssertionError(kind)


def _baseline_prototypes() -> tuple[VirtualPrototypeInput, ...]:
    raw = json.loads((BASE / "inputs/optimization_problem.json").read_text())
    source = _decode_fixture(raw["optimization_problem"])
    return tuple(
        VirtualPrototypeInput(
            prototype_id=item["prototype_id"]["value"],
            unit_weight=item["unit_weight"]["tons"],
            width=item["rule_spec_key"]["width"]["millimeters"],
            thickness=item["rule_spec_key"]["thickness"]["millimeters"],
            min_temperature=None,
            max_temperature=None,
            grade=item["display_hot_rolled_grade"],
            rule_attributes={},
        )
        for item in source["virtual_catalog"]["prototypes"]
    )


def _replace_parameters(rule_id: str, **changes) -> tuple[EditableRuleInput, ...]:
    return tuple(
        replace(item, parameters=dict(item.parameters) | changes)
        if item.rule_id == rule_id
        else item
        for item in GQGA4_INITIAL_RULES
    )


LEGAL_RULE_EDITS = (
    ("chain_weight_range", {"min_weight": Decimal("701")}, None),
    ("forbid_consecutive_reverse_width", {}, True),
    ("max_reverse_width_count", {"max_count": 1}, None),
    ("max_consecutive_virtual_sphc", {"max_count": 1}, None),
    ("reverse_width_limit", {"max_reverse_width": Decimal("21")}, None),
    ("thickness_jump_limit", {"fallback_tolerance": Decimal("0.11")}, None),
    ("temperature_overlap_min", {"min_overlap": Decimal("11")}, None),
    ("gqga4_soft_hard_connection", {"missing_grade_policy": "allow"}, None),
    ("chain_high_surface_run_count_lte", {"max_run_count": 6}, None),
    ("chain_if_narrow_real_weight_lte", {"max_real_weight": Decimal("501")}, None),
    ("chain_same_spec_real_weight_lte", {"max_real_weight": Decimal("1001")}, None),
    ("strategic_customer_priority_objective", {"contains_any": ("上汽",)}, None),
    ("forbid_late_original_due_period", {}, False),
    ("future_fill_weight_target", {"future_fill_weight_target": Decimal("1201")}, None),
    ("virtual_output_weight_ratio_limit", {"max_ratio": Decimal("0.04")}, None),
    ("controlled_order_split", {"maximum_piece_weight": Decimal("499")}, None),
)


def test_production_seed_matches_the_frozen_formal_rule_and_prototype_baselines():
    assert GQGA4_RULE_SET_TEMPLATE == _read_baseline_spec()
    assert GQGA4_INITIAL_VIRTUAL_PROTOTYPES == _baseline_prototypes()
    assert fingerprint(GQGA4_INITIAL_VIRTUAL_PROTOTYPES) == (
        "33cea496fa6909ec53e6048f142776eba25d2bdca19fee9b565f775b64df965c"
    )
    source = Path(__file__).resolve().parents[2] / "src/apsgo_v7_service/gqga4.py"
    assert "tests/" not in source.read_text(encoding="utf-8")


def test_initial_compile_injects_fixed_metadata_quality_and_allowed_deviation():
    compiled = compile_gqga4_rule_set(GQGA4_INITIAL_RULES, 1)
    spec = compiled.rule_set_spec
    assert spec == GQGA4_RULE_SET_TEMPLATE
    assert len(spec.rules) == 17
    assert sum(item.enabled for item in spec.rules) == 16
    assert [item.rule_id for item in spec.rules if not item.enabled] == [
        "forbid_consecutive_reverse_width"
    ]
    assert spec.quality_spec == GQGA4_QUALITY_SPEC
    assert spec.allowed_final_deviation_codes == GQGA4_ALLOWED_FINAL_DEVIATIONS
    assert spec.fingerprint == "d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0"
    assert load_compiled_rule_set_json(compiled.compiled_rule_set_json) == spec
    assert load_rule_set(spec).fingerprint == spec.fingerprint


def test_client_rule_order_is_ignored_and_template_order_is_authoritative():
    rules, prototypes = normalize_gqga4_rule_snapshot(
        tuple(reversed(GQGA4_INITIAL_RULES)), GQGA4_INITIAL_VIRTUAL_PROTOTYPES
    )
    assert rules == GQGA4_INITIAL_RULES
    assert prototypes == GQGA4_INITIAL_VIRTUAL_PROTOTYPES


@pytest.mark.parametrize("rule_id,parameter_changes,enabled", LEGAL_RULE_EDITS)
def test_each_rule_with_an_independent_legal_edit_compiles(rule_id, parameter_changes, enabled):
    rules = tuple(
        replace(
            item,
            enabled=item.enabled if enabled is None else enabled,
            parameters=dict(item.parameters) | parameter_changes,
        )
        if item.rule_id == rule_id
        else item
        for item in GQGA4_INITIAL_RULES
    )
    compiled = compile_gqga4_rule_set(rules, 2)
    changed = next(item for item in compiled.rule_set_spec.rules if item.rule_id == rule_id)
    assert changed.parameters == (
        dict(
            GQGA4_RULE_SET_TEMPLATE.rules[
                [item.rule_id for item in GQGA4_RULE_SET_TEMPLATE.rules].index(rule_id)
            ].parameters
        )
        | parameter_changes
    )
    assert changed.enabled is (next(item for item in rules if item.rule_id == rule_id).enabled)


@pytest.mark.parametrize("rule_id", ("chain_weight_range", "inter_chain_width_gap_objective"))
def test_disabling_a_quality_metric_producer_returns_existing_rule_set_diagnostics(rule_id):
    rules = tuple(
        replace(item, enabled=False) if item.rule_id == rule_id else item
        for item in GQGA4_INITIAL_RULES
    )
    with pytest.raises(RuleSetLoadError) as caught:
        compile_gqga4_rule_set(rules, 2)
    assert any(issue.code == "invalid_rule_set" for issue in caught.value.issues)


@pytest.mark.parametrize("case", ("missing", "duplicate", "unknown"))
def test_rule_identity_set_must_match_the_server_template(case):
    values = GQGA4_INITIAL_RULES
    if case == "missing":
        values = values[:-1]
    elif case == "duplicate":
        values = (*values, values[0])
    else:
        values = (*values[:-1], replace(values[-1], rule_id="unknown"))
    with pytest.raises(RuleSetCompilationError) as caught:
        compile_gqga4_rule_set(values, 1)
    assert any(case in issue.code for issue in caught.value.issues)
    assert all(issue.field_path and issue.message for issue in caught.value.issues)


def test_decimal_and_integral_decimal_inputs_normalize_to_declared_template_types():
    rules = _replace_parameters(
        "reverse_width_limit",
        max_reverse_width=20,
        virtual_width_tolerance=Decimal("200.00"),
    )
    rules = tuple(
        replace(item, parameters={"max_count": Decimal("2.0")})
        if item.rule_id == "max_reverse_width_count"
        else item
        for item in rules
    )
    normalized, _ = normalize_gqga4_rule_snapshot(rules, GQGA4_INITIAL_VIRTUAL_PROTOTYPES)
    by_id = {item.rule_id: item for item in normalized}
    assert by_id["reverse_width_limit"].parameters["max_reverse_width"] == Decimal("20")
    assert type(by_id["max_reverse_width_count"].parameters["max_count"]) is int
    assert compile_gqga4_rule_set(normalized, 1).rule_set_spec.fingerprint == (
        GQGA4_RULE_SET_TEMPLATE.fingerprint
    )


def test_fractional_decimal_is_not_accepted_for_an_integer_parameter():
    rules = _replace_parameters("max_reverse_width_count", max_count=Decimal("2.5"))
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(rules, GQGA4_INITIAL_VIRTUAL_PROTOTYPES)
    assert caught.value.issues[0].subject_id == "max_reverse_width_count"
    assert caught.value.issues[0].field_path.endswith(".max_count")


def test_variable_sequences_and_nullable_thickness_bounds_preserve_order_and_types():
    one_band = {
        "min": None,
        "max": None,
        "include_min": True,
        "include_max": True,
        "tolerance": 1,
        "calculation_mode": "absolute",
    }
    rules = _replace_parameters(
        "thickness_jump_limit",
        ranges=[one_band],
        fallback_tolerance=Decimal("0.10"),
    )
    rules = tuple(
        replace(item, parameters={"surface_grades": ["FD", "FC", "FB"], "max_run_count": 5})
        if item.rule_id == "chain_high_surface_run_count_lte"
        else item
        for item in rules
    )
    compiled = compile_gqga4_rule_set(rules, 2)
    by_id = {item.rule_id: item for item in compiled.rule_set_spec.rules}
    band = by_id["thickness_jump_limit"].parameters["ranges"][0]
    assert band["min"] is band["max"] is None
    assert band["tolerance"] == Decimal(1)
    assert by_id["chain_high_surface_run_count_lte"].parameters["surface_grades"] == (
        "FD",
        "FC",
        "FB",
    )
    assert load_compiled_rule_set_json(compiled.compiled_rule_set_json) == compiled.rule_set_spec


@pytest.mark.parametrize(
    "change,path_suffix", (({"unexpected": 1}, ".unexpected"), ({}, ".min_weight"))
)
def test_parameter_objects_reject_unknown_and_missing_keys(change, path_suffix):
    original = next(item for item in GQGA4_INITIAL_RULES if item.rule_id == "chain_weight_range")
    parameters = (
        dict(original.parameters) | change
        if change
        else {key: value for key, value in original.parameters.items() if key != "min_weight"}
    )
    rules = tuple(
        replace(item, parameters=parameters) if item is original else item
        for item in GQGA4_INITIAL_RULES
    )
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(rules, GQGA4_INITIAL_VIRTUAL_PROTOTYPES)
    assert caught.value.issues[0].field_path.endswith(path_suffix)


def test_rule_business_validation_remains_in_the_existing_authoritative_loader():
    rules = _replace_parameters(
        "chain_weight_range",
        min_weight=Decimal("2100"),
        max_weight=Decimal("2000"),
        target_weight=Decimal("2000"),
    )
    with pytest.raises(RuleSetLoadError) as caught:
        compile_gqga4_rule_set(rules, 2)
    assert any(issue.code == "invalid_rule_parameters" for issue in caught.value.issues)


@pytest.mark.parametrize(
    "prototype,code",
    (
        (replace(GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0], unit_weight=Decimal(0)), "non_positive"),
        (
            replace(
                GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0],
                min_temperature=Decimal("800"),
                max_temperature=Decimal("700"),
            ),
            "reversed_temperature_range",
        ),
    ),
)
def test_virtual_prototypes_are_validated_independently_from_rules(prototype, code):
    values = (prototype, *GQGA4_INITIAL_VIRTUAL_PROTOTYPES[1:])
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_rule_set_snapshot(GQGA4_RULE_SET_TEMPLATE, GQGA4_INITIAL_RULES, values)
    assert any(code in issue.code for issue in caught.value.issues)


@pytest.mark.parametrize("missing", ("width", "thickness"))
def test_virtual_prototypes_include_physical_fields_required_by_enabled_rules(missing):
    prototype = replace(GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0], **{missing: None})
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(
            GQGA4_INITIAL_RULES,
            (prototype, *GQGA4_INITIAL_VIRTUAL_PROTOTYPES[1:]),
        )
    assert any(
        issue.code == "missing_required_field" and issue.field_path.endswith(f".{missing}")
        for issue in caught.value.issues
    )


def test_disabled_physical_rule_does_not_create_a_hidden_prototype_requirement():
    rules = tuple(
        replace(item, enabled=False) if item.rule_id == "thickness_jump_limit" else item
        for item in GQGA4_INITIAL_RULES
    )
    prototype = replace(GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0], thickness=None)
    normalized, prototypes = normalize_gqga4_rule_snapshot(rules, (prototype,))
    assert (
        next(item for item in normalized if item.rule_id == "thickness_jump_limit").enabled is False
    )
    assert prototypes == (prototype,)


def test_virtual_prototype_text_is_normalized_before_duplicate_detection():
    first = replace(
        GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0],
        prototype_id=f" {GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0].prototype_id} ",
        grade=" sphc ",
        rule_attributes={"surface_grade": " fc "},
    )
    rules, prototypes = normalize_gqga4_rule_snapshot(GQGA4_INITIAL_RULES, (first,))
    assert rules == GQGA4_INITIAL_RULES
    assert prototypes[0] == replace(
        first,
        prototype_id=first.prototype_id.strip(),
        grade="SPHC",
        rule_attributes={"surface_grade": "FC"},
    )
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(
            GQGA4_INITIAL_RULES,
            (first, GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0]),
        )
    assert any(issue.code == "duplicate_prototype_id" for issue in caught.value.issues)


@pytest.mark.parametrize(
    "attributes,code",
    (
        ({"width": "shadow"}, "shadowed_core_field"),
        ({"surface_grade": 1}, "invalid_text_attribute"),
    ),
)
def test_virtual_prototype_rule_attributes_cannot_bypass_input_normalization(attributes, code):
    prototype = replace(GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0], rule_attributes=attributes)
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(GQGA4_INITIAL_RULES, (prototype,))
    assert any(issue.code == code for issue in caught.value.issues)


def test_duplicate_virtual_prototype_identity_is_rejected_without_reordering():
    values = (*GQGA4_INITIAL_VIRTUAL_PROTOTYPES, GQGA4_INITIAL_VIRTUAL_PROTOTYPES[0])
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(GQGA4_INITIAL_RULES, values)
    assert caught.value.issues[0].code == "duplicate_prototype_id"


def test_version_number_is_canonical_and_changes_the_signed_identity():
    compiled = compile_rule_set(GQGA4_RULE_SET_TEMPLATE, GQGA4_INITIAL_RULES, 2)
    assert compiled.rule_set_spec.version == "2"
    assert compiled.rule_set_spec.fingerprint == (
        "28b65b6aa28a9a773d525abcae8e3de74e73886f20876ed7a16a7becfbadfa6c"
    )
    for value in (True, 0, Decimal(1), "01"):
        with pytest.raises(ValueError, match="version_no"):
            compile_rule_set(GQGA4_RULE_SET_TEMPLATE, GQGA4_INITIAL_RULES, value)


@pytest.mark.parametrize("value", (Decimal("1e100"), 10**100))
def test_integer_parameters_use_one_runtime_representability_boundary(value):
    rules = _replace_parameters("max_reverse_width_count", max_count=value)
    with pytest.raises(RuleSetCompilationError) as caught:
        normalize_gqga4_rule_snapshot(rules, GQGA4_INITIAL_VIRTUAL_PROTOTYPES)
    assert caught.value.issues[0].field_path.endswith(".max_count")


def test_persisted_json_rejects_duplicate_unknown_and_tampered_content():
    payload = dumps_rule_set_spec(GQGA4_RULE_SET_TEMPLATE)
    duplicate = payload.replace(
        '{"allowed_final_deviation_codes"', '{"fingerprint":"x","allowed_final_deviation_codes"', 1
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_compiled_rule_set_json(duplicate)
    unknown = payload.replace("{", '{"unknown":1,', 1)
    with pytest.raises(ValueError, match="field set"):
        load_compiled_rule_set_json(unknown)
    tampered = payload.replace("700.0", "701.0", 1)
    with pytest.raises(RuleSetLoadError) as caught:
        load_compiled_rule_set_json(tampered)
    assert any(issue.code == "rule_set_fingerprint_mismatch" for issue in caught.value.issues)


def test_editor_contract_has_no_fields_for_server_owned_metadata():
    assert tuple(part.name for part in fields(EditableRuleInput)) == (
        "rule_id",
        "enabled",
        "parameters",
    )
