"""The evidence comparator must detect input or search drift, not just status."""

import json
from decimal import Decimal
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from apsgo_scheduler.api.json_codec import dumps_exact_json
from tools.verify_earliest_start_real_data import summarize


BASELINE = Path(__file__).resolve().parents[2] / (
    "docs/implementation/evidence/earliest_process_start_constraint/real_20260601"
)


def replay_files(tmp_path):
    output = tmp_path / "new"
    enabled = output / "enabled"
    enabled.mkdir(parents=True)
    for name in ("enabled_request.json", "lower_bounds.json"):
        shutil.copyfile(BASELINE / name, output / name)
    for name in ("response.json", "measurement.json", "delivery_report.json"):
        shutil.copyfile(BASELINE / "enabled" / name, enabled / name)
    shutil.copyfile(BASELINE / "enabled_request.json", enabled / "prepared_request.json")
    # Synthetic new audit fields exercise only the comparison tool, not the solver.
    mutate(enabled / "response.json", lambda data: data["core_audit"].update(
        integrity_passed=True, writeback_blocking_violation_count=6))
    return SimpleNamespace(output=output, baseline=BASELINE)


def mutate(path, change):
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    change(data)
    path.write_text(dumps_exact_json(data), encoding="utf-8")


def test_comparison_keeps_all_search_material_and_exports_diagnostic_nodes(tmp_path):
    args = replay_files(tmp_path)
    summarize(args)
    result = json.loads((args.output / "comparison.json").read_text(encoding="utf-8"))
    assert all(result["matches_previous_enabled"].values())
    assert result["plan_differences"] == []
    assert result["runs"]["enabled"]["early_node_count"] == 6
    csv = (args.output / "enabled" / "nodes.csv").read_text(encoding="utf-8-sig")
    assert "diagnostic_candidate_not_publishable" in csv
    assert len(csv.splitlines()) == 578


@pytest.mark.parametrize("case", ("request", "trace", "blocking_count", "release_flag"))
def test_comparison_rejects_incorrect_material(tmp_path, case):
    args = replay_files(tmp_path)
    response = args.output / "enabled" / "response.json"
    if case == "request":
        mutate(args.output / "enabled_request.json", lambda data: data.update(changed=True))
    elif case == "trace":
        mutate(response, lambda data: data["run_manifest"].update(trace_fingerprint="changed"))
    elif case == "blocking_count":
        mutate(response, lambda data: data["core_audit"].update(writeback_blocking_violation_count=0))
    else:
        mutate(args.output / "enabled" / "measurement.json", lambda data: data.update(publishable=True))
    with pytest.raises(AssertionError):
        summarize(args)
