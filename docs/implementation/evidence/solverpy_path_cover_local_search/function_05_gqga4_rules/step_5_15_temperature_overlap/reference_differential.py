"""Optional direct-edge comparison; incomplete edges do not prove input validity."""

import argparse
import json
import math
import runpy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import EdgeRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import TemperatureOverlapRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_temperature_overlap_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "rule_context.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
rule_id = "temperature_overlap_min"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
assert record["enabled"] and record["params"] == {"min_overlap": 10.0, "ignore_temperature": False}
assert record["reason"]["code"] == "temperature"
assert rule_context == resolved["rule_context"]
assert rule_context["virtual_sphc_temperature_adaptive"] is True
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
normal, actual, virtual = tuple(MaterialRole)
roles = (normal, actual, virtual)
counts = {
    "enabled_equivalent": 0,
    "disabled_equivalent": 0,
    "ignored_equivalent": 0,
    "raw_projection_protection": 0,
    "derived_overlap_protection": 0,
    "derived_severity_protection": 0,
}
first_expected_differences = {}


def node_pair(name, interval, role):
    lower, upper = interval
    is_virtual = role is virtual
    old = replace(
        reference["_sentinel_node"](),
        node_id=name,
        width=None,
        min_soak_temp=reference["optional_float"](lower),
        max_soak_temp=reference["optional_float"](upper),
        actual_transition_material=role is actual,
        node_type="virtual_sphc" if is_virtual else "real",
    )
    new = Node(
        name,
        None if is_virtual else name,
        None if is_virtual else name,
        None if is_virtual else "period",
        Decimal(1),
        None,
        None,
        lower,
        upper,
        "",
        role,
        {},
        VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1) if is_virtual else None,
    )
    return old, new


def compare(
    left_interval,
    right_interval,
    left_role=normal,
    right_role=normal,
    minimum=Decimal(10),
    enabled=True,
    ignore=False,
    adaptive=False,
    protection=None,
):
    label = f"{left_interval}:{right_interval}:{left_role.value}:{right_role.value}:{minimum}:{enabled}:{ignore}:{adaptive}"
    left, right = (
        node_pair("left", left_interval, left_role),
        node_pair("right", right_interval, right_role),
    )
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    dict(
                        record,
                        enabled=enabled,
                        params={"min_overlap": minimum, "ignore_temperature": ignore},
                    )
                ]
            }
        },
        dict(rule_context, virtual_sphc_temperature_adaptive=adaptive),
        {"period_order": ["period"]},
    )
    failures = reference["edge_rule_failures"](left[0], right[0], book)
    assert len(failures) <= 1 and all(item["rule_id"] == rule_id for item in failures), (
        label,
        failures,
    )
    rule = TemperatureOverlapRule(
        rule_id,
        "温度重叠",
        RuleScope.EDGE,
        enabled,
        "1",
        {
            "min_overlap": minimum,
            "ignore_temperature": ignore,
            "virtual_temperature_adaptive": adaptive,
        },
    )
    assert rule.required_fields() == (
        ("min_temperature", "max_temperature") if enabled and not ignore else ()
    )
    subject = EdgeRuleSubject("E", left[1], right[1])
    if protection:
        allowed, overlap = reference["temperature_allowed"](left[0], right[0], book)
        if protection == "raw_projection_protection":
            assert allowed and not failures
            assert any(
                value is not None and not math.isfinite(float(value))
                for value in (*left_interval, *right_interval)
            )
            error_hint = "temperature must have a finite float projection"
        elif protection == "derived_overlap_protection":
            assert not math.isfinite(overlap)
            error_hint = "E: temperature overlap must be finite"
        else:
            assert protection == "derived_severity_protection"
            assert math.isfinite(overlap) and len(failures) == 1
            assert not math.isfinite(failures[0]["severity_value"])
            error_hint = "E: temperature severity must be finite"
        try:
            rule.evaluate(subject, context)
        except ValueError as error:
            assert error_hint in str(error), (label, str(error))
        else:
            raise AssertionError((label, "nonfinite arithmetic must be rejected"))
        counts[protection] += 1
        first_expected_differences.setdefault(
            protection,
            {
                "case": label,
                "reference_allowed": allowed,
                "reference_overlap": str(overlap),
                "reference_severity": str(failures[0]["severity_value"]) if failures else None,
                "target": "ValueError",
                "boundary": "direct edge only, not complete input validation",
            },
        )
        return
    result = rule.evaluate(subject, context)
    assert result.metrics == ()
    assert len(result.violations) == len(failures), (label, failures, result)
    for failure, violation in zip(failures, result.violations):
        assert violation.rule_id == rule_id and violation.scope is RuleScope.EDGE
        assert violation.subject_id == "E" and violation.reason_code == "temperature"
        assert violation.disposition is RuleDisposition.PROHIBITED
        assert violation.severity == Decimal(str(failure["severity_value"])), (
            label,
            failure,
            violation,
        )
    counts[
        "disabled_equivalent"
        if not enabled
        else "ignored_equivalent"
        if ignore
        else "enabled_equivalent"
    ] += 1


intervals = tuple(
    tuple(None if value is None else Decimal(value) for value in interval)
    for interval in (
        (None, None),
        (None, 10),
        (0, None),
        (-100, -20),
        (-20, 20),
        (0, 0),
        (0, 10),
        (5, 15),
        (10, 30),
    )
)
for left, right, left_role, right_role, minimum in product(
    intervals, intervals, roles, roles, (Decimal(0), Decimal("1e-10"), Decimal(10), Decimal(25))
):
    compare(left, right, left_role, right_role, minimum)

# Every missing-endpoint mask remains a direct-edge pass, not permission for incomplete real input.
for mask, left_role, right_role in product(product((False, True), repeat=4), roles, roles):
    values = tuple(
        None if absent else Decimal(value) for value, absent in zip((0, 20, 10, 30), mask)
    )
    compare(values[:2], values[2:], left_role, right_role)
for left_role, right_role, enabled, ignore, adaptive in product(
    roles, roles, (False, True), (False, True), (False, True)
):
    for left, right in (
        ((Decimal(0), Decimal(10)), (Decimal(30), Decimal(40))),
        ((Decimal(0), Decimal(20)), (Decimal(10), Decimal(30))),
        ((None, Decimal(20)), (Decimal(10), Decimal(30))),
    ):
        compare(
            left, right, left_role, right_role, enabled=enabled, ignore=ignore, adaptive=adaptive
        )

# Check the actual overlap+epsilon expression using its nearest float neighbors, in both directions.
for minimum in (Decimal(0), Decimal("1e-10"), Decimal(10), Decimal("1e16")):
    boundary = float(minimum) - 1e-9
    for overlap, role in product(
        (math.nextafter(boundary, -math.inf), boundary, math.nextafter(boundary, math.inf)), roles
    ):
        left = (Decimal("-1e17"), Decimal(0))
        right = (Decimal.from_float(-overlap), Decimal("1e17"))
        compare(left, right, normal, role, minimum)
        compare(right, left, role, normal, minimum)

# Reference optional_float maps overflowing raw fields to None. Target direct rules reject them,
# except when an earlier documented bypass applies. This is not reference input-precheck acceptance.
overflow_intervals = (
    ((Decimal("-1e1000"), Decimal(0)), (Decimal(-10), Decimal(10))),
    ((Decimal(0), Decimal("1e1000")), (Decimal(0), Decimal(10))),
    ((Decimal(-10), Decimal(10)), (Decimal("-1e1000"), Decimal(0))),
    ((Decimal(0), Decimal(10)), (Decimal(0), Decimal("1e1000"))),
)
for (left, right), role in product(overflow_intervals, roles):
    compare(left, right, normal, role, protection="raw_projection_protection")
for left, right in overflow_intervals:
    compare(left, right, enabled=False)
    compare(left, right, ignore=True)
    compare(left, right, normal, virtual, adaptive=True)

derived_cases = (
    (("-1e308", "1e308"), ("-1e308", "1e308"), "10", "derived_overlap_protection"),
    (("-1e308", "-1e308"), ("1e308", "1e308"), "10", "derived_overlap_protection"),
    (("0", "0"), ("1e308", "1e308"), "0", "derived_severity_protection"),
    (("0", "0"), ("1e308", "1e308"), "1e308", "derived_severity_protection"),
)
for (left, right, minimum, protection), role in product(derived_cases, roles):
    left, right = tuple(map(Decimal, left)), tuple(map(Decimal, right))
    compare(left, right, normal, role, Decimal(minimum), protection=protection)
    compare(right, left, role, normal, Decimal(minimum), protection=protection)

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "edge_rule_failures_calls": sum(counts.values()),
            "first_expected_differences": first_expected_differences,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "direct edge only; real input completeness remains precheck responsibility; no full search, V3 or inventory",
        },
        ensure_ascii=False,
    )
)
