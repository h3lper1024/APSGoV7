"""Bounded child doubles validate acceptance bookkeeping, not GQGA4 runtime."""

import importlib.util
import json
import os
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / (
    "docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/"
    "step_07_acceptance/run_acceptance.py"
)


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("width_acceptance_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return json.loads(
        (ROOT / "tests/baselines/gqga4/performance_gate.json").read_text(encoding="utf-8")
    )


def sample(*, elapsed=170.0, stop="candidate_limit_reached", **changes):
    return {
        "returncode": 0,
        "timed_out": False,
        "comparison": {"status": "pass"},
        "elapsed_seconds": elapsed,
        "quality_passed": True,
        "stop_reason": stop,
        "semantic_signature": {"plan": "stable-plan", "trace": ["same-move"]},
        "stdout": "child output",
        "stderr": "",
        **changes,
    }


@pytest.fixture
def benchmark(runner):
    return runner.load_runtime(ROOT)[1]


@pytest.fixture
def first_difference(benchmark):
    return importlib.import_module("compare_solver_stages").first_difference


def test_determinism_collects_exactly_three_fresh_children_and_checkpoints_each(runner, gate):
    calls, checkpoints = [], []

    def candidate(phase, index):
        calls.append((phase, index))
        return sample()

    rows = runner.run_samples(
        "determinism",
        gate,
        lambda *_: pytest.fail("determinism must not start a reference child"),
        candidate,
        lambda current: checkpoints.append(deepcopy(current)),
    )
    assert calls == [("determinism", index) for index in (1, 2, 3)]
    assert [(row["solver"], row["phase"], row["index"]) for row in rows] == [
        ("new", "determinism", index) for index in (1, 2, 3)
    ]
    assert [len(current) for current in checkpoints] == [1, 2, 3]
    assert checkpoints[-1] == rows


def test_performance_uses_one_warmup_each_then_twenty_alternating_pairs(runner, gate):
    calls, checkpoints = [], []

    def measure(solver):
        def child(phase, index):
            calls.append((solver, phase, index))
            return sample(elapsed=23.65 if solver == "reference" else 170.0)

        return child

    rows = runner.run_samples(
        "performance",
        gate,
        measure("reference"),
        measure("new"),
        lambda current: checkpoints.append(deepcopy(current)),
    )
    expected = [("reference", "warmup", 1), ("new", "warmup", 1)] + [
        (solver, "samples", index)
        for index in range(1, 21)
        for solver in (("reference", "new") if index % 2 else ("new", "reference"))
    ]
    assert calls == expected
    assert [(row["solver"], row["phase"], row["index"]) for row in rows] == expected
    assert [len(current) for current in checkpoints] == list(range(1, 43))
    assert checkpoints[-1] == rows


@pytest.mark.parametrize("failure", ("exit", "timeout", "comparison"))
def test_bad_child_is_kept_and_stops_collection_without_replacement(runner, gate, failure):
    calls, checkpoints = [], []
    bad = sample(
        returncode=7 if failure == "exit" else None if failure == "timeout" else 0,
        timed_out=failure == "timeout",
        comparison={"status": "fail" if failure == "comparison" else "pass"},
        stdout="partial stdout",
        stderr="failure details",
    )

    def candidate(phase, index):
        calls.append((phase, index))
        return sample() if index == 1 else bad

    rows = runner.run_samples(
        "determinism",
        gate,
        lambda *_: pytest.fail("unexpected reference"),
        candidate,
        lambda current: checkpoints.append(deepcopy(current)),
    )
    assert calls == [("determinism", 1), ("determinism", 2)]
    assert len(rows) == 2 and checkpoints[-1] == rows
    assert all(rows[-1][key] == value for key, value in bad.items())
    assert rows[-1]["stdout"] == "partial stdout" and rows[-1]["stderr"] == "failure details"


@pytest.mark.parametrize("error_type", (OSError, KeyError, TypeError))
def test_launch_exception_is_checkpointed_as_the_last_attempt(runner, gate, error_type):
    saved = []

    def fail(*_):
        raise error_type("child could not launch")

    rows = runner.run_samples(
        "determinism", gate, fail, fail, lambda current: saved.append(deepcopy(current))
    )
    assert len(rows) == len(saved) == 1
    assert rows[0]["returncode"] is None and rows[0]["comparison"]["status"] == "fail"
    assert "child could not launch" in rows[0]["stderr"]


@pytest.mark.parametrize("stop", ("candidate_limit_reached", "local_search_complete"))
def test_three_semantically_identical_runs_accept_both_deterministic_stops(
    runner, first_difference, stop
):
    samples = [sample(stop=stop, elapsed=100 + index) for index in range(3)]
    decision = runner.determinism_decision(samples, first_difference)
    assert decision == {"status": "pass", "sample_count": 3, "first_difference": None}


@pytest.mark.parametrize(
    "stop", ("search_time_limit_reached", "finalization_time_limit_reached", "user_cancelled")
)
def test_three_time_or_cancelled_runs_are_ineligible_without_filler_children(
    runner, first_difference, gate, stop
):
    calls = []

    def candidate(phase, index):
        calls.append(index)
        return sample(stop=stop)

    rows = runner.run_samples(
        "determinism", gate, lambda *_: pytest.fail("reference"), candidate, lambda _: None
    )
    decision = runner.determinism_decision(rows, first_difference)
    assert calls == [1, 2, 3]
    assert decision["status"] == "not_eligible"
    assert decision["stop_reasons"] == [stop] * 3


def test_determinism_reports_the_first_semantic_difference_and_never_compares_timings(
    runner, first_difference
):
    rows = [sample(elapsed=100 + index, timings={"wall": index}) for index in range(3)]
    rows[1]["semantic_signature"]["trace"][0] = "different-move"
    rows[2]["semantic_signature"]["plan"] = "later-difference"
    decision = runner.determinism_decision(rows, first_difference)
    assert decision["status"] == "fail" and decision["sample_index"] == 2
    assert decision["first_difference"] == {
        "path": "$.trace[0]",
        "expected": "same-move",
        "actual": "different-move",
        "reason": "value differs",
    }


def test_incomplete_determinism_and_performance_samples_cannot_pass(runner, benchmark, gate):
    rows = [sample()] * 2
    assert runner.determinism_decision(rows, lambda *_: None)["status"] == "fail"
    assert (
        runner.performance_decision(
            [sample(elapsed=23.65)] * 19, [sample()] * 20, gate, benchmark.summarize
        )["status"]
        == "fail"
    )


def test_failed_reference_warmup_is_retained_before_any_new_solver_runs(runner, gate):
    checkpoints = []
    rows = runner.run_samples(
        "performance",
        gate,
        lambda *_: sample(comparison={"status": "fail", "first_difference": "reference changed"}),
        lambda *_: pytest.fail("a failed reference cannot be skipped"),
        lambda current: checkpoints.append(deepcopy(current)),
    )
    assert len(rows) == len(checkpoints) == 1
    assert rows[0]["solver"] == "reference" and rows[0]["phase"] == "warmup"
    assert rows[0]["comparison"]["first_difference"] == "reference changed"


@pytest.mark.parametrize(
    "seconds,expected",
    (
        ([180.0] * 20, "pass"),
        ([181.0] * 20, "fail"),
        ([170.0] * 18 + [181.0] * 2, "fail"),
        ([170.0] * 19 + [400.0], "pass"),
    ),
)
def test_performance_uses_median_and_p95_but_not_maximum(
    runner, benchmark, gate, seconds, expected
):
    decision = runner.performance_decision(
        [sample(elapsed=23.65, solver="reference")] * 20,
        [
            sample(elapsed=value, solver="new", stop="search_time_limit_reached")
            for value in seconds
        ],
        gate,
        benchmark.summarize,
    )
    assert decision["status"] == decision["new_performance_gate"] == expected
    assert decision["maximum_sample_seconds_is_gate"] is False
    assert decision["new_statistics"]["max_seconds"] == max(seconds)


@pytest.mark.parametrize("control_seconds", (10.0, 40.0))
def test_reference_drift_invalidates_before_new_solver_speed_is_judged(
    runner, benchmark, gate, control_seconds
):
    decision = runner.performance_decision(
        [sample(elapsed=control_seconds, solver="reference")] * 20,
        [sample(elapsed=300, solver="new")] * 20,
        gate,
        benchmark.summarize,
    )
    assert decision["status"] == "invalid_control"
    assert decision["new_performance_gate"] == "not_evaluated"
    assert decision["control_drift"]["median"] > 0.2
    assert decision["control_drift"]["p95"] > 0.2


@pytest.fixture
def fake_child(runner, monkeypatch, tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    protected = {"src/apsgo_scheduler/core/solver.py": "protected-code"}
    identities = {
        name: name
        for name in (
            "request_fingerprint",
            "problem_fingerprint",
            "rule_set_fingerprint",
            "policy_fingerprint",
        )
    }
    dependencies = {
        str(runner.PRECHECK): "precheck-code",
        str(runner.BASE / "quality_gate.json"): "frozen-gate",
    }
    events, launches = [], []

    def verify(repository, revision):
        events.append(("verify", repository, revision))
        return protected

    checker = SimpleNamespace(
        ROOT=root,
        EXPECTED_IDENTITIES=identities,
        verify_code_revision=verify,
        sha256=lambda path: sha256(path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(runner, "dependency_hashes", lambda _: dict(dependencies))
    ticks = count(100.0)
    monkeypatch.setattr(runner.time, "perf_counter", lambda: next(ticks))

    def emit(command, **options):
        events.append(("launch",))
        launches.append((command, options))
        output = Path(command[command.index("--output-dir") + 1])
        output.mkdir()
        for name in runner.ORIGINAL_ARTIFACTS:
            (output / name).write_text(name + "\n", encoding="utf-8")
        report = {name: f"semantic-{name}" for name in runner.SEMANTIC_FIELDS}
        report.update(
            code_revision="a" * 40,
            identities=dict(identities),
            counters={"candidate_check_count": 200000},
            observations={
                "stop_reason": "candidate_limit_reached",
                "quality_key": [0, 0, 0, 0, 100, 0, 2],
            },
            timings={"outer": 1.0},
            cache={"hits": 10},
            artifacts_sha256={
                name: checker.sha256(output / name) for name in runner.ORIGINAL_ARTIFACTS
            },
            python=runner.platform.python_version(),
            protected_hashes=protected,
            script_sha256=dependencies[str(runner.PRECHECK)],
            quality_gate_sha256=dependencies[str(runner.BASE / "quality_gate.json")],
            passed=True,
            failures=[],
        )
        (output / "quality_report.json").write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "child stdout", "child stderr")

    monkeypatch.setattr(runner.subprocess, "run", emit)
    return SimpleNamespace(
        checker=checker,
        protected=protected,
        dependencies=dependencies,
        events=events,
        launches=launches,
        repository=tmp_path / "repository",
        revision="a" * 40,
        output=tmp_path / "sample",
        emit=emit,
    )


def measure(runner, case):
    return runner.measure_new(
        case.checker, case.repository, case.revision, case.output, case.protected
    )


def test_new_child_uses_direct_python_export_paths_and_checks_both_sides(runner, fake_child):
    result = measure(runner, fake_child)
    assert result["comparison"] == {"status": "pass"}
    assert result["quality_passed"] and result["elapsed_seconds"] == 1.0
    assert [row[0] for row in fake_child.events] == ["verify", "launch", "verify"]
    ((command, options),) = fake_child.launches
    assert command == [
        sys.executable,
        str(fake_child.checker.ROOT / runner.PRECHECK),
        "--code-repository",
        str(fake_child.repository),
        "--code-revision",
        fake_child.revision,
        "--output-dir",
        str(fake_child.output),
    ]
    assert options["cwd"] == fake_child.checker.ROOT
    assert options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert options["env"]["PYTHONPATH"] == os.pathsep.join(
        str(path)
        for path in (
            fake_child.checker.ROOT / "src",
            fake_child.checker.ROOT,
            fake_child.checker.ROOT / "tools",
        )
    )
    assert options["capture_output"] and options["text"] and options["timeout"] == 600
    signature = result["semantic_signature"]
    assert "timings" not in signature and "cache" not in signature
    assert set(signature["stable_artifacts_sha256"]) == runner.ORIGINAL_ARTIFACTS - {
        "public_result.canonical.json"
    }
    assert result["stdout"] == "child stdout" and result["stderr"] == "child stderr"


@pytest.mark.parametrize(
    "damage", ("artifact", "identity", "revision", "protected", "gate", "quality")
)
def test_invalid_child_artifacts_or_identity_are_retained_but_fail(
    runner, fake_child, monkeypatch, damage
):
    def damaged(command, **options):
        result = fake_child.emit(command, **options)
        path = fake_child.output / "quality_report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        if damage == "artifact":
            (fake_child.output / "chain_detail.csv").write_text("changed", encoding="utf-8")
        elif damage == "identity":
            report["identities"]["problem_fingerprint"] = "changed"
        elif damage == "revision":
            report["code_revision"] = "b" * 40
        elif damage == "protected":
            report["protected_hashes"] = {}
        elif damage == "gate":
            report["quality_gate_sha256"] = "changed"
        else:
            report["passed"] = False
            report["failures"] = ["synthetic quality failure"]
        path.write_text(json.dumps(report), encoding="utf-8")
        return result

    monkeypatch.setattr(runner.subprocess, "run", damaged)
    result = measure(runner, fake_child)
    assert result["comparison"]["status"] == "fail"
    assert result["stdout"] == "child stdout" and result["stderr"] == "child stderr"
    assert {path.name for path in fake_child.output.iterdir()} == runner.ORIGINAL_ARTIFACTS | {
        "quality_report.json"
    }


@pytest.mark.parametrize("moment", ("before", "after"))
def test_protected_code_drift_prevents_or_invalidates_child(
    runner, fake_child, monkeypatch, moment
):
    calls = []

    def drift(*_):
        calls.append(None)
        return {} if moment == "before" or len(calls) > 1 else fake_child.protected

    monkeypatch.setattr(fake_child.checker, "verify_code_revision", drift)
    if moment == "before":
        with pytest.raises(ValueError, match="before"):
            measure(runner, fake_child)
        assert fake_child.launches == [] and not fake_child.output.exists()
    else:
        result = measure(runner, fake_child)
        assert result["comparison"]["status"] == "fail"
        assert "after" in result["comparison"]["error"]
        assert len(fake_child.launches) == 1 and fake_child.output.is_dir()


def test_new_child_timeout_keeps_partial_output_and_never_retries(runner, fake_child, monkeypatch):
    calls = []

    def timeout(command, **options):
        calls.append(command)
        fake_child.output.mkdir()
        (fake_child.output / "partial.txt").write_text("partial", encoding="utf-8")
        raise subprocess.TimeoutExpired(
            command, options["timeout"], output=b"partial stdout", stderr=b"partial stderr"
        )

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    result = measure(runner, fake_child)
    assert len(calls) == 1 and result["timed_out"] and result["returncode"] is None
    assert result["stdout"] == "partial stdout" and result["stderr"] == "partial stderr"
    assert result["comparison"]["status"] == "fail"
    assert (fake_child.output / "partial.txt").read_text(encoding="utf-8") == "partial"


def test_existing_output_is_rejected_before_loading_or_launching(runner, fake_child, monkeypatch):
    fake_child.output.mkdir()
    sentinel = fake_child.output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        measure(runner, fake_child)
    monkeypatch.setattr(
        runner, "load_runtime", lambda *_: pytest.fail("must reject before loading")
    )
    with pytest.raises(SystemExit) as failure:
        runner.main(
            [
                "--mode",
                "determinism",
                "--code-root",
                str(fake_child.checker.ROOT),
                "--code-repository",
                str(fake_child.repository),
                "--code-revision",
                fake_child.revision,
                "--output-dir",
                str(fake_child.output),
            ]
        )
    assert failure.value.code == 2 and fake_child.events == []
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("damage", ("hash", "samples", "warmup", "python"))
def test_invalid_frozen_gate_fails_before_reference_verification(
    runner, benchmark, monkeypatch, tmp_path, damage
):
    base = tmp_path / runner.BASE
    base.mkdir(parents=True)
    for name in (
        "performance_gate.json",
        "reference_manifest.json",
        "reference_performance_samples.json",
    ):
        (base / name).write_bytes((ROOT / runner.BASE / name).read_bytes())
    path = base / "performance_gate.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    field, value = {
        "hash": ("reference_manifest_sha256", "changed"),
        "samples": ("samples_per_solver", 19),
        "warmup": ("warmup_per_solver", 0),
        "python": ("python_version", "0.0.0"),
    }[damage]
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    checker = SimpleNamespace(
        ROOT=tmp_path, sha256=lambda path: sha256(path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        benchmark,
        "verify_manifest",
        lambda *_: pytest.fail("invalid gate must not verify or launch the reference"),
    )
    with pytest.raises(ValueError):
        runner.load_gate(checker, benchmark)
