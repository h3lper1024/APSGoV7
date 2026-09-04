"""One real public solve and a fail-closed frozen quality check, not performance acceptance."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import subprocess
import sys
import tarfile
from collections import defaultdict
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
for directory in (ROOT, ROOT / "src", ROOT / "tools"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from capture_solverpy_reference import load_manifest, verify_manifest  # noqa: E402

from apsgo_scheduler import core  # noqa: E402
from apsgo_scheduler.api.request import fingerprint_public_request  # noqa: E402
from apsgo_scheduler.app import solve_request  # noqa: E402
from apsgo_scheduler.core import solver as core_solver  # noqa: E402
from apsgo_scheduler.core.contracts import (  # noqa: E402
    CoreAuditStatus,
    canonical_json,
    fingerprint,
    sum_weights,
)
from apsgo_scheduler.core.model import MaterialRole  # noqa: E402
from apsgo_scheduler.core.rules.base import RuleDisposition  # noqa: E402

BASE = ROOT / "tests/baselines/gqga4"
REQUIRED_CHECKS = {
    "complete_coverage_and_conservation",
    "source_order_weight_conservation",
    "controlled_split_authorization_and_traceability",
    "search_and_uncached_audit_agree",
    "result_contract_self_check",
}
LIMIT_METRICS = {
    "maximum_chain_count": "chain_count",
    "maximum_underweight_chain_count": "underweight_chain_count",
    "maximum_prohibited_violation_count": "prohibited_violation_count",
    "maximum_virtual_output_weight_ratio": "virtual_output_weight_ratio",
    "maximum_late_original_due_period_move_count": "late_original_due_period_move_count",
}
GATE_KEYS = set(LIMIT_METRICS) | {
    "schema_version",
    "status",
    "confirmed_on",
    "scope",
    "reference_manifest_sha256",
    "expected_real_order_count",
    "expected_real_weight",
    "required_checks",
    "allowed_final_deviations",
    "reference_observation",
    "decision",
    "product_policy_note",
    "new_implementation_quality_observed_before_freeze",
}
EXPECTED_IDENTITIES = {
    "request_fingerprint": "0d9ee1cfe251306bb516a12e440120923853bf9114b57df1864880ab0ffa5a35",
    "problem_fingerprint": "cff6df6e99a6522df007c104d9cd8e3d235948e4bf36286c656d7a0326c82dea",
    "rule_set_fingerprint": "d963206b01c0d439303c1d6ae7d7374e20eaa0777801d12c0bebc89bfe6349c0",
    "policy_fingerprint": "b74e8ea92660994a71d96cb42f4717ee7e0514206ca0378458003acf51ab99ba",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_output_directory(path: Path) -> Path:
    """Never reuse even an empty existing directory or follow a symlink."""
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("output directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=False)
    return path


def evaluate_quality_gate(request, result, gate) -> dict:
    """Audit reports prove authorization; independent totals prove the frozen numerical gates."""
    checks = {}

    def check(name, passed, actual, expected):
        checks[name] = {"passed": bool(passed), "actual": actual, "expected": expected}

    check(
        "gate_schema",
        set(gate) == GATE_KEYS
        and gate.get("schema_version") == 1
        and gate.get("status") == "frozen",
        sorted(gate),
        sorted(GATE_KEYS),
    )
    required = gate.get("required_checks")
    check(
        "required_checks_supported",
        isinstance(required, list)
        and len(required) == len(REQUIRED_CHECKS)
        and set(required) == REQUIRED_CHECKS,
        required,
        sorted(REQUIRED_CHECKS),
    )
    release = result.release
    check("published_release_available", release is not None, release is not None, True)
    metrics = release.evaluation.metrics if release else {}
    for limit_name, metric_name in LIMIT_METRICS.items():
        actual, limit = metrics.get(metric_name), gate.get(limit_name)
        try:
            valid = (
                type(actual) in (int, Decimal)
                and Decimal(actual).is_finite()
                and Decimal(actual) >= 0
                and (
                    # Only an explicit null chain ceiling is disabled; missing keys still fail.
                    (limit_name == "maximum_chain_count" and limit_name in gate and limit is None)
                    or (
                        type(limit) in (int, str, Decimal)
                        and Decimal(limit).is_finite()
                        and Decimal(limit) >= 0
                        and Decimal(actual) <= Decimal(limit)
                    )
                )
            )
        except (ValueError, ArithmeticError):
            valid = False
        check(limit_name, valid, actual, limit)

    orders = {order.node_id.strip(): order for order in request.orders}
    nodes = tuple(node for chain in release.plan.chains for node in chain.nodes) if release else ()
    real = tuple(node for node in nodes if node.material_role is not MaterialRole.GENERATED_VIRTUAL)
    parents = {
        node.split_lineage.parent_node_id if node.split_lineage else node.node_id for node in real
    }
    input_weight = sum_weights(order.weight for order in request.orders)
    output_weight = sum_weights(node.weight for node in real)
    check(
        "expected_real_order_count",
        len(orders) == len(request.orders) == gate.get("expected_real_order_count")
        and parents == set(orders),
        len(orders),
        gate.get("expected_real_order_count"),
    )
    try:
        weight_matches = input_weight == output_weight == Decimal(gate["expected_real_weight"])
    except (KeyError, ValueError, ArithmeticError):
        weight_matches = False
    check(
        "expected_real_weight",
        weight_matches,
        {"input": input_weight, "scheduled": output_weight},
        gate.get("expected_real_weight"),
    )

    expected, scheduled = defaultdict(list), defaultdict(list)
    for order in request.orders:
        expected[order.source_order_id.strip()].append(order.weight)
    for node in real:
        scheduled[node.source_order_id].append(node.weight)
    sources = [
        {
            "source_order_id": source,
            "input_weight": str(sum_weights(expected[source])),
            "scheduled_weight": str(sum_weights(scheduled[source])),
            "passed": source in expected
            and source in scheduled
            and sum_weights(expected[source]) == sum_weights(scheduled[source]),
        }
        for source in sorted(set(expected) | set(scheduled))
    ]
    core_audit, result_audit = result.core_audit, result.audit_report
    facts = release.resource_facts if release else None
    facts_bound = facts is not None and (
        core_audit.derived_resource_fingerprint
        == facts.facts_fingerprint
        == fingerprint(
            {
                field.name: getattr(facts, field.name)
                for field in fields(facts)
                if field.name != "facts_fingerprint"
            }
        )
    )
    core_pass = (
        release is not None
        and core_audit.status is CoreAuditStatus.COMPLETED
        and core_audit.passed
        and not core_audit.invariant_failure_codes
        and not core_audit.action_authorization_failure_codes
        and facts_bound
    )
    check(
        "complete_coverage_and_conservation",
        core_pass
        and parents == set(orders)
        and len({node.node_id for node in nodes}) == len(nodes)
        and input_weight == output_weight,
        {
            "covered_parent_count": len(parents),
            "missing": sorted(set(orders) - parents),
            "unexpected": sorted(parents - set(orders)),
        },
        "exact original parent coverage and weight",
    )
    check(
        "source_order_weight_conservation",
        release is not None and bool(sources) and all(row["passed"] for row in sources),
        [row for row in sources if not row["passed"]],
        "all source weights equal",
    )
    partitions = facts.split_partitions if facts else ()
    split_counts = (
        core_audit.audited_split_count,
        core_audit.audited_same_period_split_count,
        core_audit.audited_future_borrow_return_count,
    )
    recorded_counts = tuple(
        result.run_manifest.counters.get(name)
        for name in (
            "accepted_split_count",
            "accepted_same_period_split_count",
            "accepted_future_borrow_return_count",
        )
    )
    check(
        "controlled_split_authorization_and_traceability",
        core_pass and split_counts == recorded_counts and split_counts[0] == len(partitions),
        {
            "audited_counts": split_counts,
            "recorded_counts": recorded_counts,
            "partition_ids": [item.partition_id for item in partitions],
        },
        "passed independent authorization audit and matching partition/mode counts",
    )
    check(
        "search_and_uncached_audit_agree",
        core_pass
        and core_audit.search_evaluation_matches
        and core_audit.audited_evaluation_fingerprint == fingerprint(release.evaluation),
        core_audit.search_evaluation_matches,
        True,
    )
    check(
        "result_contract_self_check",
        release is not None
        and result_audit.status.value == "completed"
        and result_audit.passed
        and not result_audit.failure_codes
        and result_audit.plan_fingerprint == fingerprint(release.plan)
        and result_audit.resource_fingerprint
        == core_audit.derived_resource_fingerprint
        == facts.facts_fingerprint
        and facts_bound,
        {
            "status": result_audit.status.value,
            "passed": result_audit.passed,
            "failure_codes": result_audit.failure_codes,
        },
        "completed, passed and bound to release",
    )
    violations = release.evaluation.violations if release else ()
    allowed = sorted(
        {
            item.reason_code
            for item in violations
            if item.disposition is RuleDisposition.ALLOWED_FINAL_DEVIATION
        }
    )
    configured = gate.get("allowed_final_deviations")
    check(
        "allowed_final_deviations",
        release is not None
        and isinstance(configured, list)
        and all(isinstance(code, str) for code in configured)
        and set(allowed) <= set(configured),
        allowed,
        configured,
    )
    observed_prohibited = sum(item.disposition is RuleDisposition.PROHIBITED for item in violations)
    check(
        "metric_record_consistency",
        release is not None
        and metrics.get("chain_count") == len(release.plan.chains)
        and metrics.get("prohibited_violation_count") == observed_prohibited,
        {
            "chain_count": len(release.plan.chains) if release else None,
            "prohibited_violation_record_count": observed_prohibited if release else None,
        },
        "named metrics agree with actual chains and violation records",
    )
    underweight = (
        [
            {
                "chain_id": chain.chain_id,
                "assigned_period": chain.summary.assigned_period,
                "total_weight": str(chain.summary.total_weight),
                "underweight_total_gap": str(chain.metrics.get("underweight_total_gap")),
                "reason_codes": [item.reason_code for item in chain.violations],
            }
            for chain in release.evaluation.chain_evaluations
            if chain.metrics.get("underweight_chain_count", Decimal(-1)) > 0
        ]
        if release
        else []
    )
    check(
        "underweight_detail_consistency",
        release is not None
        and all(
            "underweight_chain_count" in chain.metrics and "underweight_total_gap" in chain.metrics
            for chain in release.evaluation.chain_evaluations
        )
        and metrics.get("underweight_chain_count") == len(underweight),
        len(underweight) if release else None,
        metrics.get("underweight_chain_count"),
    )
    return {
        "passed": all(item["passed"] for item in checks.values()),
        "checks": checks,
        "failures": [name for name, item in checks.items() if not item["passed"]],
        "observations": {
            "status": result.status.value,
            "stop_reason": result.stop_reason.value,
            "quality_key": release.evaluation.quality_key if release else None,
            "metrics": dict(metrics),
        },
        "source_conservation": sources,
        "underweight_chains": underweight,
    }


def verify_code_revision(repository: Path, revision: str) -> dict:
    """Compare this checkout/export's protected bytes to a named commit, without extracting it."""
    prefix = ["git", "-C", str(repository)]
    resolved = subprocess.check_output(
        prefix + ["rev-parse", "--verify", revision + "^{commit}"], text=True
    ).strip()
    if revision != resolved:
        raise ValueError("code revision must be the full verified commit SHA")
    protected = (
        "src/apsgo_scheduler",
        "tests/app/test_input_normalizer.py",
        "tests/baselines/gqga4",
    )
    archive = subprocess.check_output(prefix + ["archive", revision, *protected])
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as snapshot:
        for entry in snapshot.getmembers():
            if entry.isdir():
                continue
            if not entry.isfile():
                raise ValueError(f"unsupported protected Git entry: {entry.name}")
            path = ROOT / entry.name
            expected = hashlib.sha256(snapshot.extractfile(entry).read()).hexdigest()
            if path.is_symlink() or not path.is_file() or sha256(path) != expected:
                raise ValueError(f"code/config differs from {revision}: {entry.name}")
            hashes[entry.name] = expected
    actual = {
        str(path.relative_to(ROOT))
        for scope in protected
        for path in ((ROOT / scope).rglob("*") if (ROOT / scope).is_dir() else (ROOT / scope,))
        if (path.is_file() or path.is_symlink())
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    if actual != set(hashes):
        raise ValueError(f"unexpected protected files: {sorted(actual ^ set(hashes))}")
    return hashes


def write_csv(path, fieldnames, rows):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def save_details(output, result, gate_report):
    schedule, chains = [], []
    if result.release:
        evaluations = {item.chain_id: item for item in result.release.evaluation.chain_evaluations}
        for chain in result.release.plan.chains:
            detail = evaluations[chain.chain_id]
            chains.append(
                {
                    "chain_id": chain.chain_id,
                    "assigned_period": chain.assigned_period,
                    "total_weight": detail.summary.total_weight,
                    "real_weight": detail.summary.real_weight,
                    "virtual_weight": detail.summary.virtual_weight,
                    "underweight_chain_count": detail.metrics.get("underweight_chain_count"),
                    "underweight_total_gap": detail.metrics.get("underweight_total_gap"),
                    "violations": ";".join(item.reason_code for item in detail.violations),
                    "node_ids": ";".join(node.node_id for node in chain.nodes),
                }
            )
            for position, node in enumerate(chain.nodes):
                virtual, split = node.virtual_lineage, node.split_lineage
                schedule.append(
                    {
                        "chain_id": chain.chain_id,
                        "assigned_period": chain.assigned_period,
                        "position": position,
                        "node_id": node.node_id,
                        "source_order_id": node.source_order_id,
                        "source_resource_id": node.source_resource_id,
                        "source_period": node.source_period,
                        "material_role": node.material_role.value,
                        "weight": node.weight,
                        "width": node.width,
                        "thickness": node.thickness,
                        "min_temperature": node.min_temperature,
                        "max_temperature": node.max_temperature,
                        "grade": node.grade,
                        "partition_id": split.partition_id if split else None,
                        "piece_index": split.piece_index if split else None,
                        "virtual_prototype_id": virtual.prototype_id if virtual else None,
                        "virtual_purpose": virtual.purpose.value if virtual else None,
                        "virtual_accepted_sequence": virtual.accepted_sequence if virtual else None,
                        "related_partition_id": virtual.related_partition_id if virtual else None,
                    }
                )
    write_csv(
        output / "schedule_detail.csv", tuple(schedule[0]) if schedule else ("node_id",), schedule
    )
    write_csv(output / "chain_detail.csv", tuple(chains[0]) if chains else ("chain_id",), chains)
    write_csv(
        output / "source_conservation.csv",
        ("source_order_id", "input_weight", "scheduled_weight", "passed"),
        gate_report["source_conservation"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--code-repository", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    protected_before = verify_code_revision(args.code_repository, args.code_revision)
    manifest_path = BASE / "reference_manifest.json"
    manifest = load_manifest(manifest_path)
    reference_before = verify_manifest(manifest)
    gate = json.loads((BASE / "quality_gate.json").read_text(encoding="utf-8"))
    if gate["reference_manifest_sha256"] != sha256(manifest_path):
        raise ValueError("frozen gate is not bound to this reference manifest")
    output = create_output_directory(args.output_dir)
    # Reuse the already-tested development fixture adapter; production imports none of it.
    from tests.app.test_input_normalizer import gqga4_request, gqga4_spec

    request = gqga4_request.__wrapped__(gqga4_spec.__wrapped__())
    captured = {"caches": [], "core_results": []}
    original_cache, original_solve = core_solver.RuleEdgeDecisionCache, core.solve

    def observe_cache(*values, **options):
        cache = original_cache(*values, **options)
        captured["caches"].append(cache)
        return cache

    def observe_core(*values, **options):
        solved = original_solve(*values, **options)
        captured["core_results"].append(solved)
        return solved

    started = perf_counter()
    with (
        patch.object(core_solver, "RuleEdgeDecisionCache", observe_cache),
        patch.object(core, "solve", observe_core),
    ):
        result = solve_request(request)
    elapsed = perf_counter() - started
    with (output / "public_result.canonical.json").open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(result) + "\n")
    trace = captured["core_results"][0].trace if len(captured["core_results"]) == 1 else ()
    with (output / "accepted_trace.canonical.json").open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(trace) + "\n")
    report = evaluate_quality_gate(request, result, gate)
    save_details(output, result, report)
    protected_after = verify_code_revision(args.code_repository, args.code_revision)
    reference_after = verify_manifest(manifest)
    identities = {name: getattr(result.run_manifest, name) for name in EXPECTED_IDENTITIES}
    observation_valid = (
        len(captured["core_results"]) == len(captured["caches"]) == 1
        and identities == EXPECTED_IDENTITIES
        and fingerprint_public_request(request) == identities["request_fingerprint"]
        and fingerprint(trace) == result.run_manifest.trace_fingerprint
        and protected_before == protected_after
        and reference_before == reference_after
    )
    if not observation_valid:
        report["passed"] = False
        report["failures"].append("observation_or_frozen_identity_mismatch")
    cache = captured["caches"][0] if len(captured["caches"]) == 1 else None
    raw_reference = json.loads((BASE / "reference_stage_expectations.json").read_text())["final"]
    v3 = json.loads((BASE / "v3_reference_manifest.json").read_text())
    report.update(
        {
            "scope": "single quality precheck; function 21 and paired performance acceptance remain incomplete",
            "code_revision": args.code_revision,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "request_id": request.request_id,
            "identities": identities,
            "public_result_fingerprint": result.result_fingerprint,
            "public_release_fingerprint": result.release.release_fingerprint
            if result.release
            else None,
            "core_result_fingerprint": captured["core_results"][0].core_result_fingerprint
            if len(captured["core_results"]) == 1
            else None,
            "core_audit_fingerprint": result.core_audit.report_fingerprint,
            "result_audit_fingerprint": result.audit_report.report_fingerprint,
            "trace_fingerprint": fingerprint(trace),
            "gate": gate,
            "quality_gate_sha256": sha256(BASE / "quality_gate.json"),
            "script_sha256": sha256(Path(__file__)),
            "protected_hashes": protected_after,
            "reference_hashes": reference_after,
            "counters": dict(result.run_manifest.counters),
            "timings": {
                "observed_public_service_seconds": elapsed,
                "stage_duration_seconds": dict(result.run_manifest.stage_duration_seconds),
            },
            "cache": {
                "hits": cache.hit_count,
                "misses": cache.miss_count,
                "entries": cache.entry_count,
                "hit_ratio": cache.hit_count / (cache.hit_count + cache.miss_count)
                if cache.hit_count + cache.miss_count
                else None,
            }
            if cache
            else None,
            "instrumentation": "one wrapper per core call/cache construction, returning original objects; no hot-path tracing",
            "reference_observations": {
                "solverpy_frozen_gate_observation": gate["reference_observation"],
                "solverpy_stage_final": {
                    name: raw_reference[name]
                    for name in ("quality", "metrics", "candidate_checks", "stop_reason")
                },
                "v3_metrics": v3["metrics"],
                "v3_quality_note": v3["quality_note"],
                "count_warning": "V3 prohibited_violation_chain_count counts chains, target prohibited_violation_count counts violation records; not interchangeable.",
            },
            "artifacts_sha256": {
                path.name: sha256(path) for path in sorted(output.iterdir()) if path.is_file()
            },
        }
    )
    with (output / "quality_report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {"passed": report["passed"], "failures": report["failures"], "output_dir": str(output)},
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
