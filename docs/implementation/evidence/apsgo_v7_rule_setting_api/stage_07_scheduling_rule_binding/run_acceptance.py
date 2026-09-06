"""Run one full GQGA4 solve from a freshly initialized database rule snapshot."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
QUALITY_HELPER = (
    ROOT
    / "docs/implementation/evidence/solverpy_path_cover_local_search"
    / "function_21_complete_acceptance"
)
for directory in (ROOT, ROOT / "src", QUALITY_HELPER):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from quality_precheck import (  # noqa: E402
    EXPECTED_IDENTITIES,
    create_output_directory,
    evaluate_quality_gate,
    sha256,
)

from apsgo_scheduler.api.json_codec import dumps_exact_json  # noqa: E402
from apsgo_scheduler.api.request import fingerprint_public_request  # noqa: E402
from apsgo_scheduler.core.contracts import fingerprint  # noqa: E402
from apsgo_v7_service import scheduling  # noqa: E402
from apsgo_v7_service.rule_management import initialize_gqga4_rules  # noqa: E402
from apsgo_v7_service.rule_store import RuleStore  # noqa: E402


def _task_input(request):
    return scheduling.SchedulingTaskInput(
        contract_version=request.contract_version,
        request_id=request.request_id,
        product_line_code=request.product_line_code,
        process_code=request.process_code,
        scenario=request.scenario,
        orders=request.orders,
        periods=request.periods,
        policy=request.policy,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    from tests.app.test_input_normalizer import gqga4_request, gqga4_spec

    source_request = gqga4_request.__wrapped__(gqga4_spec.__wrapped__())
    gate_path = ROOT / "tests/baselines/gqga4/quality_gate.json"
    reference_manifest_path = ROOT / "tests/baselines/gqga4/reference_manifest.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    database_reads = 0
    original_read = scheduling.get_active_gqga4_rules
    original_solve = scheduling.solve_request

    def counted_read(*values, **options):
        nonlocal database_reads
        database_reads += 1
        return original_read(*values, **options)

    source_paths = (
        "src/apsgo_v7_service/scheduling.py",
        "src/apsgo_v7_service/rule_management.py",
        "src/apsgo_scheduler/app/service.py",
        "docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py",
        "tests/baselines/gqga4/quality_gate.json",
        str(Path(__file__).resolve().relative_to(ROOT)),
    )
    source_before = {path: sha256(ROOT / path) for path in source_paths}

    with TemporaryDirectory(prefix="apsgo-v7-stage-07-") as directory:
        database_path = Path(directory) / "rules.sqlite3"
        initialized = initialize_gqga4_rules(database_path)
        binding_started = perf_counter()
        with patch.object(scheduling, "get_active_gqga4_rules", counted_read):
            task = scheduling.bind_gqga4_scheduling_task(_task_input(source_request), database_path)
        binding_elapsed = perf_counter() - binding_started
        active = initialized.active_rules
        pre_solve_checks = {
            "database_initialized_once": initialized.created,
            "quality_gate_bound_to_reference_manifest": (
                gate["reference_manifest_sha256"] == sha256(reference_manifest_path)
            ),
            "one_active_snapshot_read": database_reads == 1,
            "fixed_rule_identity": (
                task.rule_set_fingerprint == EXPECTED_IDENTITIES["rule_set_fingerprint"]
            ),
            "fixed_request_identity": (
                task.request_fingerprint == EXPECTED_IDENTITIES["request_fingerprint"]
            ),
            "fixed_policy_identity": (
                fingerprint(task.request.policy) == EXPECTED_IDENTITIES["policy_fingerprint"]
            ),
            "database_version_preserved": (
                task.active_rule_set_version_id == active.active_version_id
            ),
            "rule_snapshot_preserved": (
                task.request.rule_set_spec == active.rule_set_spec
                and task.request.virtual_prototypes == active.virtual_prototypes
            ),
        }
        failed_prechecks = [name for name, passed in pre_solve_checks.items() if not passed]
        if failed_prechecks:
            raise ValueError(f"pre-solve identity checks failed: {failed_prechecks}")

        output = create_output_directory(args.output_dir)
        solve_started = perf_counter()
        with patch.object(
            RuleStore,
            "open",
            side_effect=AssertionError("search must not read the rule database"),
        ) as forbidden_open:
            result = original_solve(task.request)
        solve_elapsed = perf_counter() - solve_started
        search_store_opens = forbidden_open.call_count
        bound = scheduling.BoundSchedulingResult(
            active_rule_set_version_id=task.active_rule_set_version_id,
            rule_set_fingerprint=task.rule_set_fingerprint,
            request_fingerprint=task.request_fingerprint,
            binding_fingerprint=task.binding_fingerprint,
            result=result,
        )
        database_sha256 = sha256(database_path)

    temporary_database_removed = not database_path.exists()
    source_after = {path: sha256(ROOT / path) for path in source_paths}
    request = task.request
    report = evaluate_quality_gate(request, bound.result, gate)
    manifest = bound.result.run_manifest
    identities = {name: getattr(manifest, name) for name in EXPECTED_IDENTITIES}
    binding_checks = {
        **pre_solve_checks,
        "search_rule_store_reads_zero": search_store_opens == 0,
        "temporary_database_removed": temporary_database_removed,
        "source_files_unchanged_during_solve": source_before == source_after,
        "request_identity_preserved": (
            fingerprint_public_request(request) == bound.request_fingerprint
            and bound.result.run_manifest.request_fingerprint == bound.request_fingerprint
        ),
        "result_rule_identity_preserved": (
            bound.result.run_manifest.rule_set_fingerprint == bound.rule_set_fingerprint
        ),
        "fixed_gqga4_identities_preserved": identities == EXPECTED_IDENTITIES,
    }
    failed_bindings = [name for name, passed in binding_checks.items() if not passed]
    if failed_bindings:
        report["passed"] = False
        report["failures"].extend(f"binding:{name}" for name in failed_bindings)

    report.update(
        {
            "scope": "stage 7 single functional acceptance; not paired performance acceptance",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "timings": {
                "observed_rule_binding_seconds": Decimal(str(binding_elapsed)),
                "observed_solver_seconds": Decimal(str(solve_elapsed)),
                "observed_bound_service_seconds": Decimal(str(binding_elapsed + solve_elapsed)),
            },
            "binding_checks": binding_checks,
            "active_rule_set_version_id": bound.active_rule_set_version_id,
            "rule_set_version": request.rule_set_spec.version,
            "rule_count": len(request.rule_set_spec.rules),
            "enabled_rule_count": sum(rule.enabled for rule in request.rule_set_spec.rules),
            "virtual_prototype_count": len(request.virtual_prototypes),
            "solver_policy": {
                "seed": request.policy.seed,
                "candidate_check_limit": request.policy.candidate_check_limit,
                "total_time_limit_seconds": request.policy.total_time_limit_seconds,
                "finalization_reserve_seconds": request.policy.finalization_reserve_seconds,
            },
            "rule_set_fingerprint": bound.rule_set_fingerprint,
            "request_fingerprint": bound.request_fingerprint,
            "binding_fingerprint": bound.binding_fingerprint,
            "public_result_fingerprint": bound.result.result_fingerprint,
            "bound_result_fingerprint": bound.bound_result_fingerprint,
            "identities": identities,
            "problem_fingerprint": manifest.problem_fingerprint,
            "policy_fingerprint": manifest.policy_fingerprint,
            "trace_fingerprint": manifest.trace_fingerprint,
            "core_audit_fingerprint": bound.result.core_audit.report_fingerprint,
            "result_audit_fingerprint": bound.result.audit_report.report_fingerprint,
            "counters": dict(manifest.counters),
            "database_sha256": database_sha256,
            "temporary_database_removed": temporary_database_removed,
            "quality_gate_sha256": sha256(gate_path),
            "reference_manifest_sha256": sha256(reference_manifest_path),
            "source_sha256": source_after,
        }
    )
    report_path = output / "acceptance_result.json"
    report_path.write_text(
        dumps_exact_json(report) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "failures": report["failures"],
                "elapsed_seconds": binding_elapsed + solve_elapsed,
                "output_dir": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
