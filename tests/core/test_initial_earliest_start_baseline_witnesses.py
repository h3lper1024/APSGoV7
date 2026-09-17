"""A0 hand witnesses and capture-tool guards; these are NOT solver integration tests."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "a0_capture", ROOT / "tools/capture_initial_earliest_start_baseline.py"
)
CAPTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAPTURE)
SUITE = CAPTURE.load_cases()
CASES = SUITE["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda value: value["case_id"])
def test_hand_written_clock_and_coverage(case):
    checked = CAPTURE.validate_witness(case)
    assert checked["production_observed"] is None
    assert checked["baseline_hand_check"]["early_ms"] == case["expected_baseline"]["early_ms"]


def test_manual_whole_chain_counterfactual_has_no_wait_or_extra_material():
    case = CASES[0]
    checked = CAPTURE.validate_witness(case)
    before, after = checked["baseline_hand_check"], checked["manual_counterfactual_hand_check"]
    assert before["early_node_count"] == 1 and before["early_total_ms"] == 7200000
    assert after["early_node_count"] == 0
    assert before["total_duration_ms"] == after["total_duration_ms"] == 14400000
    assert len(case["expected_baseline"]["chains"]) == len(case["manual_proposal"]["chains"])
    assert sorted(before["rows"]) == sorted(after["rows"])


def test_mixed_period_witness_uses_final_period_grouping_and_global_clock():
    case = next(case for case in CASES if case["case_id"] == "mixed_periods")
    checked = CAPTURE.validate_witness(case)["baseline_hand_check"]
    assert checked["rows"] == [2, 3, 0, 1]
    assert checked["start_ms"] == [0, 3600000, 7200000, 10800000]
    assert checked["early_ms"] == [0, 7200000, 0, 0]
    # D belongs to P1 but is assigned with C to P0. A0 records, not repairs, this fact.
    assert case["orders"][3]["source_period"] == "P1"


@pytest.mark.parametrize("damage", ["missing", "duplicate", "bool_row", "empty_chain",
    "reverse_periods", "wrong_period", "missing_lower", "bad_duration", "bad_clock",
    "bad_early", "absent_edge", "bad_count", "observed_label"])
def test_corrupt_witness_is_rejected(damage):
    case = copy.deepcopy(CASES[0])
    baseline = case["expected_baseline"]
    if damage == "missing":
        baseline["chains"][0].pop()
    elif damage == "duplicate":
        baseline["chains"][0][0] = 1
    elif damage == "bool_row":
        baseline["chains"][0][0] = False
    elif damage == "empty_chain":
        baseline["chains"].append([])
        baseline["chain_periods"].append("P0")
    elif damage == "reverse_periods":
        baseline["chain_periods"] = ["P1", "P0"]
    elif damage == "wrong_period":
        baseline["chain_periods"] = ["P1", "P1"]
    elif damage == "missing_lower":
        case["orders"][0]["earliest_offset_ms"] = None
    elif damage == "bad_duration":
        case["orders"][0]["duration_ms"] = 0
    elif damage == "bad_clock":
        baseline["start_ms"][1] += 1
    elif damage == "bad_early":
        baseline["early_ms"][1] = 0
    elif damage == "absent_edge":
        baseline["edges"] = []
    elif damage == "bad_count":
        baseline["candidate_check_count"] = 1
    else:
        baseline["origin"] = "production_observed"
    with pytest.raises(ValueError):
        CAPTURE.validate_witness(case)


def test_disabled_lower_bounds_are_not_filled_in():
    case = next(case for case in CASES if not case["earliest_enabled"])
    before = copy.deepcopy(case)
    checked = CAPTURE.validate_witness(case)
    assert checked["baseline_hand_check"]["early_node_count"] == 0
    assert case == before
    assert all(order["earliest_offset_ms"] is None for order in case["orders"])


def test_cli_output_is_create_only_and_does_not_claim_numeric_execution(tmp_path):
    output = tmp_path / "witness.json"
    assert CAPTURE.main(["--mode", "witness", "--output", str(output)]) == 0
    saved = output.read_bytes()
    report = json.loads(saved)
    assert report["status"] == "hand_witnesses_verified_only"
    assert report["numeric_executed"] is False
    assert report["production_service_accessed"] is False
    with pytest.raises(SystemExit) as error:
        CAPTURE.main(["--mode", "witness", "--output", str(output)])
    assert error.value.code == 2 and output.read_bytes() == saved


def test_numeric_environment_failure_is_reported_not_counted_as_a_pass(tmp_path, monkeypatch):
    def blocked():
        raise ValueError("explicit test environment blocker")
    monkeypatch.setattr(CAPTURE, "baseline_source_identity", blocked)
    output = tmp_path / "blocked.json"
    assert CAPTURE.main(["--mode", "numeric", "--output", str(output)]) == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "blocked_or_error"
    assert report["numeric_executed"] is False
    assert "blocker" in report["error"]
