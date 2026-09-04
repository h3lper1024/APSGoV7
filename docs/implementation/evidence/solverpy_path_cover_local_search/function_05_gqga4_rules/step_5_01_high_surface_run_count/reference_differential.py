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
from apsgo_scheduler.core.rules.concrete import HighSurfaceRunCountRule

source = Path(sys.argv[1])
source_hash = sha256(source.read_bytes()).hexdigest()
assert source_hash == "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
reference = runpy.run_path(str(source), run_name="reference_high_surface_differential")
rule_id = "chain_high_surface_run_count_lte"
choices = ("FC", "FD", "LOW", "ACTUAL", "VIRTUAL")
context = RuleEvaluationContext(("period",), {"period": 0}, ("prototype",))


def compare(sequence, limit):
    old_nodes, new_nodes = [], []
    for index, kind in enumerate(sequence):
        virtual, actual = kind == "VIRTUAL", kind == "ACTUAL"
        surface = "FB" if kind == "LOW" else "FD" if kind == "FD" else "FC"
        old_nodes.append(
            replace(
                reference["_sentinel_node"](),
                node_id=f"n-{index}",
                surface_grade=surface,
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
                weight=Decimal(1),
                width=None,
                thickness=None,
                min_temperature=None,
                max_temperature=None,
                grade="",
                material_role=MaterialRole.GENERATED_VIRTUAL
                if virtual
                else MaterialRole.ACTUAL_TRANSITION
                if actual
                else MaterialRole.NORMAL_REAL,
                rule_attributes={"surface_grade": surface},
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
        and node.surface_grade in {"FC", "FD"},
        lambda node: Decimal(1),
        Decimal(limit),
        "high-surface real run count",
    )
    rule = HighSurfaceRunCountRule(
        rule_id,
        "高表面连续数量",
        RuleScope.CHAIN,
        True,
        "1",
        {"surface_grades": ["FC", "FD"], "max_run_count": limit},
    )
    result = rule.evaluate(ChainRuleSubject("C", Chain("C", tuple(new_nodes), "period")), context)
    expected = (
        tuple((item["subject_id"], Decimal(str(item["severity_value"]))) for item in old),
        int(old_maximum),
    )
    observed = (
        tuple((item.subject_id, item.severity) for item in result.violations),
        result.metrics[0].value,
    )
    assert len(result.metrics) == 1 and result.metrics[0].metric_key == "max_high_surface_run_count"
    assert all(
        item.reason_code == "high_surface_run_count"
        and item.disposition is RuleDisposition.PROHIBITED
        for item in result.violations
    )
    assert observed == expected, (sequence, limit, expected, observed)


count = 0
skipped = 0
for length in range(1, 5):
    for sequence in product(choices, repeat=length):
        for limit in (0, 2):
            if all(kind == "VIRTUAL" for kind in sequence):
                skipped += 1
                continue
            compare(sequence, limit)
            count += 1
for length in (6, 7):
    compare(("FC",) * length, 5)
    count += 1
print(
    json.dumps(
        {
            "status": "pass",
            "compared_cases": count,
            "skipped_all_virtual_cases": skipped,
            "first_difference": None,
            "reference_sha256": source_hash,
        }
    )
)
