"""Rule-management JSON remains precise, immutable and framework independent."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from apsgo_scheduler.api.request import (
    QualityCriterionSpec,
    RuleDefinitionSpec,
    RuleSetSpec,
    VirtualPrototypeInput,
)
from apsgo_scheduler.api.rule_management import (
    ActiveRulesResponse,
    EditableRuleInput,
    RuleManagementContractError,
    RuleManagementError,
    SetActiveRulesRequest,
    SetActiveRulesResponse,
    dumps_active_rules_response,
    dumps_rule_management_error,
    dumps_set_active_rules_request,
    dumps_set_active_rules_response,
    loads_set_active_rules_request,
)
from apsgo_scheduler.core.contracts import (
    DiagnosticIssue,
    DiagnosticPhase,
    DiagnosticSeverity,
    RuleScope,
)

OPERATION_ID = "B73D48A9-29C3-42A5-B4C7-131CFE8396BA"
CANONICAL_OPERATION_ID = OPERATION_ID.lower()


def request_json() -> str:
    return f"""
    {{
      "save_operation_id": "{OPERATION_ID}",
      "expected_active_version_id": 60,
      "rules": [
        {{
          "rule_id": "rule-a",
          "enabled": true,
          "parameters": {{
            "count": 2,
            "limit": 20.00,
            "precise": 12345678901234567890.12345678901234567890,
            "labels": ["FC", "FD"]
          }}
        }}
      ],
      "virtual_prototypes": [
        {{
          "prototype_id": "prototype-a",
          "unit_weight": 20,
          "width": 1000.0,
          "thickness": null,
          "min_temperature": 650,
          "max_temperature": 700.00,
          "grade": "SPHC",
          "rule_attributes": {{"family": "virtual", "priority": 1}}
        }}
      ],
      "remark": "  调整参数  "
    }}
    """


def prototype() -> VirtualPrototypeInput:
    return VirtualPrototypeInput(
        "prototype-a",
        Decimal("20"),
        Decimal("1000"),
        None,
        Decimal("650"),
        Decimal("700"),
        "SPHC",
        {"family": "virtual"},
    )


def rule_set(**changes) -> RuleSetSpec:
    value = RuleSetSpec(
        product_line_code="LINE",
        process_code="PROCESS",
        scenario="month",
        version="2",
        rules=(
            RuleDefinitionSpec(
                "rule-a",
                "SyntheticWidthLimitRule",
                "宽度规则",
                RuleScope.EDGE,
                True,
                "1",
                {"limit": Decimal("20.00"), "count": 2},
            ),
        ),
        quality_spec=(
            QualityCriterionSpec(
                "prohibited_violation_count",
                "prohibited_violation_count",
                "minimize",
                "named_value",
                "exact_decimal",
            ),
        ),
        allowed_final_deviation_codes=frozenset({"z_deviation", "chain_weight_below_minimum"}),
        fingerprint="fingerprint-2",
    )
    return replace(value, **changes)


def active_rules(**changes) -> ActiveRulesResponse:
    values = dict(
        active_version_id=61,
        based_on_version_id=60,
        rule_set_spec=rule_set(),
        virtual_prototypes=(prototype(),),
        remark="调整参数",
        activated_at="2026-09-06T10:00:00+08:00",
        activated_by="service-process",
    )
    return ActiveRulesResponse(**(values | changes))


def issue() -> DiagnosticIssue:
    return DiagnosticIssue(
        "invalid_rule_parameters",
        DiagnosticPhase.RULE_LOADING,
        "rules[0].parameters.limit",
        "rule-a",
        "规则参数无效",
        DiagnosticSeverity.ERROR,
    )


def test_request_parser_preserves_untyped_rule_numbers_and_types_known_prototype_numbers():
    value = loads_set_active_rules_request(request_json())
    assert value.save_operation_id == CANONICAL_OPERATION_ID
    assert value.remark == "调整参数"
    assert type(value.rules[0].parameters["count"]) is int
    assert value.rules[0].parameters["limit"] == Decimal("20.00")
    assert isinstance(value.rules[0].parameters["limit"], Decimal)
    assert value.rules[0].parameters["precise"] == Decimal(
        "12345678901234567890.12345678901234567890"
    )
    assert value.rules[0].parameters["labels"] == ("FC", "FD")
    actual = value.virtual_prototypes[0]
    assert actual.unit_weight == Decimal("20")
    assert actual.width == Decimal("1000.0")
    assert actual.min_temperature == Decimal("650")


def test_request_json_round_trip_is_precise_and_keeps_decimal_identity():
    first = loads_set_active_rules_request(request_json())
    encoded = dumps_set_active_rules_request(first)
    assert '"limit":20.0' in encoded
    assert '"count":2' in encoded
    assert "decimal" not in encoded
    assert loads_set_active_rules_request(encoded) == first
    parsed = json.loads(encoded, parse_float=Decimal)
    assert isinstance(parsed["rules"][0]["parameters"]["limit"], Decimal)
    assert type(parsed["rules"][0]["parameters"]["count"]) is int


def test_decimal_with_many_trailing_zeroes_round_trips_without_expanding_output():
    rule = EditableRuleInput("rule-a", True, {"value": Decimal("1" + ("0" * 10_000))})
    value = SetActiveRulesRequest(OPERATION_ID, 60, (rule,), (), "")
    encoded = dumps_set_active_rules_request(value)
    assert '"value":1e10000' in encoded
    assert loads_set_active_rules_request(encoded) == value


def test_json_output_escapes_a_lone_surrogate_and_remains_utf8_encodable():
    first = loads_set_active_rules_request(request_json())
    first = replace(first, remark=f"中文{chr(0xD800)}")
    encoded = dumps_set_active_rules_request(first)
    assert encoded.encode("utf-8")
    assert "\\ud800" in encoded
    assert loads_set_active_rules_request(encoded) == first


def test_request_copies_and_freezes_mutable_inputs():
    parameters = {"groups": [{"limit": Decimal("20")}]}
    rules = [EditableRuleInput("rule-a", True, parameters)]
    prototypes = [prototype()]
    value = SetActiveRulesRequest(OPERATION_ID, 60, rules, prototypes, " note ")
    parameters.clear()
    rules.clear()
    prototypes.clear()
    assert value.rules[0].parameters["groups"] == ({"limit": Decimal("20")},)
    assert value.remark == "note"
    with pytest.raises(TypeError):
        value.rules[0].parameters["new"] = True
    with pytest.raises(FrozenInstanceError):
        value.expected_active_version_id = 61
    assert not hasattr(value, "__dict__")


@pytest.mark.parametrize(
    ("body", "code", "path"),
    [
        ("[]", "invalid_field_type", "body"),
        ("{}", "missing_field", "expected_active_version_id"),
        (
            json.dumps(
                {
                    "save_operation_id": CANONICAL_OPERATION_ID,
                    "expected_active_version_id": 1,
                    "rules": [],
                    "virtual_prototypes": [],
                    "extra": True,
                }
            ),
            "unknown_field",
            "extra",
        ),
        ("{", "invalid_json", "body"),
        ('{"value": NaN}', "invalid_json", "body"),
        (
            '{"save_operation_id":"a","save_operation_id":"b"}',
            "duplicate_json_key",
            "body",
        ),
    ],
)
def test_malformed_json_has_stable_locatable_diagnostics(body, code, path):
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(body)
    assert [(item.code, item.field_path) for item in caught.value.issues] == [(code, path)]
    assert caught.value.issues[0].phase is DiagnosticPhase.REQUEST_VALIDATION


@pytest.mark.parametrize(
    "number",
    ["1e1000000000000000000", "1e-999999999999999999999999999999"],
)
def test_decimal_parser_failures_are_reported_as_invalid_json(number):
    body = request_json().replace('"limit": 20.00', f'"limit": {number}')
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(body)
    assert (caught.value.issues[0].code, caught.value.issues[0].field_path) == (
        "invalid_json",
        "body",
    )


@pytest.mark.parametrize("field", ["rules", "virtual_prototypes"])
def test_rule_and_prototype_collections_must_be_arrays(field):
    body = json.loads(request_json())
    body[field] = {}
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == field


@pytest.mark.parametrize("enabled", [0, 1, "true", None])
def test_enabled_requires_a_json_boolean(enabled):
    body = json.loads(request_json())
    body["rules"][0]["enabled"] = enabled
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "rules[0].enabled"


@pytest.mark.parametrize("version", [True, False, 0, -1, "60", 60.0, None])
def test_expected_version_requires_a_positive_json_integer(version):
    body = json.loads(request_json())
    body["expected_active_version_id"] = version
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "expected_active_version_id"


def test_rule_items_reject_server_owned_fields_and_non_object_parameters():
    body = json.loads(request_json())
    body["rules"][0]["name"] = "客户端伪造名称"
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "rules[0].name"
    del body["rules"][0]["name"]
    body["rules"][0]["parameters"] = []
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "rules[0].parameters"


def test_nested_parameter_and_prototype_attribute_errors_are_located():
    body = json.loads(request_json())
    body["rules"][0]["parameters"]["invalid"] = {" ": 1}
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "rules[0].parameters"

    body = json.loads(request_json())
    body["virtual_prototypes"][0]["rule_attributes"]["invalid"] = []
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "virtual_prototypes[0].rule_attributes"


def test_rule_parameter_nesting_accepts_64_layers_and_rejects_the_next_layer():
    body = json.loads(request_json())
    parameters = 0
    for _ in range(64):
        parameters = {"value": parameters}
    body["rules"][0]["parameters"] = parameters
    parsed = loads_set_active_rules_request(json.dumps(body))
    assert loads_set_active_rules_request(dumps_set_active_rules_request(parsed)) == parsed

    body["rules"][0]["parameters"] = {"value": parameters}
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].code == "maximum_nesting_exceeded"
    assert caught.value.issues[0].field_path == "rules[0].parameters"


def test_virtual_prototype_rejects_unknown_fields():
    body = json.loads(request_json())
    body["virtual_prototypes"][0]["extra"] = True
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "virtual_prototypes[0].extra"


def test_duplicate_rule_and_prototype_identities_are_located():
    body = json.loads(request_json())
    body["rules"].append(dict(body["rules"][0]))
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "rules[1].rule_id"
    body = json.loads(request_json())
    body["virtual_prototypes"].append(dict(body["virtual_prototypes"][0]))
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "virtual_prototypes[1].prototype_id"


@pytest.mark.parametrize("operation_id", ["", "not-a-uuid", 1, None])
def test_operation_id_is_required_uuid_text(operation_id):
    body = json.loads(request_json())
    body["save_operation_id"] = operation_id
    with pytest.raises(RuleManagementContractError) as caught:
        loads_set_active_rules_request(json.dumps(body))
    assert caught.value.issues[0].field_path == "save_operation_id"


def test_active_response_is_flat_complete_and_keeps_declared_order():
    encoded = dumps_active_rules_response(active_rules())
    value = json.loads(encoded, parse_float=Decimal)
    assert set(value) == {
        "product_line_code",
        "process_code",
        "scenario",
        "active_version_id",
        "version_no",
        "based_on_version_id",
        "fingerprint",
        "rules",
        "quality_spec",
        "allowed_final_deviation_codes",
        "virtual_prototypes",
        "remark",
        "activated_at",
        "activated_by",
    }
    assert value["product_line_code"] == "LINE"
    assert value["active_version_id"] == 61
    assert value["version_no"] == 2
    assert value["based_on_version_id"] == 60
    assert value["fingerprint"] == "fingerprint-2"
    assert value["rules"][0]["sequence_no"] == 1
    assert value["rules"][0]["scope"] == "edge"
    assert value["rules"][0]["parameters"]["limit"] == Decimal("20.0")
    assert value["quality_spec"][0]["criterion_id"] == "prohibited_violation_count"
    assert value["allowed_final_deviation_codes"] == [
        "chain_weight_below_minimum",
        "z_deviation",
    ]
    assert value["virtual_prototypes"][0]["unit_weight"] == Decimal("20.0")


def test_active_response_rejects_duplicate_virtual_prototype_identities():
    with pytest.raises(ValueError, match="prototype_id"):
        active_rules(virtual_prototypes=(prototype(), prototype()))


def test_save_response_is_flat_and_does_not_reactivate_an_old_saved_version():
    current = active_rules(active_version_id=62, rule_set_spec=rule_set(version="3"))
    response = SetActiveRulesResponse(
        current,
        OPERATION_ID,
        previous_active_version_id=60,
        saved_version_id=61,
        saved_version_is_active=False,
        idempotent_replay=True,
    )
    value = json.loads(dumps_set_active_rules_response(response), parse_float=Decimal)
    assert set(value) == {
        "product_line_code",
        "process_code",
        "scenario",
        "active_version_id",
        "version_no",
        "based_on_version_id",
        "fingerprint",
        "rules",
        "quality_spec",
        "allowed_final_deviation_codes",
        "virtual_prototypes",
        "remark",
        "activated_at",
        "activated_by",
        "save_operation_id",
        "previous_active_version_id",
        "saved_version_id",
        "saved_version_is_active",
        "idempotent_replay",
    }
    assert "active_rules" not in value and "rule_set_spec" not in value
    assert value["active_version_id"] == 62
    assert value["saved_version_id"] == 61
    assert value["saved_version_is_active"] is False
    assert value["idempotent_replay"] is True
    with pytest.raises(ValueError, match="saved_version_is_active"):
        replace(response, saved_version_is_active=True)
    with pytest.raises(ValueError, match="new save"):
        replace(response, idempotent_replay=False)


def test_active_saved_version_must_use_the_previous_active_version_as_its_base():
    response = SetActiveRulesResponse(
        active_rules(),
        OPERATION_ID,
        previous_active_version_id=60,
        saved_version_id=61,
        saved_version_is_active=True,
        idempotent_replay=False,
    )
    with pytest.raises(ValueError, match="based on the previous version"):
        replace(response, previous_active_version_id=59)
    with pytest.raises(ValueError, match="based on the previous version"):
        replace(response, previous_active_version_id=59, idempotent_replay=True)


@pytest.mark.parametrize("version", ["", "0", "01", "v1"])
def test_response_requires_a_canonical_positive_rule_set_version(version):
    with pytest.raises(ValueError, match="canonical integer"):
        active_rules(rule_set_spec=rule_set(version=version))


def test_error_response_reuses_full_diagnostic_contract():
    value = RuleManagementError(
        "rule_set_validation_failed",
        "规则配置未通过校验",
        expected_active_version_id=60,
        current_active_version_id=61,
        issues=(issue(),),
    )
    actual = json.loads(dumps_rule_management_error(value))["error"]
    assert set(actual) == {
        "code",
        "message",
        "expected_active_version_id",
        "current_active_version_id",
        "issues",
    }
    assert actual["code"] == "rule_set_validation_failed"
    assert actual["expected_active_version_id"] == 60
    assert actual["current_active_version_id"] == 61
    assert actual["issues"][0] == {
        "code": "invalid_rule_parameters",
        "phase": "rule_loading",
        "field_path": "rules[0].parameters.limit",
        "subject_id": "rule-a",
        "message": "规则参数无效",
        "severity": "error",
    }
