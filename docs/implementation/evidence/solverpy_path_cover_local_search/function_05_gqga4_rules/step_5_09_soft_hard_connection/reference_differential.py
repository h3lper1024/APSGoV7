"""Optional edge-only comparison; no scheduling or external directory inventory."""

import argparse
import json
import runpy
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import EdgeRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import SoftHardConnectionRule

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--reference", required=True, type=Path)
source = parser.parse_args().reference
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_soft_hard_differential")
inputs = Path(__file__).resolve().parents[6] / "tests/baselines/gqga4/inputs"
paths = (inputs / "resolved_rules.json", inputs / "rule_context.json")
before_hashes = tuple(sha256(path.read_bytes()).hexdigest() for path in paths)
resolved, rule_context = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
rule_id = "gqga4_soft_hard_connection"
record = next(item for item in resolved["rule_snapshot"]["dsl_rules"] if item["rule_id"] == rule_id)
assert record["enabled"] and record["params"]["policy"] == {
    "missing_grade_policy": "fallback_same_hot_roll_grade",
    "product_line_code": "GQGA4",
    "transition_material_breaks_soft_hard": True,
    "virtual_sphc_allows_bridge": True,
}
assert rule_context == resolved["rule_context"]
parameters = {
    key: value for key, value in record["params"]["policy"].items() if key != "product_line_code"
}
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
normal, actual, virtual = (
    MaterialRole.NORMAL_REAL,
    MaterialRole.ACTUAL_TRANSITION,
    MaterialRole.GENERATED_VIRTUAL,
)
policies = ("deny", "allow", "pass", "ignore", "fallback_same_hot_roll_grade")
counts = {"enabled_equivalent": 0, "disabled_equivalent": 0, "blank_hot_grade_protection": 0}
first_expected_difference = None


def nodes(name, role, classification, hot_grade):
    is_virtual = role is virtual
    old = replace(
        reference["_sentinel_node"](),
        node_id=name,
        soft_hard_class=classification,
        hot_roll_grade=hot_grade,
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
        None,
        None,
        "",
        role,
        {"soft_hard_class": classification, "hot_roll_grade": hot_grade},
        VirtualLineage("prototype", VirtualPurpose.EDGE_BRIDGE, None, 1) if is_virtual else None,
    )
    return old, new


def compare(label, left, right, params, enabled=True, blank_protection=False):
    global first_expected_difference
    book = reference["parse_rule_book"](
        {
            "rule_snapshot": {
                "dsl_rules": [dict(record, enabled=enabled, params={"policy": params})]
            }
        },
        rule_context,
        {"period_order": ["period"]},
    )
    subject = EdgeRuleSubject(label, left[1], right[1])
    rule = SoftHardConnectionRule(rule_id, "软硬材连接", RuleScope.EDGE, enabled, "1", params)
    contribution = rule.evaluate(subject, context)
    failures = reference["edge_rule_failures"](left[0], right[0], book)
    assert len(failures) <= 1 and all(item["rule_id"] == rule_id for item in failures), label
    if enabled:
        allowed = reference["soft_hard_allowed"](left[0], right[0], book)
        assert allowed == (not failures), label
    assert contribution.metrics == ()
    assert len(contribution.violations) <= 1, label
    for violation in contribution.violations:
        assert violation.rule_id == rule_id and violation.scope is RuleScope.EDGE
        assert violation.subject_id == label and violation.severity == Decimal(1)
        assert violation.disposition is RuleDisposition.PROHIBITED
        assert violation.reason_code == "soft_hard_connection_not_allowed"
    if blank_protection:
        assert not failures and len(contribution.violations) == 1, label
        counts["blank_hot_grade_protection"] += 1
        if first_expected_difference is None:
            first_expected_difference = {
                "case": label,
                "reference_allowed": True,
                "target_allowed": False,
                "reason": "raw helper accepts identical whitespace-only hot grades; target rejects missing fallback",
            }
    else:
        assert len(contribution.violations) == len(failures), (label, failures, contribution)
        if failures:
            assert failures[0]["severity_value"] == float(contribution.violations[0].severity)
        counts["enabled_equivalent" if enabled else "disabled_equivalent"] += 1


# Ordinary material cases distinguish missing classes, unknown labels and raw text identity.
# Hot-grade normalization belongs to the input adapter, not this edge predicate.
text_cases = (
    ("same_class", ("soft", "A"), ("soft", "B")),
    ("different_class", ("soft", "A"), ("hard", "A")),
    ("missing_same_hot", (None, "SPHC"), ("hard", "SPHC")),
    ("blank_class", ("   ", "SPHC"), ("hard", "SPHC")),
    ("missing_different_hot", (None, "A"), (None, "B")),
    ("missing_hot", (None, None), (None, "")),
    ("unknown_same_class", ("UNKNOWN", "A"), ("UNKNOWN", "B")),
    ("case_sensitive_class", ("soft", "A"), ("SOFT", "A")),
    ("raw_hot_identity", (None, " sphc "), (None, "SPHC")),
    ("class_trim", (" soft ", "A"), ("soft", "B")),
)
for (label, left_text, right_text), policy in product(text_cases, policies):
    left, right = nodes("left", normal, *left_text), nodes("right", normal, *right_text)
    params = dict(parameters, missing_grade_policy=policy)
    for first, second in ((left, right), (right, left)):
        compare(f"{label}:{policy}:{first[1].node_id}", first, second, params)

# A False bridge switch must reject even matching classes; virtual takes precedence.
for left_role, right_role, virtual_flag, actual_flag, policy in product(
    (normal, actual, virtual), (normal, actual, virtual), (False, True), (False, True), policies
):
    params = dict(
        parameters,
        virtual_sphc_allows_bridge=virtual_flag,
        transition_material_breaks_soft_hard=actual_flag,
        missing_grade_policy=policy,
    )
    left, right = nodes("left", left_role, "soft", "A"), nodes("right", right_role, "soft", "A")
    compare(
        f"roles:{left_role.value}:{right_role.value}:{virtual_flag}:{actual_flag}:{policy}",
        left,
        right,
        params,
    )

for left_role, right_role in ((actual, normal), (virtual, normal), (virtual, actual)):
    compare(
        f"bridge_missing:{left_role.value}:{right_role.value}",
        nodes("left", left_role, None, None),
        nodes("right", right_role, None, None),
        parameters,
    )

for left_role, right_role, virtual_flag, actual_flag in product(
    (normal, actual, virtual), (normal, actual, virtual), (False, True), (False, True)
):
    params = dict(
        parameters,
        virtual_sphc_allows_bridge=virtual_flag,
        transition_material_breaks_soft_hard=actual_flag,
    )
    compare(
        f"disabled:{left_role.value}:{right_role.value}:{virtual_flag}:{actual_flag}",
        nodes("left", left_role, "soft", "A"),
        nodes("right", right_role, "hard", "B"),
        params,
        enabled=False,
    )

# Explicit defensive difference for non-normalized input, never counted as equivalence.
for blank in ("   ", "\t"):
    compare(
        f"blank_hot:{blank!r}",
        nodes("left", normal, None, blank),
        nodes("right", normal, None, blank),
        parameters,
        blank_protection=True,
    )

assert tuple(sha256(path.read_bytes()).hexdigest() for path in paths) == before_hashes
assert sha256(source.read_bytes()).hexdigest() == source_hash
print(
    json.dumps(
        {
            "status": "pass",
            "counts": counts,
            "edge_rule_failures_calls": sum(counts.values()),
            "direct_predicate_calls": counts["enabled_equivalent"]
            + counts["blank_hot_grade_protection"],
            "first_expected_difference": first_expected_difference,
            "first_unexpected_difference": None,
            "reference_and_frozen_inputs_unchanged": True,
            "reference_sha256": source_hash,
            "scope": "edge-only audit; no normalization, full search, V3 access or external inventory",
        },
        ensure_ascii=False,
    )
)
