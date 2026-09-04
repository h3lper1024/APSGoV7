"""Capture the immutable external reference in an isolated development process."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import platform
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests/baselines/gqga4/reference_manifest.json"
SCRIPT_SHA256 = "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
INPUT_FLAGS = {
    "input_orders.csv": "--input-orders",
    "optimization_problem.json": "--optimization-problem",
    "resolved_rules.json": "--resolved-rules",
    "rule_context.json": "--rule-context",
    "solver_config.json": "--solver-config",
}
OUTPUT_NAMES = {
    "run_manifest.json",
    "validation_report.json",
    "chain_summary.csv",
    "schedule_result.csv",
}


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("reference manifest must be an object with schema_version=1")
    return value


def verify_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    if set(manifest.get("inputs", {})) != set(INPUT_FLAGS):
        raise ValueError("reference manifest must identify exactly five authoritative inputs")
    if set(manifest.get("static_outputs", {})) != OUTPUT_NAMES:
        raise ValueError("reference manifest must identify exactly four static outputs")
    if manifest.get("script", {}).get("sha256") != SCRIPT_SHA256:
        raise ValueError("reference script identity differs from the approved immutable script")
    observed = {}
    for group, entries in (
        ("script", {"solver.py": manifest["script"]}),
        ("inputs", manifest["inputs"]),
        ("static_outputs", manifest["static_outputs"]),
    ):
        for name, entry in entries.items():
            path = resolve_path(entry["path"])
            if not path.is_file():
                raise ValueError(f"{group}/{name}: file missing: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != entry["sha256"]:
                raise ValueError(f"{group}/{name}: SHA-256 mismatch: {digest}")
            observed[f"{group}/{name}"] = digest
    return observed


def solver_argv(
    manifest: dict[str, Any], output_dir: Path, *, time_budget_seconds: float | None = None
) -> list[str]:
    arguments = []
    for name, flag in INPUT_FLAGS.items():
        arguments.extend([flag, str(resolve_path(manifest["inputs"][name]["path"]))])
    parameters = manifest["parameters"]
    seconds = (
        parameters["time_budget_seconds"] if time_budget_seconds is None else time_budget_seconds
    )
    return arguments + [
        "--output-dir",
        str(output_dir),
        "--seed",
        str(parameters["seed"]),
        "--time-budget-seconds",
        str(seconds),
        "--candidate-check-budget",
        str(parameters["candidate_check_budget"]),
    ]


def _phase(frame: FrameType) -> str:
    # These boundaries belong to SCRIPT_SHA256, not to a reimplementation of its search.
    if frame.f_code.co_name == "candidate_merged_sequences":
        return "whole_chain"
    if frame.f_code.co_name == "final_cross_period_split_return":
        return "controlled_split"
    if frame.f_code.co_name != "local_search":
        raise ValueError(f"unrecognized reference budget caller: {frame.f_code.co_name}")
    if frame.f_lineno < 1777:
        return "whole_chain"
    return "single_order" if frame.f_lineno < 1842 else "virtual_fill"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported capture value: {type(value).__name__}")


def capture_stages(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Instrument only the pinned script; a 600-second ceiling isolates candidate semantics."""
    verify_manifest(manifest)
    script = resolve_path(manifest["script"]["path"])
    module_name = "_apsgo_solverpy_reference_capture"
    spec = importlib.util.spec_from_file_location(module_name, script)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load reference: {script}")
    reference = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = reference
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(reference)
    finally:
        sys.dont_write_bytecode = previous_bytecode
    capture: dict[str, Any] = {
        "schema_version": 1,
        "script_sha256": SCRIPT_SHA256,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "parameters": dict(manifest["parameters"]),
        "instrumentation": {
            "method": "temporary wrappers; path-cover return profile; accepted callback locals",
            "time_budget_seconds_override": 600.0,
            "reason": "Keep instrumentation overhead outside the semantic stopping criterion.",
            "source_modified": False,
        },
        "node_catalog": {},
        "search_rounds": [],
        "candidate_count_sites": {},
        "budget_stops": [],
    }
    active: dict[str, Any] = {}
    site_counts: Counter[str] = Counter()

    def plan(chains: Any) -> list[dict[str, Any]]:
        for chain in chains:
            for node in chain.nodes:
                capture["node_catalog"][node.node_id] = asdict(node)
        return [
            {
                "assigned_period": chain.assigned_period,
                "node_ids": [node.node_id for node in chain.nodes],
            }
            for chain in chains
        ]

    def snapshot(chains: Any, evaluation: Any, budget: Any = None) -> dict[str, Any]:
        return {
            "plan": plan(chains),
            "quality": list(evaluation.quality),
            "metrics": dict(evaluation.result_metrics),
            "violations": list(evaluation.violations),
            "candidate_checks": budget.candidate_checks if budget is not None else 0,
        }

    normalize = reference.normalize_nodes

    def normalize_nodes(document: Any, rules: Any) -> Any:
        nodes = normalize(document, rules)
        capture["input"] = {
            "node_ids": [node.node_id for node in nodes],
            "real_weight": str(sum((node.weight for node in nodes), Decimal(0))),
            "period_order": list(rules.period_order),
            "period_counts": dict(Counter(node.source_period for node in nodes)),
            "rule_version": rules.rule_version,
            "rule_fingerprint": rules.fingerprint,
            "enabled_rule_ids": sorted(rules.enabled),
        }
        return nodes

    path_cover = reference.maximum_path_cover

    def maximum_path_cover(nodes: Any, rules: Any, rng: Any) -> Any:
        def returned(frame: FrameType, event: str, result: Any) -> None:
            if event != "return" or frame.f_code is not path_cover.__code__:
                return
            values = frame.f_locals
            capture["path_cover"] = {
                "ordered_node_ids": [nodes[index].node_id for index in values["indices"]],
                "adjacency": [
                    {
                        "node_id": nodes[index].node_id,
                        "successor_node_ids": [nodes[item].node_id for item in successors],
                    }
                    for index, successors in values["adjacency"].items()
                ],
                "match_left": {
                    nodes[index].node_id: nodes[item].node_id if item is not None else None
                    for index, item in values["match_left"].items()
                },
                "match_right": {
                    nodes[index].node_id: nodes[item].node_id if item is not None else None
                    for index, item in values["match_right"].items()
                },
                "paths": [[node.node_id for node in path] for path in result],
            }

        previous = sys.getprofile()
        sys.setprofile(returned)
        try:
            return path_cover(nodes, rules, rng)
        finally:
            sys.setprofile(previous)

    construct = reference.construct_initial_plan

    def construct_initial_plan(paths: Any, rules: Any, factory: Any) -> Any:
        chains = construct(paths, rules, factory)
        capture["initial_plan"] = snapshot(chains, reference.evaluate_plan(chains, rules))
        return chains

    permit = reference.SearchBudget.permit

    def budget_permit(budget: Any, count: int = 1) -> bool:
        frame = sys._getframe(1)
        phase = _phase(frame)
        section = active["section"]
        if phase != active.get("phase"):
            if active.get("phase") is not None:
                section["phase_boundaries"].append(
                    {
                        "event": "end",
                        "phase": active["phase"],
                        "candidate_checks": budget.candidate_checks,
                        "quality": active["quality"],
                    }
                )
            section["phase_boundaries"].append(
                {
                    "event": "start",
                    "phase": phase,
                    "candidate_checks": budget.candidate_checks,
                    "quality": active["quality"],
                }
            )
            active["phase"] = phase
        before = budget.candidate_checks
        allowed = permit(budget, count)
        if allowed and count:
            site_counts[f"{phase}:{frame.f_code.co_name}:{frame.f_lineno}"] += count
        if not allowed:
            capture["budget_stops"].append(
                {
                    "phase": phase,
                    "requested_count": count,
                    "candidate_checks": before,
                    "stop_reason": budget.stop_reason,
                    "source_line": frame.f_lineno,
                }
            )
        return allowed

    def search_wrapper(function: Any, split: bool = False) -> Any:
        def run(chains: Any, rules: Any, factory: Any, budget: Any, callback: Any) -> Any:
            section: dict[str, Any] = {"accepted_actions": [], "phase_boundaries": []}
            if split:
                capture["split"] = section
            else:
                section["round"] = len(capture["search_rounds"]) + 1
                capture["search_rounds"].append(section)
            initial_evaluation = reference.evaluate_plan(chains, rules)
            section["start"] = snapshot(chains, initial_evaluation, budget)
            active.update(section=section, phase=None, quality=list(initial_evaluation.quality))

            def improved(candidate: Any, evaluation: Any) -> None:
                frame = sys._getframe(1)
                values = frame.f_locals
                accepted_count = values.get("move_count", 0) if split else values["improvements"]
                if accepted_count:
                    phase = _phase(frame)
                    action = {
                        "phase": phase,
                        "source_line": frame.f_lineno,
                        "quality_before": active["quality"],
                        **snapshot(candidate, evaluation, budget),
                    }
                    index_fields = {
                        "whole_chain": ("donor_index", "target_index"),
                        "single_order": ("donor_index", "target_index", "node_index", "position"),
                        "virtual_fill": ("target_index", "position"),
                        "controlled_split": ("chain_index", "node_index"),
                    }
                    node_fields = {
                        "whole_chain": (),
                        "single_order": ("moved_node",),
                        "virtual_fill": ("filler", "prototype"),
                        "controlled_split": ("node",),
                    }
                    action.update({key: values[key] for key in index_fields[phase]})
                    action.update({key: asdict(values[key]) for key in node_fields[phase]})
                    if phase == "whole_chain":
                        action["merged_node_ids"] = [
                            node.node_id for node in values["merged_nodes"]
                        ]
                    if split:
                        action.update(
                            split_mode="FUTURE_BORROW_RETURN",
                            origin_assigned_period=values["assigned_period"],
                            target_assigned_period=values["node"].source_period,
                            fragments=[asdict(node) for node in values["fragments"]],
                            separators=[asdict(node) for node in values["separators"]],
                        )
                    section["accepted_actions"].append(action)
                    active["quality"] = list(evaluation.quality)
                callback(candidate, evaluation)

            result, evaluation, count = function(chains, rules, factory, budget, improved)
            if active["phase"] is not None:
                section["phase_boundaries"].append(
                    {
                        "event": "end",
                        "phase": active["phase"],
                        "candidate_checks": budget.candidate_checks,
                        "quality": list(evaluation.quality),
                    }
                )
            section["accepted_count"] = count
            section["final"] = snapshot(result, evaluation, budget)
            return result, evaluation, count

        return run

    reference.normalize_nodes = normalize_nodes
    reference.maximum_path_cover = maximum_path_cover
    reference.construct_initial_plan = construct_initial_plan
    reference.SearchBudget.permit = budget_permit
    reference.local_search = search_wrapper(reference.local_search)
    reference.final_cross_period_split_return = search_wrapper(
        reference.final_cross_period_split_return, split=True
    )
    try:
        report = reference.solve(
            reference.parse_args(solver_argv(manifest, output_dir, time_budget_seconds=600.0))
        )
    finally:
        sys.modules.pop(module_name, None)
        verify_manifest(manifest)
    run_manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    if report.get("status") not in {"SUCCESS", "BEST_EFFORT"}:
        raise ValueError(f"reference input or execution failed: {report.get('status')}")
    if run_manifest["stop_reason"] == "time_budget_exhausted":
        raise ValueError(
            "stage capture hit its safety ceiling; do not freeze a timing-dependent trace"
        )
    with (output_dir / "schedule_result.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    capture["candidate_count_sites"] = dict(site_counts)
    capture["final"] = {
        "quality": report["seven_level_quality"]["vector"],
        "metrics": report["result_metrics"],
        "coverage": report["coverage_and_weight_conservation"],
        "candidate_checks": run_manifest["candidate_checks"],
        "stop_reason": run_manifest["stop_reason"],
        "ordered_node_ids": [row["node_id"] for row in rows],
        "run_manifest": run_manifest,
        "artifact_hashes": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            for name in sorted(OUTPUT_NAMES)
        },
    }
    return capture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--capture-stages", action="store_true")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.manifest)
    observed = verify_manifest(manifest)
    if args.verify_only:
        if args.output_dir or args.capture_stages:
            parser.error("--verify-only cannot be combined with execution options")
        print(json.dumps({"verified": True, "hashes": observed}, ensure_ascii=False))
        return 0
    if args.output_dir is None:
        parser.error("execution requires --output-dir pointing to a new or empty directory")
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir())):
        parser.error(
            "output directory must be new or empty; existing artifacts are never overwritten"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.capture_stages:
        capture = capture_stages(manifest, output_dir)
        with (output_dir / "stage_capture.json").open("x", encoding="utf-8") as stream:
            json.dump(capture, stream, ensure_ascii=False, indent=2, default=_json_value)
            stream.write("\n")
        print(
            json.dumps(
                {
                    "captured": True,
                    "output_dir": str(output_dir),
                    "candidate_checks": capture["final"]["candidate_checks"],
                }
            )
        )
        return 0
    try:
        return subprocess.run(
            [sys.executable, str(resolve_path(manifest["script"]["path"]))]
            + solver_argv(manifest, output_dir),
            check=False,
        ).returncode
    finally:
        verify_manifest(manifest)


if __name__ == "__main__":
    raise SystemExit(main())
