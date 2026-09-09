#!/usr/bin/env python3
"""Static cross-repository check for the V7 C# monthly scheduling client."""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.core.contracts import (
    CONSTRUCTION_ORDER_KEY,
    NUMERIC_SEMANTICS_KEY,
    SolverPolicy,
)
from apsgo_v7_service import month_scheduling

EXPECTED_PROPERTIES = {
    "ApsgoV7MonthPeriod": {"period_id", "sequence"},
    "ApsgoV7MonthOrder": set(month_scheduling._ORDER_FIELDS),
    "ApsgoV7MonthSolveRequest": set(month_scheduling._ROOT_FIELDS),
    "ApsgoV7MissingGrade": {"normalized_grade", "source_order_id", "source_grade"},
    "ApsgoV7GradePreparationReport": {
        "grade_dictionary_fingerprint",
        "input_order_count",
        "matched_order_count",
        "missing_order_count",
        "missing_grades",
        "report_fingerprint",
    },
    "ApsgoV7MonthQualityValue": {"criterion_id", "metric_key", "value"},
    "ApsgoV7MonthAuditSummary": {"passed", "core", "result"},
    "ApsgoV7MonthViolation": {
        "rule_id",
        "scope",
        "subject_id",
        "reason_code",
        "message",
        "disposition",
        "severity",
    },
    "ApsgoV7SplitLineage": {
        "partition_id",
        "parent_node_id",
        "parent_source_order_id",
        "source_resource_id",
        "source_period",
        "origin_assigned_period",
        "split_mode",
        "target_assigned_period",
        "accepted_source_sequence",
        "parent_weight",
        "piece_index",
        "piece_count",
        "authorization_rule_id",
        "authorization_rule_version",
        "authorization_decision_fingerprint",
        "reason_code",
    },
    "ApsgoV7VirtualLineage": {
        "prototype_id",
        "purpose",
        "related_partition_id",
        "accepted_sequence",
    },
    "ApsgoV7MonthSolveRow": {
        "node_id",
        "source_order_id",
        "source_resource_id",
        "source_period",
        "assigned_period",
        "chain_id",
        "chain_sequence",
        "node_sequence",
        "weight",
        "width",
        "thickness",
        "min_temperature",
        "max_temperature",
        "grade",
        "grade_class",
        "hot_roll_grade",
        "soft_hard_class",
        "material_role",
        "split_lineage",
        "virtual_lineage",
        "width_warning",
        "thickness_warning",
        "temperature_warning",
        "chain_warning",
    },
    "ApsgoV7MonthSolveResponse": {
        "contract_version",
        "request_id",
        "status",
        "stop_reason",
        "publishable",
        "active_rule_set_version_id",
        "rule_set_version",
        "rule_set_fingerprint",
        "grade_dictionary_fingerprint",
        "raw_request_fingerprint",
        "typed_request_fingerprint",
        "request_fingerprint",
        "binding_fingerprint",
        "result_fingerprint",
        "bound_result_fingerprint",
        "preparation_report",
        "quality",
        "metrics",
        "issues",
        "audit_summary",
        "run_manifest",
        "violations",
        "rows",
    },
    "ApsgoV7MonthSolveApiError": {
        "code",
        "message",
        "request_id",
        "expected_active_version_id",
        "current_active_version_id",
        "issues",
    },
    "ApsgoV7MonthSolveApiErrorEnvelope": {"error"},
}


def read_utf8(path: Path) -> str:
    payload = path.read_bytes()
    if payload.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is not allowed: {path}")
    return payload.decode("utf-8")


def class_block(source: str, name: str) -> str:
    match = re.search(rf"\bclass\s+{re.escape(name)}\b", source)
    if match is None:
        raise AssertionError(f"missing C# class: {name}")
    start = source.find("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unclosed C# class: {name}")


def json_properties(source: str, name: str) -> set[str]:
    values = re.findall(r'\[JsonProperty\("([^"]+)"', class_block(source, name))
    if len(values) != len(set(values)):
        raise AssertionError(f"duplicate JsonProperty in {name}")
    return set(values)


def require(source: str, text: str, label: str) -> None:
    if text not in source:
        raise AssertionError(f"missing {label}: {text}")


def verify_csharp(csharp_root: Path) -> None:
    project = csharp_root / "SchedApp"
    scheduling = read_utf8(project / "ApsgoV7SchedulingApiClient.cs")
    configuration = read_utf8(project / "ApsgoV7Configuration.cs")
    rule_client = read_utf8(project / "ApsgoV7RuleApiClient.cs")
    app_config = read_utf8(project / "App.config")
    project_xml = read_utf8(project / "SchedApp.csproj")

    for name, expected in EXPECTED_PROPERTIES.items():
        actual = json_properties(scheduling, name)
        if actual != expected:
            raise AssertionError(
                f"{name} fields differ: missing={sorted(expected - actual)}, "
                f"extra={sorted(actual - expected)}"
            )

    issue_fields = json_properties(rule_client, "ApsgoV7RuleApiIssue")
    if issue_fields != {"code", "phase", "field_path", "subject_id", "message", "severity"}:
        raise AssertionError("ApsgoV7RuleApiIssue fields differ from DiagnosticIssue")
    require(
        class_block(rule_client, "ApsgoV7RuleApiIssue"),
        'JsonProperty("field_path", Required = Required.AllowNull)',
        "nullable diagnostic field_path",
    )

    require(configuration, "public static Uri LoadBaseUri()", "independent V7 base URI loader")
    require(rule_client, "ApsgoV7Configuration.LoadBaseUri()", "rule client base URI reuse")
    if "ApsgoV7Configuration.Load().BaseUri" in rule_client:
        raise AssertionError("rule client must not require monthly solve settings")
    if "BACKEND_ALGORITHM_URL" in rule_client:
        raise AssertionError("V7 rule client still references the legacy backend setting")

    for text, label in (
        ("CancellationToken cancellationToken", "cancellation token"),
        ("(HttpStatusCode)422", "complete 422 response branch"),
        ("IsCompleteSolveResponse(responseJson)", "422 body discriminator"),
        (
            "response.ActiveRuleSetVersionId != expectedActiveVersionId",
            "active rule version response check",
        ),
        ("expected != actual", "request identity response check"),
    ):
        require(scheduling, text, label)
    if re.search(r"\bRetry\b|Thread\.Sleep|Task\.Delay", scheduling):
        raise AssertionError("scheduling client must not retry automatically")

    config = ET.fromstring(app_config)
    values = {
        item.attrib["key"]: item.attrib["value"]
        for item in config.findall("./appSettings/add")
    }
    expected_values = {
        "PipelineV7ApiBaseUrl": "http://192.168.4.42:8001",
        "PipelineV7MonthlySolvePath": "/api/v1/scheduling/GQGA4/default/month/solve",
        "PipelineV7MonthlySolveTimeoutSeconds": "370",
    }
    for key, value in expected_values.items():
        if values.get(key) != value:
            raise AssertionError(f"unexpected {key}: {values.get(key)!r}")

    tree = ET.fromstring(project_xml)
    namespace = {"msb": "http://schemas.microsoft.com/developer/msbuild/2003"}
    includes = [item.attrib["Include"] for item in tree.findall(".//msb:Compile", namespace)]
    for name in (
        "ApsgoV7Configuration.cs",
        "ApsgoV7RuleApiClient.cs",
        "ApsgoV7SchedulingApiClient.cs",
    ):
        if includes.count(name) != 1:
            raise AssertionError(f"project must compile {name} exactly once")


def verify_python_accepts_client_shape() -> None:
    request_id = "00000000-0000-4000-8000-000000000976"
    payload = {
        "contract_version": month_scheduling.MONTH_SOLVE_CONTRACT_VERSION,
        "request_id": request_id,
        "expected_active_version_id": 1,
        "periods": [{"period_id": "BR_00000001", "sequence": 0}],
        "orders": [
            {
                "source_order_id": "合同-001",
                "source_period": "BR_00000001",
                "is_virtual": False,
                "weight": Decimal("600.1250"),
                "grade": "DC01",
                "grade_class": None,
                "hot_roll_grade": "DC01",
                "width": Decimal("1000.25"),
                "thickness": Decimal("0.8"),
                "min_temperature": Decimal("700"),
                "max_temperature": Decimal("800"),
                "customer_grade": "战略客户",
                "customer_name": "示例客户",
                "execution_standard": None,
                "surface_grade": None,
            }
        ],
    }
    policy = SolverPolicy(
        seed=590531,
        total_time_limit_seconds=Decimal("20"),
        finalization_reserve_seconds=Decimal("2"),
        candidate_check_limit=100,
        construction_order_key=CONSTRUCTION_ORDER_KEY,
        numeric_semantics_key=NUMERIC_SEMANTICS_KEY,
        whole_chain_pair_scan_slack_weight=Decimal("40"),
        maximum_virtual_bridge_nodes=2,
    )
    parsed = month_scheduling.loads_month_solve_request(
        dumps_exact_json(payload),
        policy,
    )
    if parsed.task_input.request_id != request_id:
        raise AssertionError("Python did not preserve the C# request identity")
    order = parsed.task_input.orders[0]
    if order.source_order_id != "合同-001" or order.weight != Decimal("600.1250"):
        raise AssertionError("Python did not preserve C# Unicode or decimal request values")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csharp-root", type=Path, required=True)
    args = parser.parse_args()
    verify_csharp(args.csharp_root.resolve())
    verify_python_accepts_client_shape()
    print("stage_06_csharp_client_contract: pass")


if __name__ == "__main__":
    main()
