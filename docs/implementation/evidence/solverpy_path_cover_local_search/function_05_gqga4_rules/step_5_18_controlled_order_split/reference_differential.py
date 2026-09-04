"""Optional actual-entry qualification probe; not a complete split-action comparison."""

import argparse
import inspect
import json
import math
import runpy
import sys
import time
from dataclasses import replace
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Context, Decimal, localcontext
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import ControlledSplitMode, RuleScope
from apsgo_scheduler.core.model import (
    MaterialRole,
    Node,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import ControlledSplitRuleSubject, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ControlledOrderSplitRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_controlled_split_probe")
action = reference["final_cross_period_split_return"]
lines, first_line = inspect.getsourcelines(action)
checkpoint = first_line + next(
    i for i, line in enumerate(lines) if line.strip() == "piece_count = int("
)
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "solver_config.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, policy = (json.loads(path.read_text(), parse_float=Decimal) for path in paths)
narrow = next(
    item
    for item in resolved["rule_snapshot"]["dsl_rules"]
    if item["rule_id"] == "chain_if_narrow_real_weight_lte"
)
book = reference["parse_rule_book"]({"rule_snapshot": {"dsl_rules": [narrow]}}, {}, policy)
assert (book.if_narrow_grade_class, book.if_narrow_width_exclusive, book.max_if_narrow_weight) == (
    "IF钢",
    1400,
    Decimal(500),
)
assert (
    book.final_return_enabled,
    book.final_return_allow_partial_split,
    book.final_return_only_future_borrowed,
) == (True, True, True)
assert (
    book.final_return_min_transfer_weight,
    book.final_return_max_moves,
    book.final_return_max_bridge_nodes,
    book.final_return_max_bridge_weight,
) == (Decimal(1), 10000, 2, Decimal(40))
periods = book.period_order
context = RuleEvaluationContext(periods, dict(book.period_index), ("prototype",))
parameters = dict(
    grade_class=book.if_narrow_grade_class,
    width_upper_exclusive=Decimal(1400),
    maximum_piece_weight=book.max_if_narrow_weight,
    minimum_piece_weight=book.final_return_min_transfer_weight,
    maximum_accepted_source_count=book.final_return_max_moves,
    maximum_separator_node_count=book.final_return_max_bridge_nodes,
    maximum_separator_weight=book.final_return_max_bridge_weight,
    allowed_modes=tuple(mode.value for mode in ControlledSplitMode),
)
rule = ControlledOrderSplitRule(
    "controlled_order_split", "受控订单拆分", RuleScope.ACTION_ELIGIBILITY, True, "1", parameters
)
normal, transition, virtual = tuple(MaterialRole)
arithmetic = Context(prec=28, rounding=ROUND_HALF_EVEN)


def parent(*, role=normal, width="1399", weight="600", grade="IF钢", source_period=None):
    is_virtual = role is virtual
    return Node(
        "parent",
        None if is_virtual else "order",
        None if is_virtual else "resource",
        None if is_virtual else (source_period or periods[1]),
        Decimal(weight),
        None if width is None else Decimal(width),
        None,
        None,
        None,
        "",
        role,
        {"grade_class": grade},
        VirtualLineage("prototype", VirtualPurpose.WEIGHT_FILL, None, 1) if is_virtual else None,
    )


class QualificationObserved(Exception):
    """Stop at the original action's first piece calculation, before it runs."""


reference_calls = observed_prefixes = natural_returns = 0


def observe(node, rules=book, *, origin=None, stop_at_prefix=True):
    global reference_calls, observed_prefixes, natural_returns
    origin = origin or periods[0]
    old_parent = replace(
        reference["_sentinel_node"](),
        node_id=node.node_id,
        source_order_id=node.source_order_id or "",
        source_period=node.source_period or "",
        weight=node.weight,
        grade_class=node.rule_attributes["grade_class"],
        width=None if node.width is None else float(node.width),
        actual_transition_material=node.material_role is transition,
        material_role=node.material_role.value,
        node_type="virtual_sphc" if node.material_role is virtual else "real",
    )
    anchor = replace(
        reference["_sentinel_node"](),
        node_id="anchor",
        source_order_id="anchor-order",
        source_period=origin,
        weight=Decimal(50),
    )
    chains = [reference["Chain"]([anchor, old_parent], origin)]
    snapshot = tuple((tuple(chain.nodes), chain.assigned_period) for chain in chains)
    factory = reference["VirtualFactory"]([], rules)
    budget = reference["SearchBudget"](time.monotonic() + 10, 0)
    observed = False

    def trace(frame, event, arg):
        nonlocal observed
        if frame.f_code is not action.__code__:
            return None
        if event == "line" and frame.f_lineno == checkpoint:
            assert frame.f_locals["node"].node_id == "parent"
            observed = True
            if stop_at_prefix:
                raise QualificationObserved
        return trace

    def unexpected_acceptance(*args):
        raise AssertionError("this bounded probe must not accept a split action")

    previous_trace = sys.gettrace()
    result = None
    try:
        sys.settrace(trace)
        with localcontext(arithmetic):
            result = action(chains, rules, factory, budget, unexpected_acceptance)
    except QualificationObserved:
        assert stop_at_prefix and observed
    finally:
        sys.settrace(previous_trace)
    assert sys.gettrace() is previous_trace
    assert tuple((tuple(chain.nodes), chain.assigned_period) for chain in chains) == snapshot
    assert budget.candidate_checks == 0 and budget.stop_reason == "search_complete"
    if result is not None:
        assert result[2] == 0
        assert all(v["rule_id"] == narrow["rule_id"] for v in result[1].violations)
        natural_returns += 1
    reference_calls += 1
    observed_prefixes += int(observed)
    return observed


counts = dict(
    matched_qualification=0,
    same_period_extension=0,
    disabled_agreement=0,
    exact_threshold_difference=0,
    eligible_but_action_not_accepted=0,
    target_only_context_protections=0,
    target_ambient_context_checks=0,
)
first_extension = first_precision = None


def decide(
    node, current_rule=rule, *, origin=None, source_period=None, count=0, current_context=context
):
    subject = ControlledSplitRuleSubject(
        "split-subject",
        node,
        origin or periods[0],
        source_period or node.source_period or periods[1],
        count,
    )
    decision = current_rule.evaluate_controlled_split(subject, current_context)
    for precision, rounding in ((6, ROUND_UP), (50, ROUND_DOWN)):
        with localcontext(Context(prec=precision, rounding=rounding)):
            assert current_rule.evaluate_controlled_split(subject, current_context) == decision
        counts["target_ambient_context_checks"] += 1
    if decision.eligible:
        assert decision.reason_code == decision.mode.value
        assert decision.target_assigned_period == subject.source_period
        assert all(
            getattr(decision, name) == current_rule.parameters[name]
            for name in (
                "maximum_piece_weight",
                "minimum_piece_weight",
                "maximum_accepted_source_count",
                "maximum_separator_node_count",
                "maximum_separator_weight",
            )
        )
    else:
        assert (
            decision.mode,
            decision.target_assigned_period,
            decision.maximum_piece_weight,
            decision.minimum_piece_weight,
        ) == (None,) * 4
        assert (
            decision.maximum_accepted_source_count,
            decision.maximum_separator_node_count,
            decision.maximum_separator_weight,
        ) == (0, 0, Decimal(0))
    return decision


for role, width, weight in product(
    (normal, transition, virtual),
    ("1399", str(math.nextafter(1400, -math.inf)), "1400", None),
    ("500", "500.000001", "500.0000010000001", "600"),
):
    node = parent(role=role, width=width, weight=weight)
    assert decide(node).eligible == observe(node), (role, width, weight)
    counts["matched_qualification"] += 1
for grade in ("普通钢", "if钢"):
    node = parent(grade=grade)
    assert not decide(node).eligible and not observe(node)
    counts["matched_qualification"] += 1
for weight in ("501", "1000", "1500"):
    node = parent(weight=weight, source_period=periods[0])
    decision = decide(node)
    assert decision.eligible and decision.mode is ControlledSplitMode.SAME_PERIOD_SPLIT
    assert not observe(node)
    counts["same_period_extension"] += 1
    first_extension = first_extension or {
        "weight": weight,
        "source_period": periods[0],
        "reference_prefix_reached": False,
        "target_reason": decision.reason_code,
    }
for flag in ("final_return_enabled", "final_return_allow_partial_split"):
    assert not observe(parent(), replace(book, **{flag: False}))
    assert not decide(parent(), replace(rule, enabled=False)).eligible
    counts["disabled_agreement"] += 1
zero_cap = replace(rule, parameters=parameters | {"maximum_accepted_source_count": 0})
assert not observe(parent(), replace(book, final_return_max_moves=0))
assert decide(parent(), zero_cap).reason_code == "split_source_limit"
counts["matched_qualification"] += 1

# The actual action is also run to natural completion on qualified but unusable candidates.
# These fail later because of a short tail, too many separators, or an empty prototype catalog.
for label, weight in (
    ("tail_below_minimum", "500.5"),
    ("too_many_separators", "2000"),
    ("no_separator_prototypes", "600"),
):
    node = parent(weight=weight)
    assert decide(node).eligible and observe(node, stop_at_prefix=False), label
    counts["eligible_but_action_not_accepted"] += 1

# The reference adds EPS under Context28; the target retains the exact authoritative limit.
maximum = Decimal("500.00000000000000000000000001")
precise_rule = replace(rule, parameters=parameters | {"maximum_piece_weight": maximum})
for weight in ("500.000001000000000000000000005", "500.00000100000000000000000001"):
    node = parent(weight=weight)
    decision = decide(node, precise_rule)
    assert observe(node, replace(book, max_if_narrow_weight=maximum))
    assert not decision.eligible and decision.reason_code == "source_weight_within_limit"
    counts["exact_threshold_difference"] += 1
    first_precision = first_precision or {
        "maximum_piece_weight": str(maximum),
        "source_weight": weight,
        "reference_prefix_reached": True,
        "target_reason": decision.reason_code,
    }

# Explicit target context and lineage protections have no equivalent reference leaf API.
for changes, reason in (
    ({"origin": "unknown"}, "invalid_period_relation"),
    ({"source_period": periods[0]}, "invalid_period_relation"),
    ({"count": book.final_return_max_moves}, "split_source_limit"),
    ({"origin": periods[2]}, "source_already_late"),
    (
        {
            "current_context": RuleEvaluationContext(
                tuple(reversed(periods)),
                {p: i for i, p in enumerate(reversed(periods))},
                ("prototype",),
            )
        },
        "source_already_late",
    ),
):
    assert decide(parent(), **changes).reason_code == reason
    counts["target_only_context_protections"] += 1
assert decide(parent(source_period="unknown")).reason_code == "invalid_period_relation"
assert (
    decide(parent(), replace(rule, parameters=parameters | {"allowed_modes": ()})).reason_code
    == "split_mode_not_allowed"
)
lineage = SplitLineage(
    "partition",
    "unsplit-parent",
    "order",
    "resource",
    periods[1],
    periods[0],
    ControlledSplitMode.FUTURE_BORROW_RETURN,
    periods[1],
    1,
    Decimal(1200),
    1,
    2,
    "split-rule",
    "1",
    "fixture-authorization",
    "future_return",
)
assert decide(replace(parent(), split_lineage=lineage)).reason_code == "already_split"
counts["target_only_context_protections"] += 3

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            **counts,
            "reference_actual_action_entry_calls": reference_calls,
            "reference_qualification_prefix_observed": observed_prefixes,
            "reference_natural_returns": natural_returns,
            "reference_checkpoint_line": checkpoint,
            "instrumentation": "sys.settrace observes original first piece_count line; positive prefix probes interrupt before construction; predicates and module globals are never replaced; previous tracing restored",
            "first_expected_same_period_difference": first_extension,
            "first_expected_exact_threshold_difference": first_precision,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "qualification only; original action's normalized earlier-period anchor is explicit; no candidate acceptance, input normalization, completed split action, search or quality-gate claim",
        },
        ensure_ascii=False,
    )
)
