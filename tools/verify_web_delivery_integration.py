"""One explicit HTTP-boundary replay using a backup of the active rule database."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from fastapi.testclient import TestClient
from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_v7_service.app import create_app, MONTH_SOLVE_PATH
from apsgo_v7_service.configuration import load_service_configuration
from apsgo_v7_service.enable_delivery_rules import enable_delivery_rules
from apsgo_v7_service.rule_management import get_active_gqga4_rules
from tools.profile_solver_search import load_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--timing-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    configuration = load_service_configuration(ROOT / "config/apsgo_v7_service.yaml")
    database = args.output_dir / "rules.sqlite3"
    with sqlite3.connect(configuration.database_path.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(database) as target:
            source.backup(target)
    active = get_active_gqga4_rules(database)
    if not any(r.rule_id == "delivery_due_performance" for r in active.rule_set_spec.rules):
        active = enable_delivery_rules(database, args.output_dir / "rules-before.sqlite3",
                                       expected_active_version_id=active.active_version_id).active_rules
    original = load_request(args.prepared_request)
    timing = {r["source_order_id"]: r for r in json.loads(args.timing_source.read_text(), parse_float=Decimal)["orders"]}
    body = {
        "contract_version": "v7-month-solve-v2", "request_id": str(uuid4()),
        "schedule_start_at": original.delivery_timing.schedule_start_at,
        "expected_active_version_id": active.active_version_id,
        "periods": [{"period_id": p.period_id, "sequence": p.sequence} for p in original.periods],
        "orders": [],
    }
    for order in original.orders:
        row = {k: getattr(order, k) for k in ("source_order_id", "source_period", "weight", "grade", "width", "thickness", "min_temperature", "max_temperature")}
        row.update({k: order.rule_attributes.get(k) for k in ("grade_class", "hot_roll_grade", "customer_grade", "customer_name", "execution_standard", "surface_grade")})
        raw = timing[order.source_order_id]
        for field in ("weight", "width", "thickness"):
            assert Decimal(str(raw[field])) == getattr(order, field)
        row.update(is_virtual=False, due_date=raw["due_date"],
                   furnace_speed_mpm=None if raw["furnace_speed_mpm"] is None else Decimal(str(raw["furnace_speed_mpm"])),
                   process_speed_mpm=None if raw["process_speed_mpm"] is None else Decimal(str(raw["process_speed_mpm"])))
        body["orders"].append(row)
    (args.output_dir / "request.json").write_text(dumps_exact_json(body), encoding="utf-8")
    started = perf_counter()
    with TestClient(create_app(database, monthly_solve_policy=configuration.monthly_solve_policy,
                              month_plan_dates_path=ROOT / "config/month_plan_dates.json",
                              diagnostics_directory=args.output_dir / "diagnostics")) as client:
        response = client.post(MONTH_SOLVE_PATH, content=dumps_exact_json(body), headers={"content-type": "application/json"})
    (args.output_dir / "response.json").write_bytes(response.content)
    result = json.loads(response.content, parse_float=Decimal)
    assert response.status_code == 200, result
    assert result["publishable"] and result["audit_summary"]["passed"], result.get("issues")
    assert len(result["quality"]) == 9
    assert result["run_manifest"]["algorithm_version"] == "numeric-path-cover-local-search-v1"
    nodes, dates = result["delivery_report"]["delivery_nodes"], result["latest_dates"]["rows"]
    assert [r["node_id"] for r in result["rows"]] == [r["node_id"] for r in nodes] == [r["node_id"] for r in dates]
    assert [r["completion_at"] for r in nodes] == [r["current_process_latest_at"] for r in dates] == [r["latest_dates"]["coating"] for r in dates]
    summary = dict(wall_seconds=Decimal(str(perf_counter() - started)), status=result["status"],
                   start=result["schedule_start_at"], quality=result["quality"],
                   audits=result["audit_summary"], run_manifest=result["run_manifest"],
                   node_count=len(nodes), delivery=result["delivery_report"]["delivery_summary"])
    (args.output_dir / "summary.json").write_text(dumps_exact_json(summary), encoding="utf-8")
    print(dumps_exact_json(summary))


if __name__ == "__main__":
    main()
