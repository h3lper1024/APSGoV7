"""Optional direct thickness-edge comparison; not complete input or search acceptance."""

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
from apsgo_scheduler.core.rules.concrete import ThicknessTransitionRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_thickness_transition_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "rule_context.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
rule_id = "thickness_jump_limit"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
raw_config = record["params"]["thickness_rules"]
assert record["enabled"] and record["reason"]["code"] == "thickness"
assert raw_config["basis"] == "thinner"
assert [
    (
        item["min"],
        item["max"],
        item["include_min"],
        item["include_max"],
        item["tolerance"],
        item["calculation_mode"],
    )
    for item in raw_config["ranges"]
] == [
    (None, 0.6, True, False, 0.1, "absolute"),
    (0.6, 1.1, True, True, 0.2, "absolute"),
    (1.1, 1.5, False, False, 0.3, "absolute"),
    (1.5, None, True, True, 0.5, "absolute"),
]
assert rule_context == resolved["rule_context"]
parameters = {
    "basis": "thinner",
    "fallback_tolerance": Decimal("0.1"),
    "ranges": tuple(
        dict(
            item,
            **{
                key: None if item[key] is None else Decimal(str(item[key]))
                for key in ("min", "max", "tolerance")
            },
        )
        for item in raw_config["ranges"]
    ),
}
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
normal, actual, virtual = tuple(MaterialRole)
roles = (normal, actual, virtual)
counts = {
    "enabled_equivalent": 0,
    "disabled_equivalent": 0,
    "custom_fallback_cases": 0,
    "raw_projection_protection": 0,
    "relative_tolerance_protection": 0,
    "severity_protection": 0,
}
first_expected_differences = {}
custom_fallback_output_differences = 0


def band(lower, upper, tolerance, mode="absolute", include_min=True, include_max=True):
    return {
        "min": None if lower is None else Decimal(str(lower)),
        "max": None if upper is None else Decimal(str(upper)),
        "include_min": include_min,
        "include_max": include_max,
        "tolerance": Decimal(str(tolerance)),
        "calculation_mode": mode,
    }


def node_pair(name, thickness, role):
    is_virtual = role is virtual
    old = replace(
        reference["_sentinel_node"](),
        node_id=name,
        width=None,
        thickness=reference["optional_float"](thickness),
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
        thickness,
        None,
        None,
        "",
        role,
        {},
        VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1) if is_virtual else None,
    )
    return old, new


def compare(
    left_value,
    right_value,
    params=parameters,
    left_role=normal,
    right_role=normal,
    enabled=True,
    protection=None,
    custom_fallback=False,
):
    global custom_fallback_output_differences
    label = (
        f"case-{sum(counts.values()) + 1}:{left_value}:{right_value}:"
        f"{left_role.value}:{right_role.value}:{enabled}:{params['basis']}"
    )
    left, right = (
        node_pair("left", left_value, left_role),
        node_pair("right", right_value, right_role),
    )
    # Reference has no configurable fallback; never inject target-only fallback into its inputs.
    old_config = {"basis": params["basis"], "ranges": params["ranges"]}
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [dict(record, enabled=enabled, params={"thickness_rules": old_config})]
            }
        },
        rule_context,
        {"period_order": ["period"]},
    )
    failures = reference["edge_rule_failures"](left[0], right[0], book)
    allowed, delta, tolerance = reference["thickness_allowed"](left[0], right[0], book)
    assert len(failures) <= 1 and all(item["rule_id"] == rule_id for item in failures), (
        label,
        failures,
    )
    if enabled:
        assert allowed == (not failures)
    rule = ThicknessTransitionRule(rule_id, "厚度跳跃", RuleScope.EDGE, enabled, "1", params)
    assert rule.required_fields() == (("thickness",) if enabled else ())
    subject = EdgeRuleSubject("E", left[1], right[1])
    if protection:
        if protection == "raw_projection_protection":
            assert (
                allowed
                and not failures
                and (left[0].thickness is None or right[0].thickness is None)
            )
            error_hint = "thickness must have a finite float projection"
        elif protection == "relative_tolerance_protection":
            assert math.isfinite(delta) and math.isinf(tolerance) and allowed
            error_hint = "E: thickness calculation must be finite"
        else:
            assert (
                protection == "severity_protection"
                and math.isfinite(delta)
                and math.isfinite(tolerance)
            )
            assert len(failures) == 1 and math.isinf(failures[0]["severity_value"])
            error_hint = "E: thickness severity must be finite"
        try:
            rule.evaluate(subject, context)
        except ValueError as error:
            assert error_hint in str(error), (label, str(error))
        else:
            raise AssertionError((label, "nonfinite calculation must be rejected"))
        counts[protection] += 1
        first_expected_differences.setdefault(
            protection,
            {
                "case": label,
                "reference_allowed": allowed,
                "reference_delta": str(delta),
                "reference_tolerance": str(tolerance),
                "target": "ValueError",
            },
        )
        return
    result = rule.evaluate(subject, context)
    assert result.metrics == ()
    for violation in result.violations:
        assert violation.rule_id == rule_id and violation.scope is RuleScope.EDGE
        assert violation.subject_id == "E" and violation.reason_code == "thickness"
        assert violation.disposition is RuleDisposition.PROHIBITED
    expected = tuple(Decimal(str(item["severity_value"])) for item in failures)
    observed = tuple(item.severity for item in result.violations)
    if custom_fallback:
        assert params["ranges"] == () and tolerance == 0.1 and enabled
        target_tolerance = float(params["fallback_tolerance"])
        expected_custom = (
            ()
            if delta <= target_tolerance + 1e-9
            else (Decimal(str(max(1.0, (delta - target_tolerance) / max(target_tolerance, 1e-9)))),)
        )
        assert observed == expected_custom, (label, observed, expected_custom)
        counts["custom_fallback_cases"] += 1
        if observed != expected:
            custom_fallback_output_differences += 1
            first_expected_differences.setdefault(
                "custom_fallback",
                {
                    "case": label,
                    "reference_tolerance": tolerance,
                    "target_tolerance": target_tolerance,
                    "reference_severities": [str(value) for value in expected],
                    "target_severities": [str(value) for value in observed],
                },
            )
    else:
        assert observed == expected, (label, failures, result)
        counts["enabled_equivalent" if enabled else "disabled_equivalent"] += 1


values = [None, Decimal("0.1"), Decimal("0.5"), Decimal(2)]
for boundary in (0.6, 1.1, 1.5):
    values.extend(
        Decimal.from_float(value)
        for value in (
            math.nextafter(boundary, -math.inf),
            boundary,
            math.nextafter(boundary, math.inf),
        )
    )
for left, right, left_role, right_role in product(values, values, roles, roles):
    compare(left, right, left_role=left_role, right_role=right_role)

# Exact band comparisons have no epsilon; the permitted jump comparison does.
for base, role in product(
    (Decimal("0.3"), Decimal("0.6"), Decimal("1.1"), Decimal("1.5"), Decimal(2)), roles
):
    tolerance = reference["thickness_tolerance"](float(base), raw_config)
    boundary = float(base) + tolerance + 1e-9
    for value in (
        math.nextafter(boundary, -math.inf),
        boundary,
        math.nextafter(boundary, math.inf),
    ):
        other = Decimal.from_float(value)
        compare(base, other, left_role=normal, right_role=role)
        compare(other, base, left_role=role, right_role=normal)

sample_values = tuple(map(Decimal, ("0.5", "0.6", "1", "1.1", "1.2")))
for basis, mode, include_min, include_max in product(
    ("thinner", " THICKER "), ("absolute", " RELATIVE "), (False, True), (False, True)
):
    params = dict(
        parameters, basis=basis, ranges=(band("0.6", "1.1", "0.2", mode, include_min, include_max),)
    )
    for left, right in product(sample_values, repeat=2):
        compare(left, right, params)
for ranges in (
    (),
    (band(-2, -1, "0.3"),),
    (band(None, "0.4", "0.3"), band("1.3", None, "0.7")),
    (band(None, None, "0.02"), band(None, None, "0.4")),
    (band(None, None, "0.4"), band(None, None, "0.02")),
    (band("0.6", "0.6", 0),),
):
    for left, right in product(sample_values, repeat=2):
        compare(left, right, dict(parameters, ranges=ranges))

for fallback, (left, right), role in product(
    (Decimal(0), Decimal("0.05"), Decimal("0.3")),
    (
        (Decimal("0.5"), Decimal("0.65")),
        (Decimal("0.5"), Decimal("0.5")),
        (Decimal("0.5"), Decimal("0.6")),
        (Decimal(2), Decimal("2.8")),
    ),
    roles,
):
    compare(
        left,
        right,
        dict(parameters, ranges=(), fallback_tolerance=fallback),
        right_role=role,
        custom_fallback=True,
    )

for role, reverse in product(roles, (False, True)):
    for left, right in (
        (None, Decimal(1)),
        (Decimal("0.2"), Decimal(2)),
        (Decimal("1e1000"), Decimal(1)),
    ):
        compare(
            right if reverse else left, left if reverse else right, right_role=role, enabled=False
        )
    left, right = (Decimal(1), Decimal("1e1000")) if reverse else (Decimal("1e1000"), Decimal(1))
    compare(left, right, right_role=role, protection="raw_projection_protection")
    compare(None, Decimal("1e1000"), right_role=role)
    for left, right, params, protection in (
        (
            Decimal(4),
            Decimal(5),
            dict(parameters, ranges=(band(None, None, "1e308", "relative"),)),
            "relative_tolerance_protection",
        ),
        (
            Decimal(1),
            Decimal("1e308"),
            dict(parameters, ranges=(band(None, None, 0),)),
            "severity_protection",
        ),
    ):
        compare(
            right if reverse else left,
            left if reverse else right,
            params,
            right_role=role,
            protection=protection,
        )

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "edge_rule_failures_calls": sum(counts.values()),
            "custom_fallback_output_differences": custom_fallback_output_differences,
            "first_expected_differences": first_expected_differences,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "unreachable_guard": "finite positive projected thicknesses cannot overflow their absolute difference",
            "scope": "direct edge only; no complete input acceptance, full search, V3 or inventory",
        },
        ensure_ascii=False,
    )
)
