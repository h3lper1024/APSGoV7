"""Run fixed, uninstrumented determinism or paired performance acceptance."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LEGACY_HELPER = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_22_inter_chain_width_gap/step_22_4_real_comparison/run_observed_precheck.py"
)
PRECHECK = Path(
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_21_complete_acceptance/quality_precheck.py"
)
BASE = Path("tests/baselines/gqga4")
ORIGINAL_ARTIFACTS = frozenset(
    (
        "public_result.canonical.json",
        "accepted_trace.canonical.json",
        "schedule_detail.csv",
        "chain_detail.csv",
        "source_conservation.csv",
    )
)
SEMANTIC_FIELDS = (
    "code_revision",
    "identities",
    "public_result_fingerprint",
    "public_release_fingerprint",
    "core_result_fingerprint",
    "core_audit_fingerprint",
    "result_audit_fingerprint",
    "trace_fingerprint",
    "counters",
    "observations",
)
ELIGIBLE_STOPS = frozenset(("local_search_complete", "candidate_limit_reached"))
TIME_OR_CANCEL_STOPS = frozenset(
    ("search_time_limit_reached", "finalization_time_limit_reached", "user_cancelled")
)


def load_runtime(code_root):
    """Select one export before importing fixtures, solver code or benchmark helpers."""
    root = Path(code_root).resolve(strict=True)
    for name in ("benchmark_solver_process", "compare_solver_stages"):
        module = sys.modules.get(name)
        if module is not None:
            filename = getattr(module, "__file__", None)
            if filename is None or not Path(filename).resolve().is_relative_to(root / "tools"):
                raise ValueError("run each code root in a separate Python process")
    helper = root / LEGACY_HELPER
    if helper.is_symlink() or not helper.is_file() or not helper.resolve().is_relative_to(root):
        raise ValueError("legacy loader must be a regular file inside the code root")
    spec = importlib.util.spec_from_file_location("width_acceptance_helpers", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker = module.load_checker(root)
    return checker, importlib.import_module("benchmark_solver_process")


def load_gate(checker, benchmark):
    base = checker.ROOT / BASE
    gate = json.loads((base / "performance_gate.json").read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "status": "frozen",
        "warmup_per_solver": 1,
        "samples_per_solver": 20,
        "pair_order": "alternating_reference_first_on_odd_pairs",
        "maximum_sample_seconds_is_gate": False,
        "all_samples_retained": True,
    }
    if any(
        type(gate.get(key)) is not type(value) or gate[key] != value
        for key, value in expected.items()
    ):
        raise ValueError("unsupported frozen acceptance configuration")
    if platform.python_version() != gate["python_version"]:
        raise ValueError("acceptance requires the frozen Python version")
    for key in (
        "new_solver_maximum_median_seconds",
        "new_solver_maximum_p95_seconds",
        "reference_control_maximum_absolute_median_drift_ratio",
        "reference_control_maximum_absolute_p95_drift_ratio",
    ):
        value = gate[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid frozen threshold: {key}")
    for filename, field in (
        ("reference_manifest.json", "reference_manifest_sha256"),
        ("reference_performance_samples.json", "reference_performance_sha256"),
    ):
        if checker.sha256(base / filename) != gate[field]:
            raise ValueError(f"frozen performance gate hash mismatch: {filename}")
    frozen = json.loads((base / "reference_performance_samples.json").read_text(encoding="utf-8"))
    if (
        frozen["status"] != "pass"
        or frozen["manifest_sha256"] != gate["reference_manifest_sha256"]
        or frozen["statistics"] != gate["reference_statistics"]
        or benchmark.summarize([row["elapsed_seconds"] for row in frozen["samples"]])
        != gate["reference_statistics"]
    ):
        raise ValueError("frozen reference statistics do not match their samples")
    manifest = benchmark.load_manifest(base / "reference_manifest.json")
    benchmark.verify_manifest(manifest)
    return gate, manifest


def dependency_hashes(checker):
    paths = (
        LEGACY_HELPER,
        PRECHECK,
        Path("tools/benchmark_solver_process.py"),
        Path("tools/capture_solverpy_reference.py"),
        Path("tools/compare_solver_stages.py"),
        BASE / "performance_gate.json",
        BASE / "quality_gate.json",
        BASE / "reference_manifest.json",
        BASE / "reference_performance_samples.json",
    )
    hashes = {"runner": checker.sha256(Path(__file__))}
    for relative in paths:
        path = checker.ROOT / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(checker.ROOT)
        ):
            raise ValueError(f"acceptance dependency is not a regular export file: {relative}")
        hashes[str(relative)] = checker.sha256(path)
    return hashes


def measure_new(checker, repository, revision, output, protected_hashes):
    """Time the original precheck subprocess; retain its outputs even when it fails."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("sample output already exists; choose a new path")
    if checker.verify_code_revision(repository, revision) != protected_hashes:
        raise ValueError("protected code changed before the sample")
    before = dependency_hashes(checker)
    command = [
        sys.executable,
        str(checker.ROOT / PRECHECK),
        "--code-repository",
        str(repository),
        "--code-revision",
        revision,
        "--output-dir",
        str(output),
    ]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (checker.ROOT / "src", checker.ROOT, checker.ROOT / "tools")
    )
    sample = {"command": command, "output_dir": str(output), "timed_out": False}
    started = time.perf_counter()
    try:
        process = subprocess.run(
            command,
            cwd=checker.ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
        )
        sample.update(returncode=process.returncode, stdout=process.stdout, stderr=process.stderr)
    except subprocess.TimeoutExpired as error:

        def decode(value):
            return (
                value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            )

        sample.update(
            returncode=None,
            timed_out=True,
            stdout=decode(error.stdout),
            stderr=decode(error.stderr),
        )
    except OSError as error:
        sample.update(returncode=None, stdout="", stderr=str(error))
    sample["elapsed_seconds"] = time.perf_counter() - started
    sample["comparison"] = {"status": "fail"}
    sample["quality_passed"] = False
    try:
        if checker.verify_code_revision(repository, revision) != protected_hashes:
            raise ValueError("protected code changed after the sample")
        if dependency_hashes(checker) != before:
            raise ValueError("acceptance dependency changed during the sample")
        report_path = output / "quality_report.json"
        if report_path.is_symlink() or not report_path.is_file():
            raise ValueError("sample quality report is missing or not a regular file")
        sample["quality_report_sha256"] = checker.sha256(report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        sample.update(
            counters=report["counters"],
            timings=report["timings"],
            cache=report["cache"],
            stop_reason=report["observations"]["stop_reason"],
            quality=report["observations"]["quality_key"],
            identities=report["identities"],
            artifacts_sha256=report["artifacts_sha256"],
        )
        if (
            report["code_revision"] != revision
            or report["identities"] != checker.EXPECTED_IDENTITIES
            or report["protected_hashes"] != protected_hashes
            or report["python"] != platform.python_version()
            or report["script_sha256"] != before[str(PRECHECK)]
            or report["quality_gate_sha256"] != before[str(BASE / "quality_gate.json")]
            or set(report["artifacts_sha256"]) != ORIGINAL_ARTIFACTS
        ):
            raise ValueError("sample revision, input identity or artifact binding mismatch")
        for name, digest in report["artifacts_sha256"].items():
            path = output / name
            if path.is_symlink() or not path.is_file() or checker.sha256(path) != digest:
                raise ValueError(f"sample artifact hash mismatch: {name}")
        sample["semantic_signature"] = {key: report[key] for key in SEMANTIC_FIELDS}
        sample["semantic_signature"]["stable_artifacts_sha256"] = {
            name: report["artifacts_sha256"][name]
            for name in sorted(ORIGINAL_ARTIFACTS - {"public_result.canonical.json"})
        }
        sample["quality_passed"] = report["passed"] is True and not report["failures"]
        if sample["timed_out"] or sample["returncode"] != 0 or not sample["quality_passed"]:
            raise ValueError("sample execution or quality gate failed")
        sample["comparison"] = {"status": "pass"}
    except (OSError, ValueError, KeyError, TypeError) as error:
        sample["comparison"] = {"status": "fail", "error": str(error)}
    return sample


def _passed(sample):
    return (
        sample.get("returncode") == 0
        and sample.get("timed_out") is False
        and sample.get("comparison", {}).get("status") == "pass"
        and (sample.get("solver") != "new" or sample.get("quality_passed") is True)
    )


def run_samples(mode, gate, measure_reference, measure_candidate, checkpoint):
    """Retain every attempt; a failed child stops the sequence without replacement."""
    if mode == "determinism":
        schedule = [("new", "determinism", index) for index in range(1, 4)]
    elif mode == "performance":
        schedule = [
            (solver, "warmup", index)
            for index in range(1, gate["warmup_per_solver"] + 1)
            for solver in ("reference", "new")
        ]
        for index in range(1, gate["samples_per_solver"] + 1):
            order = ("reference", "new") if index % 2 else ("new", "reference")
            schedule.extend((solver, "samples", index) for solver in order)
    else:
        raise ValueError("unknown acceptance mode")
    rows = []
    for solver, phase, index in schedule:
        measure = measure_candidate if solver == "new" else measure_reference
        try:
            row = measure(phase, index)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            row = {
                "returncode": None,
                "timed_out": False,
                "stdout": "",
                "stderr": str(error),
                "comparison": {"status": "fail", "error": str(error)},
            }
        row.update(solver=solver, phase=phase, index=index)
        rows.append(row)
        checkpoint(rows)
        if not _passed(row):
            break
    return rows


def determinism_decision(samples, first_difference):
    if len(samples) != 3 or any(not _passed(row) for row in samples):
        return {"status": "fail", "reason": "requires three successful consecutive samples"}
    reasons = [row.get("stop_reason") for row in samples]
    if any(reason in TIME_OR_CANCEL_STOPS for reason in reasons):
        return {
            "status": "not_eligible",
            "reason": "time or cancellation cut; no replacement runs",
            "stop_reasons": reasons,
        }
    if any(reason not in ELIGIBLE_STOPS for reason in reasons):
        return {"status": "fail", "reason": "unexpected stop reason", "stop_reasons": reasons}
    for index, sample in enumerate(samples[1:], 2):
        difference = first_difference(
            samples[0]["semantic_signature"], sample["semantic_signature"]
        )
        if difference is not None:
            return {
                "status": "fail",
                "reason": "semantic result differs",
                "sample_index": index,
                "first_difference": difference,
            }
    return {"status": "pass", "sample_count": 3, "first_difference": None}


def performance_decision(reference_samples, new_samples, gate, summarize):
    expected = gate["samples_per_solver"]
    if (
        len(reference_samples) != expected
        or len(new_samples) != expected
        or any(not _passed(row) for row in reference_samples + new_samples)
    ):
        return {"status": "fail", "reason": "requires all successful paired samples"}
    reference = summarize([row["elapsed_seconds"] for row in reference_samples])
    new = summarize([row["elapsed_seconds"] for row in new_samples])
    drift = {
        metric: abs(
            reference[f"{metric}_seconds"] / gate["reference_statistics"][f"{metric}_seconds"] - 1
        )
        for metric in ("median", "p95")
    }
    decision = {
        "reference_statistics": reference,
        "new_statistics": new,
        "control_drift": drift,
        "maximum_sample_seconds_is_gate": False,
    }
    if any(
        drift[metric] > gate[f"reference_control_maximum_absolute_{metric}_drift_ratio"]
        for metric in drift
    ):
        return {
            **decision,
            "status": "invalid_control",
            "reason": "reference environment drift exceeds frozen gate",
            "new_performance_gate": "not_evaluated",
        }
    passed = all(
        new[f"{metric}_seconds"] <= gate[f"new_solver_maximum_{metric}_seconds"]
        for metric in ("median", "p95")
    )
    return {
        **decision,
        "status": "pass" if passed else "fail",
        "new_performance_gate": "pass" if passed else "fail",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("determinism", "performance"), required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--code-repository", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.error("output already exists; choose a new directory to preserve previous evidence")
    checker, benchmark = load_runtime(args.code_root)
    gate, manifest = load_gate(checker, benchmark)
    protected = checker.verify_code_revision(args.code_repository, args.code_revision)
    dependencies = dependency_hashes(checker)
    reference_hashes = benchmark.verify_manifest(manifest)
    output = checker.create_output_directory(args.output_dir)
    report_path = output / "acceptance_report.json"
    report = {
        "schema_version": 1,
        "mode": args.mode,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "code_root": str(checker.ROOT),
        "code_repository": str(args.code_repository),
        "code_revision": args.code_revision,
        "environment": {
            "python": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
        },
        "gate": gate,
        "dependency_sha256_before": dependencies,
        "protected_hashes": protected,
        "reference_hashes": reference_hashes,
        "measurement_scope": gate["measurement_scope"],
        "rows": [],
        "reference_retention": "Existing reference measure retains stdout/stderr, metadata, artifact hashes and differences; its temporary raw outputs are not archived.",
        "new_retention": "Each original precheck directory retains all six raw outputs; no hot-path observer is used.",
    }

    def checkpoint(rows):
        report["rows"] = rows
        benchmark.save_report(report_path, report)
        if rows:
            row = rows[-1]
            print(
                json.dumps(
                    {
                        key: row.get(key)
                        for key in ("solver", "phase", "index", "elapsed_seconds", "comparison")
                    }
                ),
                flush=True,
            )

    checkpoint([])
    try:

        def candidate(phase, index):
            return measure_new(
                checker,
                args.code_repository,
                args.code_revision,
                output / f"{phase}_new_{index:02d}",
                protected,
            )

        rows = run_samples(
            args.mode, gate, lambda phase, index: benchmark.measure(manifest), candidate, checkpoint
        )
        if args.mode == "determinism":
            compare = importlib.import_module("compare_solver_stages")
            decision = determinism_decision(rows, compare.first_difference)
        elif any(not _passed(row) for row in rows):
            decision = {"status": "fail", "reason": "failed sample retained; sequence stopped"}
        else:
            decision = performance_decision(
                [row for row in rows if row["solver"] == "reference" and row["phase"] == "samples"],
                [row for row in rows if row["solver"] == "new" and row["phase"] == "samples"],
                gate,
                benchmark.summarize,
            )
        report["decision"] = decision
        report["dependency_sha256_after"] = dependency_hashes(checker)
        if (
            report["dependency_sha256_after"] != dependencies
            or checker.verify_code_revision(args.code_repository, args.code_revision) != protected
            or benchmark.verify_manifest(manifest) != reference_hashes
        ):
            raise ValueError("frozen code, dependencies or reference changed during acceptance")
        report["status"] = decision["status"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        report.update(status="fail", error=str(error))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    benchmark.save_report(report_path, report)
    print(json.dumps({"status": report["status"], "report": str(report_path)}), flush=True)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
