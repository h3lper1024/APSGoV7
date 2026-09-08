"""Synthetic measurements protect the scoped full-pair comparison boundaries."""

import copy
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / (
    "docs/implementation/evidence/apsgo_v7_unchanged_chain_evaluation_reuse/"
    "stage_03_comparison/run_pairs.py"
)
SPEC = importlib.util.spec_from_file_location("chain_reuse_pair_runner_test", RUNNER_PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def measurement():
    def snapshot(checks, evaluations, accepts, stop):
        return {
            "candidate_check_count": checks,
            "complete_candidate_evaluation_count": evaluations,
            "accepted_move_count": accepts,
            "stop_reason": stop,
            "plan": {"chains": [{"chain_id": "a"}, {"chain_id": "b"}]},
            "evaluation": {"violations": [{"subject_id": "a"}, {"subject_id": "b"}]},
            "quality": [0, 0, 0, 0, 11726, 660, 23],
            "trace": [{"sequence": 1}, {"sequence": 2}],
            "edge_cache": {"hits": 10, "misses": 3, "entries": 3},
        }

    return {
        "measurement_scope": "full_public_solve_with_observation",
        "public_call_wall_seconds": Decimal("131.5"),
        "public_call_cpu_seconds": Decimal("131.4"),
        "first_search_timing": {
            "wall_seconds": Decimal("65.9"),
            "cpu_seconds": Decimal("65.8"),
            "functions": [
                {
                    "function": "first",
                    "wall_seconds": Decimal(44),
                    "cpu_seconds": Decimal(43),
                    "candidate_check_count": 100,
                },
                {
                    "function": "second",
                    "wall_seconds": Decimal(21),
                    "cpu_seconds": Decimal(20),
                    "candidate_check_count": 200,
                },
            ],
        },
        "first_search": {
            "initial": snapshot(100, 0, 0, None),
            "final": snapshot(64226, 2263, 34, "local_search_complete"),
        },
        "final_search": snapshot(200000, 3697, 54, "candidate_limit_reached"),
        "result": {
            "stop_reason": "candidate_limit_reached",
            "release": {
                "resource_facts": {"real_weight": Decimal("29333.91")},
                "future_field": {"wall_seconds": Decimal("7.25")},
            },
            "core_audit": {"passed": True},
            "audit_report": {"passed": True},
            "run_manifest": {
                "code_revision": "unversioned",
                "counters": {"candidate_check_count": 200000},
                "search_was_truncated": True,
                "optimality_proven": False,
                "trace_fingerprint": "trace-original",
                "stage_duration_seconds": {"local_search": Decimal(66), "audit": Decimal(2)},
            },
        },
    }


def parent_at(value, path):
    for key in path[:-1]:
        value = value[key]
    return value


TIMING_PATHS = (
    ("public_call_wall_seconds",),
    ("public_call_cpu_seconds",),
    ("first_search_timing", "wall_seconds"),
    ("first_search_timing", "cpu_seconds"),
    ("first_search_timing", "functions", 0, "wall_seconds"),
    ("first_search_timing", "functions", 0, "cpu_seconds"),
    ("result", "run_manifest", "stage_duration_seconds", "local_search"),
    ("result", "run_manifest", "stage_duration_seconds", "audit"),
)


def test_stable_measurement_ignores_explicit_timing_values_without_mutating_input():
    original = measurement()
    before = copy.deepcopy(original)
    changed = copy.deepcopy(original)
    for path in TIMING_PATHS:
        parent_at(changed, path)[path[-1]] = Decimal("999.123")
    stable = RUNNER.stable_measurement(original)
    assert stable == RUNNER.stable_measurement(changed)
    assert original == before
    assert changed["public_call_wall_seconds"] == Decimal("999.123")
    assert stable["result"]["run_manifest"]["stage_duration_seconds"] == {
        "local_search": None,
        "audit": None,
    }
    stable["final_search"]["trace"][0]["sequence"] = 99
    assert original == before


@pytest.mark.parametrize("path", TIMING_PATHS)
def test_stable_measurement_retains_missing_timing_locations(path):
    original = measurement()
    changed = copy.deepcopy(original)
    del parent_at(changed, path)[path[-1]]
    assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("first_search", "initial", "plan", "chains"), [{"chain_id": "b"}, {"chain_id": "a"}]),
        (
            ("first_search", "final", "evaluation", "violations"),
            [{"subject_id": "b"}, {"subject_id": "a"}],
        ),
        (("final_search", "trace"), [{"sequence": 2}, {"sequence": 1}]),
        (("final_search", "quality"), [0, 0, 0, 0, 11727, 660, 23]),
        (("final_search", "edge_cache", "hits"), 11),
        (("first_search_timing", "functions", 0, "candidate_check_count"), 101),
        (("first_search_timing", "functions", 0, "function"), "changed"),
        (("result", "release", "resource_facts", "real_weight"), Decimal("29333.90")),
        (("result", "release", "future_field", "wall_seconds"), Decimal("7.26")),
        (("result", "run_manifest", "code_revision"), "different-code"),
        (("result", "run_manifest", "counters", "candidate_check_count"), 199999),
        (("result", "run_manifest", "search_was_truncated"), False),
        (("result", "run_manifest", "optimality_proven"), True),
        (("result", "run_manifest", "trace_fingerprint"), "trace-changed"),
    ],
)
def test_stable_measurement_preserves_ordered_business_fields_and_manifest(path, replacement):
    original = measurement()
    changed = copy.deepcopy(original)
    parent_at(changed, path)[path[-1]] = replacement
    assert RUNNER.stable_measurement(original) != RUNNER.stable_measurement(changed)


def test_fixed_work_accepts_full_frozen_counts_and_both_audits():
    original = measurement()
    before = copy.deepcopy(original)
    assert RUNNER.require_fixed_work(original) is None
    assert original == before


@pytest.mark.parametrize(
    ("path", "replacement", "message"),
    [
        (("first_search", "final", "candidate_check_count"), 64225, "frozen work prefix"),
        (
            ("first_search", "final", "complete_candidate_evaluation_count"),
            2262,
            "frozen work prefix",
        ),
        (("first_search", "final", "accepted_move_count"), 33, "frozen work prefix"),
        (("final_search", "candidate_check_count"), 199999, "frozen work prefix"),
        (("final_search", "complete_candidate_evaluation_count"), 3696, "frozen work prefix"),
        (("final_search", "accepted_move_count"), 53, "frozen work prefix"),
        (
            ("first_search", "final", "stop_reason"),
            "search_time_limit_reached",
            "frozen work prefix",
        ),
        (("final_search", "stop_reason"), "search_time_limit_reached", "frozen work prefix"),
        (("result", "stop_reason"), "finalization_time_limit_reached", "both audits passing"),
        (("result", "core_audit", "passed"), False, "both audits passing"),
        (("result", "audit_report", "passed"), False, "both audits passing"),
        (("result", "release"), None, "both audits passing"),
        (
            ("measurement_scope",),
            "first_local_search_only_no_release",
            "full observed public solve",
        ),
    ],
)
def test_fixed_work_rejects_wrong_counts_stops_audits_release_and_scope(path, replacement, message):
    changed = measurement()
    parent_at(changed, path)[path[-1]] = replacement
    with pytest.raises(ValueError, match=message):
        RUNNER.require_fixed_work(changed)


def test_probe_keeps_actual_first_search_and_restores_observers(tmp_path, monkeypatch):
    from apsgo_scheduler.core import neighborhoods
    from tests.app.test_solver_profile_tool import prepared
    from tools.profile_solver_search import measure
    from tools.verify_solver_diagnostics import _read_json, _sha256

    path = RUNNER_PATH.with_name("probe_reuse.py")
    spec = importlib.util.spec_from_file_location("chain_reuse_probe_test", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    prepared_path, request = prepared(tmp_path)
    monkeypatch.setattr(probe, "PREPARED_SHA256", _sha256(prepared_path))
    original = neighborhoods._evaluate_candidate_plan
    expected = measure(request)
    output = tmp_path / "probe"
    arguments = [
        "--code-root",
        str(ROOT),
        "--prepared-request",
        str(prepared_path),
        "--output-dir",
        str(output),
    ]
    assert probe.main(arguments) == 0
    assert neighborhoods._evaluate_candidate_plan is original
    actual = _read_json(output / "measurement.json")
    assert actual["first_search"] == expected["first_search"]
    summary = _read_json(output / "probe_summary.json")
    assert summary["status"] == "completed"
    counts = summary["counts"]
    assert counts["candidate_attempts"] == counts["candidate_completed"]
    assert counts["possible_chain_evaluations"] == (
        counts["new_chain_entries"] + counts["reused_chain_entries"]
    )
    assert (output / "first_search.pstats").is_file()
    before = {item.name: _sha256(item) for item in output.iterdir()}
    with pytest.raises(FileExistsError):
        probe.main(arguments)
    assert {item.name: _sha256(item) for item in output.iterdir()} == before
