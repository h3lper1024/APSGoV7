"""Observe same-process requests or first-search functions; not a paired timing run."""

import argparse
import logging
import runpy
import sys
from decimal import Decimal
from pathlib import Path
from time import perf_counter, process_time
from unittest.mock import patch


class BridgeEvents(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        event = getattr(record, "solver_event", "")
        if event.startswith("solver_bridge_numeric_"):
            self.events.append({"event": event, **record.solver_details})


def require_first_search(measurement, blocks, events, *, numeric):
    final = measurement["first_search"]["final"]
    counts = tuple(final[key] for key in ("candidate_check_count",
                   "complete_candidate_evaluation_count", "accepted_move_count"))
    if (measurement["measurement_scope"] != "first_local_search_only_no_release"
            or counts != (64226, 2263, 34) or final["stop_reason"] != "local_search_complete"):
        raise ValueError("profile did not finish the frozen first-search work")
    modes = {event.get("mode") for event in events
             if event["event"] == "solver_bridge_numeric_ready" and event.get("nopython") is True}
    if numeric and (blocks["count"] <= 0 or modes != {"single", "double"}):
        raise ValueError("profile did not execute both native bridge modes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("code-root", "prepared-request", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mode", choices=("warm", "restart", "profile"), required=True)
    args = parser.parse_args(argv)
    root, output = args.code_root.resolve(strict=True), args.output_dir.absolute()
    if output.exists():
        raise FileExistsError(output)
    sys.path[:0] = [str(root), str(root / "src"), str(root / "tools")]
    from tools import profile_solver_search as profiler
    from tools.compare_solver_stages import first_difference
    from tools.verify_solver_diagnostics import _code_identity, _read_json, _sha256, _write_json

    checks = runpy.run_path(str(Path(__file__).with_name("run_pairs.py")))
    if profiler.ROOT != root or _sha256(root / "tools/profile_solver_search.py") != checks["TOOL_SHA256"]:
        raise ValueError("the loaded measurement tool does not match the frozen source")
    if _sha256(args.prepared_request) != checks["PREPARED_SHA256"]:
        raise ValueError("prepared request differs from the stage 0 frozen bytes")
    module_key = "apsgo_scheduler.core._bridge_numeric"
    if module_key in sys.modules:
        raise ValueError("run the probe in a fresh process")
    output.mkdir(parents=True, exist_ok=False)
    events = BridgeEvents()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("apsgo_scheduler.core.virtual_material")
    logger.addHandler(events)
    summary = {
        "status": "running", "mode": args.mode, "source": _code_identity(root),
        "probe_sha256": _sha256(Path(__file__)), "prepared_request_sha256": checks["PREPARED_SHA256"],
        "warning": "separate from the five cold pairs; profile mode includes observation overhead",
        "runs": [],
    }
    try:
        if args.mode in {"warm", "restart"}:
            reference = None
            for index in ((1, 2) if args.mode == "warm" else (1,)):
                events.events.clear()
                module = sys.modules.get(module_key)
                before = bool(module and module._scan_block.nopython_signatures)
                run = output / f"request_{index}"
                profiler.main(["--prepared-request", str(args.prepared_request),
                               "--output-dir", str(run), "--scope", "full"])
                measurement = _read_json(run / "measurement.json")
                checks["require_fixed_work"](measurement)
                stable = checks["stable_measurement"](measurement)
                difference = None if reference is None else first_difference(reference, stable)
                module = sys.modules.get(module_key)
                after = bool(module and module._scan_block.nopython_signatures)
                modes = {event.get("mode") for event in events.events
                         if event["event"] == "solver_bridge_numeric_ready" and event.get("nopython") is True}
                if difference or before != (index == 2) or not after or modes != {"single", "double"}:
                    raise ValueError(f"warm execution or business comparison failed: {difference}")
                reference = stable
                summary["runs"].append({"request": index, "compiled_before": before,
                    "compiled_after": after, "events": list(events.events), "first_difference": difference,
                    "wall_seconds": measurement["public_call_wall_seconds"],
                    "cpu_seconds": measurement["public_call_cpu_seconds"]})
        else:
            blocks = {"count": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0,
                      "first_wall_seconds": None, "first_cpu_seconds": None,
                      "max_subsequent_wall_seconds": 0.0}
            numeric_file = root / "src/apsgo_scheduler/core/_bridge_numeric.py"
            if numeric_file.is_file():
                from apsgo_scheduler.core import _bridge_numeric as numeric
                original = numeric._scan_block

                def block(*values):
                    started, cpu = perf_counter(), process_time()
                    result = original(*values)
                    wall, used = perf_counter() - started, process_time() - cpu
                    blocks["count"] += 1
                    blocks["wall_seconds"] += wall
                    blocks["cpu_seconds"] += used
                    if blocks["count"] == 1:
                        blocks.update(first_wall_seconds=wall, first_cpu_seconds=used)
                    else:
                        blocks["max_subsequent_wall_seconds"] = max(blocks["max_subsequent_wall_seconds"], wall)
                    block.nopython_signatures = original.nopython_signatures
                    return result

                context = patch.object(numeric, "_scan_block", block)
            else:
                from contextlib import nullcontext
                context = nullcontext()
            with context:
                profiler.main(["--prepared-request", str(args.prepared_request),
                    "--output-dir", str(output / "first_search"), "--scope", "first", "--profile"])
            measurement = _read_json(output / "first_search/measurement.json")
            require_first_search(measurement, blocks, events.events, numeric=numeric_file.is_file())
            if numeric_file.is_file() and not original.nopython_signatures:
                raise ValueError("native dispatcher has no compiled signature")
            summary.update(blocks={key: Decimal(str(value)) if type(value) is float else value
                                   for key, value in blocks.items()}, events=events.events,
                profile_scope="cProfile covers first local search only; blocks cover all calls until that exit",
                block_timing_scope="native calls only, including first JIT; excludes array preparation and module import")
        summary["status"] = "passed"
    except BaseException as error:
        summary.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        logger.removeHandler(events)
        _write_json(output / "probe_summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
