"""Only declared timing/cache values may differ in fixed-work bridge comparisons."""

import copy
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.app.test_chain_reuse_measurement import TIMING_PATHS, measurement, parent_at

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / (
    "docs/implementation/evidence/apsgo_v7_numpy_numba_virtual_bridge/"
    "stage_04_comparison/run_pairs.py"
)
SPEC = importlib.util.spec_from_file_location("numeric_bridge_pair_runner_test", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
PROBE_SPEC = importlib.util.spec_from_file_location("numeric_bridge_profile_guard_test",
                                                  RUNNER_PATH.with_name("probe_bridge.py"))
PROBE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(PROBE)

CACHE_PATHS = tuple(
    (*prefix, "edge_cache", key)
    for prefix in (("first_search", "initial"), ("first_search", "final"), ("final_search",))
    for key in ("hits", "misses", "entries")
)


def test_stable_projection_masks_only_nine_cache_counts_and_existing_timing_values():
    assert tuple(RUNNER.CACHE_COUNT_PATHS) == CACHE_PATHS
    original = measurement()
    before = copy.deepcopy(original)
    changed = copy.deepcopy(original)
    for path in TIMING_PATHS:
        parent_at(changed, path)[path[-1]] = Decimal("999.125")
    for path in CACHE_PATHS:
        parent_at(changed, path)[path[-1]] = 999
    stable = RUNNER.stable_measurement(original)
    assert stable == RUNNER.stable_measurement(changed)
    assert original == before
    assert stable["final_search"]["edge_cache"] == {"hits": None, "misses": None, "entries": None}
    assert changed["final_search"]["edge_cache"]["hits"] == 999
    stable["final_search"]["plan"]["chains"].reverse()
    assert original == before


@pytest.mark.parametrize("path", (*TIMING_PATHS, *CACHE_PATHS,
                                  ("first_search", "initial", "edge_cache"),
                                  ("first_search", "final", "edge_cache"),
                                  ("final_search", "edge_cache")))
def test_removed_timing_or_cache_locations_remain_comparison_differences(path):
    original = measurement()
    changed = copy.deepcopy(original)
    del parent_at(changed, path)[path[-1]]
    assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


@pytest.mark.parametrize("path,replacement", (
    (("final_search", "edge_cache", "new_counter"), 1),
    (("final_search", "edge_cache", "wall_seconds"), Decimal(1)),
    (("first_search", "new_snapshot"), {"edge_cache": {"hits": 1, "misses": 1, "entries": 1}}),
    (("result", "release", "edge_cache"), {"hits": 1, "misses": 1, "entries": 1}),
    (("first_search_timing", "functions", 0, "future_duration"), Decimal(1)),
    (("result", "run_manifest", "stage_duration_seconds", "new_stage"), Decimal(1)),
    (("new_timing",), {"wall_seconds": Decimal(1), "cpu_seconds": Decimal(1)}),
))
def test_new_timing_cache_or_unknown_locations_are_not_silently_discarded(path, replacement):
    original = measurement()
    changed = copy.deepcopy(original)
    parent_at(changed, path)[path[-1]] = replacement
    assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


@pytest.mark.parametrize("replacement", ({"nested_counter": 1}, None, True, -1, Decimal(1), "1"))
@pytest.mark.parametrize("path", (CACHE_PATHS[0], CACHE_PATHS[4], CACHE_PATHS[8]))
def test_cache_allowance_rejects_non_counts_instead_of_hiding_structural_changes(path, replacement):
    changed = measurement()
    parent_at(changed, path)[path[-1]] = replacement
    with pytest.raises(ValueError):
        RUNNER.stable_measurement(changed)


@pytest.mark.parametrize("path,replacement", (
    (("first_search", "initial", "plan", "chains"), [{"chain_id": "b"}, {"chain_id": "a"}]),
    (("first_search", "final", "evaluation", "violations"), [{"subject_id": "b"}, {"subject_id": "a"}]),
    (("final_search", "trace"), [{"sequence": 2}, {"sequence": 1}]),
    (("final_search", "quality"), [0, 0, 0, 0, 11727, 660, 23]),
    (("final_search", "candidate_check_count"), 199999),
    (("final_search", "complete_candidate_evaluation_count"), 3696),
    (("final_search", "accepted_move_count"), 53),
    (("first_search_timing", "functions", 0, "candidate_check_count"), 101),
    (("first_search_timing", "functions", 0, "function"), "different_function"),
    (("result", "release", "resource_facts", "real_weight"), Decimal("29333.90")),
    (("result", "release", "future_field", "wall_seconds"), Decimal("7.26")),
    (("result", "run_manifest", "code_revision"), "different_code"),
    (("result", "run_manifest", "counters", "candidate_check_count"), 199999),
    (("result", "run_manifest", "search_was_truncated"), False),
    (("result", "run_manifest", "optimality_proven"), True),
    (("result", "run_manifest", "trace_fingerprint"), "different_trace"),
    (("result", "stop_reason"), "search_time_limit_reached"),
    (("result", "core_audit", "passed"), False),
    (("result", "audit_report", "passed"), False),
    (("result", "release"), None),
))
def test_all_ordered_business_data_and_non_cache_manifest_fields_remain_exact(path, replacement):
    original = measurement()
    changed = copy.deepcopy(original)
    parent_at(changed, path)[path[-1]] = replacement
    assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


def test_added_business_fingerprints_and_ordered_nodes_remain_bound():
    original = measurement()
    original["result"]["result_fingerprint"] = "result-original"
    original["final_search"]["evaluation_fingerprint"] = "evaluation-original"
    original["final_search"]["plan"]["chains"][0]["nodes"] = [{"id": "a"}, {"id": "b"}]
    for path, value in (
        (("result", "result_fingerprint"), "result-changed"),
        (("final_search", "evaluation_fingerprint"), "evaluation-changed"),
        (("final_search", "plan", "chains", 0, "nodes"), [{"id": "b"}, {"id": "a"}]),
    ):
        changed = copy.deepcopy(original)
        parent_at(changed, path)[path[-1]] = value
        assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


def test_frozen_work_gate_accepts_original_counts_and_audits_without_mutation():
    original = measurement()
    before = copy.deepcopy(original)
    assert RUNNER.require_fixed_work(original) is None
    assert original == before


@pytest.mark.parametrize("path,replacement", (
    (("measurement_scope",), "first_local_search_only_no_release"),
    (("first_search", "final", "candidate_check_count"), 64225),
    (("first_search", "final", "complete_candidate_evaluation_count"), 2262),
    (("first_search", "final", "accepted_move_count"), 33),
    (("first_search", "final", "stop_reason"), "search_time_limit_reached"),
    (("final_search", "candidate_check_count"), 199999),
    (("final_search", "complete_candidate_evaluation_count"), 3696),
    (("final_search", "accepted_move_count"), 53),
    (("final_search", "stop_reason"), "user_cancelled"),
    (("result", "stop_reason"), "finalization_time_limit_reached"),
    (("result", "release"), None),
    (("result", "core_audit", "passed"), False),
    (("result", "audit_report", "passed"), False),
))
def test_partial_work_cancellation_or_failed_audit_is_not_an_equivalence_sample(path, replacement):
    changed = measurement()
    parent_at(changed, path)[path[-1]] = replacement
    with pytest.raises(ValueError):
        RUNNER.require_fixed_work(changed)


def first_search_probe():
    value = measurement()
    value["measurement_scope"] = "first_local_search_only_no_release"
    del value["result"]
    events = [{"event": "solver_bridge_numeric_ready", "mode": mode, "nopython": True,
               "found": mode == "double"} for mode in ("single", "double")]
    return value, {"count": 2}, events


@pytest.mark.parametrize("numeric", (False, True))
def test_profile_guard_accepts_complete_first_work_and_requires_native_modes_only_for_new_code(numeric):
    value, blocks, events = first_search_probe()
    if not numeric:
        blocks, events = {"count": 0}, []
    before = copy.deepcopy((value, blocks, events))
    assert PROBE.require_first_search(value, blocks, events, numeric=numeric) is None
    assert (value, blocks, events) == before


@pytest.mark.parametrize("path,replacement", (
    (("measurement_scope",), "full_public_solve_with_observation"),
    (("first_search", "final", "candidate_check_count"), 64225),
    (("first_search", "final", "complete_candidate_evaluation_count"), 2262),
    (("first_search", "final", "accepted_move_count"), 33),
    (("first_search", "final", "stop_reason"), "search_time_limit_reached"),
))
def test_profile_guard_rejects_wrong_scope_work_or_time_cut(path, replacement):
    value, blocks, events = first_search_probe()
    parent_at(value, path)[path[-1]] = replacement
    with pytest.raises(ValueError, match="frozen first-search work"):
        PROBE.require_first_search(value, blocks, events, numeric=True)


@pytest.mark.parametrize("case", ("no_blocks", "negative_blocks", "missing_single", "missing_double",
                                  "single_not_nopython", "double_not_nopython"))
def test_profile_guard_rejects_missing_actual_numeric_execution_evidence(case):
    value, blocks, events = first_search_probe()
    if case in ("no_blocks", "negative_blocks"):
        blocks["count"] = 0 if case == "no_blocks" else -1
    elif case.startswith("missing_"):
        events = [event for event in events if event["mode"] != case.removeprefix("missing_")]
    else:
        for event in events:
            if case.startswith(event["mode"]):
                event["nopython"] = False
    with pytest.raises(ValueError, match="both native bridge modes"):
        PROBE.require_first_search(value, blocks, events, numeric=True)


def identity_sample():
    source = {"production_source_files": {"src/apsgo_scheduler/core/virtual_material.py": "a" * 64},
              "production_source_sha256": "b" * 64}
    policy = {"total_time_limit_seconds": Decimal(900), "finalization_reserve_seconds": Decimal(10),
              "candidate_check_limit": 200000, "seed": 590531}
    value = {
        "prepared_request_sha256": RUNNER.PREPARED_SHA256,
        "original_request_fingerprint": RUNNER.REQUEST_FINGERPRINT,
        "measured_request_fingerprint": RUNNER.REQUEST_FINGERPRINT,
        "original_policy": policy, "measured_policy": copy.deepcopy(policy),
        "platform": "synthetic-platform", "python": "3.10.18",
        "tool_sha256": RUNNER.TOOL_SHA256, "profile_enabled": False, "requested_scope": "full",
        "source": copy.deepcopy(source),
    }
    return value, source


def test_identity_guard_binds_frozen_input_mode_tool_and_exact_expected_source():
    value, source = identity_sample()
    before = copy.deepcopy(value)
    stable = RUNNER.require_identity(value, source)
    assert set(stable) == set(RUNNER.IDENTITY_KEYS)
    assert stable["original_policy"] == stable["measured_policy"]
    assert value == before


@pytest.mark.parametrize("path,replacement", (
    (("prepared_request_sha256",), "different-input"),
    (("original_request_fingerprint",), "different-request"),
    (("measured_request_fingerprint",), "different-measured-request"),
    (("measured_policy", "candidate_check_limit"), 199999),
    (("tool_sha256",), "different-tool"),
    (("profile_enabled",), True),
    (("requested_scope",), "first"),
    (("source", "production_source_files"), {"src/other.py": "a" * 64}),
    (("source", "production_source_sha256"), "different-source"),
))
def test_identity_guard_rejects_source_input_policy_or_measurement_mode_mismatch(path, replacement):
    value, source = identity_sample()
    parent_at(value, path)[path[-1]] = replacement
    with pytest.raises(ValueError, match="source, tool, input or measurement mode changed"):
        RUNNER.require_identity(value, source)


@pytest.mark.parametrize("field", ("platform", "python"))
def test_machine_identity_remains_in_the_cross_sample_comparison(field):
    value, source = identity_sample()
    changed = copy.deepcopy(value)
    changed[field] = "different-environment"
    assert RUNNER.require_identity(value, source) != RUNNER.require_identity(changed, source)


def synthetic_runner_inputs(tmp_path, monkeypatch):
    """Stop before any process starts; this fixture never runs a solver."""
    from tools import profile_solver_search, verify_solver_diagnostics

    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    prepared = tmp_path / "prepared.json"
    old_files = {"src/apsgo_scheduler/core/virtual_material.py": "old-file"}
    new_files = {"src/apsgo_scheduler/core/virtual_material.py": "new-file",
                 "src/apsgo_scheduler/core/_bridge_numeric.py": "added-file"}
    sources = {
        old_root: {"production_source_files": old_files,
                   "production_source_sha256": RUNNER.OLD_SOURCE_SHA256},
        new_root: {"production_source_files": new_files, "production_source_sha256": "new-source"},
    }
    hashes = {
        prepared: RUNNER.PREPARED_SHA256,
        old_root / "tools/profile_solver_search.py": RUNNER.TOOL_SHA256,
        new_root / "tools/profile_solver_search.py": RUNNER.TOOL_SHA256,
        RUNNER_PATH: "runner-source", RUNNER.REFERENCE_SCRIPT: RUNNER.REFERENCE_SHA256,
    }
    policy = SimpleNamespace(total_time_limit_seconds=Decimal(900), finalization_reserve_seconds=Decimal(10),
                             candidate_check_limit=200000, seed=590531)
    monkeypatch.setattr(verify_solver_diagnostics, "_sha256", lambda path: hashes[Path(path)])
    monkeypatch.setattr(verify_solver_diagnostics, "_code_identity", lambda root: copy.deepcopy(sources[Path(root)]))
    monkeypatch.setattr(profile_solver_search, "load_request", lambda _: SimpleNamespace(policy=policy))
    monkeypatch.setattr(RUNNER.sys, "path", [str(ROOT / "tools"), *RUNNER.sys.path])

    def forbidden(*args, **kwargs):
        raise AssertionError("guard tests must not launch a measurement process")

    monkeypatch.setattr(RUNNER.subprocess, "run", forbidden)
    output = tmp_path / "cohort"
    arguments = ["--old-root", str(old_root), "--new-root", str(new_root),
                 "--prepared-request", str(prepared), "--output", str(output)]
    return arguments, output, sources, hashes, policy


def test_existing_output_is_rejected_without_overwriting_or_starting_measurement(tmp_path, monkeypatch):
    arguments, output, _, _, _ = synthetic_runner_inputs(tmp_path, monkeypatch)
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_text("keep original evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        RUNNER.main(arguments)
    assert {item.name: item.read_text() for item in output.iterdir()} == {
        "existing.txt": "keep original evidence",
    }


@pytest.mark.parametrize("change", ("prepared", "old_source", "extra_source", "missing_source", "tool", "policy"))
def test_frozen_preflight_changes_fail_before_creating_output(tmp_path, monkeypatch, change):
    arguments, output, sources, hashes, policy = synthetic_runner_inputs(tmp_path, monkeypatch)
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    if change == "prepared":
        hashes[tmp_path / "prepared.json"] = "changed-input"
    elif change == "old_source":
        sources[old_root]["production_source_sha256"] = "changed-old-source"
    elif change == "extra_source":
        sources[new_root]["production_source_files"]["src/apsgo_scheduler/core/unrelated.py"] = "unrelated"
    elif change == "missing_source":
        del sources[new_root]["production_source_files"]["src/apsgo_scheduler/core/_bridge_numeric.py"]
    elif change == "tool":
        hashes[new_root / "tools/profile_solver_search.py"] = "changed-tool"
    else:
        policy.total_time_limit_seconds = Decimal(901)
    with pytest.raises(ValueError):
        RUNNER.main(arguments)
    assert not output.exists()


def test_recorded_dependency_versions_are_the_three_approved_exact_versions():
    expected = {"numpy": "2.2.6", "numba": "0.65.1", "llvmlite": "0.47.0"}
    assert RUNNER.DEPENDENCY_VERSIONS == expected
    assert RUNNER.dependency_versions() == expected


@pytest.mark.parametrize("package", ("numpy", "numba", "llvmlite"))
def test_changed_dependency_version_cannot_enter_the_cohort(monkeypatch, package):
    monkeypatch.setattr(RUNNER, "package_version", lambda name: (
        "different-version" if name == package else RUNNER.DEPENDENCY_VERSIONS[name]
    ))
    with pytest.raises(ValueError):
        RUNNER.dependency_versions()
