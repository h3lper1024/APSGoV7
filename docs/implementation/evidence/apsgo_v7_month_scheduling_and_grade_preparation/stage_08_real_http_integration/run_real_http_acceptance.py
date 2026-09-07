#!/usr/bin/env python3
"""Run the frozen 531-order GQGA4 request through a real V7 HTTP service."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[5]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from apsgo_scheduler.api.json_codec import dumps_exact_json  # noqa: E402
from apsgo_scheduler.core.contracts import SolverPolicy  # noqa: E402
from apsgo_v7_service.migrate_gqga4_grade_dictionary import (  # noqa: E402
    migrate_gqga4_grade_dictionary,
)
from apsgo_v7_service.month_scheduling import (  # noqa: E402
    MONTH_SOLVE_CONTRACT_VERSION,
    loads_month_solve_request,
)


GET_ACTIVE_RULES_PATH = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
MONTH_SOLVE_PATH = "/api/v1/scheduling/GQGA4/default/month/solve"
MIGRATION_OPERATION_ID = "00000000-0000-4000-8000-000000000808"
DEFAULT_REQUEST_ID = "00000000-0000-4000-8000-000000000531"
VALID_ROLES = frozenset(("normal_real", "actual_transition", "virtual_sphc"))
TRUE_VIRTUAL_VALUES = frozenset(("是", "1", "TRUE", "T", "虚拟"))
FALSE_VIRTUAL_VALUES = frozenset(("", "否", "0", "FALSE", "F", "NO", "N", "真实"))
TRUNCATED_STOP_REASONS = frozenset(
    (
        "candidate_limit_reached",
        "search_time_limit_reached",
        "finalization_time_limit_reached",
        "user_cancelled",
    )
)
HEX_DIGITS = frozenset("0123456789abcdef")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_file_state(path: Path) -> dict[str, dict[str, object]]:
    state = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        state[candidate.name] = (
            {"exists": True, "size": candidate.stat().st_size, "sha256": sha256(candidate)}
            if candidate.exists()
            else {"exists": False}
        )
    return state


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def first_text(*values) -> str | None:
    for value in values:
        text = optional_text(value)
        if text is not None:
            return text
    return None


def decimal_text(value, label: str) -> Decimal:
    text = optional_text(value)
    require(text is not None, f"{label} is blank")
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise AssertionError(f"{label} is not decimal: {text}") from error
    require(number.is_finite(), f"{label} is not finite")
    return number


def optional_positive_decimal(value) -> Decimal | None:
    if value is None:
        return None
    number = Decimal(str(value))
    require(number.is_finite(), "physical value is not finite")
    return number if number > 0 else None


def period_number(value, source_order_id: str) -> int:
    text = optional_text(value) or ""
    parts = text.split("-")
    require(len(parts) in (1, 3), f"{source_order_id}: invalid RollPos {text!r}")
    require(all(part.strip().isdigit() and int(part.strip()) > 0 for part in parts), f"{source_order_id}: invalid RollPos {text!r}")
    return int(parts[0].strip())


def is_virtual_input(row: sqlite3.Row, source_order_id: str) -> bool:
    if optional_text(row["VirtualBelongs"]) is not None:
        return True
    value = (optional_text(row["IsVirtual"]) or "").upper()
    if value in FALSE_VIRTUAL_VALUES:
        return False
    if value in TRUE_VIRTUAL_VALUES:
        return True
    raise AssertionError(f"{source_order_id}: unknown IsVirtual value {value!r}")


def expected_material_role(order: dict) -> str:
    transition = (
        (order["customer_grade"] or "") != "战略客户"
        and (order["hot_roll_grade"] or "").upper() == "SPHC"
        and (order["execution_standard"] or "") == "Q/TB 305-2017"
    )
    return "actual_transition" if transition else "normal_real"


def load_csharp_orders(
    database_path: Path,
    version: str,
) -> tuple[list[dict], list[dict]]:
    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT Id, OrderNumber, RollPos, RollSeq, CoatingShortage,
                   Grade, GradeClass, HMGrade, Width, Thickness,
                   MinTemperature, MaxTemperature, CustomerGrade,
                   TerminalCustomerName, StrategicCustomer, CustomerName,
                   ExecutionStandard, SurfaceGrade, SurfaceQuality,
                   IsVirtual, VirtualBelongs
              FROM SchedRecord
             WHERE Version = ? AND ProductLine = ? AND Step = ?
             ORDER BY RollSeq, Id
            """,
            (version, "四镀锌", "配置规则"),
        ).fetchall()
    require(len(rows) == 531, f"expected 531 C# records, got {len(rows)}")

    orders = []
    period_numbers = []
    for row in rows:
        weight = decimal_text(row["CoatingShortage"], f"row {row['Id']} CoatingShortage")
        require(weight > 0, f"row {row['Id']} has nonpositive CoatingShortage")
        source_order_id = first_text(row["OrderNumber"], row["Id"])
        require(source_order_id is not None, f"row {row['Id']} has no source identity")
        require(not is_virtual_input(row, source_order_id), f"{source_order_id}: positive virtual input")
        number = period_number(row["RollPos"], source_order_id)
        period_numbers.append(number)
        minimum = optional_positive_decimal(row["MinTemperature"])
        maximum = optional_positive_decimal(row["MaxTemperature"])
        require(minimum is None or maximum is None or minimum <= maximum, f"{source_order_id}: reversed temperature range")
        order = {
            "source_order_id": source_order_id,
            "source_period": f"BR_{number:08d}",
            "is_virtual": False,
            "weight": weight,
            "grade": first_text(row["Grade"]),
            "grade_class": optional_text(row["GradeClass"]),
            "hot_roll_grade": optional_text(row["HMGrade"]),
            "width": optional_positive_decimal(row["Width"]),
            "thickness": optional_positive_decimal(row["Thickness"]),
            "min_temperature": minimum,
            "max_temperature": maximum,
            "customer_grade": optional_text(row["CustomerGrade"]),
            "customer_name": first_text(
                row["TerminalCustomerName"],
                row["StrategicCustomer"],
                row["CustomerName"],
            ),
            "execution_standard": optional_text(row["ExecutionStandard"]),
            "surface_grade": first_text(row["SurfaceGrade"], row["SurfaceQuality"]),
        }
        require(order["grade"] is not None, f"{source_order_id}: grade is blank")
        orders.append(order)

    identities = [order["source_order_id"] for order in orders]
    require(len(set(identities)) == len(identities), "C# input contains duplicate source_order_id")
    periods = [
        {"period_id": f"BR_{number:08d}", "sequence": sequence}
        for sequence, number in enumerate(sorted(set(period_numbers)))
    ]
    return orders, periods


def frozen_value(row: dict[str, str], name: str, *, numeric: bool = False):
    value = optional_text(row[name])
    return decimal_text(value, name) if value is not None and numeric else value


def compare_frozen_input(orders: list[dict], frozen_input: Path) -> dict[str, object]:
    with frozen_input.open("r", encoding="utf-8", newline="") as stream:
        frozen = list(csv.DictReader(stream))
    require(len(frozen) == len(orders) == 531, "frozen and C# order counts differ")
    mapping = (
        ("source_order_id", "source_order_id", False),
        ("source_period", "source_period", False),
        ("weight", "连镀欠交", True),
        ("grade", "牌号", False),
        ("grade_class", "钢种大类", False),
        ("hot_roll_grade", "热轧牌号", False),
        ("width", "宽度", True),
        ("thickness", "厚度", True),
        ("min_temperature", "均热段温度最小值", True),
        ("max_temperature", "均热段温度最大值", True),
        ("customer_grade", "客户等级", False),
        ("customer_name", "战略客户名称", False),
        ("execution_standard", "执行标准", False),
        ("surface_grade", "表面等级", False),
    )
    mismatches = []
    for index, (actual, expected) in enumerate(zip(orders, frozen)):
        for actual_name, expected_name, numeric in mapping:
            expected_value = frozen_value(expected, expected_name, numeric=numeric)
            if actual[actual_name] != expected_value:
                mismatches.append(
                    {
                        "index": index,
                        "source_order_id": actual["source_order_id"],
                        "field": actual_name,
                        "csharp": actual[actual_name],
                        "frozen": expected_value,
                    }
                )
    require(not mismatches, f"C# input differs from frozen CSV: {mismatches[:5]}")
    return {"order_count": len(orders), "compared_field_count": len(mapping), "mismatch_count": 0}


def load_policy(path: Path) -> tuple[dict, SolverPolicy]:
    data = read_json(path)
    require(isinstance(data, dict), "solver policy must be a JSON object")
    expected = {
        "seed",
        "total_time_limit_seconds",
        "finalization_reserve_seconds",
        "candidate_check_limit",
        "construction_order_key",
        "numeric_semantics_key",
        "whole_chain_pair_scan_slack_weight",
        "maximum_virtual_bridge_nodes",
    }
    require(set(data) == expected, "solver policy fields changed")
    policy = SolverPolicy(
        seed=data["seed"],
        total_time_limit_seconds=Decimal(str(data["total_time_limit_seconds"])),
        finalization_reserve_seconds=Decimal(str(data["finalization_reserve_seconds"])),
        candidate_check_limit=data["candidate_check_limit"],
        construction_order_key=data["construction_order_key"],
        numeric_semantics_key=data["numeric_semantics_key"],
        whole_chain_pair_scan_slack_weight=Decimal(
            str(data["whole_chain_pair_scan_slack_weight"])
        ),
        maximum_virtual_bridge_nodes=data["maximum_virtual_bridge_nodes"],
    )
    return data, policy


def backup_database(source_path: Path, target_path: Path) -> None:
    require(not target_path.exists(), f"temporary database already exists: {target_path}")
    source_uri = f"file:{source_path.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    with sqlite3.connect(target_path) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchone() == ("ok",), "temporary database copy is corrupt")


def write_service_configuration(path: Path, database_path: Path, port: int, policy: dict) -> None:
    path.write_text(
        "\n".join(
            (
                f"database_path: {database_path}",
                "database_timeout_seconds: 5.0",
                "listen_host: 127.0.0.1",
                f"listen_port: {port}",
                "monthly_solve:",
                f"  seed: {policy['seed']}",
                f"  total_time_limit_seconds: {policy['total_time_limit_seconds']}",
                f"  finalization_reserve_seconds: {policy['finalization_reserve_seconds']}",
                f"  candidate_check_limit: {policy['candidate_check_limit']}",
                "  whole_chain_pair_scan_slack_weight: "
                f"{policy['whole_chain_pair_scan_slack_weight']}",
                f"  maximum_virtual_bridge_nodes: {policy['maximum_virtual_bridge_nodes']}",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: bytes | None = None,
    timeout: float = 5,
) -> tuple[int, bytes, object]:
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    request = Request(base_url + path, data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            content = response.read()
    except HTTPError as error:
        status = error.code
        content = error.read()
    body = json.loads(content.decode("utf-8"), parse_float=Decimal)
    return status, content, body


def wait_for_server(process: subprocess.Popen, base_url: str, log_path: Path) -> dict:
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            status, _, body = http_json(base_url, "GET", GET_ACTIVE_RULES_PATH)
            if status == 200 and isinstance(body, dict):
                return body
            last_error = RuntimeError(f"GET returned HTTP {status}")
        except (ConnectionError, TimeoutError, URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.05)
    logs = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
    raise RuntimeError(f"V7 service did not become ready: {last_error}; log={logs}")


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def require_fingerprint(value, name: str) -> None:
    require(
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= HEX_DIGITS,
        f"{name} is not a lowercase SHA-256 identity",
    )


def validate_identity_and_quality(
    request: dict,
    parsed_request,
    active: dict,
    response: dict,
    quality_gate: dict,
) -> dict[str, object]:
    require(response.get("contract_version") == request["contract_version"], "response contract version changed")
    require(response.get("request_id") == request["request_id"], "response request_id changed")
    require(response.get("status") == "success", f"unexpected solve status: {response.get('status')}")
    require(response.get("publishable") is True, "response is not publishable")
    require(response.get("issues") == [], "successful response contains issues")
    require(response.get("violations") == [], "GQGA4 acceptance requires zero violations")
    require(response.get("active_rule_set_version_id") == active.get("active_version_id"), "active version identity changed")
    require(response.get("rule_set_version") == str(active.get("version_no")), "rule-set version changed")
    require(response.get("rule_set_fingerprint") == active.get("fingerprint"), "rule-set fingerprint changed")
    require(response.get("raw_request_fingerprint") == parsed_request.raw_request_fingerprint, "raw request fingerprint changed")
    require(response.get("typed_request_fingerprint") == parsed_request.typed_request_fingerprint, "typed request fingerprint changed")

    manifest = response.get("run_manifest")
    require(isinstance(manifest, dict), "run_manifest is missing")
    require(manifest.get("request_fingerprint") == response.get("request_fingerprint"), "manifest request fingerprint changed")
    require(manifest.get("rule_set_fingerprint") == response.get("rule_set_fingerprint"), "manifest rule-set fingerprint changed")
    require(manifest.get("stop_reason") == response.get("stop_reason"), "manifest stop reason changed")
    require(
        manifest.get("search_was_truncated")
        is (response.get("stop_reason") in TRUNCATED_STOP_REASONS),
        "search truncation flag disagrees with stop reason",
    )

    for name in (
        "rule_set_fingerprint",
        "grade_dictionary_fingerprint",
        "raw_request_fingerprint",
        "typed_request_fingerprint",
        "request_fingerprint",
        "binding_fingerprint",
        "result_fingerprint",
        "bound_result_fingerprint",
    ):
        require_fingerprint(response.get(name), name)
    for name in ("problem_fingerprint", "policy_fingerprint", "trace_fingerprint", "deterministic_run_fingerprint"):
        require_fingerprint(manifest.get(name), f"run_manifest.{name}")

    metrics = response.get("metrics")
    quality = response.get("quality")
    quality_spec = active.get("quality_spec")
    require(isinstance(metrics, dict), "metrics are missing")
    require(isinstance(quality, list) and isinstance(quality_spec, list), "quality contract is missing")
    require(len(quality) == len(quality_spec) == 7, "expected seven quality criteria")
    for index, (actual, declared) in enumerate(zip(quality, quality_spec)):
        require(actual.get("criterion_id") == declared.get("criterion_id"), f"quality[{index}] criterion changed")
        require(actual.get("metric_key") == declared.get("metric_key"), f"quality[{index}] metric changed")
        require(actual.get("value") == metrics.get(actual["metric_key"]), f"quality[{index}] value differs from metrics")

    require(Decimal(str(metrics["underweight_chain_count"])) <= Decimal(str(quality_gate["maximum_underweight_chain_count"])), "underweight chain gate failed")
    require(Decimal(str(metrics["underweight_total_gap"])) == 0, "underweight gap must be zero")
    require(Decimal(str(metrics["prohibited_violation_count"])) <= Decimal(str(quality_gate["maximum_prohibited_violation_count"])), "prohibited violation gate failed")
    require(Decimal(str(metrics["late_original_due_period_move_count"])) <= Decimal(str(quality_gate["maximum_late_original_due_period_move_count"])), "late source-period gate failed")

    counters = manifest.get("counters")
    require(isinstance(counters, dict), "run_manifest counters are missing")
    candidate_count = counters.get("candidate_check_count")
    require(type(candidate_count) is int and 0 <= candidate_count <= parsed_request.task_input.policy.candidate_check_limit, "candidate check budget exceeded")
    if response.get("stop_reason") == "candidate_limit_reached":
        require(candidate_count == parsed_request.task_input.policy.candidate_check_limit, "candidate stop did not consume the configured limit")
    return {
        "status": response["status"],
        "stop_reason": response["stop_reason"],
        "candidate_check_count": candidate_count,
        "quality": quality,
        "metric_count": len(metrics),
    }


def validate_audits(response: dict) -> dict[str, object]:
    audit = response.get("audit_summary")
    require(isinstance(audit, dict) and audit.get("passed") is True, "dual audit did not pass")
    core = audit.get("core")
    result = audit.get("result")
    require(isinstance(core, dict) and isinstance(result, dict), "audit details are missing")
    require(core.get("status") == "completed" and core.get("passed") is True, "core audit did not pass")
    require(core.get("invariant_failure_codes") == [], "core invariants failed")
    require(core.get("action_authorization_failure_codes") == [], "action authorization failed")
    require(core.get("search_evaluation_matches") is True, "search and audited evaluations differ")
    require(result.get("status") == "completed" and result.get("passed") is True, "result audit did not pass")
    require(result.get("failure_codes") == [], "result audit contains failures")
    require(core.get("derived_resource_fingerprint") == result.get("resource_fingerprint"), "resource identities differ between audits")
    for owner, fields in (
        (core, ("audited_evaluation_fingerprint", "derived_resource_fingerprint", "report_fingerprint")),
        (result, ("plan_fingerprint", "resource_fingerprint", "draft_fingerprint", "report_fingerprint")),
    ):
        for name in fields:
            require_fingerprint(owner.get(name), f"audit.{name}")
    return {
        "passed": True,
        "core_report_fingerprint": core["report_fingerprint"],
        "result_report_fingerprint": result["report_fingerprint"],
    }


def validate_split_partitions(
    rows: list[dict],
    input_by_source: dict[str, dict],
    response: dict,
) -> dict[str, int]:
    partitions = defaultdict(list)
    separator_counts = Counter()
    for row in rows:
        lineage = row.get("split_lineage")
        virtual_lineage = row.get("virtual_lineage")
        if lineage is not None:
            require(isinstance(lineage, dict), "split lineage must be an object")
            partitions[lineage.get("partition_id")].append((row, lineage))
        if isinstance(virtual_lineage, dict) and virtual_lineage.get("purpose") == "split_separator":
            partition_id = virtual_lineage.get("related_partition_id")
            require(isinstance(partition_id, str) and partition_id, "split separator has no partition")
            separator_counts[partition_id] += 1

    mode_counts = Counter()
    for partition_id, pieces in partitions.items():
        require(isinstance(partition_id, str) and partition_id, "split partition identity is blank")
        first_row, first = pieces[0]
        piece_count = first.get("piece_count")
        require(type(piece_count) is int and piece_count >= 2, f"{partition_id}: invalid piece_count")
        require(len(pieces) == piece_count, f"{partition_id}: incomplete split partition")
        require(sorted(item[1].get("piece_index") for item in pieces) == list(range(1, piece_count + 1)), f"{partition_id}: incomplete piece indexes")
        common = {key: value for key, value in first.items() if key != "piece_index"}
        require(all({key: value for key, value in lineage.items() if key != "piece_index"} == common for _, lineage in pieces), f"{partition_id}: split lineage differs between pieces")

        source_id = first.get("parent_source_order_id")
        require(source_id in input_by_source, f"{partition_id}: unknown parent source")
        source = input_by_source[source_id]
        mode = first.get("split_mode")
        require(mode in ("same_period_split", "future_borrow_return"), f"{partition_id}: invalid split mode")
        mode_counts[mode] += 1
        require(first.get("parent_node_id") == source_id, f"{partition_id}: parent node changed")
        require(first.get("source_resource_id") == source_id, f"{partition_id}: source resource changed")
        require(first.get("source_period") == source["source_period"], f"{partition_id}: source period changed")
        require(first.get("target_assigned_period") == source["source_period"], f"{partition_id}: target period changed")
        require(first.get("parent_weight") == source["weight"], f"{partition_id}: parent weight changed")
        require(sum(Decimal(str(row["weight"])) for row, _ in pieces) == source["weight"], f"{partition_id}: piece weight is not conserved")
        require(all(row.get("source_order_id") == source_id for row, _ in pieces), f"{partition_id}: row source changed")
        require(all(row.get("assigned_period") == source["source_period"] for row, _ in pieces), f"{partition_id}: pieces were not returned to the source period")
        require(type(first.get("accepted_source_sequence")) is int and first["accepted_source_sequence"] >= 1, f"{partition_id}: invalid acceptance sequence")
        require_fingerprint(first.get("authorization_decision_fingerprint"), f"{partition_id}.authorization_decision_fingerprint")
        for name in ("authorization_rule_id", "authorization_rule_version", "reason_code"):
            require(isinstance(first.get(name), str) and first[name].strip(), f"{partition_id}: {name} is blank")
        if mode == "same_period_split":
            require(first.get("origin_assigned_period") == source["source_period"], f"{partition_id}: same-period origin changed")
        else:
            require(first.get("origin_assigned_period") != source["source_period"], f"{partition_id}: future-borrow split has no borrowed origin")
        require(separator_counts[partition_id] == piece_count - 1, f"{partition_id}: separator count differs from piece_count - 1")

    require(set(separator_counts) == set(partitions), "orphan split separator exists")
    core = response["audit_summary"]["core"]
    counters = response["run_manifest"]["counters"]
    actual = {
        "split_count": len(partitions),
        "same_period_split_count": mode_counts["same_period_split"],
        "future_borrow_return_count": mode_counts["future_borrow_return"],
    }
    require(actual["split_count"] == core.get("audited_split_count") == counters.get("accepted_split_count"), "split totals differ from audit or manifest")
    require(actual["same_period_split_count"] == core.get("audited_same_period_split_count") == counters.get("accepted_same_period_split_count"), "same-period split totals differ")
    require(actual["future_borrow_return_count"] == core.get("audited_future_borrow_return_count") == counters.get("accepted_future_borrow_return_count"), "future-borrow split totals differ")
    return actual


def validate_rows(
    orders: list[dict],
    periods: list[dict],
    response: dict,
    quality_gate: dict,
    conservation_path: Path,
) -> dict[str, object]:
    rows = response.get("rows")
    require(isinstance(rows, list) and rows, "publishable response has no rows")
    input_by_source = {order["source_order_id"]: order for order in orders}
    period_sequence = {period["period_id"]: period["sequence"] for period in periods}
    output_by_source = defaultdict(list)
    seen_nodes = set()
    seen_chains = set()
    chain_count_by_period = Counter()
    current_chain = None
    current_period = None
    current_chain_sequence = None
    expected_node_sequence = 0

    for row in rows:
        require(isinstance(row, dict), "response row must be an object")
        node_id = row.get("node_id")
        require(isinstance(node_id, str) and node_id and node_id not in seen_nodes, "empty or duplicate node_id")
        seen_nodes.add(node_id)
        role = row.get("material_role")
        require(role in VALID_ROLES, f"{node_id}: unsupported material role")
        assigned_period = row.get("assigned_period")
        require(assigned_period in period_sequence, f"{node_id}: unknown assigned period")
        require(Decimal(str(row.get("weight"))) > 0, f"{node_id}: nonpositive weight")

        chain_id = row.get("chain_id")
        if chain_id != current_chain:
            require(isinstance(chain_id, str) and chain_id and chain_id not in seen_chains, f"{node_id}: repeated chain segment")
            seen_chains.add(chain_id)
            chain_count_by_period[assigned_period] += 1
            require(row.get("chain_sequence") == chain_count_by_period[assigned_period], f"{chain_id}: invalid chain sequence")
            current_chain = chain_id
            current_period = assigned_period
            current_chain_sequence = row["chain_sequence"]
            expected_node_sequence = 1
        else:
            expected_node_sequence += 1
            require(assigned_period == current_period and row.get("chain_sequence") == current_chain_sequence, f"{chain_id}: chain identity changed within a chain")
        require(row.get("node_sequence") == expected_node_sequence, f"{chain_id}: invalid node sequence")

        if role == "virtual_sphc":
            require(all(row.get(name) is None for name in ("source_order_id", "source_resource_id", "source_period", "split_lineage", "soft_hard_class")), f"{node_id}: virtual source fields are not null")
            lineage = row.get("virtual_lineage")
            require(isinstance(lineage, dict), f"{node_id}: virtual lineage is missing")
            require(lineage.get("purpose") in ("edge_bridge", "weight_fill", "split_separator"), f"{node_id}: invalid virtual purpose")
            require(type(lineage.get("accepted_sequence")) is int and lineage["accepted_sequence"] >= 1, f"{node_id}: invalid virtual sequence")
            for name in ("prototype_id",):
                require(isinstance(lineage.get(name), str) and lineage[name], f"{node_id}: {name} is blank")
            if lineage["purpose"] != "split_separator":
                require(lineage.get("related_partition_id") is None, f"{node_id}: non-separator refers to a partition")
            for name in ("width", "thickness", "min_temperature", "max_temperature", "hot_roll_grade"):
                require(row.get(name) is not None, f"{node_id}: virtual prototype field {name} is missing")
            continue

        source_id = row.get("source_order_id")
        require(source_id in input_by_source, f"{node_id}: unknown real source")
        source = input_by_source[source_id]
        require(row.get("source_resource_id") == source_id, f"{node_id}: source resource changed")
        require(row.get("source_period") == source["source_period"], f"{node_id}: source period changed")
        require(period_sequence[assigned_period] <= period_sequence[source["source_period"]], f"{node_id}: original order was delayed")
        require(role == expected_material_role(source), f"{node_id}: material role changed")
        if row.get("split_lineage") is None:
            require(node_id == source_id, f"{node_id}: unsplit node identity changed")
        output_by_source[source_id].append(row)

    require(set(output_by_source) == set(input_by_source), "real source coverage is incomplete")
    conservation_rows = []
    for order in orders:
        source_id = order["source_order_id"]
        pieces = output_by_source[source_id]
        output_weight = sum(Decimal(str(row["weight"])) for row in pieces)
        require(output_weight == order["weight"], f"{source_id}: source weight is not conserved")
        conservation_rows.append((source_id, order["weight"], output_weight, len(pieces), True))
    with conservation_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("source_order_id", "input_weight", "output_weight", "piece_count", "passed"))
        writer.writerows(conservation_rows)

    real_rows = [row for row in rows if row["material_role"] != "virtual_sphc"]
    virtual_rows = [row for row in rows if row["material_role"] == "virtual_sphc"]
    real_weight = sum(Decimal(str(row["weight"])) for row in real_rows)
    virtual_weight = sum(Decimal(str(row["weight"])) for row in virtual_rows)
    total_weight = real_weight + virtual_weight
    ratio = virtual_weight / total_weight if total_weight else Decimal(0)
    metrics = response["metrics"]
    require(len(input_by_source) == quality_gate["expected_real_order_count"], "input source count differs from gate")
    require(real_weight == Decimal(str(quality_gate["expected_real_weight"])), "real output weight differs from gate")
    require(virtual_weight == Decimal(str(metrics["generated_virtual_weight"])), "virtual output weight differs from metrics")
    require(ratio == Decimal(str(metrics["virtual_output_weight_ratio"])), "virtual output ratio differs from metrics")
    require(ratio <= Decimal(str(quality_gate["maximum_virtual_output_weight_ratio"])), "virtual output ratio gate failed")

    split_counts = validate_split_partitions(rows, input_by_source, response)
    missing_ids = {
        item["source_order_id"]
        for item in response["preparation_report"]["missing_grades"]
    }
    source_classes = {}
    for source_id, source_rows in output_by_source.items():
        values = {row.get("soft_hard_class") for row in source_rows}
        require(len(values) == 1, f"{source_id}: split pieces have different soft/hard classes")
        source_classes[source_id] = next(iter(values))
    require({source_id for source_id, value in source_classes.items() if value is None} == missing_ids, "missing dictionary sources differ from null output classifications")
    require(set(source_classes.values()) <= {"软钢", "硬钢", None}, "unknown soft/hard classification")
    class_counts = Counter(source_classes.values())
    role_counts = Counter(expected_material_role(order) for order in orders)
    require(class_counts == Counter({"软钢": 525, "硬钢": 4, None: 2}), "soft/hard source counts changed")
    require(role_counts == Counter({"normal_real": 470, "actual_transition": 61}), "material role counts changed")

    return {
        "input_source_count": len(orders),
        "real_row_count": len(real_rows),
        "virtual_row_count": len(virtual_rows),
        "chain_count": len(seen_chains),
        "real_weight": real_weight,
        "virtual_weight": virtual_weight,
        "virtual_output_weight_ratio": ratio,
        "source_class_counts": {"soft": class_counts["软钢"], "hard": class_counts["硬钢"], "missing": class_counts[None]},
        "material_role_counts": dict(sorted(role_counts.items())),
        **split_counts,
    }


def write_exact_json(path: Path, value) -> bytes:
    payload = dumps_exact_json(value).encode("utf-8")
    path.write_bytes(payload + b"\n")
    return payload


def existing_file(value: str) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"not a file: {value}")
    return path


def empty_output_directory(value: str) -> Path:
    path = Path(value).resolve(strict=False)
    if path.is_symlink():
        raise argparse.ArgumentTypeError("output directory must not be a symbolic link")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise argparse.ArgumentTypeError("output directory must be absent or empty")
    return path


def run_acceptance(arguments) -> dict[str, object]:
    started = time.monotonic()
    source_paths = {
        "csharp_database": arguments.csharp_database,
        "v3_rule_database": arguments.v3_rule_database,
        "formal_v7_rule_database": arguments.v7_rule_database,
    }
    states_before = {
        name: {"path": str(path), "files": database_file_state(path)}
        for name, path in source_paths.items()
    }
    orders, periods = load_csharp_orders(
        arguments.csharp_database,
        arguments.csharp_version,
    )
    frozen_comparison = compare_frozen_input(orders, arguments.frozen_input)
    policy_data, policy = load_policy(arguments.solver_policy)
    quality_gate = read_json(arguments.quality_gate)
    require(isinstance(quality_gate, dict), "quality gate must be a JSON object")
    require(policy.candidate_check_limit == 200000, "stage 8 requires the frozen 200000 candidate limit")
    require(policy.total_time_limit_seconds == Decimal("180"), "stage 8 requires the frozen 180-second solver budget")
    require([period["period_id"] for period in periods] == [
        "BR_00000001",
        "BR_00000002",
        "BR_00000003",
        "BR_00000006",
    ], "C# period directory changed")

    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    process = None
    with tempfile.TemporaryDirectory(prefix="apsgo_v7_stage08_") as directory:
        temporary = Path(directory)
        temporary_database = temporary / "rules.sqlite3"
        migration_backup = temporary / "rules.v1.backup.sqlite3"
        service_configuration = temporary / "service.yaml"
        service_log = temporary / "service.log"
        backup_database(arguments.v7_rule_database, temporary_database)
        with sqlite3.connect(temporary_database) as connection:
            require(connection.execute("PRAGMA user_version").fetchone() == (1,), "formal V7 copy is not schema v1")

        migration = migrate_gqga4_grade_dictionary(
            database_path=temporary_database.resolve(),
            v3_database_path=arguments.v3_rule_database,
            backup_path=migration_backup.resolve(),
            save_operation_id=MIGRATION_OPERATION_ID,
            expected_active_version_id=1,
        )
        saved = migration.save_result
        require(saved.saved_version_is_active, "temporary migrated version is not active")
        require(saved.previous_active_version_id == 1, "temporary migration did not start from version 1")
        require(len(migration.source.snapshot.entries) == 230, "temporary migration did not import 230 dictionary rows")
        with sqlite3.connect(temporary_database) as connection:
            require(connection.execute("PRAGMA user_version").fetchone() == (2,), "temporary migration did not produce schema v2")
            require(connection.execute("PRAGMA integrity_check").fetchone() == ("ok",), "temporary migrated database is corrupt")

        port = free_loopback_port()
        write_service_configuration(service_configuration, temporary_database, port, policy_data)
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONPATH": os.pathsep.join(
                    part
                    for part in (str(SRC), str(ROOT), environment.get("PYTHONPATH", ""))
                    if part
                ),
            }
        )
        base_url = f"http://127.0.0.1:{port}"
        with service_log.open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "apsgo_v7_service.app", "--config", str(service_configuration)],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                active = wait_for_server(process, base_url, service_log)
                require(active.get("active_version_id") == saved.active_rules.active_version_id, "HTTP active version differs from migration")
                require(active.get("fingerprint") == saved.active_rules.rule_set_spec.fingerprint, "HTTP rule fingerprint differs from migration")
                require(len(active.get("rules", [])) == 17, "active rule count changed")
                require(sum(rule.get("enabled") is True for rule in active["rules"]) == 16, "enabled rule count changed")
                require(len(active.get("quality_spec", [])) == 7, "quality criterion count changed")
                require(len(active.get("virtual_prototypes", [])) == 27, "virtual prototype count changed")

                request_data = {
                    "contract_version": MONTH_SOLVE_CONTRACT_VERSION,
                    "request_id": arguments.request_id,
                    "expected_active_version_id": active["active_version_id"],
                    "periods": periods,
                    "orders": orders,
                }
                request_payload = dumps_exact_json(request_data).encode("utf-8")
                parsed_request = loads_month_solve_request(request_payload, policy)
                write_exact_json(arguments.output_dir / "request.canonical.json", request_data)

                solve_started = time.monotonic()
                http_status, response_payload, response_data = http_json(
                    base_url,
                    "POST",
                    MONTH_SOLVE_PATH,
                    payload=request_payload,
                    timeout=240,
                )
                solve_elapsed = time.monotonic() - solve_started
            finally:
                stop_process(process)
                process = None

        if http_status != 200:
            logs = service_log.read_text(encoding="utf-8", errors="replace")[-6000:]
            raise AssertionError(
                f"month solve returned HTTP {http_status}: {response_payload[-4000:]!r}; log={logs}"
            )
        require(isinstance(response_data, dict), "month solve response must be a JSON object")
        canonical_response = dumps_exact_json(response_data).encode("utf-8")
        require(response_payload == canonical_response, "HTTP response is not exact canonical JSON")
        write_exact_json(arguments.output_dir / "response.canonical.json", response_data)

        preparation = response_data.get("preparation_report")
        require(isinstance(preparation, dict), "grade preparation report is missing")
        require(preparation.get("input_order_count") == 531, "grade preparation input count changed")
        require(preparation.get("matched_order_count") == 529, "grade preparation matched count changed")
        require(preparation.get("missing_order_count") == 2, "grade preparation missing count changed")
        require(preparation.get("grade_dictionary_fingerprint") == migration.source.snapshot.dictionary_fingerprint, "grade dictionary fingerprint changed during solve")
        require(response_data.get("grade_dictionary_fingerprint") == migration.source.snapshot.dictionary_fingerprint, "response dictionary fingerprint differs from migration")
        missing = preparation.get("missing_grades")
        require(
            isinstance(missing, list)
            and {(item.get("source_order_id"), item.get("source_grade")) for item in missing}
            == {
                ("0030124824-000050", "HC220YD+Z-GL"),
                ("0030125170-000010", "HC220YD+Z-GL"),
            },
            "missing grade details changed",
        )

        identity_quality = validate_identity_and_quality(
            request_data,
            parsed_request,
            active,
            response_data,
            quality_gate,
        )
        audit = validate_audits(response_data)
        rows = validate_rows(
            orders,
            periods,
            response_data,
            quality_gate,
            arguments.output_dir / "source_conservation.csv",
        )

    states_after = {
        name: {"path": str(path), "files": database_file_state(path)}
        for name, path in source_paths.items()
    }
    require(states_after == states_before, "one or more source databases or sidecars changed")
    report = {
        "status": "pass",
        "platform": {"system": os.uname().sysname, "release": os.uname().release, "machine": os.uname().machine},
        "python": sys.version.split()[0],
        "csharp_version": arguments.csharp_version,
        "source_database_states_before": states_before,
        "source_database_states_after": states_after,
        "source_databases_unchanged": True,
        "frozen_input": {"path": str(arguments.frozen_input), "sha256": sha256(arguments.frozen_input), **frozen_comparison},
        "solver_policy": {"path": str(arguments.solver_policy), "sha256": sha256(arguments.solver_policy), **policy_data},
        "quality_gate": {"path": str(arguments.quality_gate), "sha256": sha256(arguments.quality_gate)},
        "temporary_migration": {
            "previous_active_version_id": saved.previous_active_version_id,
            "active_version_id": saved.active_rules.active_version_id,
            "rule_set_fingerprint": saved.active_rules.rule_set_spec.fingerprint,
            "grade_dictionary_entry_count": len(migration.source.snapshot.entries),
            "grade_dictionary_fingerprint": migration.source.snapshot.dictionary_fingerprint,
            "backup_created": migration.backup.created if migration.backup else False,
            "temporary_files_retained": False,
        },
        "http": {
            "status": http_status,
            "request_bytes": len(request_payload),
            "request_sha256": hashlib.sha256(request_payload).hexdigest(),
            "response_bytes": len(response_payload),
            "response_sha256": hashlib.sha256(response_payload).hexdigest(),
            "solve_elapsed_seconds": Decimal(f"{solve_elapsed:.6f}"),
            "single_sample_within_180_seconds": solve_elapsed <= 180,
        },
        "preparation_report": preparation,
        "identity_and_quality": identity_quality,
        "audit": audit,
        "rows": rows,
        "full_acceptance_elapsed_seconds": Decimal(f"{time.monotonic() - started:.6f}"),
        "scope_note": "单次真实 HTTP 功能验收，不代替 20 对性能样本或 Windows 页面/回写验收。",
    }
    write_exact_json(arguments.output_dir / "acceptance_report.json", report)
    return report


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csharp-database", type=existing_file, required=True)
    parser.add_argument("--csharp-version", required=True)
    parser.add_argument("--v7-rule-database", type=existing_file, required=True)
    parser.add_argument("--v3-rule-database", type=existing_file, required=True)
    parser.add_argument("--frozen-input", type=existing_file, required=True)
    parser.add_argument(
        "--solver-policy",
        type=existing_file,
        default=ROOT / "tests/baselines/gqga4/gqga4_solver_policy.json",
    )
    parser.add_argument(
        "--quality-gate",
        type=existing_file,
        default=ROOT / "tests/baselines/gqga4/quality_gate.json",
    )
    parser.add_argument("--request-id", default=DEFAULT_REQUEST_ID)
    parser.add_argument("--output-dir", type=empty_output_directory, required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    arguments = parse_arguments(argv)
    try:
        report = run_acceptance(arguments)
    except Exception as error:
        print(dumps_exact_json({"status": "fail", "error": str(error)}), file=sys.stderr)
        return 1
    print(
        dumps_exact_json(
            {
                "status": report["status"],
                "http": report["http"],
                "preparation_report": {
                    name: report["preparation_report"][name]
                    for name in ("input_order_count", "matched_order_count", "missing_order_count")
                },
                "rows": report["rows"],
                "output_dir": str(arguments.output_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
