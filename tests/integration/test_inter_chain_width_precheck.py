"""Small real pipelines verify additional observations without a GQGA4 search."""

import csv
import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from apsgo_scheduler.app import service
from apsgo_scheduler.app.input_normalizer import normalize_input
from apsgo_scheduler.app.rule_set_loader import load_rule_set
from apsgo_scheduler.core import initial_solution, neighborhoods
from apsgo_scheduler.core.contracts import fingerprint, sum_decimals
from apsgo_scheduler.core.model import MaterialRole, SchedulePlan
from tests.app import test_input_normalizer as fixtures
from tests.app.test_chain_order_release import released_case
from tests.core.rules.test_inter_chain_width_gap import endpoint
from tests.core.search.test_chain_order import chain, search_case
from tests.core.search.test_chain_order_integration import add_gap
from tests.core.search.test_controlled_order_split import split_case

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / (
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_22_inter_chain_width_gap/step_22_4_real_comparison/run_observed_precheck.py"
)
D = Decimal


@pytest.fixture
def observer():
    spec = importlib.util.spec_from_file_location("width_precheck_observer_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker(observer):
    return observer.load_checker(ROOT)


def small_request(monkeypatch, *, native=True):
    _, args, _ = released_case(monkeypatch, cross_period=True)
    request = args[1]
    if not native:
        request = replace(
            request,
            rule_set_spec=fixtures.make_spec(
                rules=request.rule_set_spec.rules[:-1],
                quality_spec=request.rule_set_spec.quality_spec[:-1],
            ),
        )
    return replace(request, policy=replace(request.policy, candidate_check_limit=200))


def test_loader_selects_this_root_and_rejects_mixed_revision_imports(observer, tmp_path):
    assert observer.load_checker(ROOT).ROOT == ROOT
    with pytest.raises(ValueError, match="separate Python process"):
        observer.load_checker(tmp_path)


def test_real_order_stage_keeps_state_trace_counts_and_budget_clock_calls(observer, checker):
    states, contexts, clock_calls = [], [], []
    original = neighborhoods.improve_chain_order
    for wrapped in (False, True):
        state, context = search_case()
        calls = []

        def clock():
            calls.append(None)
            return 1.0

        context.factory.budget.clock = clock
        if wrapped:
            with observer.observe_precheck(checker) as observed:
                returned = neighborhoods.improve_chain_order(state, context)
        else:
            returned = original(state, context)
        assert returned is state
        states.append(state)
        contexts.append(context)
        clock_calls.append(len(calls))
    assert fingerprint(states[0]) == fingerprint(states[1])
    assert contexts[0].accepted_move_traces == contexts[1].accepted_move_traces
    assert clock_calls[0] == clock_calls[1]
    (row,) = observed["stages"]
    assert (
        row["inclusive_delta"]
        == row["exclusive_delta"]
        == {
            "candidate_check_count": 9,
            "complete_candidate_evaluation_count": 9,
            "accepted_move_count": 2,
        }
    )
    assert row["quality_after"][-1] == 500
    assert neighborhoods.improve_chain_order is original
    _, before, after = observed["order_plans"][0]
    assert observer.plan_comparison(checker, before, after)["same_chain_contents"]


@pytest.mark.parametrize("native", (False, True))
def test_real_public_pipeline_returns_original_release_and_core_trace(
    observer, checker, monkeypatch, native
):
    request = small_request(monkeypatch, native=native)
    monkeypatch.setattr(service, "monotonic", lambda: 100.0)
    baseline = checker.solve_request(request)
    with observer.observe_precheck(checker) as observed:
        result = checker.solve_request(request)
    assert result.release == baseline.release
    assert result.run_manifest.counters == baseline.run_manifest.counters
    assert result.run_manifest.trace_fingerprint == baseline.run_manifest.trace_fingerprint
    assert observed["public_results"] == [result]
    problem, active, _, core = observed["core_calls"][0]
    assert core.release.canonical_plan is result.release.plan
    assert fingerprint(core.trace) == result.run_manifest.trace_fingerprint
    assert (
        sum(row["exclusive_delta"]["candidate_check_count"] for row in observed["stages"])
        == result.run_manifest.counters["candidate_check_count"]
    )
    rows, diagnostic = observer.boundary_details(checker, result, problem, active)
    assert len(rows) == len(result.release.plan.chains) - 1 == 2
    assert any(row["cross_period"] for row in rows)
    assert diagnostic["kind"] == (
        "native_seven_level" if native else "historical_stable_grouped_derived"
    )
    assert diagnostic["agrees_with_native_metric"] is (True if native else None)
    if native:
        assert D(diagnostic["absolute_gap_sum"]) == result.release.evaluation.quality_key[6]


def test_split_parent_exclusive_counts_do_not_double_count_one_real_replay(observer, checker):
    state, context = add_gap(*split_case())
    with observer.observe_precheck(checker) as observed:
        assert checker.core_solver.run_controlled_order_split(state, context) is state
    parent, *children = observed["stages"]
    assert state.split_sequence == 1
    assert parent["stage"] == "controlled_split_and_replay"
    assert len(children) == 4
    assert {row["round"] for row in children} == {"post_split_replay"}
    for key, total in observer.counters(context).items():
        assert sum(row["exclusive_delta"][key] for row in observed["stages"]) == total
        assert parent["inclusive_delta"][key] == total
    assert parent["exclusive_delta"]["accepted_move_count"] == 1
    assert parent["exclusive_elapsed_seconds"] >= 0


def test_first_initial_group_change_and_pair_prefix_are_bounded_read_only(observer, checker):
    plan = SchedulePlan((chain("late", period="P2"), chain("early", period="P9")))
    state, context = search_case()
    donor, target, _ = state.current_plan.chains
    original = neighborhoods._candidate_merged_sequences
    expected = list(original(donor, target, context, 0))
    checks_per_call = context.factory.budget.candidate_check_count
    before = fingerprint((state, context.accepted_move_traces))
    with observer.observe_precheck(checker) as observed:
        grouped = initial_solution.stable_group_plan(plan, {"P9": 0, "P2": 1})
        assert initial_solution.stable_group_plan(grouped, {"P9": 0, "P2": 1}) is grouped
        for _ in range(observer.PREFIX_LIMIT + 3):
            assert (
                list(neighborhoods._candidate_merged_sequences(donor, target, context, 0))
                == expected
            )
    assert observed["first_group_change"] == (plan, grouped)
    assert len(observed["enumeration_prefix"]) == observer.PREFIX_LIMIT
    assert observed["enumeration_prefix"][0]["donor_chain_id"] == donor.chain_id
    assert fingerprint((state, context.accepted_move_traces)) == before
    assert context.factory.budget.candidate_check_count == checks_per_call * (
        observer.PREFIX_LIMIT + 4
    )


def test_old_derivation_preserves_original_and_keeps_actual_virtual_cross_period_endpoint(
    observer, checker
):
    virtual = endpoint("virtual", "1300", "virtual")
    late = chain("late", 1500, "P2")
    early = replace(chain("early", 1000, "P9"), nodes=(chain("real", 1000, "P9").nodes[0], virtual))
    plan = SchedulePlan((late, early))
    result = SimpleNamespace(release=SimpleNamespace(plan=plan))
    before = fingerprint(plan)
    rows, diagnostic = observer.boundary_details(
        checker,
        result,
        SimpleNamespace(period_order=("P9", "empty", "P2")),
        SimpleNamespace(rules=()),
    )
    assert fingerprint(plan) == before == diagnostic["original_plan_fingerprint"]
    assert diagnostic["derived_plan_fingerprint"] != before
    assert diagnostic["raw_metric"] is diagnostic["quality_value"] is None
    assert diagnostic["absolute_gap_sum"] == "200"
    assert rows[0]["left_node_id"] == virtual.node_id
    assert rows[0]["left_role"] == MaterialRole.GENERATED_VIRTUAL.value and rows[0]["cross_period"]


@pytest.mark.parametrize("field", ("raw_metric", "quality"))
def test_native_boundary_metric_or_quality_mismatch_is_rejected(
    observer, checker, monkeypatch, field
):
    result, args, _ = released_case(monkeypatch)
    evaluation = result.release.evaluation
    changed = replace(
        evaluation,
        **(
            {"metrics": dict(evaluation.metrics) | {observer.METRIC: D(1)}}
            if field == "raw_metric"
            else {"quality_key": (*evaluation.quality_key[:6], D(1))}
        ),
    )
    damaged = SimpleNamespace(release=replace(result.release, evaluation=changed))
    with pytest.raises(ValueError, match="disagree"):
        observer.boundary_details(checker, damaged, args[2], load_rule_set(args[1].rule_set_spec))


def test_observation_context_restores_all_patches_after_error(observer, checker):
    original = (checker.core.solve, checker.solve_request, neighborhoods.improve_chain_order)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with observer.observe_precheck(checker):
            raise RuntimeError("synthetic interruption")
    assert original == (
        checker.core.solve,
        checker.solve_request,
        neighborhoods.improve_chain_order,
    )


def test_supplement_runs_real_precheck_keeps_six_files_and_binds_new_artifacts(
    observer, checker, monkeypatch, tmp_path
):
    request = small_request(monkeypatch)
    active = load_rule_set(request.rule_set_spec)
    problem = normalize_input(request, active)
    monkeypatch.setattr(service, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        fixtures, "gqga4_spec", SimpleNamespace(__wrapped__=lambda: request.rule_set_spec)
    )
    monkeypatch.setattr(fixtures, "gqga4_request", SimpleNamespace(__wrapped__=lambda _: request))
    monkeypatch.setattr(checker, "verify_code_revision", lambda *_: {})
    monkeypatch.setattr(
        checker,
        "EXPECTED_IDENTITIES",
        {
            "request_fingerprint": checker.fingerprint_public_request(request),
            "problem_fingerprint": problem.input_fingerprint,
            "rule_set_fingerprint": active.fingerprint,
            "policy_fingerprint": fingerprint(request.policy),
        },
    )
    monkeypatch.setattr(observer, "load_checker", lambda _: checker)
    original_main = checker.main
    original_files = {}
    output = tmp_path / "observed"

    def precheck(argv):
        status = original_main(argv)
        original_files.update({path.name: path.read_bytes() for path in output.iterdir()})
        return status

    monkeypatch.setattr(checker, "main", precheck)
    # The unchanged formal 531-order gate must fail for this three-order test, not be weakened.
    assert (
        observer.main(
            [
                "--code-root",
                str(ROOT),
                "--code-repository",
                str(ROOT),
                "--code-revision",
                "synthetic",
                "--output-dir",
                str(output),
            ]
        )
        == 2
    )
    assert len(original_files) == 6
    assert all(
        (output / name).read_bytes() == contents for name, contents in original_files.items()
    )
    manifest = json.loads((output / "observation_manifest.json").read_text())
    assert manifest["quality_report_sha256"] == checker.sha256(output / "quality_report.json")
    assert manifest["runner_sha256"] == checker.sha256(SCRIPT)
    assert set(manifest["artifacts_sha256"]) == {"observation.json", "chain_boundary_detail.csv"}
    assert all(
        checker.sha256(output / name) == digest
        for name, digest in manifest["artifacts_sha256"].items()
    )
    with (output / "chain_boundary_detail.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == observer.BOUNDARY_FIELDS
    assert {row["cross_period"] for row in rows} <= {"True", "False"}
    observation = json.loads((output / "observation.json").read_text())
    assert observation["boundary_diagnostic"]["agrees_with_native_metric"]
    assert observation["native_quality_levels"] == 7


def test_frozen_old_and_new_real_artifacts_replay_without_solving(observer, checker):
    """These are observed run identities, not a new acceptance or width-gap threshold."""
    revisions = {
        "run_old_six": "bb5d92c1f21b89a0c17091d07bdd486cb59dcdf1",
        "run_new_seven": "b02ec8c2d89f02fc2cb0bcbb65afee0f262e9f1c",
    }
    historical = ROOT / (
        "docs/implementation/evidence/solverpy_path_cover_local_search/"
        "function_21_complete_acceptance/budget_200000/run_shared"
    )
    periods = json.loads((ROOT / "tests/baselines/gqga4/inputs/solver_config.json").read_text())[
        "period_order"
    ]
    period_index = {period: index for index, period in enumerate(periods)}

    def rows(path):
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def field(encoded, name):
        assert encoded[0] == "object"
        return dict(encoded[1])[name]

    def encoded_hash(encoded):
        return sha256(
            json.dumps(encoded, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    for directory, revision in revisions.items():
        output = SCRIPT.parent / directory
        report = json.loads((output / "quality_report.json").read_text())
        observation = json.loads((output / "observation.json").read_text())
        manifest = json.loads((output / "observation_manifest.json").read_text())
        native = directory == "run_new_seven"
        assert (
            report["code_revision"]
            == observation["code_revision"]
            == manifest["code_revision"]
            == revision
        )
        assert manifest["schema_version"] == observation["schema_version"] == 1
        assert report["passed"] and not report["failures"]
        assert all(check["passed"] for check in report["checks"].values())
        assert manifest["runner_sha256"] == checker.sha256(SCRIPT)
        assert manifest["quality_report_sha256"] == checker.sha256(output / "quality_report.json")
        assert set(report["artifacts_sha256"]) == {
            "accepted_trace.canonical.json",
            "public_result.canonical.json",
            "chain_detail.csv",
            "schedule_detail.csv",
            "source_conservation.csv",
        }
        assert set(manifest["artifacts_sha256"]) == {
            "observation.json",
            "chain_boundary_detail.csv",
        }
        for name, digest in (report["artifacts_sha256"] | manifest["artifacts_sha256"]).items():
            assert checker.sha256(output / name) == digest
        assert (
            sha256(
                (output / "accepted_trace.canonical.json").read_bytes().rstrip(b"\n")
            ).hexdigest()
            == report["trace_fingerprint"]
            == observation["trace_fingerprint"]
        )

        plan = field(
            field(json.loads((output / "public_result.canonical.json").read_text()), "release"),
            "plan",
        )
        encoded_chains = field(plan, "chains")[1]
        chains = rows(output / "chain_detail.csv")
        schedule = rows(output / "schedule_detail.csv")
        by_chain = {chain["chain_id"]: [] for chain in chains}
        for node in schedule:
            by_chain[node["chain_id"]].append(node)
        assert list(dict.fromkeys(node["chain_id"] for node in schedule)) == list(by_chain)
        assert [field(chain, "chain_id") for chain in encoded_chains] == list(by_chain)
        for current_chain in chains:
            members = by_chain[current_chain["chain_id"]]
            assert [int(node["position"]) for node in members] == list(range(len(members)))
            assert current_chain["node_ids"].split(";") == [node["node_id"] for node in members]
            assert all(
                node["assigned_period"] == current_chain["assigned_period"] for node in members
            )
        if not native:
            chains = sorted(chains, key=lambda chain: period_index[chain["assigned_period"]])
            encoded_chains = sorted(
                encoded_chains, key=lambda chain: period_index[field(chain, "assigned_period")]
            )
        diagnostic = observation["boundary_diagnostic"]
        derived = [
            "object",
            [
                [name, ["array", encoded_chains] if name == "chains" else value]
                for name, value in plan[1]
            ],
        ]
        assert diagnostic["original_plan_fingerprint"] == encoded_hash(plan)
        assert diagnostic["derived_plan_fingerprint"] == encoded_hash(derived)
        boundaries = rows(output / "chain_boundary_detail.csv")
        assert len(boundaries) == diagnostic["boundary_count"] == len(chains) - 1
        gaps = []
        for index, (left, right, boundary) in enumerate(zip(chains, chains[1:], boundaries), 1):
            tail, head = by_chain[left["chain_id"]][-1], by_chain[right["chain_id"]][0]
            gap = sum_decimals((D(tail["width"]), D(head["width"]).copy_negate())).copy_abs()
            gaps.append(gap)
            assert boundary == dict(
                zip(
                    observer.BOUNDARY_FIELDS,
                    (
                        str(index),
                        left["chain_id"],
                        right["chain_id"],
                        left["assigned_period"],
                        right["assigned_period"],
                        tail["node_id"],
                        head["node_id"],
                        tail["material_role"],
                        head["material_role"],
                        tail["width"],
                        head["width"],
                        str(gap),
                        str(left["assigned_period"] != right["assigned_period"]),
                    ),
                )
            )
        total = sum_decimals(gaps)
        assert total == D(diagnostic["absolute_gap_sum"])
        assert (
            observation["native_quality_levels"]
            == len(report["observations"]["quality_key"])
            == (7 if native else 6)
        )
        if native:
            assert (
                diagnostic["kind"] == "native_seven_level"
                and diagnostic["agrees_with_native_metric"]
            )
            assert diagnostic["original_plan_fingerprint"] == diagnostic["derived_plan_fingerprint"]
            assert (
                total
                == D(diagnostic["raw_metric"])
                == D(diagnostic["quality_value"])
                == D(report["observations"]["metrics"][observer.METRIC])
                == D(report["observations"]["quality_key"][6])
            )
        else:
            assert diagnostic["kind"] == "historical_stable_grouped_derived"
            assert (
                diagnostic["raw_metric"]
                is diagnostic["quality_value"]
                is diagnostic["agrees_with_native_metric"]
                is None
            )
            for name in (
                "chain_detail.csv",
                "schedule_detail.csv",
                "source_conservation.csv",
                "accepted_trace.canonical.json",
            ):
                assert (output / name).read_bytes() == (historical / name).read_bytes()
        for name, total in observation["stage_exclusive_totals"].items():
            assert (
                total
                == report["counters"][name]
                == sum(
                    stage["exclusive_delta"][name] for stage in observation["stage_observations"]
                )
            )
            for stage in observation["stage_observations"]:
                assert (
                    stage["inclusive_delta"][name] == stage["after"][name] - stage["before"][name]
                )
