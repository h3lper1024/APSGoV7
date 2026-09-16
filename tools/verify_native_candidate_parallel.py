"""Bounded real-descriptor whole-candidate native serial/parallel experiment."""

import argparse
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter, process_time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import numba
import numpy as np
from apsgo_scheduler.core import _numeric_refinement as refinement
from apsgo_scheduler.core._numeric_batch import NumericCandidateBatchWorkspace
from apsgo_scheduler.core._numeric_candidate_kernel import NumericDeferredCandidateFailure
from apsgo_scheduler.core._numeric_evaluation import NumericEvaluationContext
from apsgo_scheduler.core._numeric_state import NumericNodeColumns
from tools import verify_unified_numeric_search_prefix as prefix
from tools.profile_numeric_solver import _peak_rss_bytes
from tools.verify_numeric_serial_batch import record_json
from tools.verify_solver_diagnostics import _code_identity, _sha256


class SamplesReady(Exception):
    """Intentional diagnostic stop; not a successful or failed full solve."""


def capture_samples(request, destination, count):
    contexts, groups, positions = [], [], {}
    original, total = refinement._prepare_descriptor_batch, 0

    def capture(state, budget, descriptions, maximum, diagnostics, key, pool=None):
        nonlocal total
        identity = state.plan.fingerprint
        if identity not in positions:
            positions[identity] = len(contexts)
            contexts.append(NumericEvaluationContext(state.task, state.program, state.quality,
                state.plan, state.evaluation))
        selected = descriptions[:count - total]
        entries = [(d.copy(), *refinement._descriptor_settings(state, d, maximum)) for d in selected]
        for d, _, _ in entries:
            d.setflags(write=False)
        groups.append((positions[identity], entries, state.virtual_sequence, state.split_sequence))
        total += len(entries)
        if total == count:
            raise SamplesReady()
        return original(state, budget, descriptions, maximum, diagnostics, key, pool)

    args = ["verify_unified_numeric_search_prefix.py", "--source-root", str(ROOT),
        "--prepared-request", str(request), "--through-split", "--refinement-checks", "20000",
        "--output-dir", str(destination)]
    with patch.object(sys, "argv", args), patch.object(refinement, "_prepare_descriptor_batch", capture):
        try:
            prefix.main()
        except SamplesReady:
            pass
    if total != count:
        raise AssertionError(f"expected {count} real descriptions; captured {total}")
    return contexts, groups


def result_digest(attempts):
    digest = hashlib.sha256()

    def scalar(value):
        digest.update(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())

    def array(value):
        scalar((str(value.dtype), value.shape))
        digest.update(value.tobytes(order="C"))

    scalar(len(attempts))
    for workspace, result in attempts:
        if isinstance(result, NumericDeferredCandidateFailure):
            scalar(("error", str(result.error)))
            continue
        scalar((result.status, result.prepared, result.admissible,
            result.virtual_sequence, result.split_sequence, result.cleaned_variant_exists,
            workspace.chain_count, workspace.changed_count, workspace.node_count, workspace.event_count))
        array(result.affected_rows)
        array(result.descriptor)
        for name in ("starts", "stops", "private", "ids", "periods"):
            array(getattr(workspace, name)[:workspace.chain_count])
        array(workspace.changed_rows[:workspace.changed_count])
        for field in fields(NumericNodeColumns):
            array(getattr(workspace.nodes, field.name)[:workspace.node_count])
        for name in workspace.derived._fields:
            array(getattr(workspace.derived, name)[:, :workspace.node_count])
        for name in ("event_node_ends", "event_group_ends"):
            array(getattr(workspace, name)[:workspace.event_count])
        scalar(result.summary is not None)
        if result.summary is not None:
            for value in result.summary:
                array(value)
    return digest.hexdigest()


def measure(contexts, groups, mode, expected=None):
    pools = [NumericCandidateBatchWorkspace(c.task, c.program, c.quality, c.plan,
        c.previous_evaluation, _native_executor=None if mode == "single" else mode) for c in contexts]
    digests, wall, cpu, evaluated = [], 0.0, 0.0, 0
    for context_index, entries, virtual_sequence, split_sequence in groups:
        pool = pools[context_index]
        started, cpu_started = perf_counter(), process_time()
        if mode == "single":
            results = [list(pool.attempts(d, p, variants, virtual_sequence=virtual_sequence,
                split_sequence=split_sequence, allows_continue=lambda: True)) for d, p, variants in entries]
        else:
            results = pool.prepare_many(entries, virtual_sequence=virtual_sequence,
                split_sequence=split_sequence, allows_continue=lambda: True)
        wall += perf_counter() - started
        cpu += process_time() - cpu_started
        # Verification is outside the measured candidate preparation interval.
        for attempts in results:
            digest = result_digest(attempts)
            if expected is not None and digest != expected[len(digests)]:
                raise AssertionError(f"first candidate output difference: {mode}, descriptor {len(digests)}")
            digests.append(digest)
            evaluated += sum(not isinstance(r, NumericDeferredCandidateFailure) and r.summary is not None
                             for _, r in attempts)
        started, cpu_started = perf_counter(), process_time()
        pool.release()
        wall += perf_counter() - started
        cpu += process_time() - cpu_started
    return digests, {"wall_seconds": wall, "cpu_seconds": cpu, "evaluated_attempts": evaluated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-request", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    contexts, groups = capture_samples(args.prepared_request, args.output_dir / "sampling_prefix", 256)
    source = _code_identity(ROOT)
    record_json(args.output_dir / "samples.json", {
        "source": source, "tool_sha256": _sha256(Path(__file__)), "input_sha256": _sha256(args.prepared_request),
        "sampling": "normal prefix; intentionally interrupted after 256 generated descriptors; not a final solve",
        "contexts": [{"task": c.task.fingerprint, "plan": c.plan.fingerprint,
            "program": c.program.fingerprint, "quality": c.quality.fingerprint} for c in contexts],
        "groups": [{"context": index, "virtual_sequence": virtual, "split_sequence": split,
            "descriptions": [d.tolist() for d, _, _ in entries],
            "variants": [variants for _, _, variants in entries],
            "policies": [{"bridge": p.maximum_bridge_nodes, "maximum_weight": p.maximum_changed_chain_weight,
                "reject": [kind.value for kind in p.reject_prohibited_kinds]} for _, p, _ in entries]}
            for index, entries, virtual, split in groups],
    })
    print(json.dumps({"phase": "samples_ready", "descriptions": 256, "contexts": len(contexts)}), flush=True)
    previous_threads = numba.get_num_threads()
    if previous_threads < 8:
        raise RuntimeError("this experiment requires the configured Numba thread maximum to allow eight workers")
    modes = [("single", "single", 1), ("native_serial", "serial", 1)]
    modes += [(f"parallel_{threads}", "parallel", threads) for threads in (1, 2, 4, 8)]
    expected, results = None, {}
    try:
        for label, mode, threads in modes:
            numba.set_num_threads(threads)
            values, warm = measure(contexts, groups, mode, expected)
            if expected is None:
                expected = values
            results[label] = {"warmup": warm, "threads": threads, "samples": []}
            print(json.dumps({"phase": "warmup", "mode": label}), flush=True)
        # Rotate ordering to reduce one-way warm-cache/thermal ordering bias.
        for repeat in range(5):
            for label, mode, threads in modes[repeat:] + modes[:repeat]:
                numba.set_num_threads(threads)
                _, sample = measure(contexts, groups, mode, expected)
                results[label]["samples"].append(sample)
            print(json.dumps({"phase": "hot_round_complete", "round": repeat + 1}), flush=True)
    finally:
        numba.set_num_threads(previous_threads)
    for value in results.values():
        value["median_wall_seconds"] = statistics.median(s["wall_seconds"] for s in value["samples"])
        value["median_cpu_seconds"] = statistics.median(s["cpu_seconds"] for s in value["samples"])
    record_json(args.output_dir / "hotspot.json", {
        "source": source, "tool_sha256": _sha256(Path(__file__)), "input_sha256": _sha256(args.prepared_request), "descriptions": 256,
        "scope": "descriptor validation, private frame preparation, repair, resources, evaluation, output wrapping and release; base context/pool construction, verification, source prefix and final audit excluded; not end-to-end",
        "all_candidate_outputs_equal": True, "output_digests": expected,
        "environment": {"platform": platform.platform(), "python": sys.version, "numpy": np.__version__,
            "numba": numba.__version__, "logical_cpus": os.cpu_count(), "numba_maximum_threads": previous_threads,
            "process_peak_rss_bytes_after_all_modes": _peak_rss_bytes()}, "results": results,
    })
    print(json.dumps({label: value["median_wall_seconds"] for label, value in results.items()}), flush=True)


if __name__ == "__main__":
    main()
