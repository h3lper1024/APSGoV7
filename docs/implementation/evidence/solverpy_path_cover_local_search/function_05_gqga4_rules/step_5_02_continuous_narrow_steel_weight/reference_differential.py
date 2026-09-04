"""Optional local reference check; never imported by production or portable tests."""

import json
import runpy
import sys
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from itertools import product
from pathlib import Path

from apsgo_scheduler.core.contracts import RuleScope
from apsgo_scheduler.core.model import Chain, MaterialRole, Node, VirtualLineage, VirtualPurpose
from apsgo_scheduler.core.rules.base import ChainRuleSubject, RuleDisposition, RuleEvaluationContext
from apsgo_scheduler.core.rules.concrete import ContinuousNarrowSteelWeightRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_narrow_weight_differential")
rule_id = "chain_if_narrow_real_weight_lte"
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))
D = Decimal
# raw grade text, width, weight, role
choices = (
    ("IF钢", D("1300"), D("250"), "real"),
    ("IF钢", D("1300"), D("125.25"), "real"),
    ("if钢", D("1300"), D("250"), "real"),
    ("IF钢", D("1400"), D("250"), "real"),
    ("IF钢", None, D("250"), "real"),
    ("IF钢", D("1300"), D("250"), "actual"),
    ("IF钢", D("1300"), D("250"), "virtual"),
)


def compare(sequence, limit, width_limit=D("1400"), grade_limit="IF钢"):
    old_nodes, new_nodes = [], []
    for index, (grade, width, weight, role) in enumerate(sequence):
        virtual = role == "virtual"
        actual = role == "actual"
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=f"n-{index}",
                grade_class=grade.strip(),
                width=None if width is None else float(width),
                weight=weight,
                actual_transition_material=actual,
                node_type="virtual_sphc" if virtual else "real",
            )
        )
        new_nodes.append(
            Node(
                node_id=f"n-{index}",
                source_order_id=None if virtual else f"o-{index}",
                source_resource_id=None if virtual else f"r-{index}",
                source_period=None if virtual else "period",
                weight=weight,
                width=width,
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=MaterialRole.GENERATED_VIRTUAL
                if virtual
                else MaterialRole.ACTUAL_TRANSITION
                if actual
                else MaterialRole.NORMAL_REAL,
                rule_attributes={"grade_class": grade},
                virtual_lineage=VirtualLineage(
                    "prototype", VirtualPurpose.EDGE_BRIDGE, None, index + 1
                )
                if virtual
                else None,
            )
        )
    old, old_maximum, _ = reference["_segment_violations"](
        "C",
        old_nodes,
        rule_id,
        lambda node: not node.is_virtual
        and not node.actual_transition_material
        and node.grade_class == grade_limit
        and node.width is not None
        and node.width < float(width_limit),
        lambda node: node.weight,
        limit,
        "IF narrow real run weight",
    )
    rule = ContinuousNarrowSteelWeightRule(
        rule_id=rule_id,
        name="窄钢连续真实重量",
        scope=RuleScope.CHAIN,
        enabled=True,
        version="1",
        parameters={
            "grade_class": grade_limit,
            "width_upper_exclusive": width_limit,
            "max_real_weight": limit,
        },
    )
    result = rule.evaluate(ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period")), context)
    expected = (
        tuple((item["subject_id"], Decimal(str(item["severity_value"]))) for item in old),
        old_maximum,
    )
    observed = (
        tuple((item.subject_id, item.severity) for item in result.violations),
        result.metrics[0].value,
    )
    assert (
        len(result.metrics) == 1 and result.metrics[0].metric_key == "max_if_narrow_real_run_weight"
    )
    assert all(
        item.reason_code == "if_narrow_run_weight"
        and item.disposition is RuleDisposition.PROHIBITED
        for item in result.violations
    )
    assert observed == expected, (sequence, limit, width_limit, grade_limit, expected, observed)


exhaustive_count = skipped = boundary_count = 0
for length in range(1, 5):
    for sequence in product(choices, repeat=length):
        for limit in (D("0"), D("500")):
            if all(item[-1] == "virtual" for item in sequence):
                skipped += 1
                continue
            compare(sequence, limit)
            exhaustive_count += 1
for weights in (
    ("500",),
    ("500.000001",),
    ("500.0000011",),
    ("250", "250"),
    ("250", "250.000001"),
    ("250", "250.0000011"),
):
    compare(tuple(("IF钢", D("1300"), D(weight), "real") for weight in weights), D("500"))
    boundary_count += 1
for weight in ("0.0000009", "0.000001", "0.0000011"):
    compare((("IF钢", D("1300"), D(weight), "real"),), D("0"))
    boundary_count += 1
for width in ("1399.99999999999999", "1399.9999999999998", "1400", "1400.0000000000001", "1401"):
    compare((("IF钢", D(width), D("600"), "real"),), D("500"))
    boundary_count += 1
for width, bound in (
    ("1200", "1200.00000000000001"),
    ("1200", "1200.0000000000002"),
    ("0.0999999999999999999999", "0.1"),
):
    compare((("IF钢", D(width), D("600"), "real"),), D("500"), D(bound))
    boundary_count += 1
for raw_grade, configured_grade in (
    (" IF钢 ", "IF钢"),
    (" IF钢 ", " IF钢 "),
    ("if钢", "IF钢"),
    ("ALT", "ALT"),
):
    compare(((raw_grade, D("1300"), D("600"), "real"),), D("500"), grade_limit=configured_grade)
    boundary_count += 1
print(
    json.dumps(
        {
            "status": "pass",
            "compared_cases": exhaustive_count + boundary_count,
            "exhaustive_cases": exhaustive_count,
            "boundary_cases": boundary_count,
            "skipped_all_virtual_cases": skipped,
            "first_difference": None,
            "reference_sha256": source_hash,
        }
    )
)
