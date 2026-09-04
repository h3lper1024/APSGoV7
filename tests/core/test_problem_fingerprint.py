"""Problem identity follows normalized business input, not a particular run."""

from dataclasses import fields, replace
from decimal import Decimal, localcontext

import pytest

from apsgo_scheduler.api.request import OrderInput, VirtualPrototypeInput
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
from apsgo_scheduler.core.model import MaterialRole

from ..app.test_input_normalizer import make_request

D = Decimal


def business_request():
    request = make_request()
    order = replace(
        request.orders[0],
        grade="STEEL",
        weight=D("12.5"),
        min_temperature=D("600"),
        max_temperature=D("700"),
        rule_attributes={"hot_roll_grade": "SPHC", "batch": "a", "flag": True, "rank": 1},
    )
    prototype = replace(
        request.virtual_prototypes[0],
        grade="VIRTUAL",
        min_temperature=D("600"),
        max_temperature=D("700"),
        rule_attributes={"batch": "prototype", "flag": True},
    )
    return replace(
        request,
        orders=(order, request.orders[1]),
        virtual_prototypes=(prototype, request.virtual_prototypes[1]),
    )


def problem(request):
    return normalize_input(request, load_rule_set(request.rule_set_spec))


ORDER_CHANGES = (
    ("node_id", "different-node"),
    ("source_order_id", "different-order"),
    ("source_resource_id", "different-resource"),
    ("source_period", "P1"),
    ("weight", D("13.125")),
    ("width", D("1111")),
    ("thickness", D("1.25")),
    ("min_temperature", D("610")),
    ("max_temperature", D("710")),
    ("grade", "DIFFERENT"),
    ("material_role", MaterialRole.ACTUAL_TRANSITION),
    ("rule_attributes", {"hot_roll_grade": "SPHC", "batch": "b", "flag": True, "rank": 1}),
)
PROTOTYPE_CHANGES = (
    ("prototype_id", "different-prototype"),
    ("unit_weight", D("25")),
    ("width", D("1234")),
    ("thickness", D("1.125")),
    ("min_temperature", D("620")),
    ("max_temperature", D("720")),
    ("grade", "OTHER-VIRTUAL"),
    ("rule_attributes", {"batch": "prototype", "flag": False}),
)


def test_every_public_order_and_prototype_field_has_a_fingerprint_case():
    assert {name for name, _ in ORDER_CHANGES} == {item.name for item in fields(OrderInput)}
    assert {name for name, _ in PROTOTYPE_CHANGES} == {
        item.name for item in fields(VirtualPrototypeInput)
    }


@pytest.mark.parametrize("name,value", ORDER_CHANGES)
def test_each_normalized_order_field_changes_problem_identity(name, value):
    request = business_request()
    changed = replace(request.orders[0], **{name: value})
    assert getattr(changed, name) != getattr(request.orders[0], name)
    candidate = replace(request, orders=(changed, request.orders[1]))
    assert problem(candidate).input_fingerprint != problem(request).input_fingerprint


@pytest.mark.parametrize("name,value", PROTOTYPE_CHANGES)
def test_each_normalized_prototype_field_changes_problem_identity(name, value):
    request = business_request()
    changed = replace(request.virtual_prototypes[0], **{name: value})
    assert getattr(changed, name) != getattr(request.virtual_prototypes[0], name)
    candidate = replace(request, virtual_prototypes=(changed, request.virtual_prototypes[1]))
    assert problem(candidate).input_fingerprint != problem(request).input_fingerprint


@pytest.mark.parametrize("name", ("product_line_code", "process_code", "scenario"))
def test_each_business_context_identity_is_in_the_fingerprint(name):
    request = business_request()
    spec = replace(request.rule_set_spec, **{name: "different"})
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    candidate = replace(request, rule_set_spec=spec, **{name: "different"})
    assert problem(candidate).input_fingerprint != problem(request).input_fingerprint


def test_attribute_mapping_key_order_is_irrelevant():
    request = business_request()
    candidate = replace(
        request,
        orders=tuple(
            replace(item, rule_attributes=dict(reversed(tuple(item.rule_attributes.items()))))
            for item in request.orders
        ),
        virtual_prototypes=tuple(
            replace(item, rule_attributes=dict(reversed(tuple(item.rule_attributes.items()))))
            for item in request.virtual_prototypes
        ),
    )
    assert problem(candidate).input_fingerprint == problem(request).input_fingerprint


def test_order_and_prototype_sequence_are_semantic_not_mapping_order():
    request = business_request()
    expected = problem(request).input_fingerprint
    assert problem(replace(request, orders=request.orders[::-1])).input_fingerprint != expected
    assert (
        problem(
            replace(request, virtual_prototypes=request.virtual_prototypes[::-1])
        ).input_fingerprint
        != expected
    )


def test_period_sequence_not_request_listing_order_defines_identity():
    request = business_request()
    expected = problem(request).input_fingerprint
    assert problem(replace(request, periods=request.periods[::-1])).input_fingerprint == expected
    changed = tuple(replace(item, sequence=1 - item.sequence) for item in request.periods)
    assert problem(replace(request, periods=changed)).input_fingerprint != expected


def test_request_number_rule_version_and_solver_policy_are_separate_identities():
    request = business_request()
    spec = replace(request.rule_set_spec, version="new-rules-version")
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    candidate = replace(
        request,
        request_id="another-run",
        rule_set_spec=spec,
        policy=replace(request.policy, seed=request.policy.seed + 1),
    )
    original, normalized = problem(request), problem(candidate)
    assert original.problem_id != normalized.problem_id
    assert original.input_fingerprint == normalized.input_fingerprint


def test_normalized_spelling_and_decimal_scale_produce_identical_identity():
    request = business_request()
    original = request.orders[0]
    changed = replace(
        original,
        node_id=f" {original.node_id} ",
        source_order_id=f" {original.source_order_id} ",
        source_resource_id=f" {original.source_resource_id} ",
        source_period=f" {original.source_period} ",
        grade=" steel ",
        weight=D("12.500"),
        width=D("1110.000"),
        rule_attributes=original.rule_attributes | {"hot_roll_grade": " sphc "},
    )
    request = replace(request, orders=(replace(original, width=D("1110")), request.orders[1]))
    candidate = replace(request, orders=(changed, request.orders[1]))
    expected = problem(request).input_fingerprint
    with localcontext() as context:
        context.prec = 2
        assert problem(candidate).input_fingerprint == expected
    assert request.orders[0].grade == "STEEL"
    assert candidate.orders[0].grade == " steel "
