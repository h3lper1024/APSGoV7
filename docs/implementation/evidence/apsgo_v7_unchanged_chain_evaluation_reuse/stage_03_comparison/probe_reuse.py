"""Observe first-search reuse through the existing profiler; not a timing sample."""

import argparse
import sys
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PREPARED_SHA256 = "11bdd096717414a25b14e7a219b860e6e84953f17e9c02bbf8e298da9266829c"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("code-root", "prepared-request", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.code_root.resolve(strict=True)
    output = args.output_dir.absolute()
    if output.exists():
        raise FileExistsError(output)
    sys.path[:0] = [str(root), str(root / "src")]
    from apsgo_scheduler.core import neighborhoods
    from apsgo_scheduler.core.rules.rule_set import ProcessRuleSet
    from tools import profile_solver_search as profiler
    from tools.verify_solver_diagnostics import _sha256, _write_json

    if profiler.ROOT != root or Path(neighborhoods.__file__).resolve() != (
        root / "src/apsgo_scheduler/core/neighborhoods.py"
    ):
        raise ValueError("loaded solver/profile tool does not belong to --code-root")
    if _sha256(args.prepared_request) != PREPARED_SHA256:
        raise ValueError("prepared request differs from the stage 0 frozen bytes")

    counts = Counter(dict.fromkeys((
        "candidate_attempts", "candidate_completed", "fallback_candidates", "cold_candidates",
        "possible_chain_evaluations", "possible_node_evaluations", "reused_chain_entries",
        "reused_node_evaluations", "new_chain_entries", "zero_hit_candidates",
    ), 0))
    maxima, dispatch, candidate_dispatch = Counter(), Counter(), Counter()
    summary = {
        "status": "running", "code_root": str(root), "probe_sha256": _sha256(Path(__file__)),
        "prepared_request_sha256": PREPARED_SHA256,
        "scope": "public solve until first-search exit; includes initial and quick checks",
        "timing_warning": "profiling and counting overhead; excluded from five timing pairs",
        "retention_scope": "current bound holder plus one evaluated candidate, not process bytes",
        "node_count_semantics": "distinct Node object identities within each entry group",
        "counts": counts, "maxima": maxima, "dispatch": dispatch,
        "candidate_dispatch": candidate_dispatch,
    }
    in_candidate = False
    original_candidate = neighborhoods._evaluate_candidate_plan

    def retained(current, candidate):
        distinct = {id(entry): entry for entry in (*current.values(), *candidate.values())}
        groups = (("current", current), ("candidate", candidate), ("distinct", distinct))
        for name, entries in groups:
            maxima[name + "_entries"] = max(maxima[name + "_entries"], len(entries))
            nodes = {id(node) for entry in entries.values() for node in entry.chain.nodes}
            maxima[name + "_nodes"] = max(maxima[name + "_nodes"], len(nodes))

    def candidate(plan, state, rule_set, rule_context, previous):
        nonlocal in_candidate
        counts["candidate_attempts"] += 1
        current = previous.entries if (
            previous is not None and previous.matches(state, rule_set, rule_context)
        ) else {}
        retained(current, {})
        counts["possible_chain_evaluations"] += len(plan.chains)
        counts["possible_node_evaluations"] += sum(len(chain.nodes) for chain in plan.chains)
        in_candidate = True
        try:
            result = original_candidate(plan, state, rule_set, rule_context, previous)
        finally:
            in_candidate = False
        _, holder = result
        counts["candidate_completed"] += 1
        if holder is None:
            counts["fallback_candidates"] += 1
            return result
        counts["cold_candidates"] += int(not current)
        reused = {key: entry for key, entry in holder.entries.items() if current.get(key) is entry}
        counts["reused_chain_entries"] += len(reused)
        counts["reused_node_evaluations"] += sum(
            len(entry.node_contributions) for entry in reused.values()
        )
        counts["new_chain_entries"] += len(holder.entries) - len(reused)
        counts["zero_hit_candidates"] += int(not reused)
        retained(current, holder.entries)
        return result

    def observe(name, original):
        def wrapped(self, *args, **kwargs):
            dispatch[name] += 1
            if in_candidate:
                candidate_dispatch[name] += 1
            return original(self, *args, **kwargs)
        return wrapped

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(neighborhoods, "_evaluate_candidate_plan", candidate))
            for name in ("evaluate_complete_chain", "evaluate_node", "evaluate_plan"):
                stack.enter_context(patch.object(
                    ProcessRuleSet, name, observe(name, getattr(ProcessRuleSet, name)),
                ))
            code = profiler.main([
                "--prepared-request", str(args.prepared_request), "--output-dir", str(output),
                "--scope", "first", "--profile",
            ])
            if code != 0:
                raise RuntimeError(f"profile tool returned {code}")
        if counts["candidate_attempts"] != counts["candidate_completed"]:
            raise ValueError("probe observed an incomplete candidate evaluation")
        if not counts["fallback_candidates"] and (
            counts["possible_chain_evaluations"] != counts["reused_chain_entries"]
            + counts["new_chain_entries"]
            or candidate_dispatch["evaluate_complete_chain"] != counts["new_chain_entries"]
            or counts["possible_node_evaluations"] != counts["reused_node_evaluations"]
            + candidate_dispatch["evaluate_node"]
            or candidate_dispatch["evaluate_plan"] != counts["candidate_completed"]
        ):
            raise ValueError("probe execution counts do not reconcile with candidate entries")
        summary["status"] = "completed"
    except BaseException as error:
        summary.update(status="failed", error={
            "type": type(error).__name__, "message": str(error),
        })
        raise
    finally:
        # The original tool owns output creation; never add files to a rejected old run.
        if (output / "input_identity.json").is_file():
            try:
                _write_json(output / "probe_summary.json", summary)
            except Exception as error:
                if summary["status"] == "completed":
                    raise
                print(f"Could not write partial probe summary: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
