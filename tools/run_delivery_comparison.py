"""Replay old or delivery objectives with identical physical inputs and budgets."""

import argparse
from contextlib import nullcontext
from decimal import Decimal
import json
import logging
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from apsgo_scheduler.api.request import fingerprint_public_request
from apsgo_scheduler.app.delivery_request import prepare_delivery_request
from apsgo_scheduler.app.delivery_report import build_delivery_report, delivery_plan_report
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.core.contracts import fingerprint
from apsgo_scheduler.core import width_optimization
from apsgo_v7_service.diagnostics import _json_values
from tools.profile_solver_search import _counters, load_request, measure
from tools.verify_solver_diagnostics import _code_identity, _sha256, _write_json


def prepare(source_request, timing_source, start, virtual_speed, *, include_backlog_clearance=False,
            second_precision=False):
    old = load_request(source_request)
    raw = json.loads(timing_source.read_text(encoding="utf-8"))
    rows = {item["source_order_id"]: item for item in raw["orders"]}
    if len(rows) != len(raw["orders"]) or set(rows) != {order.source_order_id for order in old.orders}:
        raise ValueError("timing source does not match all original orders")
    timing = {}
    for order in old.orders:
        values = rows[order.source_order_id]
        if any(Decimal(values[key]) != getattr(order, key) for key in ("weight", "width", "thickness")):
            raise ValueError(f"{order.source_order_id}: timing source physical fields differ")
        timing[order.source_order_id] = {
            key: values[key] if key == "due_date" or values[key] is None else Decimal(values[key])
            for key in ("due_date", "furnace_speed_mpm", "process_speed_mpm")
        }
    new = prepare_delivery_request(old, schedule_start_at=start, order_timing=timing, virtual_speed_mpm=virtual_speed,
                                   include_backlog_clearance=include_backlog_clearance,
                                   second_precision=second_precision)
    return old, new


def observe_recipes(original, observations):
    """Observe attempted post-refinement recipes; the original callback still decides once."""
    def observed(state, context, recipe):
        action, source = recipe[:2]
        chains = state.current_plan.chains
        nodes = chains[source].nodes
        if action == "delivery_intra_move":
            nodes = nodes[recipe[2]:recipe[2] + 1]
        elif action in ("width_node_move", "width_node_exchange", "width_block_move", "width_block_exchange"):
            _, _, target, start, stop, other_start, other_stop = recipe
            nodes = nodes[start:stop] + chains[target].nodes[other_start:other_stop]
        stats = observations.setdefault("actions", {}).setdefault(action, dict(
            attempted_proposals=0, candidate_checks=0, complete_evaluations=0, accepted=0, backlog_originals={}))
        timing = context.factory.cache.context.delivery_timing
        for node in nodes:
            key = node.source_order_id
            if timing is not None and key in timing.orders and timing.orders[key].due_hours <= 0:
                stats["backlog_originals"][key] = True
                if action == "width_node_move":
                    observations.setdefault("first_backlog_node_moves", {}).setdefault(key, dict(
                        at_candidate_check=context.factory.budget.candidate_check_count,
                        recipe=recipe, source_chain_id=chains[source].chain_id,
                        target_chain_id=chains[target].chain_id, node_id=node.node_id))
        before = _counters(state, context)
        stats["attempted_proposals"] += 1
        try:
            return original(state, context, recipe)
        finally:
            after = _counters(state, context)
            # The scanner charged one attempt before entering this callback; bridges charge inside it.
            stats["candidate_checks"] += 1 + after["candidate_check_count"] - before["candidate_check_count"]
            stats["complete_evaluations"] += after["complete_candidate_evaluation_count"] - before["complete_candidate_evaluation_count"]
            stats["accepted"] += after["accepted_move_count"] - before["accepted_move_count"]
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--timing-source", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--virtual-speed", type=Decimal, required=True)
    parser.add_argument("--variant", choices=("old", "delivery", "delivery-backlog-priority", "delivery-backlog-seconds", "prepare"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--observe-search-opportunities", action="store_true",
                        help="Record post-refinement attempts without changing their construction or acceptance")
    args = parser.parse_args()
    old, new = prepare(args.prepared_request, args.timing_source, args.start, args.virtual_speed,
                       include_backlog_clearance=args.variant in ("delivery-backlog-priority", "delivery-backlog-seconds"),
                       second_precision=args.variant == "delivery-backlog-seconds")
    request = old if args.variant == "old" else new
    args.output_dir.mkdir(parents=True, exist_ok=False)
    metadata = dict(variant=args.variant, code=_code_identity(ROOT),
                    source_request_sha256=_sha256(args.prepared_request), timing_source_sha256=_sha256(args.timing_source),
                    start=args.start, virtual_speed_mpm=args.virtual_speed,
                    original_input_order_preserved=old.orders == new.orders, policy_unchanged=old.policy == new.policy)
    if args.observe_search_opportunities:
        metadata["search_opportunity_observation"] = True
    _write_json(args.output_dir / "input_binding.json", metadata)
    _write_json(args.output_dir / "prepared_request.json", dict(
        request=_json_values(request), request_fingerprint=fingerprint_public_request(request),
        rule_set_fingerprint=request.rule_set_spec.fingerprint,
    ))
    load_request(args.output_dir / "prepared_request.json")
    if args.variant == "prepare":
        print("backend request prepared and identity round-trip verified")
        return 0
    with (args.output_dir / "execution.log").open("x", encoding="utf-8") as log:
        logging.basicConfig(stream=log, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        results = []
        try:
            opportunities = dict(scope="attempted_post_refinement_recipes_including_bridge_checks_not_prescan_queries")
            observer = (patch.object(width_optimization, "_try_width_recipe", observe_recipes(
                width_optimization._try_width_recipe, opportunities)) if args.observe_search_opportunities else nullcontext())
            with observer:
                observation = measure(request, scope="full", result_observer=results.append)
            _write_json(args.output_dir / "measurement.json", observation)
            if args.observe_search_opportunities:
                _write_json(args.output_dir / "search_opportunities.json", opportunities)
            result = results[0]
            if args.variant in ("delivery", "delivery-backlog-priority", "delivery-backlog-seconds"):
                report = build_delivery_report(request, result)
            else:
                plan = result.release.plan if result.release else result.diagnostic_candidate.plan
                report = delivery_plan_report(plan, normalize_input(new).delivery_timing)
                report.update(kind="historical_objective_comparison_only", plan_fingerprint=fingerprint(plan))
            _write_json(args.output_dir / "delivery_report.json", report)
            print(json.dumps(dict(status=result.status.value, wall_seconds=str(observation["public_call_wall_seconds"]),
                                  cpu_seconds=str(observation["public_call_cpu_seconds"]),
                                  summary=report["delivery_summary"] if report else None), default=str, ensure_ascii=False))
            return 0 if result.release is not None and result.core_audit.passed and result.audit_report.passed else 2
        except Exception:
            _write_json(args.output_dir / "failure.json", {"traceback": traceback.format_exc()})
            raise


if __name__ == "__main__":
    raise SystemExit(main())
