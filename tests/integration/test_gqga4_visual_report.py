"""Audit chart inputs and summaries without importing Plotly or running a solver."""

import builtins
import importlib.util
import json
import shutil
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / (
    "docs/implementation/evidence/solverpy_path_cover_local_search/"
    "function_21_complete_acceptance/budget_200000"
)
INPUT = ROOT / "tests/baselines/gqga4/inputs/input_orders.csv"
RULES = ROOT / "tests/baselines/gqga4/gqga4_rule_set_spec_six_level_historical.json"


def import_report():
    spec = importlib.util.spec_from_file_location(
        "gqga4_visual_report_test_target", BASE / "visualization/generate_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reporter():
    return import_report()


def test_import_does_not_require_plotly(monkeypatch):
    original_import = builtins.__import__

    def without_plotly(name, *args, **kwargs):
        assert not name.startswith("plotly"), "Plotly must remain an optional rendering dependency"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_plotly)
    assert callable(import_report().summarize)


def test_frozen_full_data_totals_dates_order_and_splits(reporter):
    data = reporter.load_data(BASE / "run_shared", INPUT, RULES)
    chains = reporter.read_csv(BASE / "run_shared/chain_detail.csv")
    nodes = reporter.read_csv(BASE / "run_shared/schedule_detail.csv")
    assert (len(data["nodes"]), data["source_count"], len(data["chains"])) == (553, 531, 22)
    assert (data["virtual_count"], data["virtual_weight"], data["real_weight"]) == (
        20,
        D(400),
        D("29333.91"),
    )
    assert data["missing_temperature_count"] == 0
    assert [(row["date"], row["source_count"], row["weight"]) for row in data["due"]] == [
        ("2026-05-31", 96, D("5386.03")),
        ("2026-06-05", 6, D("404.34")),
        ("2026-06-10", 20, D("847.48")),
        ("2026-06-15", 5, D("160.40")),
        ("2026-06-30", 404, D("22535.66")),
    ]
    assert [row["chain_id"] for row in data["chains"]] == [row["chain_id"] for row in chains]
    assert [row["node_id"] for row in data["nodes"]] == [row["node_id"] for row in nodes]
    assert [row["sequence"] for row in data["nodes"]] == list(range(1, 554))
    real = [row for row in data["nodes"] if row["material_role"] != reporter.VIRTUAL]
    counts = Counter(row["source_order_id"] for row in real)
    assert len(real) == 533
    assert {key: count for key, count in counts.items() if count > 1} == {
        "0002002073-000010": 2,
        "0002002055-000120": 2,
    }
    for chain in data["chains"]:
        group = data["nodes"][chain["start"] - 1 : chain["end"]]
        assert len(group) == chain["node_count"]
        assert {row["small_roll"] for row in group} == {chain["small_roll"]}
        assert sum((D(row["weight"]) for row in group), D(0)) == chain["total_weight"]


def test_cli_defaults_load_historical_rules_and_full_evidence(
    reporter, tmp_path, monkeypatch, capsys
):
    class StubFigure:
        def to_html(self, **_kwargs):
            return "<div>Rendering stub</div>"

    output = tmp_path / "report"
    monkeypatch.setattr("sys.argv", ["generate_report.py", "--output-dir", str(output)])
    monkeypatch.setattr(reporter, "build_figures", lambda _data: (StubFigure(),) * 3)
    reporter.main()
    manifest = json.loads(capsys.readouterr().out)
    assert (manifest["node_count"], manifest["source_count"], manifest["chain_count"]) == (
        553,
        531,
        22,
    )
    assert (
        manifest["virtual_count"],
        D(manifest["virtual_weight"]),
        D(manifest["real_weight"]),
    ) == (20, D(400), D("29333.91"))
    assert manifest == json.loads((output / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def small_case():
    physical = dict(width="1000", thickness="1", min_temperature="700", max_temperature="800")
    nodes = [
        dict(
            physical,
            chain_id="z",
            assigned_period="BR2",
            position=str(i),
            node_id=node,
            source_order_id=source,
            material_role=role,
            weight=weight,
        )
        for i, (node, source, role, weight) in enumerate(
            [
                ("piece1", "source", "normal_real", "4"),
                ("generated", "", "virtual_sphc", "2"),
                ("piece2", "source", "normal_real", "6"),
            ]
        )
    ]
    nodes.append(
        dict(
            physical,
            chain_id="a",
            assigned_period="BR1",
            position="0",
            node_id="virtual-looking-real",
            source_order_id="transition",
            material_role="actual_transition",
            weight="8",
        )
    )
    chains = [
        dict(
            chain_id="z",
            assigned_period="BR2",
            node_ids="piece1;generated;piece2",
            total_weight="12",
            real_weight="10",
            virtual_weight="2",
        ),
        dict(
            chain_id="a",
            assigned_period="BR1",
            node_ids="virtual-looking-real",
            total_weight="8",
            real_weight="8",
            virtual_weight="0",
        ),
    ]
    originals = [
        {
            "source_order_id": key,
            "交货日期": "2026-06-30",
            "连镀欠交": weight,
            "宽度": "1000",
            "厚度": "1",
            "均热段温度最小值": "700",
            "均热段温度最大值": "800",
        }
        for key, weight in [("source", "10"), ("transition", "8")]
    ]
    return chains, nodes, originals


def test_piece_deduplication_virtual_role_and_explicit_missing_fields(reporter, small_case):
    chains, nodes, originals = small_case
    originals[0]["交货日期"] = " "
    nodes[1]["min_temperature"] = ""
    data = reporter.summarize(chains, nodes, originals)
    assert [row["chain_id"] for row in data["chains"]] == ["z", "a"]
    assert (data["source_count"], data["virtual_count"], data["virtual_weight"]) == (2, 1, D(2))
    assert data["due"] == [
        dict(date="2026-06-30", source_count=1, weight=D(8)),
        dict(date="未提供", source_count=1, weight=D(10)),
    ]
    assert data["chains"][0]["due_counts"] == {"未提供": 1}
    assert data["nodes"][1]["delivery_date"] == "不适用"
    assert data["nodes"][1]["min_temperature"] == ""
    assert data["missing_temperature_count"] == 1


@pytest.mark.parametrize(
    "collection,index,field,value,message",
    [
        (2, 1, "source_order_id", "source", "Duplicate or empty source"),
        (0, 1, "chain_id", "z", "Duplicate chain_id"),
        (1, 2, "node_id", "piece1", "Duplicate node_id"),
        (0, 0, "node_ids", "piece2;generated;piece1", "node order"),
        (1, 1, "position", "2", "Noncontiguous"),
        (1, 0, "assigned_period", "BR1", "Assigned period"),
        (1, 0, "weight", "5", "Chain weights"),
        (1, 0, "weight", "0", "Nonpositive"),
        (2, 0, "连镀欠交", "11", "per-source weight"),
        (1, 1, "source_order_id", "source", "Virtual node"),
        (1, 1, "material_role", "virtual", "Unknown chain or material role"),
        (1, 0, "width", "999", "Physical attribute mismatch"),
        (1, 1, "min_temperature", "900", "Reversed temperature"),
        (1, 1, "max_temperature", "NaN", "Nonfinite"),
        (1, 1, "max_temperature", "Infinity", "Nonfinite"),
        (2, 0, "交货日期", "2026-02-30", "day is out of range"),
    ],
)
def test_rejects_inconsistent_data(reporter, small_case, collection, index, field, value, message):
    small_case[collection][index][field] = value
    with pytest.raises(ValueError, match=message):
        reporter.summarize(*small_case)


def test_rejects_reordered_chain_groups(reporter, small_case):
    chains, nodes, originals = small_case
    with pytest.raises(ValueError, match="Schedule order"):
        reporter.summarize(chains, nodes[-1:] + nodes[:-1], originals)


@pytest.mark.parametrize("damage", ["passed", "failures", "check", "artifact", "input", "rules"])
def test_load_data_rejects_failed_audit_or_hash_changes(reporter, tmp_path, damage):
    result = tmp_path / "result"
    result.mkdir()
    report = json.loads((BASE / "run_shared/quality_report.json").read_text(encoding="utf-8"))
    for name in report["artifacts_sha256"]:
        shutil.copyfile(BASE / "run_shared" / name, result / name)
    input_path, rules_path = tmp_path / "input.csv", tmp_path / "rules.json"
    shutil.copyfile(INPUT, input_path)
    shutil.copyfile(RULES, rules_path)
    if damage == "passed":
        report["passed"] = False
    elif damage == "failures":
        report["failures"] = ["synthetic failed audit"]
    elif damage == "check":
        next(iter(report["checks"].values()))["passed"] = False
    else:
        target = {
            "artifact": result / "schedule_detail.csv",
            "input": input_path,
            "rules": rules_path,
        }[damage]
        target.write_bytes(target.read_bytes() + b"\n")
    (result / "quality_report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="successful quality report|hash mismatch"):
        reporter.load_data(result, input_path, rules_path)
