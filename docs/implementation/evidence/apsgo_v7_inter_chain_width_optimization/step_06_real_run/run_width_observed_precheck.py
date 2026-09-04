"""Observe the original public precheck without changing its six audited outputs."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

LEGACY_HELPER = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_22_inter_chain_width_gap/step_22_4_real_comparison/run_observed_precheck.py"
)
PRECHECK = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_21_complete_acceptance/quality_precheck.py"
)
COUNT_NAMES = (
    "candidate_check_count",
    "complete_candidate_evaluation_count",
    "accepted_move_count",
)
ACTION_NAMES = (
    "width_node_move",
    "width_node_exchange",
    "width_block_move",
    "width_block_exchange",
    "width_chain_cut",
    "width_chain_order_relocation",
)
ORIGINAL_ARTIFACTS = frozenset(
    (
        "public_result.canonical.json",
        "accepted_trace.canonical.json",
        "schedule_detail.csv",
        "chain_detail.csv",
        "source_conservation.csv",
    )
)


def load_helpers(code_root):
    root = Path(code_root).resolve(strict=True)
    path = root / LEGACY_HELPER
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError("the legacy observer helper must be a regular file inside the code root")
    spec = importlib.util.spec_from_file_location("width_observer_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_checker(code_root):
    # This existing loader rejects mixed revision imports before selecting src/tests.
    return load_helpers(code_root).load_checker(code_root)


def _reason(context):
    reason = context.factory.budget.stop_reason
    return reason.value if reason is not None else None


def _delta(before, after):
    return {key: after[key] - before[key] for key in COUNT_NAMES}


@contextmanager
def observe_precheck(checker):
    """Read existing values only: no candidate peeking, budget calls or clock polling."""
    counters = load_helpers(checker.ROOT).counters
    split = importlib.import_module("apsgo_scheduler.core.controlled_split")
    width = importlib.import_module("apsgo_scheduler.core.width_optimization")
    observed = {
        "stages": [],
        "actions": {
            name: {**dict.fromkeys(COUNT_NAMES, 0), "exception_count": 0} for name in ACTION_NAMES
        },
        "batches": [],
        "core_calls": [],
        "public_results": [],
    }

    def snapshot(state, context):
        return {
            "counters": counters(context),
            "quality_key": state.current_evaluation.quality_key,
            "chain_count": len(state.current_plan.chains),
            "stop_reason": _reason(context),
        }

    def stage_wrapper(name, original, *, nested=False):
        def call(state, context):
            row = {"stage": name, "nested": nested, "before": snapshot(state, context)}
            observed["stages"].append(row)
            try:
                return original(state, context)
            finally:
                row["after"] = snapshot(state, context)
                row["delta"] = _delta(row["before"]["counters"], row["after"]["counters"])

        return call

    original_core, original_public = checker.core.solve, checker.solve_request

    def core_call(problem, rule_set, policy, runtime):
        result = original_core(problem, rule_set, policy, runtime)
        observed["core_calls"].append((problem, rule_set, policy, result))
        return result

    def public_call(request):
        result = original_public(request)
        observed["public_results"].append(result)
        return result

    original_recipe, original_batch = width._try_width_recipe, width._scan_width_batch

    def recipe_call(state, context, recipe):
        before = counters(context)
        row = observed["actions"][recipe[0]]
        # The scanner has already consumed this check before calling the recipe.
        row["candidate_check_count"] += 1
        try:
            return original_recipe(state, context, recipe)
        except Exception:
            row["exception_count"] += 1
            raise
        finally:
            after = counters(context)
            for key in COUNT_NAMES[1:]:
                row[key] += after[key] - before[key]

    def batch_call(state, context, recipes, try_recipe, allowance):
        row = {
            "family": recipes.gi_code.co_name,
            "generation": state.accepted_move_count,
            "allowance": allowance,
            "before": counters(context),
            "accepted": False,
            "exhausted": False,
            "outcome": "exception",
        }
        observed["batches"].append(row)
        try:
            result = original_batch(state, context, recipes, try_recipe, allowance)
            row["accepted"], row["exhausted"] = result
            row["outcome"] = (
                "accepted"
                if result[0]
                else "naturally_exhausted"
                if result[1]
                else "interrupted"
                if context.factory.budget.stop_reason is not None
                else "batch_exhausted"
            )
            return result
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            row["after"] = counters(context)
            row["delta"] = _delta(row["before"], row["after"])
            row["stop_reason"] = _reason(context)

    with ExitStack() as stack:
        stack.enter_context(patch.object(checker.core, "solve", core_call))
        stack.enter_context(patch.object(checker, "solve_request", public_call))
        for name in ("run_local_search", "run_controlled_order_split", "run_width_optimization"):
            label = {
                "run_local_search": "local_search",
                "run_controlled_order_split": "controlled_split_and_replay",
                "run_width_optimization": "width_optimization",
            }[name]
            stack.enter_context(
                patch.object(
                    checker.core_solver,
                    name,
                    stage_wrapper(label, getattr(checker.core_solver, name)),
                )
            )
        stack.enter_context(
            patch.object(
                split,
                "run_local_search",
                stage_wrapper("post_split_replay", split.run_local_search, nested=True),
            )
        )
        stack.enter_context(patch.object(width, "_try_width_recipe", recipe_call))
        stack.enter_context(patch.object(width, "_scan_width_batch", batch_call))
        yield observed


def reconcile_counts(observed, core_metrics):
    """Reconcile inclusive root stages and width details without counting replay twice."""
    zero = dict.fromkeys(COUNT_NAMES, 0)

    def checked(values):
        if any(type(values[key]) is not int or values[key] < 0 for key in COUNT_NAMES):
            raise ValueError("observed counters must be nonnegative integers")
        return values

    def total(rows):
        return {key: sum(checked(row)[key] for row in rows) for key in COUNT_NAMES}

    for row in observed["stages"]:
        before = checked(row["before"]["counters"])
        after = checked(row["after"]["counters"])
        if checked(row["delta"]) != _delta(before, after):
            raise ValueError("stage counter delta does not match its snapshots")
    stages = observed["stages"]
    stage_totals = total([row["delta"] for row in stages if not row["nested"]])
    if stage_totals != {key: getattr(core_metrics, key) for key in COUNT_NAMES}:
        raise ValueError("root stage counters disagree with actual core metrics")
    replays = [row for row in stages if row["stage"] == "post_split_replay"]
    parents = [row for row in stages if row["stage"] == "controlled_split_and_replay"]
    width = [row for row in stages if row["stage"] == "width_optimization"]
    if len(width) > 1 or len(replays) > 1 or (replays and len(parents) != 1):
        raise ValueError("unexpected duplicate width phase or split replay")
    if replays and (
        not replays[0]["nested"]
        or any(replays[0]["delta"][key] > parents[0]["delta"][key] for key in COUNT_NAMES)
    ):
        raise ValueError("replay counters must remain inside their split parent")
    width_delta = width[0]["delta"] if width else zero
    action_totals = total(list(observed["actions"].values()))
    for row in observed["batches"]:
        before, after = checked(row["before"]), checked(row["after"])
        if checked(row["delta"]) != _delta(before, after):
            raise ValueError("batch counter delta does not match its snapshots")
        if row["delta"]["candidate_check_count"] > row["allowance"]:
            raise ValueError("batch exceeded its original allowance")
    batch_totals = total([row["delta"] for row in observed["batches"]])
    if action_totals != width_delta or batch_totals != width_delta:
        raise ValueError("width action or batch counters disagree with the width stage")
    return {
        "stage_totals": stage_totals,
        "width_action_totals": action_totals,
        "width_batch_totals": batch_totals,
    }


def dependency_hashes(checker):
    return {
        str(path): checker.sha256(checker.ROOT / path)
        for path in (LEGACY_HELPER, PRECHECK, Path("tools/capture_solverpy_reference.py"))
    }


def write_supplement(checker, observed, output, revision):
    from apsgo_scheduler.core.chain_order import chain_order_objective_index

    output = Path(output)
    targets = (output / "width_observation.json", output / "width_observation_manifest.json")
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("width observation output already exists")
    if len(observed["core_calls"]) != 1 or len(observed["public_results"]) != 1:
        raise ValueError("evidence requires exactly one actual core call and public result")
    problem, rules, policy, core = observed["core_calls"][0]
    result = observed["public_results"][0]
    report_path = output / "quality_report.json"
    if report_path.is_symlink() or not report_path.is_file():
        raise ValueError("original quality report must be a regular file")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report["code_revision"] != revision or set(report["artifacts_sha256"]) != ORIGINAL_ARTIFACTS:
        raise ValueError("original report does not bind the revision and exact audited artifacts")
    for name, digest in report["artifacts_sha256"].items():
        path = output / name
        if path.is_symlink() or not path.is_file() or checker.sha256(path) != digest:
            raise ValueError(f"original precheck artifact hash mismatch: {name}")
    identities = {
        "request_fingerprint": result.run_manifest.request_fingerprint,
        "problem_fingerprint": problem.input_fingerprint,
        "rule_set_fingerprint": rules.fingerprint,
        "policy_fingerprint": checker.fingerprint(policy),
    }
    if (
        report["public_result_fingerprint"] != result.result_fingerprint
        or report["core_result_fingerprint"] != core.core_result_fingerprint
        or report["identities"] != identities
        or any(getattr(result.run_manifest, key) != value for key, value in identities.items())
    ):
        raise ValueError("observed result or input identities differ from the original precheck")
    trace = checker.fingerprint(core.trace)
    if trace != report["trace_fingerprint"] or trace != result.run_manifest.trace_fingerprint:
        raise ValueError("observed core/public trace differs from the original precheck")
    totals = reconcile_counts(observed, core.metrics)
    index = chain_order_objective_index(rules)
    if index is None:
        raise ValueError("width observation requires a declared enabled width objective")
    roots = [row for row in observed["stages"] if not row["nested"]]
    final_quality = result.release.evaluation.quality_key if result.release else None
    observation = {
        "schema_version": 1,
        "code_revision": revision,
        "quality_gate_passed": report["passed"],
        "problem_fingerprint": problem.input_fingerprint,
        "rule_set_fingerprint": rules.fingerprint,
        "policy_fingerprint": checker.fingerprint(policy),
        "public_result_fingerprint": result.result_fingerprint,
        "core_result_fingerprint": core.core_result_fingerprint,
        "trace_fingerprint": trace,
        "width_quality_position": index + 1,
        "initial_quality_key": roots[0]["before"]["quality_key"] if roots else None,
        "final_quality_key": final_quality,
        "final_width_gap": final_quality[index] if final_quality is not None else None,
        "stages": observed["stages"],
        "actions": observed["actions"],
        "batches": observed["batches"],
        "reconciled_counts": totals,
        "instrumentation": "Evidence-only wrappers return original objects and read existing counters; no candidate peeking, budget, cancellation or solver-clock polling. Timings include observer overhead and are not performance acceptance.",
    }
    with targets[0].open("x", encoding="utf-8") as stream:
        json.dump(observation, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.write("\n")
    manifest = {
        "schema_version": 1,
        "code_revision": revision,
        "quality_report_sha256": checker.sha256(report_path),
        "runner_sha256": checker.sha256(Path(__file__)),
        "dependency_sha256": dependency_hashes(checker),
        "artifacts_sha256": {targets[0].name: checker.sha256(targets[0])},
    }
    with targets[1].open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return observation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--code-repository", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    checker = load_checker(args.code_root)
    dependencies = dependency_hashes(checker)
    adapter_hash = checker.sha256(Path(__file__))
    with observe_precheck(checker) as observed:
        status = checker.main(
            [
                "--code-repository",
                str(args.code_repository),
                "--code-revision",
                args.code_revision,
                "--output-dir",
                str(args.output_dir),
            ]
        )
    if dependencies != dependency_hashes(checker) or adapter_hash != checker.sha256(Path(__file__)):
        raise ValueError("observer or dependency bytes changed during the solve")
    write_supplement(checker, observed, args.output_dir, args.code_revision)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
