"""Small real releases verify the acceptance gate without running the full benchmark."""

import csv
import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from apsgo_scheduler.api.request import RuleDefinitionSpec, fingerprint_public_request
from apsgo_scheduler.app import service
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core.contracts import (
    RuleScope,
    SearchStopReason,
    SolveStatus,
    fingerprint,
    sum_weights,
)
from tests.app.test_input_normalizer import (
    gqga4_request,
    gqga4_spec,
    make_order,
    make_request,
    make_spec,
)
from tests.app.test_result_contract_audit import rich_case

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT
    / "docs/implementation/evidence/solverpy_path_cover_local_search/function_21_complete_acceptance/quality_precheck.py"
)
D = Decimal


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("quality_precheck_test_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_formal_request_identities_match_precheck_without_solving(checker):
    request = gqga4_request.__wrapped__(gqga4_spec.__wrapped__())
    rules = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, rules)
    assert request.policy.candidate_check_limit == 200000
    assert checker.EXPECTED_IDENTITIES == {
        "request_fingerprint": fingerprint_public_request(request),
        "problem_fingerprint": problem.input_fingerprint,
        "rule_set_fingerprint": rules.fingerprint,
        "policy_fingerprint": fingerprint(request.policy),
    }


def public_case(monkeypatch, *, underweight=False, split=False):
    definitions = (
        RuleDefinitionSpec(
            "weight",
            "ChainWeightRangeRule",
            "链重",
            RuleScope.CHAIN,
            True,
            "1",
            {
                "min_weight": D(100) if underweight else D(1),
                "max_weight": D(1000),
                "target_weight": D(1000),
            },
        ),
        RuleDefinitionSpec(
            "late", "LateOriginalPeriodMoveRule", "延后", RuleScope.PLAN, True, "1", {}
        ),
        RuleDefinitionSpec(
            "virtual",
            "VirtualOutputRatioRule",
            "虚拟比例",
            RuleScope.PLAN,
            True,
            "1",
            {"max_ratio": D("0.5")},
        ),
    )
    if split:
        _, request, *_ = rich_case()
        specification = make_spec(
            rules=(*request.rule_set_spec.rules, *definitions),
            quality_spec=request.rule_set_spec.quality_spec,
        )
        request = replace(request, rule_set_spec=specification)
    else:
        specification = make_spec(
            rules=definitions, allowed_final_deviation_codes={"chain_weight_below_minimum"}
        )
        request = make_request(rule_set_spec=specification)
    monkeypatch.setattr(service, "monotonic", lambda: 100.0)
    result = service.solve_request(request)
    assert result.release is not None, result.issues
    gate = json.loads(
        (ROOT / "tests/baselines/gqga4/quality_gate.json").read_text(encoding="utf-8")
    )
    # These are synthetic fixtures; the frozen benchmark file is never changed.
    gate.update(
        expected_real_order_count=len(request.orders),
        expected_real_weight=str(sum_weights(item.weight for item in request.orders)),
    )
    if split:
        gate["maximum_virtual_output_weight_ratio"] = "0.5"
    return request, result, gate


def with_corrupted_release(result, **changes):
    """Model a damaged observation while preserving the ordinary public constructors."""
    copied = replace(result)
    object.__setattr__(copied, "release", replace(result.release, **changes))
    return copied


def test_real_small_public_success_passes_all_required_gate_checks(checker, monkeypatch):
    request, result, gate = public_case(monkeypatch)
    assert result.status is SolveStatus.SUCCESS
    report = checker.evaluate_quality_gate(request, result, gate)
    assert report["passed"], report
    assert report["failures"] == []
    assert all(item["passed"] for item in report["checks"].values())


def test_explicit_no_chain_ceiling_allows_a_real_23_chain_release(checker, monkeypatch):
    request, _, gate = public_case(monkeypatch)
    request = replace(
        request,
        orders=tuple(make_order(index, weight=D(800), source_period="P0") for index in range(23)),
        virtual_prototypes=(),
    )
    result = service.solve_request(request)
    assert result.release is not None and len(result.release.plan.chains) == 23
    assert result.core_audit.passed and result.audit_report.passed
    gate.update(expected_real_order_count=23, expected_real_weight=str(23 * 800))
    assert gate["maximum_chain_count"] is None
    report = checker.evaluate_quality_gate(request, result, gate)
    assert report["passed"], report
    assert report["checks"]["maximum_chain_count"] == {
        "passed": True,
        "actual": 23,
        "expected": None,
    }
    gate["maximum_chain_count"] = 22
    capped = checker.evaluate_quality_gate(request, result, gate)
    assert capped["failures"] == ["maximum_chain_count"]


@pytest.mark.parametrize(
    "name",
    (
        "maximum_chain_count",
        "maximum_underweight_chain_count",
        "maximum_prohibited_violation_count",
        "maximum_virtual_output_weight_ratio",
        "maximum_late_original_due_period_move_count",
    ),
)
@pytest.mark.parametrize("missing", (False, True))
def test_only_explicit_null_chain_limit_is_optional(checker, monkeypatch, name, missing):
    request, result, gate = public_case(monkeypatch)
    if missing:
        del gate[name]
    else:
        gate[name] = None
    report = checker.evaluate_quality_gate(request, result, gate)
    allowed = name == "maximum_chain_count" and not missing
    assert report["passed"] is allowed
    assert report["checks"][name]["passed"] is allowed
    assert report["checks"]["gate_schema"]["passed"] is (not missing)


@pytest.mark.parametrize("actual", (None, True, "1", D(-1), D("NaN"), D("Infinity")))
def test_no_chain_ceiling_still_rejects_invalid_chain_metric(checker, monkeypatch, actual):
    request, result, gate = public_case(monkeypatch)
    evaluation = replace(result.release.evaluation)
    # A damaged observation must not inherit a passing result from a disabled ceiling.
    object.__setattr__(evaluation, "metrics", dict(evaluation.metrics) | {"chain_count": actual})
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, evaluation=evaluation), gate
    )
    assert not report["passed"]
    assert not report["checks"]["maximum_chain_count"]["passed"]


@pytest.mark.parametrize("missing", (False, True))
def test_no_chain_ceiling_preserves_actual_chain_count_consistency(checker, monkeypatch, missing):
    request, result, gate = public_case(monkeypatch)
    metrics = dict(result.release.evaluation.metrics)
    if missing:
        del metrics["chain_count"]
    else:
        metrics["chain_count"] += 1
    evaluation = replace(result.release.evaluation, metrics=metrics)
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, evaluation=evaluation), gate
    )
    assert not report["passed"]
    assert not report["checks"]["metric_record_consistency"]["passed"]


def test_generic_allowed_underweight_release_still_fails_fixed_zero_underweight_gate(
    checker, monkeypatch
):
    request, result, gate = public_case(monkeypatch, underweight=True)
    assert result.status is SolveStatus.PUBLISHABLE_WITH_ALLOWED_DEVIATION
    assert result.confirmation_required
    report = checker.evaluate_quality_gate(request, result, gate)
    assert not report["passed"]
    assert report["underweight_chains"]


@pytest.mark.parametrize(
    "metric",
    (
        "underweight_chain_count",
        "prohibited_violation_count",
        "late_original_due_period_move_count",
        "virtual_output_weight_ratio",
    ),
)
def test_missing_required_metric_never_defaults_to_a_passing_zero(checker, monkeypatch, metric):
    request, result, gate = public_case(monkeypatch)
    evaluation = result.release.evaluation
    assert metric in evaluation.metrics
    changed = replace(
        evaluation,
        metrics={key: value for key, value in evaluation.metrics.items() if key != metric},
    )
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, evaluation=changed), gate
    )
    assert not report["passed"]
    assert report["failures"]


@pytest.mark.parametrize("metric", ("underweight_chain_count", "underweight_total_gap"))
def test_missing_per_chain_underweight_metric_returns_a_failed_report(checker, monkeypatch, metric):
    request, result, gate = public_case(monkeypatch, underweight=True)
    evaluation = result.release.evaluation
    first, *others = evaluation.chain_evaluations
    assert first.metrics["underweight_chain_count"] > 0
    changed = replace(
        evaluation,
        chain_evaluations=(
            replace(
                first, metrics={key: value for key, value in first.metrics.items() if key != metric}
            ),
            *others,
        ),
    )
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, evaluation=changed), gate
    )
    assert not report["passed"]
    assert not report["checks"]["underweight_detail_consistency"]["passed"]


def test_missing_release_cannot_borrow_diagnostic_candidate_as_acceptance(checker, monkeypatch):
    request, result, gate = public_case(monkeypatch)
    failed = replace(
        result,
        status=SolveStatus.FAILED,
        stop_reason=SearchStopReason.SYSTEM_ERROR,
        release=None,
        run_manifest=replace(
            result.run_manifest,
            stop_reason=SearchStopReason.SYSTEM_ERROR,
            search_was_truncated=False,
        ),
    )
    assert failed.diagnostic_candidate is not None
    report = checker.evaluate_quality_gate(request, failed, gate)
    assert not report["passed"] and report["failures"]


def test_real_split_release_is_counted_by_supported_parents_not_extra_piece_nodes(
    checker, monkeypatch
):
    request, result, gate = public_case(monkeypatch, split=True)
    facts = result.release.resource_facts
    assert facts.split_partitions
    assert len([item for item in facts.assignments if item.source_order_id is not None]) > len(
        request.orders
    )
    report = checker.evaluate_quality_gate(request, result, gate)
    assert report["passed"], report
    assert len(report["source_conservation"]) == len(request.orders)


def test_source_allocation_mismatch_is_not_hidden_by_unchanged_global_total(checker, monkeypatch):
    request, result, gate = public_case(monkeypatch)
    facts = result.release.resource_facts
    first, second = facts.assignments
    changed = replace(
        facts,
        assignments=(
            replace(first, weight=first.weight + D(1)),
            replace(second, weight=second.weight - D(1)),
        ),
    )
    chain = result.release.plan.chains[0]
    left, right = chain.nodes
    plan = replace(
        result.release.plan,
        chains=(
            replace(
                chain,
                nodes=(
                    replace(left, weight=left.weight + D(1)),
                    replace(right, weight=right.weight - D(1)),
                ),
            ),
        ),
    )
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, plan=plan, resource_facts=changed), gate
    )
    assert not report["passed"]
    assert not report["checks"]["source_order_weight_conservation"]["passed"]


def test_split_fact_authorization_metadata_cannot_drift_from_piece_lineage(checker, monkeypatch):
    request, result, gate = public_case(monkeypatch, split=True)
    facts = result.release.resource_facts
    changed = replace(
        facts,
        split_partitions=(
            replace(facts.split_partitions[0], authorization_decision_fingerprint="changed"),
            *facts.split_partitions[1:],
        ),
    )
    report = checker.evaluate_quality_gate(
        request, with_corrupted_release(result, resource_facts=changed), gate
    )
    assert not report["passed"]
    assert not report["checks"]["controlled_split_authorization_and_traceability"]["passed"]


def test_unknown_required_check_cannot_be_silently_ignored(checker, monkeypatch):
    request, result, gate = public_case(monkeypatch)
    gate["required_checks"].append("unknown_required_invariant")
    report = checker.evaluate_quality_gate(request, result, gate)
    assert not report["passed"]
    assert report["failures"]


@pytest.mark.parametrize(
    "name,value",
    (
        ("expected_real_order_count", 1),
        ("expected_real_weight", "19.99"),
        ("maximum_chain_count", 0),
    ),
)
def test_each_explicit_scalar_gate_is_enforced(checker, monkeypatch, name, value):
    request, result, gate = public_case(monkeypatch)
    gate[name] = value
    assert not checker.evaluate_quality_gate(request, result, gate)["passed"]


def test_output_directory_must_be_new_and_never_overwrite_existing_evidence(checker, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "evidence.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        checker.create_output_directory(existing)
    assert marker.read_text(encoding="utf-8") == "keep"
    new = tmp_path / "new"
    assert checker.create_output_directory(new) == new
    assert new.is_dir()


def test_csv_uses_lf_for_stable_git_artifact_hashes_and_roundtrips_text(checker, tmp_path):
    output = tmp_path / "detail.csv"
    rows = [{"node_id": "订单-001", "value": 'a,b "quoted"'}]
    checker.write_csv(output, ("node_id", "value"), rows)
    raw = output.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert not raw.startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8", newline="") as stream:
        assert list(csv.DictReader(stream)) == rows


@pytest.mark.parametrize("existing_target", (False, True))
def test_output_directory_rejects_existing_and_dangling_symlinks(
    checker, tmp_path, existing_target
):
    target = tmp_path / "target"
    if existing_target:
        target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises((ValueError, FileExistsError)):
        checker.create_output_directory(linked)
    assert linked.is_symlink()
    assert target.exists() is existing_target
