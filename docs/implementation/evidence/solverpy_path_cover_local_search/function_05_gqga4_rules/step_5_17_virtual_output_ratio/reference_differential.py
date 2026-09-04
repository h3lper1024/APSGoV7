"""Optional leaf-rule comparison; reference plan evaluation is not a solver search."""

import argparse
import json
import runpy
from dataclasses import replace
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Context, Decimal, localcontext
from hashlib import sha256
from pathlib import Path

from apsgo_scheduler.core.contracts import ControlledSplitMode, RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SchedulePlan,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.resource_facts import EvaluationResourceView
from apsgo_scheduler.core.rules.base import PlanRuleSubject, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import VirtualOutputRatioRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_virtual_output_ratio_probe")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "solver_config.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, policy = (json.loads(path.read_text(), parse_float=Decimal) for path in paths)
rule_id, metric = "virtual_output_weight_ratio_limit", "virtual_output_weight_ratio"
definition = next(
    item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id
)
assert definition["enabled"] and definition["params"] == {
    "denominator": "final_output_total_weight",
    "max_ratio": Decimal("0.05"),
}
periods = tuple(policy["period_order"])
context = RuleEvaluationContext(periods, dict(zip(periods, range(len(periods)))), ("prototype",))
arithmetic = Context(prec=28, rounding=ROUND_HALF_EVEN)
epsilon = Decimal("0.000001")
real, transition, virtual = tuple(MaterialRole)
ambient_contexts = ((6, ROUND_UP), (28, ROUND_HALF_EVEN), (50, ROUND_DOWN))


def make_case(parts, split=False):
    """Small declared fixture, not the future production resource-view derivation."""
    period = periods[1] if split else periods[0]
    nodes, old_chains, chains = [], [], []
    for chain_index, items in enumerate(parts):
        current, old = [], []
        for role, weight in items:
            index, weight = len(nodes), Decimal(weight)
            name, is_virtual = f"n-{index}", role is virtual
            order = "split-source" if split and index < 2 else name
            lineage = None
            old_split = {}
            if split and index < 2:
                lineage = SplitLineage(
                    "partition",
                    "parent",
                    order,
                    order,
                    period,
                    periods[0],
                    ControlledSplitMode.FUTURE_BORROW_RETURN,
                    period,
                    1,
                    Decimal(900),
                    index + 1,
                    2,
                    "split-rule",
                    "1",
                    "fixture-authorization",
                    "future_return",
                )
                old_split = dict(
                    split_piece_index=index + 1,
                    split_piece_count=2,
                    split_authorized=True,
                    split_origin_assigned_period=periods[0],
                    split_original_weight=Decimal(900),
                    split_reason="rolling_final_cross_period_return",
                )
            node = Node(
                name,
                None if is_virtual else order,
                None if is_virtual else order,
                None if is_virtual else period,
                weight,
                None,
                None,
                None,
                None,
                "",
                role,
                {},
                VirtualLineage("prototype", VirtualPurpose.WEIGHT_FILL, None, index + 1)
                if is_virtual
                else None,
                lineage,
            )
            current.append(node)
            nodes.append(node)
            old.append(
                replace(
                    reference["_sentinel_node"](),
                    node_id=name,
                    source_order_id="" if is_virtual else order,
                    source_period="" if is_virtual else period,
                    weight=weight,
                    material_role=role.value,
                    actual_transition_material=role is transition,
                    node_type="virtual_sphc" if is_virtual else "real",
                    virtual_prototype_id="prototype" if is_virtual else "",
                    **old_split,
                )
            )
        chains.append(Chain(f"c-{chain_index}", tuple(current), period))
        old_chains.append(reference["Chain"](old, period))
    # All fixture inputs have at most 29 significant digits and bounded exponents.
    with localcontext(Context(prec=120)):
        real_weight = sum((n.weight for n in nodes if n.material_role is not virtual), Decimal(0))
        virtual_weight = sum((n.weight for n in nodes if n.material_role is virtual), Decimal(0))
        total = real_weight + virtual_weight
    view = EvaluationResourceView(
        (),
        tuple(n.node_id for n in nodes if n.material_role is virtual),
        ("partition",) if split else (),
        real_weight,
        virtual_weight,
        real_weight if split else Decimal(0),
        Decimal(0),
    )
    return SchedulePlan(tuple(chains)), view, old_chains, arithmetic.divide(virtual_weight, total)


def rule_book(limit, enabled):
    return reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [
                    dict(
                        definition,
                        enabled=enabled,
                        params={"denominator": "final_output_total_weight", "max_ratio": limit},
                    )
                ]
            }
        },
        {},
        policy,
    )


counts = dict(
    enabled_full_entry_equivalent=0,
    disabled_metric_omission=0,
    exact_weight_aggregation_cases=0,
    exact_weight_raw_ratio_differences=0,
    exact_weight_violation_differences=0,
    target_ambient_context_checks=0,
)
first_disabled = first_precision = first_precision_verdict = None


def compare(label, parts, limit, *, enabled=True, split=False, precise=False):
    global first_disabled, first_precision, first_precision_verdict
    limit = Decimal(limit)
    plan, view, old_chains, expected_ratio = make_case(parts, split)
    snapshot = tuple((tuple(c.nodes), c.assigned_period) for c in old_chains)
    book = rule_book(limit, enabled)
    with localcontext(arithmetic):
        actual = reference["evaluate_plan"](old_chains, book)
        # The report does not expose this raw value: reconstruct its documented arithmetic
        # separately, then verify its six-place projection against the actual full entry.
        old_real = sum(
            (n.weight for c in old_chains for n in c.nodes if not n.is_virtual), Decimal(0)
        )
        old_virtual = sum(
            (n.weight for c in old_chains for n in c.nodes if n.is_virtual), Decimal(0)
        )
        old_ratio = old_virtual / (old_real + old_virtual)
        assert actual.result_metrics[metric] == reference["decimal_text"](old_ratio)
        assert actual.result_metrics["real_weight"] == reference["decimal_text"](old_real)
        assert actual.result_metrics["virtual_weight"] == reference["decimal_text"](old_virtual)
        assert actual.result_metrics["final_output_total_weight"] == reference["decimal_text"](
            old_real + old_virtual
        )
    assert tuple((tuple(c.nodes), c.assigned_period) for c in old_chains) == snapshot, label
    assert all(v["rule_id"] == rule_id and v["prohibited"] for v in actual.violations), (
        label,
        actual.violations,
    )
    expected_bad = enabled and expected_ratio > arithmetic.add(limit, epsilon)
    old_bad = enabled and old_ratio > arithmetic.add(book.virtual_budget_ratio, epsilon)
    assert len(actual.violations) == int(old_bad), label
    if old_bad:
        assert actual.violations[0]["severity_value"] == float(
            arithmetic.subtract(old_ratio, limit)
        )
    rule = VirtualOutputRatioRule(
        rule_id, "虚拟材料产出比例", RuleScope.PLAN, enabled, "1", {"max_ratio": limit}
    )
    baseline = None
    for precision, rounding in ambient_contexts:
        with localcontext(Context(prec=precision, rounding=rounding)):
            result = rule.evaluate(PlanRuleSubject("plan", plan, view), context)
        if baseline is None:
            baseline = result
        assert result == baseline, label
        assert tuple((m.metric_key, m.value) for m in result.metrics) == (
            ((metric, expected_ratio),) if enabled else ()
        ), label
        assert len(result.violations) == int(expected_bad), label
        if expected_bad:
            failure = result.violations[0]
            assert (
                failure.rule_id,
                failure.scope,
                failure.subject_id,
                failure.reason_code,
                failure.disposition.value,
            ) == (
                rule_id,
                RuleScope.PLAN,
                "plan",
                "virtual_budget",
                "prohibited",
            ), label
            assert failure.severity == arithmetic.subtract(expected_ratio, limit), label
            if not precise:
                assert float(failure.severity) == actual.violations[0]["severity_value"], label
        counts["target_ambient_context_checks"] += 1
    if not enabled:
        counts["disabled_metric_omission"] += 1
        first_disabled = first_disabled or {
            "case": label,
            "reference_display": actual.result_metrics[metric],
            "target_metrics": [],
        }
    elif precise:
        counts["exact_weight_aggregation_cases"] += 1
        if expected_ratio != old_ratio:
            counts["exact_weight_raw_ratio_differences"] += 1
            first_precision = first_precision or {
                "case": label,
                "reference_arithmetic_not_raw_report": str(old_ratio),
                "target_raw_ratio": str(expected_ratio),
                "reference_display": actual.result_metrics[metric],
            }
        if expected_bad != old_bad:
            counts["exact_weight_violation_differences"] += 1
            first_precision_verdict = first_precision_verdict or {
                "case": label,
                "max_ratio": str(limit),
                "reference_violation": old_bad,
                "target_violation": expected_bad,
            }
    else:
        assert expected_ratio == old_ratio and expected_bad == old_bad, label
        with localcontext(arithmetic):
            assert reference["decimal_text"](expected_ratio) == actual.result_metrics[metric], label
        counts["enabled_full_entry_equivalent"] += 1


samples = (
    ("no_virtual", (((real, "95"),),), False),
    ("five_percent", (((real, "95"), (virtual, "5")),), False),
    ("one_third", (((real, "2"), (virtual, "1")),), False),
    (
        "actual_transition_in_denominator",
        (((real, "60"), (transition, "35"), (virtual, "5")),),
        False,
    ),
    ("actual_transition_only_real_role", (((transition, "95"), (virtual, "5")),), False),
    (
        "multiple_chains",
        (((real, "40"), (virtual, "2")), ((transition, "55"), (virtual, "3"))),
        False,
    ),
    ("almost_all_virtual", (((real, "0.000001"), (virtual, "1")),), False),
    ("authorized_future_split_return", (((real, "400"), (real, "500"), (virtual, "50")),), True),
)
for label, parts, split in samples:
    for limit in ("0", "0.05", "0.333333", "1", "1.5"):
        compare(f"{label}:limit={limit}", parts, limit, split=split)
    compare(f"{label}:disabled", parts, "0", enabled=False, split=split)
for fraction in (
    "0.05",
    "0.05000099999999999999999999999",
    "0.050001",
    "0.05000100000000000000000000001",
):
    with localcontext(Context(prec=120)):
        remainder = Decimal(1) - Decimal(fraction)
    compare(f"epsilon:{fraction}", (((real, str(remainder)), (virtual, fraction)),), "0.05")
for extra in ("4e-28", "6e-28"):
    for limit in ("0.05", "0.4999989999999999999999999999"):
        compare(
            f"exact_weight:{extra}:limit={limit}",
            (((real, "1"), (real, extra), (virtual, "1")),),
            limit,
            precise=True,
        )
assert counts["exact_weight_raw_ratio_differences"] == 4
assert counts["exact_weight_violation_differences"] == 1

# Empty reference plans are accepted, but the target model requires a nonempty plan.
# Check the zero-view leaf boundary independently with an anchor plan; this deliberately
# is not an assertion that the supplied view has been derived from that anchor plan.
with localcontext(arithmetic):
    empty = reference["evaluate_plan"]([], rule_book(Decimal(0), True))
assert empty.violations == () and empty.result_metrics[metric] == "0"
anchor, _, _, _ = make_case((((real, "1"),),))
zero = EvaluationResourceView((), (), (), Decimal(0), Decimal(0), Decimal(0), Decimal(0))
rule = VirtualOutputRatioRule(
    rule_id, "虚拟材料产出比例", RuleScope.PLAN, True, "1", {"max_ratio": Decimal(0)}
)
zero_result = rule.evaluate(PlanRuleSubject("zero-view-only", anchor, zero), context)
assert zero_result.violations == () and zero_result.metrics[0].value == Decimal(0)
disabled = replace(rule, enabled=False).evaluate(
    PlanRuleSubject("disabled-no-view", anchor, None), context
)
assert disabled.metrics == disabled.violations == ()

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            **counts,
            "reference_full_entry_calls": sum(
                counts[key]
                for key in (
                    "enabled_full_entry_equivalent",
                    "disabled_metric_omission",
                    "exact_weight_aggregation_cases",
                )
            )
            + 1,
            "reference_empty_plan_case": 1,
            "target_isolated_zero_view_case": 1,
            "target_disabled_without_resource_view_case": 1,
            "first_disabled_representation_difference": first_disabled,
            "first_expected_exact_weight_difference": first_precision,
            "first_expected_exact_weight_verdict_difference": first_precision_verdict,
            "first_unexpected_difference": None,
            "reference_projection": "actual evaluate_plan report is six-place display; raw reference ratio is separately reconstructed Context28 arithmetic",
            "expected_product_difference": "exact aggregation needing more than 28 significant digits may change the final Context28 ratio or boundary verdict",
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "leaf rule only; no input adapter, resource-view derivation, search, full quality key or quality-gate acceptance",
        },
        ensure_ascii=False,
    )
)
