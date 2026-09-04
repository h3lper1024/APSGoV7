"""Verify reference evidence without treating best effort as product acceptance."""

import copy
import csv
import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "tests" / "baselines" / "gqga4"
INPUT_HASHES = {
    "input_orders.csv": "d178cc19ac6ef2807962beb110333f8702c8e2eeec153f9b0f94236913060941",
    "optimization_problem.json": "8edb7f4110384af14d58e9bdc7caec490f3de053401173db952854275966abdc",
    "resolved_rules.json": "5f045f8415ea0b3fd77256897e28b2e24c9dd6f661cd3e9f0bb48448bb3cf44a",
    "rule_context.json": "90091b7c7bbcf4373c6745a8cde15c4501ff7246628fd5b7824ad69d451347c7",
    "solver_config.json": "f539c3231e524097c964a6751046f1ec0d3e0cd89e7d74ebea0d282f65eec47c",
}
OUTPUT_HASHES = {
    "run_manifest.json": "41c7127eb1e75308da92e670975f97ea30577dd8fb5954014cef96ed427419f8",
    "validation_report.json": "4ff4fe71f25cc21d2b42ece7599c642059fcb52dc464625bdb3804dabdf16f35",
    "chain_summary.csv": "4d0b7f5e69f856443fecb149667c508542076793f8c529f9f0d1aa84e9544c44",
    "schedule_result.csv": "444927fb70f0afbd602d9de2673ca978b8ec4be9d4c46a964dba0a43d1722655",
}
SCRIPT_HASH = "87f564407f0cefeef3c66a7724e534f3621ac0a52207fd5b114a0e33f7f97318"
V3_HASH = "1a28933a36d019380f17116dbf8411b5163bd30910bf633bec8496c8f8c13897"
V3_IGNORED_METADATA = frozenset({".DS_Store", "rolling_final_repair/.DS_Store"})


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def load_tool(name):
    if name in {"compare_solver_stages", "benchmark_solver_process"}:
        load_tool("capture_solverpy_reference")
    if name == "benchmark_solver_process":
        load_tool("compare_solver_stages")
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def v3_identity_paths(directory):
    observed = []
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink() or (path.is_file() and relative not in V3_IGNORED_METADATA):
            observed.append(relative)
    return sorted(observed, key=lambda value: value.encode("utf-8"))


def test_five_inputs_have_frozen_bytes_and_provenance():
    manifest = read_json(BASELINE / "reference_manifest.json")
    assert manifest["schema_version"] == 1
    assert set(manifest["inputs"]) == set(INPUT_HASHES)
    for name, expected in INPUT_HASHES.items():
        entry = manifest["inputs"][name]
        assert entry["path"] == f"tests/baselines/gqga4/inputs/{name}"
        data = (ROOT / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"] == expected
        assert len(data) == entry["bytes"]
        if name == "input_orders.csv":
            assert entry["source"] == (
                "/Users/miles/Documents/Codex/2026-09-02/"
                "gqga4-531-1-input-orders-csv/work/archive/input_orders.csv"
            )
        else:
            old_path = (
                "tests/fixtures/migration/gqga4_v5_reference/optimization_problem.json"
                if name == "optimization_problem.json"
                else f"baselines/gqga4/{name}"
            )
            assert entry["source"] == f"dbf5500b6877fcc4290d90486d9ae3614d9652b8:{old_path}"
    assert len(read_csv(BASELINE / "inputs" / "input_orders.csv")) == 531
    summary = read_json(BASELINE / "inputs" / "optimization_problem.json")["summary"]
    assert summary["node_count"] == summary["source_count"] == 531


def test_fixed_reference_identity_and_result_are_not_product_success():
    manifest = read_json(BASELINE / "reference_manifest.json")
    script = Path(manifest["script"]["path"])
    assert script == Path(
        "/Users/miles/Documents/Codex/2026-09-02/gqga4-531-1-input-orders-csv/outputs/solver.py"
    )
    assert (
        hashlib.sha256(script.read_bytes()).hexdigest()
        == manifest["script"]["sha256"]
        == SCRIPT_HASH
    )
    assert set(manifest["static_outputs"]) == set(OUTPUT_HASHES)
    for name, expected in OUTPUT_HASHES.items():
        entry = manifest["static_outputs"][name]
        assert Path(entry["path"]) == script.parent / name
        assert (
            hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
            == entry["sha256"]
            == expected
        )
    assert manifest["parameters"] == {
        "seed": 590531,
        "time_budget_seconds": 30,
        "candidate_check_budget": 100000,
    }
    assert manifest["formal_environment"]["python"] == "3.10.18"
    assert manifest["historical_environment"]["python"] == "3.9.6"
    run = read_json(script.parent / "run_manifest.json")
    report = read_json(script.parent / "validation_report.json")
    assert run["input_hashes"] == INPUT_HASHES
    assert run["candidate_checks"] == 94024
    assert run["stop_reason"] == "search_complete"
    assert run["status"] == report["status"] == "BEST_EFFORT"
    assert (
        run["best_quality_vector"]
        == report["seven_level_quality"]["vector"]
        == [1, 70.3, 0, 0.0, 22, 540.0, 22694.88]
    )
    assert report["result_metrics"]["real_order_count"] == 531
    assert Decimal(report["result_metrics"]["real_weight"]) == Decimal("29333.91")
    assert len(read_csv(script.parent / "schedule_result.csv")) == 559
    assert len(read_csv(script.parent / "chain_summary.csv")) == 22


def test_v3_complete_file_manifest_and_metrics():
    manifest = read_json(BASELINE / "v3_reference_manifest.json")
    directory = Path(manifest["root"])
    files = manifest["files"]
    paths = [entry["path"] for entry in files]
    assert len(paths) == len(set(paths)) == manifest["file_count"] == 170
    assert paths == sorted(paths, key=lambda value: value.encode("utf-8"))
    assert paths == v3_identity_paths(directory)
    serialized = ""
    for entry in files:
        relative = Path(entry["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        path = directory / relative
        assert not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        serialized += f"{entry['path']}\t{entry['sha256']}\n"
    assert (
        hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        == manifest["manifest_sha256"]
        == V3_HASH
    )
    evidence = manifest["evidence"]
    schedule = directory / evidence["schedule"]
    assert schedule.read_bytes() == (directory / evidence["identical_final_schedule"]).read_bytes()
    rows = read_csv(schedule)
    checks = read_csv(directory / evidence["rules"])
    stored = read_json(directory / evidence["metrics"])
    states = {row["chain_id"]: row["weight_status"] for row in rows}
    assert set(states.values()) == {"ok", "lt_min"}
    assert {key for key, value in states.items() if value == "lt_min"} == {
        row["chain_id"] for row in checks if row["weight_ok"] == "False"
    }
    real = sum(
        (Decimal(row["weight"]) for row in rows if not row["node_type"].startswith("virtual")),
        Decimal(0),
    )
    virtual = sum(
        (Decimal(row["weight"]) for row in rows if row["node_type"].startswith("virtual")),
        Decimal(0),
    )
    observed = {
        "chain_count": len({row["chain_id"] for row in rows}),
        "underweight_chain_count": sum(row["weight_ok"] == "False" for row in checks),
        "real_weight": str(real),
        "virtual_weight": str(virtual.quantize(Decimal(1))),
        "prohibited_violation_chain_count_excluding_underweight": sum(
            any(
                reason != "weight"
                for reason in row["hard_rule_failed_reasons"].split("|")
                if reason
            )
            for row in checks
        ),
    }
    assert (
        observed
        == manifest["metrics"]
        == {
            "chain_count": 37,
            "underweight_chain_count": 3,
            "real_weight": "29333.91",
            "virtual_weight": "1080",
            "prohibited_violation_chain_count_excluding_underweight": 1,
        }
    )
    assert stored["chain_count"] == observed["chain_count"]
    assert stored["weight_failed_chain_count"] == observed["underweight_chain_count"]
    assert Decimal(str(stored["real_weight"])) == real
    assert Decimal(str(stored["virtual_weight"])) == virtual


@pytest.mark.parametrize(
    "metadata",
    [(), (".DS_Store",), ("rolling_final_repair/.DS_Store",), tuple(sorted(V3_IGNORED_METADATA))],
)
def test_v3_identity_ignores_only_approved_regular_metadata_files(tmp_path, metadata):
    protected = tmp_path / "input_orders.csv"
    protected.write_bytes(b"frozen input\n")
    before = hashlib.sha256(protected.read_bytes()).hexdigest()
    for relative in metadata:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Finder metadata\n")
    assert v3_identity_paths(tmp_path) == ["input_orders.csv"]
    assert hashlib.sha256(protected.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "relative", ["unexpected.csv", "other/.DS_Store", "rolling_final_repair/nested/.DS_Store"]
)
def test_v3_identity_keeps_unapproved_files_visible(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unexpected\n")
    observed = v3_identity_paths(tmp_path)
    assert relative in observed
    assert observed != []


@pytest.mark.parametrize("relative", sorted(V3_IGNORED_METADATA))
@pytest.mark.parametrize("target_kind", ["regular", "missing", "directory"])
def test_v3_identity_never_hides_symlinks_at_approved_metadata_paths(
    tmp_path, relative, target_kind
):
    target = tmp_path / "target"
    if target_kind == "regular":
        target.write_bytes(b"protected\n")
    elif target_kind == "directory":
        target.mkdir()
    link = tmp_path / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=target_kind == "directory")
    assert relative in v3_identity_paths(tmp_path)
    assert link.is_symlink()


@pytest.mark.parametrize("exists", [False, True])
def test_reference_verifier_rejects_missing_or_changed_input(tmp_path, exists):
    tool = load_tool("capture_solverpy_reference")
    manifest = copy.deepcopy(read_json(BASELINE / "reference_manifest.json"))
    changed = tmp_path / "input_orders.csv"
    if exists:
        changed.write_bytes(b"altered reference input\n")
    manifest["inputs"]["input_orders.csv"]["path"] = str(changed)
    with pytest.raises(ValueError):
        tool.verify_manifest(manifest)


def test_reference_verifier_accepts_frozen_files():
    tool = load_tool("capture_solverpy_reference")
    assert tool.verify_manifest(read_json(BASELINE / "reference_manifest.json"))


def test_field_difference_reports_first_changed_value():
    tool = load_tool("compare_solver_stages")
    expected = {"quality": [1, 70.3], "status": "BEST_EFFORT"}
    assert tool.first_difference(expected, copy.deepcopy(expected)) is None
    difference = tool.first_difference(expected, {"quality": [1, 80.3], "status": "BEST_EFFORT"})
    assert difference["path"] == "$.quality[1]"
    assert difference["expected"] == 70.3
    assert difference["actual"] == 80.3


def test_semantic_comparison_ignores_only_declared_runtime_metadata():
    tool = load_tool("compare_solver_stages")
    common = {"status": "BEST_EFFORT", "time_budget_seconds": 30, "candidate_checks": 94024}
    assert tool.semantic_manifest(
        {**common, "actual_runtime_seconds": 1}
    ) == tool.semantic_manifest({**common, "actual_runtime_seconds": 2})
    assert tool.semantic_manifest(common) != tool.semantic_manifest(
        {**common, "candidate_checks": 94023}
    )
    assert tool.semantic_manifest(common) != tool.semantic_manifest(
        {**common, "time_budget_seconds": 60}
    )


def test_reference_performance_statistics_use_declared_methods():
    result = load_tool("benchmark_solver_process").summarize(list(range(20, 0, -1)))
    assert result["sample_count"] == 20
    assert result["median_seconds"] == 10.5
    assert result["p95_seconds"] == 19
    assert result["max_seconds"] == 20


@pytest.mark.parametrize("samples", [[], [-1], [float("nan")], [float("inf")]])
def test_reference_performance_statistics_reject_invalid_samples(samples):
    with pytest.raises(ValueError):
        load_tool("benchmark_solver_process").summarize(samples)


def test_reference_timeout_preserves_failure_details_and_rechecks_hashes(monkeypatch):
    tool = load_tool("benchmark_solver_process")
    events = []
    monkeypatch.setattr(tool, "verify_manifest", lambda manifest: events.append("verify"))

    def timeout(command, **kwargs):
        events.append("run")
        raise tool.subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=b"partial stdout", stderr=b"partial stderr"
        )

    monkeypatch.setattr(tool.subprocess, "run", timeout)
    sample = tool.measure(read_json(BASELINE / "reference_manifest.json"))
    assert events == ["verify", "run", "verify"]
    assert sample["timed_out"] is True
    assert sample["returncode"] is None
    assert sample["elapsed_seconds"] >= 0
    assert sample["stdout"] == "partial stdout"
    assert sample["stderr"] == "partial stderr"
    assert sample["comparison"]["status"] == "fail"
    assert sample["command"][0] == sys.executable


def test_report_save_preserves_existing_temporary_symlink_target(tmp_path):
    output = tmp_path / "samples.json"
    protected = tmp_path / "user_owned.txt"
    protected.write_text("do not overwrite", encoding="utf-8")
    existing_temp = tmp_path / "samples.json.tmp"
    existing_temp.symlink_to(protected)
    report = {"status": "running", "samples": []}
    load_tool("benchmark_solver_process").save_report(output, report)
    assert protected.read_text(encoding="utf-8") == "do not overwrite"
    assert existing_temp.is_symlink()
    assert read_json(output) == report
    assert set(tmp_path.iterdir()) == {output, protected, existing_temp}


@pytest.mark.parametrize(
    "relative_path",
    [
        "tests/baselines/gqga4/reference_performance_samples.json",
        "docs/implementation/evidence/solverpy_path_cover_local_search/"
        "function_01_reference_baseline/clean_export_performance_samples.json",
    ],
)
def test_saved_reference_performance_contains_all_successful_samples(relative_path):
    report = read_json(ROOT / relative_path)
    assert report["status"] == "pass"
    assert report["environment"]["python"] == "3.10.18"
    assert len(report["warmup"]) == report["warmup_requested"] == 1
    assert len(report["samples"]) == report["samples_requested"] == 20
    assert (
        report["manifest_sha256"]
        == hashlib.sha256((BASELINE / "reference_manifest.json").read_bytes()).hexdigest()
    )
    for section in (report["warmup"], report["samples"]):
        assert [sample["index"] for sample in section] == list(range(1, len(section) + 1))
        for sample in section:
            assert sample["returncode"] == 0
            assert sample["comparison"]["status"] == "pass"
            assert sample["candidate_checks"] == 94024
            assert sample["stop_reason"] == "search_complete"
            assert sample["quality"] == [1, 70.3, 0, 0.0, 22, 540.0, 22694.88]
    assert report["statistics"] == load_tool("benchmark_solver_process").summarize(
        [sample["elapsed_seconds"] for sample in report["samples"]]
    )


def test_confirmed_quality_gate_removes_only_the_user_cancelled_chain_ceiling():
    gate = read_json(BASELINE / "quality_gate.json")
    assert gate["schema_version"] == 1
    assert gate["status"] == "frozen"
    assert gate["confirmed_on"] == "2026-09-04"
    assert (
        gate["reference_manifest_sha256"]
        == hashlib.sha256((BASELINE / "reference_manifest.json").read_bytes()).hexdigest()
    )
    assert gate["maximum_chain_count"] is None
    assert "User explicitly removed the chain-count acceptance ceiling" in gate["decision"]
    assert gate["maximum_underweight_chain_count"] == 0
    assert gate["maximum_prohibited_violation_count"] == 0
    assert gate["expected_real_order_count"] == 531
    assert Decimal(gate["expected_real_weight"]) == Decimal("29333.91")
    assert Decimal(gate["maximum_virtual_output_weight_ratio"]) == Decimal("0.05")
    assert gate["maximum_late_original_due_period_move_count"] == 0
    assert gate["allowed_final_deviations"] == []
    assert gate["required_checks"] == [
        "complete_coverage_and_conservation",
        "source_order_weight_conservation",
        "controlled_split_authorization_and_traceability",
        "search_and_uncached_audit_agree",
        "result_contract_self_check",
    ]
    assert gate["new_implementation_quality_observed_before_freeze"] is False
    stage = read_json(BASELINE / "reference_stage_expectations.json")
    observation = gate["reference_observation"]
    assert observation["status"] == stage["final"]["run_manifest"]["status"] == "BEST_EFFORT"
    assert observation["chain_count"] == stage["final"]["quality"][4] == 22
    assert observation["underweight_chain_count"] == stage["final"]["quality"][2] == 0
    assert observation["prohibited_violation_count"] == stage["final"]["quality"][0] == 1
    assert observation["prohibited_violation_count"] > gate["maximum_prohibited_violation_count"]
    assert observation["passes_this_gate"] is False
    proposal = read_json(
        ROOT / "docs/implementation/evidence/solverpy_path_cover_local_search/"
        "function_01_reference_baseline/gate_proposal.json"
    )
    assert proposal["status"] == "superseded_by_user_confirmed_gates"
    assert proposal["superseded_by"] == [
        "tests/baselines/gqga4/quality_gate.json",
        "tests/baselines/gqga4/performance_gate.json",
    ]


def test_confirmed_performance_gate_binds_full_process_samples_and_control_policy():
    gate = read_json(BASELINE / "performance_gate.json")
    report = read_json(BASELINE / "reference_performance_samples.json")
    assert gate["schema_version"] == 1
    assert gate["status"] == "frozen"
    assert gate["confirmed_on"] == "2026-09-03"
    for key, name in (
        ("reference_manifest_sha256", "reference_manifest.json"),
        ("reference_performance_sha256", "reference_performance_samples.json"),
    ):
        assert gate[key] == hashlib.sha256((BASELINE / name).read_bytes()).hexdigest()
    assert gate["reference_statistics"] == report["statistics"]
    assert gate["python_version"] == report["environment"]["python"] == "3.10.18"
    assert gate["warmup_per_solver"] == 1
    assert gate["samples_per_solver"] == 20
    assert gate["pair_order"] == "alternating_reference_first_on_odd_pairs"
    assert gate["new_solver_maximum_median_seconds"] == 180
    assert gate["new_solver_maximum_p95_seconds"] == 180
    assert gate["maximum_sample_seconds_is_gate"] is False
    assert gate["reference_control_maximum_absolute_median_drift_ratio"] == 0.2
    assert gate["reference_control_maximum_absolute_p95_drift_ratio"] == 0.2
    assert gate["drift_formula"] == "abs(control / frozen_reference - 1)"
    assert gate["all_samples_retained"] is True
    assert gate["new_implementation_performance_observed_before_freeze"] is False


def test_saved_stage_trace_preserves_paths_acceptance_and_reference_result():
    stage = read_json(BASELINE / "reference_stage_expectations.json")
    manifest = read_json(BASELINE / "reference_manifest.json")
    assert stage["schema_version"] == 1
    assert stage["script_sha256"] == SCRIPT_HASH
    assert stage["environment"]["python"] == "3.10.18"
    assert stage["parameters"] == manifest["parameters"]
    assert stage["instrumentation"]["source_modified"] is False
    assert stage["instrumentation"]["time_budget_seconds_override"] == 600
    input_ids = stage["input"]["node_ids"]
    assert len(input_ids) == len(set(input_ids)) == 531
    assert Decimal(stage["input"]["real_weight"]) == Decimal("29333.91")
    graph = stage["path_cover"]
    ordered = graph["ordered_node_ids"]
    assert len(ordered) == 531 and set(ordered) == set(input_ids)
    rank = {node: index for index, node in enumerate(ordered)}
    successors = {row["node_id"]: row["successor_node_ids"] for row in graph["adjacency"]}
    assert all(
        rank[left] < rank[right] for left, targets in successors.items() for right in targets
    )
    path_ids = [node for path in graph["paths"] for node in path]
    assert len(graph["paths"]) == 17
    assert len(path_ids) == len(set(path_ids)) == 531 and set(path_ids) == set(input_ids)
    for path in graph["paths"]:
        assert all(right in successors[left] for left, right in zip(path, path[1:]))
    for left, right in graph["match_left"].items():
        if right is not None:
            assert graph["match_right"][right] == left
    assert len(stage["initial_plan"]["plan"]) == 31
    assert stage["initial_plan"]["quality"] == [2, 170.3, 15, 4003.93, 31, 0.0, 21354.53]
    assert len(stage["search_rounds"]) == 2
    first, second = stage["search_rounds"]
    assert [
        first["accepted_count"],
        stage["split"]["accepted_count"],
        second["accepted_count"],
    ] == [40, 1, 9]
    previous = stage["initial_plan"]["quality"]
    checks = 0
    for section in (first, stage["split"], second):
        assert section["start"]["quality"] == previous
        assert len(section["accepted_actions"]) == section["accepted_count"]
        for action in section["accepted_actions"]:
            assert action["quality_before"] == previous
            assert tuple(action["quality"]) < tuple(previous)
            assert action["candidate_checks"] >= checks
            assert len(action["plan"]) == action["quality"][4]
            assert all(
                node in stage["node_catalog"]
                for chain in action["plan"]
                for node in chain["node_ids"]
            )
            previous = action["quality"]
            checks = action["candidate_checks"]
        assert section["final"]["quality"] == previous
    final = stage["final"]
    assert final["quality"] == previous == [1, 70.3, 0, 0.0, 22, 540.0, 22694.88]
    assert final["candidate_checks"] == sum(stage["candidate_count_sites"].values()) == 94024
    assert final["stop_reason"] == "search_complete"
    assert len(final["ordered_node_ids"]) == 559
    assert load_tool("compare_solver_stages").compare_frozen_stage(manifest, stage)["equivalent"]
