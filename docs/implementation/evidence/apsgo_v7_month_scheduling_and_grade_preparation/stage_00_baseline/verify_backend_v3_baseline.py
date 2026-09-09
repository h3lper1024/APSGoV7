"""Read-only verification for the V7 soft/hard-grade migration baseline."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sqlite3
import sys
import types
from collections import Counter
from pathlib import Path


EXPECTED_HASHES = {
    "input_orders.csv": "d178cc19ac6ef2807962beb110333f8702c8e2eeec153f9b0f94236913060941",
    "optimization_problem.json": "8edb7f4110384af14d58e9bdc7caec490f3de053401173db952854275966abdc",
    "aps_rule_dsl.sqlite3": "2c4e44c4b4c2060cb54890217ea7097164b4c88e25a452796a931883df4cce7e",
    "sqlite_repository.py": "ed10349638614d8b9cfc4c8b005130905e35b02830fe6269aca66317ed0ee4ab",
    "grade_dictionary.py": "b64892659fb81ba6eb97e5216f209c7141409f498e03f2962623ef0b5b51a8ed",
    "loader.py": "f5d52f5616df40c328139e21988ea105b21dc9e1b1fc78da3e9f8f9ba180a090",
    "field_mapping.yaml": "d2940bf6c9d686b6eed1aa37a6870210cdc0b4500589ae176a0cb4fe31a45f5d",
}
EXPECTED_DICTIONARY_PROJECTION = "42e398452fa9c785b41e3fc7d528d5a1a7a0553f57d2792d9e87c0b560f15614"
EXPECTED_COMPARISON = "460ddb273fd36e9d8fb50c3feedaef460aa4457fef0fcfc425f19eac607ed017"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _decode_frozen_value(value):
    if not isinstance(value, dict) or "$type" not in value:
        return value
    if value["$type"] == "dataclass":
        return {name: _decode_frozen_value(item) for name, item in value["fields"]}
    if value["$type"] in {"tuple", "list"}:
        return [_decode_frozen_value(item) for item in value["items"]]
    if value["$type"] in {"enum", "decimal", "datetime"}:
        return value["value"]
    raise ValueError(f"unsupported frozen value type: {value['$type']}")


def _load_v3_modules(v3_root: Path):
    package = types.ModuleType("pipeline_v3")
    package.__path__ = [str(v3_root)]
    package.__package__ = "pipeline_v3"
    sys.modules["pipeline_v3"] = package
    return (
        importlib.import_module("pipeline_v3.utils.loader"),
        importlib.import_module("pipeline_v3.rules_engine.grade_dictionary"),
        importlib.import_module("pipeline_v3.rules_engine.db.sqlite_repository"),
    )


def _dictionary_summary(database_path: Path) -> dict:
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM aps_gqga4_grade_dictionary
                WHERE product_line_code = 'GQGA4'
                ORDER BY UPPER(TRIM(grade)), id
                """
            )
        ]
    projection = []
    for row in rows:
        projection.append(
            {
                key: value
                for key, value in row.items()
                if key not in {"id", "created_at", "updated_at"}
            }
        )
    normalized = [str(row["grade"]).strip().upper() for row in rows]
    counts = Counter(row["soft_hard_class"] for row in rows)
    return {
        "integrity_check": integrity,
        "row_count": len(rows),
        "enabled_count": sum(row["enabled"] == 1 for row in rows),
        "soft_count": counts["软钢"],
        "hard_count": counts["硬钢"],
        "empty_grade_count": sum(not grade for grade in normalized),
        "duplicate_normalized_grade_count": len(normalized) - len(set(normalized)),
        "canonical_projection_sha256": _canonical_sha256(projection),
    }


def verify(arguments) -> dict:
    v3_root = arguments.v3_root.resolve()
    database = arguments.v3_database.resolve()
    optimization_problem = arguments.optimization_problem.resolve()
    input_orders = arguments.input_orders.resolve()
    sources = {
        "input_orders.csv": input_orders,
        "optimization_problem.json": optimization_problem,
        "aps_rule_dsl.sqlite3": database,
        "sqlite_repository.py": v3_root / "rules_engine/db/sqlite_repository.py",
        "grade_dictionary.py": v3_root / "rules_engine/grade_dictionary.py",
        "loader.py": v3_root / "utils/loader.py",
        "field_mapping.yaml": v3_root / "config/field_mapping.yaml",
    }
    hashes = {name: _sha256(path) for name, path in sources.items()}
    if hashes != EXPECTED_HASHES:
        raise ValueError("baseline source hashes changed")

    loader, dictionary, repository_module = _load_v3_modules(v3_root)
    loaded = loader.load_order_nodes(input_orders)
    repository = repository_module.SqliteRuleRepository(
        db_path=database,
        init=False,
        read_only=True,
    )
    enriched = dictionary.enrich_nodes_with_grade_dictionary(
        loaded.nodes,
        product_line_code="GQGA4",
        repository=repository,
    )
    frozen = _decode_frozen_value(
        json.loads(optimization_problem.read_text(encoding="utf-8"))["optimization_problem"]
    )
    expected = {
        node["node_id"]["value"]: node["classifications"]["soft_or_hard"]
        for node in frozen["nodes"]
    }
    comparison = sorted(
        (
            {
                "source_order_id": node.source_order_id,
                "source_grade": node.grade,
                "normalized_grade": node.grade.strip().upper(),
                "actual_soft_hard_class": node.soft_hard_class or None,
                "expected_soft_hard_class": expected[node.source_order_id],
            }
            for node in enriched
        ),
        key=lambda item: item["source_order_id"],
    )
    comparison_hash = _canonical_sha256(comparison)
    differences = [
        item
        for item in comparison
        if item["actual_soft_hard_class"] != item["expected_soft_hard_class"]
    ]
    classes = Counter(item["actual_soft_hard_class"] for item in comparison)
    missing = [
        item["source_order_id"]
        for item in comparison
        if item["actual_soft_hard_class"] is None
    ]
    transition_nodes = [
        node
        for node in enriched
        if node.customer_grade.strip() != "战略客户"
        and node.hot_roll_grade.strip().upper() == "SPHC"
        and node.execution_standard.strip() == "Q/TB 305-2017"
    ]
    dictionary_summary = _dictionary_summary(database)
    if dictionary_summary != {
        "integrity_check": "ok",
        "row_count": 230,
        "enabled_count": 230,
        "soft_count": 131,
        "hard_count": 99,
        "empty_grade_count": 0,
        "duplicate_normalized_grade_count": 0,
        "canonical_projection_sha256": EXPECTED_DICTIONARY_PROJECTION,
    }:
        raise ValueError("GQGA4 source dictionary baseline changed")
    if (
        loaded.report.row_count != 531
        or loaded.report.node_count != 531
        or len(expected) != 531
        or len(comparison) != 531
        or differences
        or comparison_hash != EXPECTED_COMPARISON
        or classes != Counter({"软钢": 525, "硬钢": 4, None: 2})
        or sorted(missing) != ["0030124824-000050", "0030125170-000010"]
        or len(transition_nodes) != 61
        or any(node.soft_hard_class != "软钢" for node in transition_nodes)
    ):
        raise ValueError("531-order soft/hard-grade comparison baseline changed")
    return {
        "status": "pass",
        "source_hashes": hashes,
        "dictionary": dictionary_summary,
        "orders": {
            "row_count": loaded.report.row_count,
            "node_count": loaded.report.node_count,
            "total_weight": loaded.report.total_weight,
            "soft_count": classes["软钢"],
            "hard_count": classes["硬钢"],
            "missing_count": classes[None],
            "missing_source_order_ids": sorted(missing),
            "actual_transition_count": len(transition_nodes),
            "actual_transition_soft_count": sum(
                node.soft_hard_class == "软钢" for node in transition_nodes
            ),
            "comparison_difference_count": len(differences),
            "canonical_comparison_sha256": comparison_hash,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v3-root", type=Path, required=True)
    parser.add_argument("--v3-database", type=Path, required=True)
    parser.add_argument("--optimization-problem", type=Path, required=True)
    parser.add_argument("--input-orders", type=Path, required=True)
    print(json.dumps(verify(parser.parse_args()), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
