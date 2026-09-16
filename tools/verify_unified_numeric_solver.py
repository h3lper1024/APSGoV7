"""Freeze bounded, ordered whole-flow evidence without changing the solver."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from contextlib import ExitStack, contextmanager
from dataclasses import fields
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import numpy as np
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core import _numeric_search as search
from apsgo_scheduler.core import _numeric_solver as solver
from apsgo_scheduler.core.budget import SolveRuntimeBudget
from tools.profile_numeric_solver import derive_numeric_request, _request_envelope, _run_once
from tools.profile_solver_search import load_request
from tools.verify_solver_diagnostics import _sha256, _write_json


def plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


class OrderedDigest:
    """Order-sensitive full stream hash with bounded first/last examples."""

    def __init__(self, sample_limit=8):
        if type(sample_limit) is not int or sample_limit < 1:
            raise ValueError("positive sample limit required")
        self.count = 0
        self.digest = sha256()
        self.first = []
        self.last = deque(maxlen=sample_limit)
        self.sample_limit = sample_limit

    def add(self, record):
        record = plain(record)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.digest.update(payload.encode("utf-8") + b"\n")
        self.count += 1
        if len(self.first) < self.sample_limit:
            self.first.append(record)
        self.last.append(record)

    def snapshot(self):
        return dict(count=self.count, sha256=self.digest.hexdigest(),
                    first=self.first, last=list(self.last))


def state_snapshot(state, budget):
    return dict(task=state.task.fingerprint, plan=state.plan.fingerprint,
                generation=state.plan.generation, quality=plain(state.evaluation.quality_key),
                candidate_checks=budget.candidate_check_count,
                complete_evaluations=state.complete_candidate_evaluation_count,
                accepted=state.accepted_move_count, virtual_sequence=state.virtual_sequence,
                split_sequence=state.split_sequence, replay_count=state.replay_count,
                stop_reason=plain(budget.stop_reason))


class WholeFlowTrace:
    """Diagnostic hooks are installed only around one explicit measurement run."""

    def __init__(self):
        self.stage = "preparation"
        self.streams = {key: OrderedDigest() for key in (
            "quota", "edits", "refinement_proposals", "evaluated_attempts", "accepted"
        )}
        self.phases = []
        self.construction = {}
        self.calls = Counter()
        self.refinement_diagnostics = None

    def snapshot(self):
        return dict(schema="unified_numeric_baseline_v1",
                    streams={key: value.snapshot() for key, value in self.streams.items()},
                    phases=self.phases, construction=self.construction,
                    refinement_diagnostics=self.refinement_diagnostics)

    @contextmanager
    def install(self):
        with ExitStack() as stack:
            def bind(owner, name, factory):
                stack.enter_context(patch.object(owner, name, factory(getattr(owner, name))))

            def permit(original):
                def wrapped(budget, count=1):
                    allowed = original(budget, count)
                    if count:
                        self.streams["quota"].add((self.stage, budget.candidate_check_count,
                                                   count, allowed, plain(budget.stop_reason)))
                        if allowed and budget.candidate_check_count % 50000 == 0:
                            print(json.dumps({"phase": self.stage,
                                              "checks": budget.candidate_check_count}), flush=True)
                    return allowed
                return wrapped

            def edit(original):
                def wrapped(item):
                    original(item)
                    self.streams["edits"].add((self.stage, {f.name: getattr(item, f.name)
                                                         for f in fields(item)}))
                return wrapped

            def attempt(original):
                def wrapped(state, budget, edit, *args, **kwargs):
                    before = budget.candidate_check_count
                    accepted = original(state, budget, edit, *args, **kwargs)
                    self.streams["evaluated_attempts"].add((self.stage, before, accepted,
                        state.complete_candidate_evaluation_count, state.plan.fingerprint))
                    return accepted
                return wrapped

            def proposal(original):
                def wrapped(state, budget, recipe, *args, **kwargs):
                    self.streams["refinement_proposals"].add((state.plan.generation,
                        budget.candidate_check_count, recipe))
                    return original(state, budget, recipe, *args, **kwargs)
                return wrapped

            def commit(original):
                def wrapped(state, *args, **kwargs):
                    result = original(state, *args, **kwargs)
                    move = state.accepted_moves[-1]
                    self.streams["accepted"].add(dict(stage=self.stage,
                        move={f.name: getattr(move, f.name) for f in fields(move)},
                        task=state.task.fingerprint, plan=state.plan.fingerprint,
                        virtual_sequence=state.virtual_sequence, split_sequence=state.split_sequence))
                    return result
                return wrapped

            def phase(name, state_position=0, budget_position=1):
                def factory(original):
                    def wrapped(*args, **kwargs):
                        state, budget = args[state_position], args[budget_position]
                        self.calls[name] += 1
                        previous = self.stage
                        self.stage = f"{name}:{self.calls[name]}"
                        record = dict(stage=self.stage, before=state_snapshot(state, budget))
                        self.phases.append(record)
                        try:
                            return original(*args, **kwargs)
                        finally:
                            record["after"] = state_snapshot(state, budget)
                            if name == "refinement":
                                # Physical precomputation is not logical quota or business output.
                                values = kwargs["diagnostics"].snapshot()
                                self.refinement_diagnostics = {k: v for k, v in values.items()
                                                               if "seconds" not in k}
                            print(json.dumps({"phase": self.stage, **record["after"]}), flush=True)
                            self.stage = previous
                    return wrapped
                return factory

            def construct(name, array_names):
                def factory(original):
                    def wrapped(*args, **kwargs):
                        result = original(*args, **kwargs)
                        record = {key: plain(getattr(result, key)) for key in array_names}
                        if name == "initial":
                            record["plan"] = None if result.plan is None else result.plan.fingerprint
                        self.construction[name] = record
                        print(json.dumps({"phase": name, "complete": result.complete}), flush=True)
                        return result
                    return wrapped
                return factory

            bind(SolveRuntimeBudget, "permit", permit)
            bind(search.NumericCandidateEdit, "__post_init__", edit)
            bind(search.NumericSearchState, "commit", commit)
            bind(search, "_try_prepared_candidate", attempt)
            bind(refinement, "_try_overlay_candidate", attempt)
            bind(refinement, "_try_recipe", proposal)
            # Both imported aliases must be hooked: first search and unique split replay.
            for owner in (search, refinement):
                bind(owner, "_run_numeric_local_search", phase("local_search"))
            bind(refinement, "improve_numeric_controlled_split", phase("split_and_replay"))
            bind(refinement, "improve_numeric_refinement", phase("refinement"))
            for name in ("whole_chain", "real_node_relocation", "virtual_weight_fill", "chain_order"):
                bind(search, f"improve_numeric_{name}", phase(name, 3, 4))
            bind(solver, "build_numeric_construction_graph", construct("graph", (
                "ordered_rows", "adjacency_offsets", "adjacency_rows", "fingerprint",
                "checked_edge_count", "allowed_edge_count")))
            bind(solver, "numeric_minimum_path_cover", construct("cover", (
                "matching_successor", "path_offsets", "path_rows", "fingerprint")))
            bind(solver, "construct_numeric_initial_plan", construct("initial", ("complete",)))
            yield self


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    source = load_request(args.prepared_request)
    request = derive_numeric_request(source)
    trace = WholeFlowTrace()
    _write_json(args.output_dir / "input_identity.json", dict(
        source_request_sha256=_sha256(args.prepared_request),
        numeric_request=_request_envelope(request),
        tool_sha256=_sha256(Path(__file__)), platform=platform.platform(),
        python=platform.python_version(),
        production_files={str(p.relative_to(ROOT)): _sha256(p)
                          for p in sorted((ROOT / "src").rglob("*.py"))},
    ))
    try:
        with trace.install():
            measurement = _run_once(request, args.output_dir / "run_01")
    finally:
        _write_json(args.output_dir / "trace.json", trace.snapshot())
        _write_json(args.output_dir / "timing_boundary.json", {
            "load_derive_solve_report_and_write_seconds": str(perf_counter() - started),
            "mode": "diagnostic_not_performance",
            "excluded": ["interpreter_startup", "imports", "HTTP"],
            "solver_timer": "measurement.json: solve_request including both audits and return object",
        })
    return 0 if measurement["core_audit"]["passed"] and measurement["application_audit"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
