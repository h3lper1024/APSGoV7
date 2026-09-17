"""Prepare and replay release-time acceptance data without touching the live service."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BEIJING = timezone(timedelta(hours=8))


def read_export(source, baseline):
    """Match originals, never use scheduled pieces or borrowed periods as inputs."""
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("duplicate CSV columns")
        rows = list(reader)
    with baseline.open(encoding="utf-8-sig", newline="") as stream:
        originals = list(csv.DictReader(stream))
    groups = defaultdict(list)
    for row in rows:
        if row["虚拟材"] not in ("是", "否"):
            raise ValueError("unknown virtual marker")
        if row["虚拟材"] == "否":
            groups[row["合同完全号"]].append(row)
    if len(originals) != len({r["source_order_id"] for r in originals}):
        raise ValueError("duplicate baseline originals")
    if set(groups) != {r["source_order_id"] for r in originals}:
        raise ValueError("source IDs differ from frozen originals")
    numbers = ("连镀欠交", "宽度", "厚度", "均热段温度最小值", "均热段温度最大值", "炉区速度")
    texts = ("交货日期", "牌号", "热轧牌号", "执行标准", "表面等级", "客户等级")
    selected = []
    for original in originals:
        key = original["source_order_id"]
        pieces = groups[key]
        row = pieces[0]
        for piece in pieces:
            for field in (*numbers, *texts, "最早连镀时间", "客户", "战略客户名称", "钢种大类"):
                if piece[field] != row[field]:
                    raise ValueError(f"{key}: conflicting original field {field}")
        for field in numbers:
            if not Decimal(row[field]).is_finite() or Decimal(row[field]) != Decimal(original[field]):
                raise ValueError(f"{key}: changed physical field {field}")
        for field in texts:
            if row[field] != original[field]:
                raise ValueError(f"{key}: changed source field {field}")
        customer = row["战略客户名称"].strip() or row["客户"].strip()
        if customer != original["战略客户名称"].strip():
            raise ValueError(f"{key}: changed effective customer")
        if sum(Decimal(p["排产重量 / t"]) for p in pieces) != Decimal(row["连镀欠交"]):
            raise ValueError(f"{key}: piece weights do not conserve original")
        lower = datetime.fromisoformat(row["最早连镀时间"].removeprefix("'"))
        if lower.tzinfo is not None or lower.microsecond:
            raise ValueError(f"{key}: expected exported Beijing time to seconds")
        selected.append((original, row, lower.replace(tzinfo=BEIJING).isoformat()))
    preflight = dict(csv_sha256=sha256(source.read_bytes()).hexdigest(),
        baseline_sha256=sha256(baseline.read_bytes()).hexdigest(), csv_rows=len(rows),
        real_nodes=sum(map(len, groups.values())), original_orders=len(groups),
        virtual_nodes=sum(r["虚拟材"] == "是" for r in rows),
        split_originals=sum(len(v) > 1 for v in groups.values()),
        original_weight=sum(Decimal(r["连镀欠交"]) for _, r, _ in selected),
        original_order_and_source_period="frozen baseline, verified IDs and business fields",
        historical_early_nodes=sum(
            datetime.fromisoformat(r["连镀开始时间"].removeprefix("'"))
            < datetime.fromisoformat(r["最早连镀时间"].removeprefix("'"))
            for values in groups.values() for r in values))
    return selected, preflight


def plan_differences(left, right, path="plan"):
    """Keep every difference visible, including expected lineage identities."""
    if type(left) is not type(right):
        return [dict(path=path, old=left, new=right)]
    if isinstance(left, dict):
        return [d for key in sorted(left.keys() | right.keys())
            for d in plan_differences(left.get(key), right.get(key), f"{path}.{key}")]
    if isinstance(left, list):
        if len(left) != len(right):
            return [dict(path=path, old_length=len(left), new_length=len(right))]
        return [d for index, (a, b) in enumerate(zip(left, right))
            for d in plan_differences(a, b, f"{path}[{index}]")]
    return [] if left == right else [dict(path=path, old=left, new=right)]


def prepare(args):
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    from apsgo_scheduler.api.rule_management import EditableRuleInput, SetActiveRulesRequest
    from apsgo_v7_service.configuration import load_service_configuration
    from apsgo_v7_service.month_scheduling import loads_month_solve_request
    from apsgo_v7_service.scheduling import bind_gqga4_scheduling_task
    from apsgo_v7_service.rule_management import get_active_gqga4_rules, set_active_gqga4_rules
    from tools.profile_numeric_solver import _request_envelope
    from tools.verify_solver_diagnostics import _write_json

    rows, preflight = read_export(args.csv, ROOT / "tests/baselines/gqga4/inputs/input_orders.csv")
    configuration = load_service_configuration(ROOT / "config/apsgo_v7_service.yaml")
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.output / "isolated_rules.sqlite3"
    with sqlite3.connect(configuration.database_path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(database) as target:
            source.backup(target)
    active = get_active_gqga4_rules(database)
    if any(r.rule_id == "earliest_process_start" for r in active.rule_set_spec.rules):
        raise ValueError("expected pre-activation rule snapshot")
    period_ids = json.loads((ROOT / "tests/baselines/gqga4/inputs/solver_config.json").read_text())["period_order"]
    if set(period_ids) != {o["source_period"] for o, _, _ in rows}:
        raise ValueError("source periods differ from the frozen catalogue")
    body = dict(contract_version="v7-month-solve-v2", request_id=str(uuid4()),
        schedule_start_at=args.start, expected_active_version_id=active.active_version_id,
        periods=[dict(period_id=p, sequence=i) for i, p in enumerate(period_ids)], orders=[])
    numeric = dict(weight="连镀欠交", width="宽度", thickness="厚度",
        min_temperature="均热段温度最小值", max_temperature="均热段温度最大值", furnace_speed_mpm="炉区速度")
    text = dict(grade="牌号", grade_class="钢种大类", hot_roll_grade="热轧牌号",
        customer_grade="客户等级", execution_standard="执行标准", surface_grade="表面等级", due_date="交货日期")
    for original, row, lower in rows:
        order = {key: Decimal(row[column]) for key, column in numeric.items()}
        order.update({key: row[column].strip() or None for key, column in text.items()})
        order.update(source_order_id=original["source_order_id"], source_period=original["source_period"],
            is_virtual=False, customer_name=row["战略客户名称"].strip() or row["客户"].strip(), process_speed_mpm=None)
        body["orders"].append(order)
    for variant in ("old", "disabled", "enabled"):
        if variant != "old":
            body["contract_version"] = "v7-month-solve-v3"
            for order, (_, _, lower) in zip(body["orders"], rows):
                order["earliest_start_at"] = lower
            current = get_active_gqga4_rules(database)
            saved = set_active_gqga4_rules(SetActiveRulesRequest(save_operation_id=str(uuid4()),
                expected_active_version_id=current.active_version_id,
                rules=tuple(EditableRuleInput(r.rule_id, r.enabled, r.parameters) for r in active.rule_set_spec.rules)
                    + (EditableRuleInput("earliest_process_start", variant == "enabled", {}),),
                virtual_prototypes=active.virtual_prototypes, remark="最早开工真实数据隔离验收"), database)
            body["expected_active_version_id"] = saved.active_rules.active_version_id
        parsed = loads_month_solve_request(dumps_exact_json(body), configuration.monthly_solve_policy)
        bound = bind_gqga4_scheduling_task(parsed.task_input, database,
            expected_active_version_id=parsed.expected_active_version_id)
        _write_json(args.output / f"{variant}_http_request.json", body)
        _write_json(args.output / f"{variant}_request.json", _request_envelope(bound.request))
    _write_json(args.output / "lower_bounds.json", {o["source_order_id"]: lower for o, _, lower in rows})
    preflight.update(schedule_start_at=args.start, active_version=active.active_version_id,
        source_database_sha256=sha256(configuration.database_path.read_bytes()).hexdigest(),
        configuration_sha256=sha256((ROOT / "config/apsgo_v7_service.yaml").read_bytes()).hexdigest())
    _write_json(args.output / "preflight.json", preflight)
    print(dumps_exact_json(preflight), flush=True)


def replay(args):
    from dataclasses import replace
    from time import perf_counter
    from apsgo_scheduler.app.service import solve_request
    from tests.core.test_numeric_boundary_audit import numeric_request
    from tools.profile_numeric_solver import _run_once
    from tools.profile_solver_search import load_request
    from tools.verify_solver_diagnostics import _write_json

    request = load_request(args.request)
    warm = numeric_request()
    warm = replace(warm, policy=replace(warm.policy, candidate_check_limit=500,
        total_time_limit_seconds=Decimal(310), finalization_reserve_seconds=Decimal(10)))
    started = perf_counter()
    result = solve_request(warm)
    warm_seconds = perf_counter() - started
    print(f"warmup_complete seconds={warm_seconds:.3f} status={result.status.value}", flush=True)
    measurement = _run_once(request, args.output)
    _write_json(args.output / "warmup.json", dict(seconds=Decimal(str(warm_seconds)),
        candidate_limit=500, status=result.status.value, code_root=str(args.code_root)))
    print(json.dumps(measurement, default=str, ensure_ascii=False), flush=True)


def summarize(args):
    from apsgo_scheduler.api.json_codec import dumps_exact_json
    from tools.verify_solver_diagnostics import _write_json

    def read(path):
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)

    bounds = read(args.output / "lower_bounds.json")
    summaries, plans, counters, traces = {}, {}, {}, {}
    for variant in ("old", "disabled", "enabled"):
        directory = args.output / variant
        request = read(args.output / f"{variant}_request.json")["request"]
        result = read(directory / "response.json")
        report = read(directory / "delivery_report.json")
        measurement = read(directory / "measurement.json")
        candidate = result["release"] or result["diagnostic_candidate"]
        plan = candidate["plan"]
        plans[variant] = plan
        counters[variant] = result["run_manifest"]["counters"]
        traces[variant] = result["run_manifest"]["trace_fingerprint"]
        scheduled = [(chain, node) for chain in plan["chains"] for node in chain["nodes"]]
        assert [node["node_id"] for _, node in scheduled] == [r["node_id"] for r in report["delivery_nodes"]]
        previous = datetime.fromisoformat(request["delivery_timing"]["schedule_start_at"])
        details, totals, duration_totals = [], defaultdict(Decimal), defaultdict(int)
        for position, ((chain, node), timing) in enumerate(zip(scheduled, report["delivery_nodes"]), 1):
            begin, end = (datetime.fromisoformat(timing[f]) for f in ("start_at", "completion_at"))
            assert begin == previous and end >= begin, "implicit wait or invalid time"
            previous = end
            source = node["source_order_id"]
            virtual = node["virtual_lineage"] is not None
            lower = None if virtual else datetime.fromisoformat(bounds[source])
            delta = timedelta(0) if lower is None else max(timedelta(0), lower - begin)
            seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000
            if not virtual:
                totals[source] += Decimal(node["weight"])
                duration_totals[source] += timing["completion_milliseconds"] - timing["start_milliseconds"]
            else:
                rate = Decimal(request["delivery_timing"]["virtual_hours_per_tonne"][node["virtual_lineage"]["prototype_id"]])
                expected = int((rate * Decimal(node["weight"]) * 3600000).to_integral_value(rounding=ROUND_HALF_UP))
                assert timing["completion_milliseconds"] - timing["start_milliseconds"] == expected
            if "early_start_seconds" in timing:
                assert seconds == Decimal(timing["early_start_seconds"]), "independent lower-bound check differs"
            details.append(dict(publishable=measurement["publishable"],
                artifact_kind="released_plan" if measurement["publishable"] else "diagnostic_candidate_not_publishable",
                position=position, node_id=node["node_id"], source_order_id=source, source_period=node["source_period"],
                assigned_period=chain["assigned_period"], chain_id=chain["chain_id"], virtual=virtual,
                weight=node["weight"], earliest_start_at=None if lower is None else lower.isoformat(),
                start_at=timing["start_at"], completion_at=timing["completion_at"], early_seconds=seconds))
        assert totals == {o["source_order_id"]: Decimal(o["weight"]) for o in request["orders"]}
        assert duration_totals == {o["source_order_id"]:
            int((Decimal(o["duration_hours"]) * 3600000).to_integral_value(rounding=ROUND_HALF_UP))
            for o in request["delivery_timing"]["orders"]}, "source duration changed after splitting"
        early = [r for r in details if r["early_seconds"] > 0]
        with (directory / "nodes.csv").open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(details[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(details)
        summaries[variant] = dict(measurement, delivery=report["delivery_summary"],
            early_node_count=len(early), early_original_count=len({r["source_order_id"] for r in early}),
            early_total_seconds=sum((r["early_seconds"] for r in early), Decimal(0)),
            no_wait_and_conservation_verified=True,
            early_nodes=early,
            quality_by_name=dict(zip((q["metric_key"] for q in request["rule_set_spec"]["quality_spec"]),
                                    measurement["quality_key"])))
    differences = plan_differences(plans["old"], plans["disabled"])
    identity_suffixes = (".split_lineage.authorization_decision_fingerprint",
        ".split_lineage.partition_id", ".virtual_lineage.related_partition_id")
    comparison = dict(runs=summaries, disabled_plan_differences=differences, disabled_matches_old=dict(
        complete_plan=plans["old"] == plans["disabled"],
        plan_except_listed_lineage_identities=all(d["path"].endswith(identity_suffixes) for d in differences),
        quality=summaries["old"]["quality_key"] == summaries["disabled"]["quality_key"],
        counters=counters["old"] == counters["disabled"],
        search_trace=traces["old"] == traces["disabled"]))
    _write_json(args.output / "comparison.json", comparison)
    print(dumps_exact_json({k: {name: run[name] for name in (
        "status", "stop_reason", "publishable", "quality_key", "wall_seconds", "cpu_seconds",
        "early_node_count", "early_original_count", "early_total_seconds")}
        for k, run in summaries.items()}), flush=True)
    print(comparison["disabled_matches_old"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run", "summarize"))
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--start", default="2026-06-01T00:00:00+08:00")
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.code_root), str(args.code_root / "src")]
    {"prepare": prepare, "run": replay, "summarize": summarize}[args.mode](args)


if __name__ == "__main__":
    main()
