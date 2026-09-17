"""A0 witnesses and pinned original-construction capture; never run local search.

The witness mode checks hand-written data only. Numeric mode requires a complete
checkout at the pinned src tree and records the actual production constructors.
Neither mode edits golden expectations, the rule database, or service settings.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests/baselines/initial_construction_earliest_start/a0_cases.json"
PINNED_REVISION = "08a4d24faf897afdcd6c20a796a6f85f145cbc1a"
PINNED_SRC_TREE = "d6c2952075634e78cf4c4a621a45f99f577ff184"


def environment() -> dict:
    versions = {}
    for name in ("numpy", "numba", "llvmlite", "pytest", "fastapi", "uvicorn"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "dependencies": versions}


def load_cases(path: Path = CASES) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "apsgo-initial-earliest-a0-v1":
        raise ValueError("unsupported witness schema")
    cases = payload.get("cases", [])
    ids = [case["case_id"] for case in cases]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("nonempty, unique case identities required")
    return payload


def clock_witness(case: dict, chains: list, periods: list) -> dict:
    """Independent sequential addition, NOT a chain readiness implementation."""
    orders = case["orders"]
    rows = [row for chain in chains for row in chain]
    if any(type(row) is not int for row in rows) or sorted(rows) != list(range(len(orders))):
        raise ValueError("each input row must occur exactly once")
    if not chains or len(chains) != len(periods) or any(not chain for chain in chains):
        raise ValueError("nonempty chains and one period per chain required")
    period_index = {value: i for i, value in enumerate(case["period_order"])}
    assigned = [period_index[value] for value in periods]
    if assigned != sorted(assigned):
        raise ValueError("production periods must not move backwards")
    for chain, period in zip(chains, assigned):
        if period != min(period_index[orders[row]["source_period"]] for row in chain):
            raise ValueError("chain period must retain the earliest source period")
    now, starts, ends, early = 0, [], [], []
    for row in rows:
        order = orders[row]
        duration, lower = order["duration_ms"], order["earliest_offset_ms"]
        if type(duration) is not int or duration <= 0:
            raise ValueError("positive integer milliseconds required")
        if case["earliest_enabled"] and type(lower) is not int:
            raise ValueError("enabled earliest rule requires an integer lower bound")
        starts.append(now)
        early.append(max(0, lower - now) if case["earliest_enabled"] else 0)
        now += duration
        ends.append(now)
    return {"rows": rows, "start_ms": starts, "end_ms": ends, "early_ms": early,
            "early_node_count": sum(value > 0 for value in early),
            "early_total_ms": sum(early), "total_duration_ms": now}


def validate_witness(case: dict) -> dict:
    baseline = case["expected_baseline"]
    count = len(case["orders"])
    if baseline["origin"] != "hand_calculated_not_observed":
        raise ValueError("hand-written expectations must not claim production observation")
    if sorted(baseline["ordered_rows"]) != list(range(count)):
        raise ValueError("construction order must cover every input")
    paths = baseline["cover_paths"]
    if sorted(row for path in paths for row in path) != list(range(count)):
        raise ValueError("path cover must cover every input once")
    edges = {tuple(edge) for edge in baseline["edges"]}
    rank = {row: position for position, row in enumerate(baseline["ordered_rows"])}
    if any(a not in rank or b not in rank or rank[a] >= rank[b] for a, b in edges):
        raise ValueError("construction edges must be forward and known")
    if any((a, b) not in edges for path in paths for a, b in zip(path, path[1:])):
        raise ValueError("path uses an absent construction edge")
    if len(baseline["chain_ids"]) != len(baseline["chains"]) or len(set(baseline["chain_ids"])) != len(baseline["chain_ids"]):
        raise ValueError("unique chain identities required")
    actual = clock_witness(case, baseline["chains"], baseline["chain_periods"])
    for key in ("start_ms", "early_ms"):
        if actual[key] != baseline[key]:
            raise ValueError(f"{case['case_id']}: hand-written {key} does not match addition")
    if baseline["candidate_check_count"] != 0:
        raise ValueError("initial construction must not consume search candidates")
    proposal = case.get("manual_proposal")
    counterfactual = None
    if proposal is not None:
        counterfactual = clock_witness(case, proposal["chains"], proposal["chain_periods"])
        for key in ("start_ms", "early_ms"):
            if counterfactual[key] != proposal[key]:
                raise ValueError(f"{case['case_id']}: incorrect manual proposal {key}")
    return {"case_id": case["case_id"], "baseline_hand_check": actual,
            "manual_counterfactual_hand_check": counterfactual,
            "production_observed": None}


def baseline_source_identity(root: Path = ROOT) -> dict:
    """No remote calls. Refuse changed code rather than rewriting old expectations."""
    def git(*args: str) -> str:
        completed = subprocess.run(["git", "-C", str(root), *args], check=True,
                                   capture_output=True, text=True, timeout=10)
        return completed.stdout.strip()
    head = git("rev-parse", "HEAD")
    src_tree = git("rev-parse", "HEAD:src")
    if src_tree != PINNED_SRC_TREE or git("status", "--porcelain", "--untracked-files=all", "--", "src", "tests", "pyproject.toml"):
        raise ValueError("baseline requires the pinned src tree and clean src/tests/pyproject")
    helpers = {
        "tests/core/test_numeric_evaluation.py": "5cede9492aa25681e695585fe60fe19cdcf676a7",
        "tests/core/test_numeric_rules.py": "5f669857f838a7b0c93a3ea8742171d84604a01c",
        "tests/app/test_input_normalizer.py": "eb9e73afc0e79e4e9a6a34408227f214af5519e4",
    }
    for relative, expected_blob in helpers.items():
        data = (root / relative).read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        if blob != expected_blob:
            raise ValueError(f"baseline fixture helper changed: {relative}")
    expected = {"numpy": "2.2.6", "numba": "0.65.1", "llvmlite": "0.47.0"}
    for name, version in expected.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"baseline dependency mismatch: {name} requires {version}")
    return {"head": head, "src_tree": src_tree, "pinned_revision": PINNED_REVISION}


def numeric_case(case: dict, suite: dict) -> dict:
    """Exercise repository fixtures and production graph/cover/layout/evaluation.

    Imports are lazy so witness-only checks do not masquerade as solver tests.
    This is a development tool: the existing test fixture helpers must be present.
    """
    for directory in (ROOT, ROOT / "src"):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    from dataclasses import replace
    from apsgo_scheduler.api.request import fingerprint_public_request
    from apsgo_scheduler.app.input_normalizer import normalize_input
    from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set
    from apsgo_scheduler.core._numeric_construction import (
        build_numeric_construction_graph, construct_numeric_initial_plan, numeric_minimum_path_cover,
    )
    from apsgo_scheduler.core._numeric_evaluation import NumericQualityProgram, evaluate_numeric_plan
    from apsgo_scheduler.core._numeric_rules import NumericReason, NumericRuleProgram
    from apsgo_scheduler.core._numeric_state import NumericPlan, NumericTask
    from apsgo_scheduler.core.budget import SolveRuntimeBudget
    from apsgo_scheduler.core.contracts import INTEGER_NUMERIC_SEMANTICS_KEY, RuleScope, fingerprint
    from apsgo_scheduler.core.delivery_timing import DeliveryTimingInput, OrderTimingInput
    from tests.app.test_input_normalizer import make_order, make_request
    from tests.core.test_numeric_evaluation import numeric_quality_spec
    from tests.core.test_numeric_rules import attributes, definition

    spec = numeric_quality_spec()
    earliest = replace(definition("a0_earliest", "EarliestProcessStartRule", RuleScope.PLAN, {}),
                       enabled=case["earliest_enabled"])
    spec = replace(spec, rules=(*spec.rules, earliest))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    orders = tuple(make_order(i, weight=Decimal(row["weight_tonnes"]),
        width=Decimal(row["width_mm"]), thickness=Decimal(row["thickness_mm"]),
        min_temperature=Decimal(row["min_temperature"]), max_temperature=Decimal(row["max_temperature"]),
        grade=f"A0-G{i}", source_period=row["source_period"],
        rule_attributes=attributes(surface_grade="", grade_class="ordinary", customer_name="ordinary"))
        for i, row in enumerate(case["orders"]))
    request = make_request(rule_set_spec=spec, orders=orders)
    policy = replace(request.policy, seed=suite["seed"],
        total_time_limit_seconds=Decimal(suite["policy"]["total_time_limit_seconds"]),
        finalization_reserve_seconds=Decimal(suite["policy"]["finalization_reserve_seconds"]),
        candidate_check_limit=0, numeric_semantics_key=INTEGER_NUMERIC_SEMANTICS_KEY,
        whole_chain_pair_scan_slack_weight=Decimal(suite["policy"]["whole_chain_pair_scan_slack_weight"]),
        maximum_virtual_bridge_nodes=suite["policy"]["maximum_virtual_bridge_nodes"])
    start = datetime.fromisoformat(case["schedule_start_at"])
    timing_rows = []
    with localcontext() as ctx:
        ctx.prec = 50
        for row, order in zip(case["orders"], orders):
            lower = row["earliest_offset_ms"]
            if lower is not None and lower % 1000:
                raise ValueError("external lower bounds must retain second precision")
            text = None if lower is None else (start + timedelta(milliseconds=lower)).isoformat(timespec="seconds")
            timing_rows.append(OrderTimingInput(order.source_order_id, row["due_date"],
                Decimal(row["duration_ms"]) / Decimal(3600000), earliest_start_at=text))
    timing = DeliveryTimingInput(case["schedule_start_at"], tuple(timing_rows),
        {prototype.prototype_id: Decimal("0.1") for prototype in request.virtual_prototypes})
    request = replace(request, policy=policy, delivery_timing=timing)
    rules = load_rule_set(spec)
    problem = normalize_input(request, rules)
    task = NumericTask.build(problem, rules, timing)
    program = NumericRuleProgram.compile(task, rules)
    quality = NumericQualityProgram.compile(task, program, rules)
    # Pin row meanings before comparing arrays; never confuse source and node indices.
    if tuple(task.node_ids[:len(orders)]) != tuple(order.node_id for order in orders):
        raise ValueError("fixture row identities changed")
    budget = SolveRuntimeBudget.from_policy(policy, monotonic())
    graph = build_numeric_construction_graph(task, program, budget, seed=suite["seed"])
    if not graph.complete:
        raise RuntimeError(f"graph interrupted: {budget.stop_reason}")
    cover = numeric_minimum_path_cover(graph, budget)
    if not cover.complete:
        raise RuntimeError(f"cover interrupted: {budget.stop_reason}")
    initial = construct_numeric_initial_plan(task, program, quality, graph, cover, budget)
    if not initial.complete:
        raise RuntimeError(f"initial construction interrupted: {budget.stop_reason}")
    def slices(rows, offsets):
        return [rows[int(a):int(b)].tolist() for a, b in zip(offsets, offsets[1:])]
    plan, evaluation = initial.plan, initial.evaluation
    chains = slices(plan.node_rows, plan.chain_offsets)
    edges = sorted([int(row), int(target)] for position, row in enumerate(graph.ordered_rows)
        for target in graph.adjacency_rows[int(graph.adjacency_offsets[position]):int(graph.adjacency_offsets[position+1])])
    starts = [int(end) - int(task.nodes.duration_ms[int(row)])
              for row, end in zip(plan.node_rows, evaluation.delivery.node_end_ms)]
    expected = case["expected_baseline"]
    fields = {"ordered_rows": graph.ordered_rows.tolist(), "edges": edges,
        "cover_paths": slices(cover.path_rows, cover.path_offsets), "chains": chains,
        "chain_ids": plan.chain_ids.tolist(),
        "chain_periods": [task.period_ids[int(p)] for p in plan.chain_periods],
        "start_ms": starts, "candidate_check_count": budget.candidate_check_count}
    mismatches = [key for key, value in fields.items() if value != expected[key]]
    violations = [value for value in evaluation.violations if value.reason is NumericReason.EARLY_START]
    early_count = sum(value > 0 for value in expected["early_ms"])
    early_severity = sum(expected["early_ms"]) * 1000
    if len(violations) != early_count or sum(value.severity for value in violations) != early_severity:
        mismatches.append("early_violations")
    gap = sum(abs(int(case["orders"][a[-1]]["width_mm"]) - int(case["orders"][b[0]]["width_mm"]))
              for a, b in zip(expected["chains"], expected["chains"][1:]))
    expected_quality = [early_count, early_severity, 0, 0, 0, 0, gap * int(task.units.width), 0, len(chains)]
    if evaluation.quality_key.tolist() != expected_quality:
        mismatches.append("complete_quality_key")
    counterfactual = None
    if case.get("manual_proposal"):
        proposed = case["manual_proposal"]["chains"]
        offsets = [0]
        for chain in proposed:
            offsets.append(offsets[-1] + len(chain))
        manual = NumericPlan.build(task, [r for chain in proposed for r in chain],
                                   offsets, (1, 0), (0, 0))
        checked = evaluate_numeric_plan(task, program, quality, manual)
        counterfactual = {"kind": "manual_plan_not_implemented_optimizer",
                          "quality_key": checked.quality_key.tolist()}
        if any(v.reason is NumericReason.EARLY_START for v in checked.violations):
            mismatches.append("manual_counterfactual")
    return {"case_id": case["case_id"], "observed": fields,
        "quality_key": evaluation.quality_key.tolist(), "expected_quality_key": expected_quality,
        "early_count": len(violations), "early_total_ms": sum(v.severity for v in violations) // 1000,
        "graph_checked_edges": graph.checked_edge_count, "stop_reason": None if budget.stop_reason is None else budget.stop_reason.value,
        "identities": {"request": fingerprint_public_request(request), "problem": problem.input_fingerprint,
            "rule_set": rules.fingerprint, "numeric_task": task.fingerprint,
            "prototypes": fingerprint(request.virtual_prototypes), "policy": fingerprint(policy),
            "grade_dictionary": None,
            "graph": graph.fingerprint, "cover": cover.fingerprint, "initial": initial.fingerprint},
        "manual_counterfactual": counterfactual, "mismatches": mismatches,
        "final_solver_result": None, "attribution_scope": "initial_only_no_search_executed"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("witness", "numeric"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="New JSON file; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists; choose a new path")
    report = {"mode": args.mode, "environment": environment(), "status": "not_run",
              "numeric_executed": False, "production_service_accessed": False}
    code = 0
    try:
        suite = load_cases()
        report["fixture_sha256"] = hashlib.sha256(CASES.read_bytes()).hexdigest()
        report["witnesses"] = [validate_witness(case) for case in suite["cases"]]
        if args.mode == "numeric":
            report["source"] = baseline_source_identity()
            report["numeric_cases"] = []
            for case in suite["cases"]:
                report["numeric_attempted"] = True
                report["numeric_cases"].append(numeric_case(case, suite))
            report["numeric_executed"] = True
            code = int(any(case["mismatches"] for case in report["numeric_cases"]))
            report["status"] = "failed" if code else "numeric_baseline_verified"
        else:
            report["status"] = "hand_witnesses_verified_only"
    except Exception as error:  # CLI boundary: preserve failure, never convert it to a pass.
        code = 2
        report.update(status="blocked_or_error", error_type=type(error).__name__, error=str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"{report['status']}: {args.output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
