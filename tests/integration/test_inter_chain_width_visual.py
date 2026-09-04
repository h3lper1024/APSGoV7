"""Native width-gap charts require hash-bound, ordered endpoint evidence."""

import csv
import json
from decimal import Decimal as D
from decimal import localcontext

import pytest

from tests.integration.test_gqga4_visual_report import ROOT, import_report


@pytest.fixture(scope="module")
def reporter():
    return import_report()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def write_csv(path, rows, fields=None):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or tuple(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def save_case(case, reporter):
    folder = case["folder"]
    for name, key in (("chain_detail.csv", "chains"), ("schedule_detail.csv", "nodes")):
        write_csv(folder / name, case[key])
    write_csv(case["input"], case["originals"])
    write_json(case["rules_path"], case["rules"])
    write_csv(folder / "chain_boundary_detail.csv", case["boundaries"], reporter.BOUNDARY_FIELDS)
    report = case["report"]
    report["artifacts_sha256"] = {
        name: reporter.sha256(folder / name) for name in ("chain_detail.csv", "schedule_detail.csv")
    }
    report["reference_hashes"] = {"inputs/input_orders.csv": reporter.sha256(case["input"])}
    report["protected_hashes"] = {
        "tests/baselines/gqga4/gqga4_rule_set_spec.json": reporter.sha256(case["rules_path"])
    }
    write_json(folder / "quality_report.json", report)
    write_json(folder / "observation.json", case["observation"])
    write_json(
        folder / "observation_manifest.json",
        {
            "schema_version": 1,
            "code_revision": "synthetic",
            "runner_sha256": "b" * 64,
            "quality_report_sha256": reporter.sha256(folder / "quality_report.json"),
            "artifacts_sha256": {
                name: reporter.sha256(folder / name)
                for name in ("observation.json", "chain_boundary_detail.csv")
            },
        },
    )


@pytest.fixture
def small_case(tmp_path, reporter):
    nodes = [
        dict(
            chain_id=chain,
            assigned_period=period,
            position=position,
            node_id=identity,
            source_order_id="" if role == reporter.VIRTUAL else identity,
            material_role=role,
            width=width,
            weight=weight,
            thickness="1",
            min_temperature="700",
            max_temperature="800",
            partition_id="",
        )
        for chain, period, position, identity, role, width, weight in (
            ("chain-z", "Z-first", "0", "real-1", "normal_real", "1000", "4"),
            ("chain-z", "Z-first", "1", "virtual-1", "virtual_sphc", "1100", "2"),
            ("chain-a", "Z-first", "0", "real-2", "actual_transition", "1200", "8"),
            ("chain-m", "A-last", "0", "real-3", "normal_real", "1000", "6"),
        )
    ]
    chains = [
        dict(
            chain_id=identity,
            assigned_period=period,
            node_ids=ids,
            total_weight=weight,
            real_weight=real,
            virtual_weight=virtual,
        )
        for identity, period, ids, weight, real, virtual in (
            ("chain-z", "Z-first", "real-1;virtual-1", "6", "4", "2"),
            ("chain-a", "Z-first", "real-2", "8", "8", "0"),
            ("chain-m", "A-last", "real-3", "6", "6", "0"),
        )
    ]
    originals = [
        {
            "source_order_id": row["node_id"],
            "交货日期": "2026-06-30",
            "连镀欠交": row["weight"],
            "宽度": row["width"],
            "厚度": "1",
            "均热段温度最小值": "700",
            "均热段温度最大值": "800",
        }
        for row in nodes
        if row["source_order_id"]
    ]
    boundaries = [
        dict(
            boundary_index=str(index),
            left_chain_id=left["chain_id"],
            right_chain_id=right["chain_id"],
            left_period=left["assigned_period"],
            right_period=right["assigned_period"],
            left_node_id=left["node_id"],
            right_node_id=right["node_id"],
            left_role=left["material_role"],
            right_role=right["material_role"],
            left_width=left["width"],
            right_width=right["width"],
            absolute_gap=str(abs(D(left["width"]) - D(right["width"]))),
            cross_period=str(left["assigned_period"] != right["assigned_period"]),
        )
        for index, (left, right) in enumerate(((nodes[1], nodes[2]), (nodes[2], nodes[3])), 1)
    ]
    rule = dict(rule_type="InterChainWidthGapRule", rule_id="gap", enabled=True)
    case = dict(
        folder=tmp_path,
        input=tmp_path / "input.csv",
        rules_path=tmp_path / "rules.json",
        nodes=nodes,
        chains=chains,
        originals=originals,
        boundaries=boundaries,
        rules={
            "rules": [
                dict(
                    rule_id="chain_weight_range",
                    rule_type="ChainWeightRangeRule",
                    enabled=True,
                    parameters={"min_weight": 1, "max_weight": 100},
                ),
                rule,
            ],
            "quality_spec": [{"metric_key": f"level-{index}"} for index in range(6)]
            + [
                dict(
                    metric_key=reporter.GAP_METRIC,
                    direction="minimize",
                    aggregation="sum",
                    numeric_projection="exact_decimal",
                )
            ],
        },
        report={
            "passed": True,
            "checks": {"audit": {"passed": True}},
            "failures": [],
            "public_result_fingerprint": "c" * 64,
            "observations": {
                "metrics": {reporter.GAP_METRIC: "300"},
                "quality_key": [0, "0", 0, "0", 3, "2", "300"],
            },
        },
        observation={
            "schema_version": 1,
            "boundary_diagnostic": dict(
                kind="native_seven_level",
                original_plan_fingerprint="a" * 64,
                derived_plan_fingerprint="a" * 64,
                rule_id="gap",
                metric_key=reporter.GAP_METRIC,
                raw_metric="300",
                quality_value="300",
                absolute_gap_sum="300",
                boundary_count=2,
                agrees_with_native_metric=True,
            ),
        },
    )
    save_case(case, reporter)
    return case


def load(case, reporter):
    return reporter.load_data(case["folder"], case["input"], case["rules_path"])


def remove_supplement(case):
    for name in ("observation_manifest.json", "observation.json", "chain_boundary_detail.csv"):
        (case["folder"] / name).unlink()


def test_native_boundaries_include_virtual_endpoint_and_cross_period(reporter, small_case):
    data = load(small_case, reporter)
    assert data["boundaries"] == small_case["boundaries"]
    assert data["boundary_total"] == D(300)
    assert data["boundaries"][0]["left_node_id"] == "virtual-1"
    assert [row["cross_period"] for row in data["boundaries"]] == ["False", "True"]
    assert (len(data["chains"]), len(data["nodes"]), data["source_count"]) == (3, 4, 3)


def test_current_full_rerun_uses_fifth_priority_and_audited_export_boundaries(reporter):
    """Freeze the observed rerun, not a new chain-count or width-gap acceptance ceiling."""
    folder = ROOT / (
        "docs/implementation/evidence/solverpy_path_cover_local_search/"
        "function_22_inter_chain_width_gap/step_22_6_priority_rerun/run_current"
    )
    data = reporter.load_data(
        folder,
        ROOT / "tests/baselines/gqga4/inputs/input_orders.csv",
        ROOT / "tests/baselines/gqga4/gqga4_rule_set_spec.json",
    )
    assert data["result_fingerprint"] == (
        "a20c1e8b082a66d828e838431cbbcca4f7a7de9952059764fc3961c054ec7563"
    )
    assert (len(data["nodes"]), data["source_count"], len(data["chains"])) == (549, 531, 22)
    assert (data["virtual_count"], data["virtual_weight"], data["real_weight"]) == (
        16,
        D(320),
        D("29333.91"),
    )
    assert data["boundary_quality_position"] == 5
    assert len(data["boundaries"]) == 21 and data["boundary_total"] == D(11218)
    assert [
        row["boundary_index"] for row in data["boundaries"] if row["cross_period"] == "True"
    ] == [
        "18",
        "20",
    ]
    assert data["boundaries"][19]["left_role"] == reporter.VIRTUAL
    assert "observation_manifest_sha256" not in data


@pytest.mark.parametrize("supplement", [False, True])
def test_reordered_priority_uses_declared_position_and_actual_endpoints(
    reporter, small_case, supplement, monkeypatch, tmp_path
):
    criteria = small_case["rules"]["quality_spec"]
    quality = small_case["report"]["observations"]["quality_key"]
    criteria[4], criteria[6] = criteria[6], criteria[4]
    quality[4], quality[6] = quality[6], quality[4]
    save_case(small_case, reporter)
    if not supplement:
        remove_supplement(small_case)
    data = load(small_case, reporter)
    assert data["boundaries"] == small_case["boundaries"]
    assert data["boundary_total"] == D(300)
    assert data["boundary_quality_position"] == 5

    class StubFigure:
        def to_html(self, **kwargs):
            return f'<div id="{kwargs["div_id"]}"></div>'

    monkeypatch.setattr(reporter, "build_figures", lambda _data: (StubFigure(),) * 4)
    output = tmp_path / "html"
    manifest = reporter.write_outputs(data, output)
    html = (output / "report.html").read_text(encoding="utf-8")
    assert "第五项评分一致" in html and "第七项评分一致" not in html
    if supplement:
        assert "observation_manifest_sha256" in manifest
    else:
        assert manifest["boundary_source"] == "audited_schedule_exports"
        assert manifest["boundary_quality_position"] == 5
        assert "observation_manifest_sha256" not in manifest
    assert len(reporter.read_csv(output / "chain_boundary_detail.csv")) == 2


@pytest.mark.parametrize("damage", ["metric", "quality", "chain_detail.csv", "schedule_detail.csv"])
def test_plain_precheck_requires_matching_metric_quality_and_hash_bound_exports(
    reporter, small_case, damage
):
    if damage == "metric":
        small_case["report"]["observations"]["metrics"][reporter.GAP_METRIC] = "301"
    elif damage == "quality":
        small_case["report"]["observations"]["quality_key"][6] = "301"
    save_case(small_case, reporter)
    remove_supplement(small_case)
    if damage.endswith(".csv"):
        del small_case["report"]["artifacts_sha256"][damage]
        write_json(small_case["folder"] / "quality_report.json", small_case["report"])
    with pytest.raises(ValueError, match="Boundary"):
        load(small_case, reporter)


@pytest.mark.parametrize(
    "filename", ["observation_manifest.json", "observation.json", "chain_boundary_detail.csv"]
)
def test_missing_supplement_never_falls_back_to_six_levels(reporter, small_case, filename):
    (small_case["folder"] / filename).unlink()
    with pytest.raises(FileNotFoundError):
        load(small_case, reporter)


@pytest.mark.parametrize(
    "damage", ["report_binding", "extra_artifact", "observation_hash", "boundary_hash"]
)
def test_rejects_unbound_supplement(reporter, small_case, damage):
    folder = small_case["folder"]
    manifest_path = folder / "observation_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if damage == "report_binding":
        manifest["quality_report_sha256"] = "wrong"
    elif damage == "extra_artifact":
        manifest["artifacts_sha256"]["extra.csv"] = "wrong"
    else:
        name = "observation.json" if damage == "observation_hash" else "chain_boundary_detail.csv"
        manifest["artifacts_sha256"][name] = "wrong"
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="Boundary"):
        load(small_case, reporter)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "historical_stable_grouped_derived"),
        ("derived_plan_fingerprint", "different"),
        ("rule_id", "other"),
        ("metric_key", "other"),
        ("agrees_with_native_metric", False),
        ("boundary_count", 3),
        ("raw_metric", "301"),
        ("quality_value", "301"),
        ("absolute_gap_sum", "301"),
    ],
)
def test_resigned_observation_still_requires_native_consistency(reporter, small_case, field, value):
    small_case["observation"]["boundary_diagnostic"][field] = value
    save_case(small_case, reporter)
    with pytest.raises(ValueError, match="native|Boundary"):
        load(small_case, reporter)


@pytest.mark.parametrize(
    "field,value",
    [
        ("boundary_index", "2"),
        ("left_chain_id", "chain-a"),
        ("right_chain_id", "chain-m"),
        ("left_period", "A-last"),
        ("right_period", "A-last"),
        ("left_node_id", "real-1"),
        ("right_node_id", "real-3"),
        ("left_role", "normal_real"),
        ("right_role", "normal_real"),
        ("left_width", "1000"),
        ("right_width", "1000"),
        ("absolute_gap", "200"),
        ("cross_period", "True"),
        ("absolute_gap", "NaN"),
    ],
)
def test_resigned_boundary_rows_must_match_actual_order(reporter, small_case, field, value):
    small_case["boundaries"][0][field] = value
    save_case(small_case, reporter)
    with pytest.raises(ValueError, match="Boundary|Nonfinite"):
        load(small_case, reporter)


@pytest.mark.parametrize(
    "damage", ["missing_row", "extra_row", "metric", "quality", "disabled", "schema"]
)
def test_rejects_extra_missing_or_inconsistent_evidence(reporter, small_case, damage):
    if damage == "missing_row":
        small_case["boundaries"].pop()
    elif damage == "extra_row":
        small_case["boundaries"].append(small_case["boundaries"][0])
    elif damage == "metric":
        small_case["report"]["observations"]["metrics"][reporter.GAP_METRIC] = "301"
    elif damage == "quality":
        small_case["report"]["observations"]["quality_key"][6] = "301"
    elif damage == "disabled":
        small_case["rules"]["rules"][-1]["enabled"] = False
    else:
        small_case["observation"]["schema_version"] = 2
    save_case(small_case, reporter)
    with pytest.raises(ValueError, match="Boundary|native|enabled"):
        load(small_case, reporter)


@pytest.mark.parametrize("supplement", [False, True])
def test_single_chain_has_no_boundary_and_explicit_zero(reporter, small_case, supplement):
    small_case["chains"] = small_case["chains"][:1]
    small_case["nodes"] = small_case["nodes"][:2]
    small_case["originals"] = small_case["originals"][:1]
    small_case["boundaries"] = []
    diagnostic = small_case["observation"]["boundary_diagnostic"]
    diagnostic.update(boundary_count=0, raw_metric="0", quality_value="0", absolute_gap_sum="0")
    small_case["report"]["observations"] = {
        "metrics": {reporter.GAP_METRIC: "0"},
        "quality_key": [0, "0", 0, "0", 1, "2", "0"],
    }
    save_case(small_case, reporter)
    if not supplement:
        remove_supplement(small_case)
    data = load(small_case, reporter)
    assert data["boundaries"] == [] and data["boundary_total"] == 0


@pytest.mark.parametrize("supplement", [False, True])
def test_boundary_arithmetic_is_exact_under_low_ambient_precision(reporter, small_case, supplement):
    widths = [f"1000.{('0' * 29)}{digit}" for digit in (1, 3, 9)]
    for row, width in zip(small_case["nodes"][1:], widths):
        row["width"] = width
    for row, width in zip(small_case["originals"][1:], widths[1:]):
        row["宽度"] = width
    for index, gap in enumerate(("2E-30", "6E-30")):
        small_case["boundaries"][index].update(
            left_width=widths[index],
            right_width=widths[index + 1],
            absolute_gap=gap,
        )
    small_case["observation"]["boundary_diagnostic"].update(
        raw_metric="8E-30",
        quality_value="8E-30",
        absolute_gap_sum="8E-30",
    )
    small_case["report"]["observations"]["metrics"][reporter.GAP_METRIC] = "8E-30"
    small_case["report"]["observations"]["quality_key"][6] = "8E-30"
    save_case(small_case, reporter)
    if not supplement:
        remove_supplement(small_case)
    with localcontext() as context:
        context.prec = 2
        assert load(small_case, reporter)["boundary_total"] == D("8E-30")


def test_six_level_has_three_figures_and_dynamic_html_counts(
    reporter, small_case, monkeypatch, tmp_path
):
    small_case["rules"]["rules"].pop()
    small_case["rules"]["quality_spec"].pop()
    save_case(small_case, reporter)
    data = load(small_case, reporter)
    assert "boundaries" not in data

    class StubFigure:
        def to_html(self, **kwargs):
            return f'<div id="{kwargs["div_id"]}"></div>'

    monkeypatch.setattr(reporter, "build_figures", lambda _data: (StubFigure(),) * 3)
    output = tmp_path / "html"
    manifest = reporter.write_outputs(data, output)
    html = (output / "report.html").read_text(encoding="utf-8")
    assert "3 条链" in html and "全部 4 个排程节点" in html
    assert "22 条链" not in html and "553 个" not in html
    assert "chain-boundaries" not in html and "boundary_count" not in manifest


def test_real_plotly_keeps_all_nodes_and_embeds_fourth_chart(reporter, small_case, tmp_path):
    pytest.importorskip("plotly")
    data = load(small_case, reporter)
    figures = reporter.build_figures(data)
    assert len(figures) == 4
    assert sum(len(trace.x) for trace in figures[0].data if trace.legendgroup == "width") == 4
    assert tuple(figures[3].data[0].y) == (100.0, 200.0)
    assert tuple(figures[3].data[0].marker.color) == (reporter.BLUE, reporter.ORANGE)
    assert "virtual-1" in figures[3].data[0].customdata[0]
    output = tmp_path / "html"
    manifest = reporter.write_outputs(data, output)
    html = (output / "report.html").read_text(encoding="utf-8")
    assert html.count('class="plotly-graph-div"') == 4
    assert 'id="chain-boundaries"' in html and 'id="boundaries"' in html
    assert "已发布生产链序" in html and "全部 4 个排程节点" in html
    assert 'href="chain_boundary_detail.csv"' in html
    assert manifest["boundary_count"] == 2 and D(manifest["boundary_total"]) == 300
    assert (output / "chain_boundary_detail.csv").read_bytes() == (
        small_case["folder"] / "chain_boundary_detail.csv"
    ).read_bytes()
    assert (
        reporter.sha256(output / "chain_boundary_detail.csv")
        == manifest["artifact_sha256"]["chain_boundary_detail.csv"]
    )
