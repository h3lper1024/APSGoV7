"""Tiny real solves check width instrumentation, never the full GQGA4 benchmark."""

import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apsgo_scheduler.app import service
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
from apsgo_scheduler.core import controlled_split, width_optimization
from apsgo_scheduler.core.contracts import fingerprint
from tests.core.search.test_chain_order_integration import add_gap
from tests.core.search.test_controlled_order_split import split_case
from tests.core.search.test_width_optimization_baseline import width_case
from tests.integration.test_inter_chain_width_precheck import small_request

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / (
    "docs/implementation/evidence/apsgo_v7_inter_chain_width_optimization/"
    "step_06_real_run/run_width_observed_precheck.py"
)


@pytest.fixture
def observer():
    spec = importlib.util.spec_from_file_location("width_optimization_observer_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker(observer):
    return observer.load_checker(ROOT)


def metrics(context):
    return SimpleNamespace(
        candidate_check_count=context.factory.budget.candidate_check_count,
        complete_candidate_evaluation_count=context.complete_candidate_evaluation_count,
        accepted_move_count=len(context.accepted_move_traces),
    )


@pytest.fixture
def width_observed(observer, checker):
    state, context = width_case()
    with observer.observe_precheck(checker) as observed:
        assert checker.core_solver.run_width_optimization(state, context) is state
    return state, context, observed


def test_width_observation_preserves_real_state_trace_counts_and_all_budget_polls(
    observer, checker
):
    results = []
    original = checker.core_solver.run_width_optimization
    for observed_run in (False, True):
        state, context = width_case()
        clock_reads, cancellation_reads = [], []
        context.factory.budget.clock = lambda: clock_reads.append(None) or 1.0
        context.factory.budget.cancellation = SimpleNamespace(
            is_cancelled=lambda: cancellation_reads.append(None) or False
        )
        if observed_run:
            with observer.observe_precheck(checker) as observed:
                assert checker.core_solver.run_width_optimization(state, context) is state
        else:
            assert original(state, context) is state
        results.append(
            (
                fingerprint(state),
                context.accepted_move_traces,
                vars(metrics(context)),
                context.factory.budget.stop_reason,
                len(clock_reads),
                len(cancellation_reads),
            )
        )
    assert results[0] == results[1]
    assert state.current_evaluation.quality_key[4] < 700
    assert checker.core_solver.run_width_optimization is original
    totals = observer.reconcile_counts(observed, metrics(context))
    assert totals["stage_totals"] == vars(metrics(context))
    assert totals["width_action_totals"] == totals["width_batch_totals"] == totals["stage_totals"]
    (stage,) = observed["stages"]
    assert stage["stage"] == "width_optimization" and stage["nested"] is False
    assert stage["before"]["quality_key"][4] == 700
    assert stage["after"]["quality_key"] == state.current_evaluation.quality_key
    assert observed["batches"]
    assert {row["family"] for row in observed["batches"]} <= {
        "_node_recipes",
        "_block_recipes",
        "_cut_recipes",
        "_order_recipes",
    }
    assert set(observed["actions"]) == set(observer.ACTION_NAMES)
    assert any(row["accepted_move_count"] for row in observed["actions"].values())
    assert all(row["exception_count"] == 0 for row in observed["actions"].values())


def test_nonzero_split_replay_is_included_in_parent_but_not_counted_twice(observer, checker):
    state, context = add_gap(*split_case(), tail=True)
    with observer.observe_precheck(checker) as observed:
        checker.core_solver.run_controlled_order_split(state, context)
        checker.core_solver.run_width_optimization(state, context)
    parent, replay, width = observed["stages"]
    assert parent["stage"] == "controlled_split_and_replay" and not parent["nested"]
    assert replay["stage"] == "post_split_replay" and replay["nested"]
    assert width["stage"] == "width_optimization" and not width["nested"]
    assert state.split_sequence == 1
    assert replay["delta"]["candidate_check_count"] > 0
    totals = observer.reconcile_counts(observed, metrics(context))
    for name in observer.COUNT_NAMES:
        assert totals["stage_totals"][name] == parent["delta"][name] + width["delta"][name]
        assert parent["delta"][name] >= replay["delta"][name]
    assert sum(row["delta"]["candidate_check_count"] for row in observed["stages"]) > (
        context.factory.budget.candidate_check_count
    )
    replay["nested"] = False
    with pytest.raises(ValueError, match="root stage"):
        observer.reconcile_counts(observed, metrics(context))


@pytest.mark.parametrize(
    "damage",
    (
        "negative",
        "boolean",
        "stage_snapshot",
        "batch_snapshot",
        "action_total",
        "allowance",
        "duplicate_width",
    ),
)
def test_invalid_observation_counts_are_rejected(observer, width_observed, damage):
    _, context, original = width_observed
    observed = deepcopy(original)
    action = observed["actions"]["width_node_move"]
    batch = next(row for row in observed["batches"] if row["delta"]["candidate_check_count"])
    if damage == "negative":
        action["candidate_check_count"] = -1
    elif damage == "boolean":
        action["candidate_check_count"] = True
    elif damage == "stage_snapshot":
        observed["stages"][0]["after"]["counters"]["candidate_check_count"] += 1
    elif damage == "batch_snapshot":
        batch["after"]["candidate_check_count"] += 1
    elif damage == "action_total":
        action["candidate_check_count"] += 1
    elif damage == "allowance":
        batch["allowance"] = batch["delta"]["candidate_check_count"] - 1
    else:
        observed["stages"].append(deepcopy(observed["stages"][0]))
    with pytest.raises(ValueError):
        observer.reconcile_counts(observed, metrics(context))


def test_candidate_exception_is_observed_without_partial_state_and_patches_are_restored(
    observer, checker, monkeypatch
):
    state, context = width_case()
    before = fingerprint(state)
    bindings = (
        (checker.core, "solve"),
        (checker, "solve_request"),
        (checker.core_solver, "run_local_search"),
        (checker.core_solver, "run_controlled_order_split"),
        (checker.core_solver, "run_width_optimization"),
        (controlled_split, "run_local_search"),
        (width_optimization, "_try_width_recipe"),
        (width_optimization, "_scan_width_batch"),
    )
    originals = tuple(getattr(owner, name) for owner, name in bindings)

    def fail_candidate(*_):
        raise RuntimeError("synthetic candidate interruption")

    monkeypatch.setattr(width_optimization, "_try_segment_edit", fail_candidate)
    with pytest.raises(RuntimeError, match="synthetic candidate interruption"):
        with observer.observe_precheck(checker) as observed:
            checker.core_solver.run_width_optimization(state, context)
    assert fingerprint(state) == before
    assert tuple(getattr(owner, name) for owner, name in bindings) == originals
    assert vars(metrics(context)) == {
        "candidate_check_count": 1,
        "complete_candidate_evaluation_count": 0,
        "accepted_move_count": 0,
    }
    assert observed["actions"]["width_node_move"]["exception_count"] == 1
    assert observed["batches"][0]["outcome"] == "exception"
    assert "synthetic candidate interruption" in observed["batches"][0]["error"]
    assert observer.reconcile_counts(observed, metrics(context))["stage_totals"] == vars(
        metrics(context)
    )


@pytest.fixture
def tiny_public_artifacts(observer, checker, monkeypatch, tmp_path):
    request = small_request(monkeypatch)
    old = request.rule_set_spec.quality_spec
    spec = replace(request.rule_set_spec, quality_spec=(*old[:4], old[6], old[5], old[4]))
    request = replace(
        request,
        rule_set_spec=replace(spec, fingerprint=fingerprint_rule_set_spec(spec)),
    )
    monkeypatch.setattr(service, "monotonic", lambda: 100.0)
    plain = checker.solve_request(request)
    with observer.observe_precheck(checker) as observed:
        result = checker.solve_request(request)
    assert result.release == plain.release
    assert result.run_manifest.counters == plain.run_manifest.counters
    assert result.run_manifest.trace_fingerprint == plain.run_manifest.trace_fingerprint
    assert result.core_audit.passed and result.audit_report.passed
    ((problem, active, policy, core),) = observed["core_calls"]
    assert observed["public_results"] == [result]
    output = tmp_path / "observed"
    output.mkdir()
    (output / "public_result.canonical.json").write_text(
        checker.canonical_json(result) + "\n", encoding="utf-8"
    )
    (output / "accepted_trace.canonical.json").write_text(
        checker.canonical_json(core.trace) + "\n", encoding="utf-8"
    )
    gate = json.loads((checker.BASE / "quality_gate.json").read_text(encoding="utf-8"))
    report = checker.evaluate_quality_gate(request, result, gate)
    # The frozen 531-order quality gate must remain failed for this tiny synthetic input.
    assert report["passed"] is False
    checker.save_details(output, result, report)
    report.update(
        code_revision="synthetic-width-observation",
        trace_fingerprint=checker.fingerprint(core.trace),
        public_result_fingerprint=result.result_fingerprint,
        core_result_fingerprint=core.core_result_fingerprint,
        identities={
            "request_fingerprint": result.run_manifest.request_fingerprint,
            "problem_fingerprint": problem.input_fingerprint,
            "rule_set_fingerprint": active.fingerprint,
            "policy_fingerprint": checker.fingerprint(policy),
        },
        artifacts_sha256={
            name: checker.sha256(output / name) for name in observer.ORIGINAL_ARTIFACTS
        },
    )
    (output / "quality_report.json").write_text(
        json.dumps(report, default=str) + "\n", encoding="utf-8"
    )
    return observed, output, report


def test_supplement_preserves_six_original_files_and_binds_dynamic_width_position(
    observer, checker, tiny_public_artifacts
):
    observed, output, report = tiny_public_artifacts
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert len(before) == 6
    observation = observer.write_supplement(checker, observed, output, report["code_revision"])
    assert all((output / name).read_bytes() == content for name, content in before.items())
    assert observation["quality_gate_passed"] is False
    assert observation["width_quality_position"] == 5
    assert (
        observation["final_width_gap"]
        == observed["public_results"][0].release.evaluation.quality_key[4]
    )
    manifest = json.loads((output / "width_observation_manifest.json").read_text(encoding="utf-8"))
    assert manifest["quality_report_sha256"] == checker.sha256(output / "quality_report.json")
    assert manifest["runner_sha256"] == checker.sha256(SCRIPT)
    assert manifest["dependency_sha256"] == observer.dependency_hashes(checker)
    assert manifest["artifacts_sha256"] == {
        "width_observation.json": checker.sha256(output / "width_observation.json")
    }
    with pytest.raises(ValueError, match="already exists"):
        observer.write_supplement(checker, observed, output, report["code_revision"])


@pytest.mark.parametrize(
    "damage",
    (
        "artifact",
        "revision",
        "trace",
        "artifact_list",
        "multiple_core",
        "no_public",
        "counts",
        "public_result",
        "core_result",
        "input_identity",
    ),
)
def test_damaged_original_binding_or_counters_prevents_supplement_writes(
    observer, checker, tiny_public_artifacts, damage
):
    observed, output, report = tiny_public_artifacts
    if damage == "artifact":
        (output / "chain_detail.csv").write_text("damaged\n", encoding="utf-8")
    elif damage == "revision":
        report["code_revision"] = "different-revision"
    elif damage == "trace":
        report["trace_fingerprint"] = "different-trace"
    elif damage == "artifact_list":
        report["artifacts_sha256"].pop("chain_detail.csv")
    elif damage == "multiple_core":
        observed["core_calls"].append(observed["core_calls"][0])
    elif damage == "no_public":
        observed["public_results"].clear()
    elif damage == "public_result":
        report["public_result_fingerprint"] = "different-public-result"
    elif damage == "core_result":
        report["core_result_fingerprint"] = "different-core-result"
    elif damage == "input_identity":
        report["identities"]["problem_fingerprint"] = "different-problem"
    else:
        observed["stages"][0]["delta"]["candidate_check_count"] += 1
    (output / "quality_report.json").write_text(
        json.dumps(report, default=str) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        observer.write_supplement(checker, observed, output, "synthetic-width-observation")
    assert not (output / "width_observation.json").exists()
    assert not (output / "width_observation_manifest.json").exists()
