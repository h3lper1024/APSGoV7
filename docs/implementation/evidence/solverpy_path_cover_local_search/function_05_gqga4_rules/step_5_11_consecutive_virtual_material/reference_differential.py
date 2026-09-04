"""Optional chain-rule comparison; no normalization, full search or external inventory."""

import argparse
import json
import runpy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import ControlledSplitMode, RuleScope
from apsgo_scheduler.core.model import (
    Chain,
    MaterialRole,
    Node,
    SplitLineage,
    VirtualLineage,
    VirtualPurpose,
)
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ConsecutiveVirtualMaterialRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_consecutive_virtual_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "rule_context.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
rule_id = "max_consecutive_virtual_sphc"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
assert record["enabled"] and record["params"] == {"max_count": 2}
assert record["reason"]["code"] == "virtual_sphc_run"
assert rule_context == resolved["rule_context"]
assert rule_context[rule_id] == 2
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
purposes = tuple(VirtualPurpose)
limits = (0, 1, 2, 5)
counts = {"enabled_equivalent": 0, "disabled_metric_omission": 0}
first_expected_difference = None


def nodes(pattern, purpose=None):
    old_nodes, new_nodes = [], []
    for index, role in enumerate(pattern):
        virtual = role == "V"
        name = f"n-{index}"
        grade = "OTHER" if virtual and index % 2 else "SPHC"
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=name,
                source_order_id=name,
                source_period="period",
                weight=Decimal(1),
                width=None,
                grade=grade,
                actual_transition_material=role == "A",
                node_type="virtual_sphc" if virtual else "real",
                split_piece_index=1 if role == "S" else 0,
                split_piece_count=2 if role == "S" else 1,
                split_authorized=role == "S",
                split_original_weight=Decimal(2) if role == "S" else Decimal(0),
            )
        )
        selected_purpose = purpose or purposes[index % len(purposes)]
        new_nodes.append(
            Node(
                name,
                None if virtual else name,
                None if virtual else name,
                None if virtual else "period",
                Decimal(1),
                None,
                None,
                None,
                None,
                grade,
                MaterialRole.GENERATED_VIRTUAL
                if virtual
                else (MaterialRole.ACTUAL_TRANSITION if role == "A" else MaterialRole.NORMAL_REAL),
                {},
                VirtualLineage(
                    "prototype",
                    selected_purpose,
                    f"partition-{index}"
                    if selected_purpose is VirtualPurpose.SPLIT_SEPARATOR
                    else None,
                    index + 1,
                )
                if virtual
                else None,
                SplitLineage(
                    f"partition-{index}",
                    f"parent-{index}",
                    name,
                    name,
                    "period",
                    "period",
                    ControlledSplitMode.SAME_PERIOD_SPLIT,
                    "period",
                    1,
                    Decimal(2),
                    1,
                    2,
                    "split",
                    "1",
                    "decision",
                    "authorized",
                )
                if role == "S"
                else None,
            )
        )
    return old_nodes, tuple(new_nodes)


def compare(pattern, limit, enabled=True, purpose=None):
    global first_expected_difference
    label = f"{pattern}:{limit}:{enabled}:{purpose}"
    old_nodes, new_nodes = nodes(pattern, purpose)
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [dict(record, enabled=enabled, params={"max_count": limit})]
            }
        },
        dict(rule_context, max_consecutive_virtual_sphc=limit),
        {"period_order": ["period"]},
    )
    old_chain = reference["Chain"](old_nodes, "period")
    before_chain = (tuple(old_chain.nodes), old_chain.assigned_period)
    old = reference["evaluate_chain"](old_chain, 0, book, "C")
    assert (tuple(old_chain.nodes), old_chain.assigned_period) == before_chain, label
    assert all(item["rule_id"] == rule_id for item in old.violations), (label, old.violations)
    for item in old.violations:
        start, end = item["positions"]
        assert item["scope"] == "chain_segment" and item["prohibited"]
        assert item["subject_id"] == f"C:virtual:{start}-{end}"
    rule = ConsecutiveVirtualMaterialRule(
        rule_id, "连续虚拟材料", RuleScope.CHAIN, enabled, "1", {"max_count": limit}
    )
    new = rule.evaluate(ChainRuleSubject("C", Chain("C", new_nodes, "period")), context)
    projected = []
    for violation in new.violations:
        assert violation.rule_id == rule_id and violation.scope is RuleScope.CHAIN
        assert violation.disposition is RuleDisposition.PROHIBITED
        assert violation.reason_code == "virtual_sphc_run"
        assert violation.subject_id.startswith(f"C:{rule_id}:")
        positions = tuple(int(value) for value in violation.subject_id.rsplit(":", 1)[1].split("-"))
        projected.append((positions, violation.severity))
    # Reference uses :virtual: in subject IDs; target uses the business rule ID.
    expected = [
        (tuple(item["positions"]), Decimal(str(item["severity_value"]))) for item in old.violations
    ]
    assert projected == expected, (label, expected, projected)
    if enabled:
        assert tuple((item.metric_key, item.value) for item in new.metrics) == (
            (rule_id, old.summary[rule_id]),
        ), label
        counts["enabled_equivalent"] += 1
    else:
        assert not new.violations and not new.metrics and not old.violations, label
        counts["disabled_metric_omission"] += 1
        if first_expected_difference is None:
            first_expected_difference = {
                "case": label,
                "reference_metric": old.summary[rule_id],
                "reference_run_ok": old.summary["virtual_sphc_run_ok"],
                "target_metrics": [],
                "reason": "disabled target emits no contribution; reference still computes metric and run flag",
            }


# Exhaustive short chains include both real roles; all-virtual chains are invalid in the target model.
for length in range(1, 6):
    for roles in product("RAV", repeat=length):
        if set(roles) != {"V"}:
            for limit in limits:
                compare("".join(roles), limit)

# Longer boundaries cover leading/trailing runs, actual-material and split-piece breaks.
for size, limit in product((0, 1, 2, 3, 5, 6, 20), limits):
    for pattern in (
        "V" * size + "R",
        "R" + "V" * size,
        "V" * size + "A" + "V" * (size + 1),
        "V" * size + "S" + "V" * size + "R",
    ):
        compare(pattern, limit)
for purpose, limit in product(purposes, limits):
    compare("VVVSVVVV", limit, purpose=purpose)
for pattern, limit in product(("VVVR", "R", "AVR", "VVSVVR"), limits):
    compare(pattern, limit, enabled=False)

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "evaluate_chain_calls": sum(counts.values()),
            "first_expected_difference": first_expected_difference,
            "first_unexpected_difference": None,
            "subject_identity_projection": "reference C:virtual:start-end -> target C:rule_id:start-end",
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "isolated actual evaluate_chain; no normalization, full search, V3 or external inventory",
        },
        ensure_ascii=False,
    )
)
