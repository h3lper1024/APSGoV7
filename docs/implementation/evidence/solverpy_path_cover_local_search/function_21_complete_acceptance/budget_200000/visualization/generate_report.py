"""Render audited CSV evidence with Plotly; never run or modify the solver."""

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, localcontext
from html import escape
from pathlib import Path

D = Decimal
ROOT = Path(__file__).resolve().parents[7]
HERE = Path(__file__).resolve().parent
VIRTUAL = "virtual_sphc"
ROLES = {
    "normal_real": "真实非过渡材",
    "actual_transition": "真实过渡材",
    VIRTUAL: "虚拟材",
}
BLUE, ORANGE, GREEN, PURPLE = "#2166ac", "#db7718", "#23856d", "#8054b1"
GAP_METRIC = "inter_chain_width_gap"
BOUNDARY_FIELDS = (
    "boundary_index",
    "left_chain_id",
    "right_chain_id",
    "left_period",
    "right_period",
    "left_node_id",
    "right_node_id",
    "left_role",
    "right_role",
    "left_width",
    "right_width",
    "absolute_gap",
    "cross_period",
)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def number(value):
    result = D(value)
    if not result.is_finite():
        raise ValueError(f"Nonfinite value: {value}")
    return result


def due_date(value):
    return date.fromisoformat(value.strip()).isoformat() if value.strip() else "未提供"


def summarize(chains, nodes, originals):
    """Keep exported order; count original orders once and scheduled weights by piece."""
    source = {row["source_order_id"]: row for row in originals}
    if len(source) != len(originals) or "" in source:
        raise ValueError("Duplicate or empty source_order_id")
    chain_ids = [row["chain_id"] for row in chains]
    if len(set(chain_ids)) != len(chain_ids):
        raise ValueError("Duplicate chain_id")
    if len({row["node_id"] for row in nodes}) != len(nodes):
        raise ValueError("Duplicate node_id")
    by_chain = defaultdict(list)
    for row in nodes:
        if row["chain_id"] not in chain_ids or row["material_role"] not in ROLES:
            raise ValueError("Unknown chain or material role")
        by_chain[row["chain_id"]].append(row)
    if [row for key in chain_ids for row in by_chain[key]] != nodes:
        raise ValueError("Schedule order differs from chain export order")
    scheduled = defaultdict(D)
    enriched, summaries = [], []
    due_weights, due_counts = defaultdict(D), Counter()
    for row in originals:
        due_counts[due_date(row["交货日期"])] += 1
    for index, chain in enumerate(chains, 1):
        group = by_chain[chain["chain_id"]]
        if [row["node_id"] for row in group] != chain["node_ids"].split(";"):
            raise ValueError("Chain node order differs from declared node_ids")
        if [int(row["position"]) for row in group] != list(range(len(group))):
            raise ValueError("Noncontiguous chain positions")
        real_weight, virtual_weight, virtual_count = D(0), D(0), 0
        per_due, per_due_ids = defaultdict(D), defaultdict(set)
        start = len(enriched) + 1
        for row in group:
            if row["assigned_period"] != chain["assigned_period"]:
                raise ValueError("Assigned period differs between chain and node")
            weight = number(row["weight"])
            if weight <= 0:
                raise ValueError("Nonpositive scheduled weight")
            item = dict(row, sequence=len(enriched) + 1, small_roll=f"{index:02d}")
            item["delivery_date"] = "不适用"
            if row["material_role"] == VIRTUAL:
                if row["source_order_id"]:
                    raise ValueError("Virtual node must not claim an original order")
                virtual_weight += weight
                virtual_count += 1
            else:
                original = source[row["source_order_id"]]
                delivery = due_date(original["交货日期"])
                item["delivery_date"] = delivery
                scheduled[row["source_order_id"]] += weight
                real_weight += weight
                due_weights[delivery] += weight
                per_due[delivery] += weight
                per_due_ids[delivery].add(row["source_order_id"])
                for field, raw_field in (
                    ("width", "宽度"),
                    ("thickness", "厚度"),
                    ("min_temperature", "均热段温度最小值"),
                    ("max_temperature", "均热段温度最大值"),
                ):
                    if row[field] and number(row[field]) != number(original[raw_field]):
                        raise ValueError(f"Physical attribute mismatch: {row['node_id']}.{field}")
            for field in ("width", "thickness", "min_temperature", "max_temperature"):
                if row[field]:
                    number(row[field])
            if row["min_temperature"] and row["max_temperature"]:
                if number(row["min_temperature"]) > number(row["max_temperature"]):
                    raise ValueError("Reversed temperature interval")
            enriched.append(item)
        total = real_weight + virtual_weight
        if (total, real_weight, virtual_weight) != tuple(
            number(chain[key]) for key in ("total_weight", "real_weight", "virtual_weight")
        ):
            raise ValueError("Chain weights do not reconcile")
        summaries.append(
            dict(
                small_roll=f"{index:02d}",
                chain_id=chain["chain_id"],
                assigned_period=chain["assigned_period"],
                start=start,
                end=len(enriched),
                node_count=len(group),
                real_node_count=len(group) - virtual_count,
                source_count=len(
                    {row["source_order_id"] for row in group if row["source_order_id"]}
                ),
                total_weight=total,
                real_weight=real_weight,
                virtual_weight=virtual_weight,
                virtual_count=virtual_count,
                due_weights=dict(per_due),
                due_counts={key: len(ids) for key, ids in per_due_ids.items()},
            )
        )
    expected = {key: number(row["连镀欠交"]) for key, row in source.items()}
    if dict(scheduled) != expected:
        raise ValueError("Original coverage or per-source weight does not reconcile")
    dates = sorted(due_counts, key=lambda value: (value == "未提供", value))
    return dict(
        nodes=enriched,
        chains=summaries,
        due=[
            dict(date=day, source_count=due_counts[day], weight=due_weights[day]) for day in dates
        ],
        source_count=len(source),
        real_weight=sum(scheduled.values(), D(0)),
        virtual_count=sum(row["virtual_count"] for row in summaries),
        virtual_weight=sum((row["virtual_weight"] for row in summaries), D(0)),
        missing_temperature_count=sum(
            not row["min_temperature"] or not row["max_temperature"] for row in nodes
        ),
    )


def load_data(result_dir, input_orders, rules_path):
    report = json.loads((result_dir / "quality_report.json").read_text())
    if (
        not report["passed"]
        or report["failures"]
        or not all(check["passed"] for check in report["checks"].values())
    ):
        raise ValueError("Expected an audited, successful quality report")
    for name, digest in report["artifacts_sha256"].items():
        if Path(name).name != name or sha256(result_dir / name) != digest:
            raise ValueError(f"Artifact hash mismatch: {name}")
    if sha256(input_orders) != report["reference_hashes"]["inputs/input_orders.csv"]:
        raise ValueError("Original input hash mismatch")
    if (
        sha256(rules_path)
        != report["protected_hashes"]["tests/baselines/gqga4/gqga4_rule_set_spec.json"]
    ):
        raise ValueError("Rule configuration hash mismatch")
    data = summarize(
        read_csv(result_dir / "chain_detail.csv"),
        read_csv(result_dir / "schedule_detail.csv"),
        read_csv(input_orders),
    )
    rules = json.loads(rules_path.read_text())
    data["weight_limits"] = next(
        rule["parameters"]
        for rule in rules["rules"]
        if rule["rule_id"] == "chain_weight_range" and rule["enabled"]
    )
    data["result_fingerprint"] = report["public_result_fingerprint"]
    data["report_sha256"] = sha256(result_dir / "quality_report.json")
    gap_criteria = [item for item in rules["quality_spec"] if item["metric_key"] == GAP_METRIC]
    if gap_criteria:
        producers = [
            item
            for item in rules["rules"]
            if item["rule_type"] == "InterChainWidthGapRule" and item["enabled"] is True
        ]
        if (
            len(producers) != 1
            or len(gap_criteria) != 1
            or len(rules["quality_spec"]) != 7
            or any(
                gap_criteria[0][key] != value
                for key, value in (
                    ("direction", "minimize"),
                    ("aggregation", "sum"),
                    ("numeric_projection", "exact_decimal"),
                )
            )
        ):
            raise ValueError("Expected one enabled width-gap objective in the seven-level quality")
        gap_index = rules["quality_spec"].index(gap_criteria[0])
        load_boundaries(data, result_dir, report, producers[0]["rule_id"], gap_index)
    return data


def load_boundaries(data, result_dir, report, rule_id, gap_index):
    """Recompute audited endpoints; validate any historical supplement instead of ignoring it."""
    manifest_path = result_dir / "observation_manifest.json"
    supplemental = ("observation_manifest.json", "observation.json", "chain_boundary_detail.csv")
    has_supplement = any(
        (result_dir / name).exists() or (result_dir / name).is_symlink() for name in supplemental
    )
    rows, diagnostic = [], None
    if not {"chain_detail.csv", "schedule_detail.csv"} <= set(report["artifacts_sha256"]):
        raise ValueError("Boundary source exports must be hash-bound to the audited report")
    if has_supplement:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest["schema_version"] != 1
            or manifest["quality_report_sha256"] != data["report_sha256"]
            or set(manifest["artifacts_sha256"])
            != {"observation.json", "chain_boundary_detail.csv"}
        ):
            raise ValueError(
                "Boundary manifest does not bind the audited report and exact artifacts"
            )
        for name, digest in manifest["artifacts_sha256"].items():
            if sha256(result_dir / name) != digest:
                raise ValueError(f"Boundary artifact hash mismatch: {name}")
        observation = json.loads((result_dir / "observation.json").read_text(encoding="utf-8"))
        diagnostic = observation["boundary_diagnostic"]
        if (
            observation["schema_version"] != 1
            or diagnostic["kind"] != "native_seven_level"
            or diagnostic["rule_id"] != rule_id
            or diagnostic["metric_key"] != GAP_METRIC
            or not diagnostic["original_plan_fingerprint"]
            or diagnostic["original_plan_fingerprint"] != diagnostic["derived_plan_fingerprint"]
            or diagnostic["agrees_with_native_metric"] is not True
        ):
            raise ValueError("Expected unchanged native seven-level boundary evidence")
        with (result_dir / "chain_boundary_detail.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != BOUNDARY_FIELDS:
                raise ValueError("Boundary CSV fields do not match the declared schema")
            rows = list(reader)
    chains = data["chains"]
    if has_supplement and (
        len(rows) != max(0, len(chains) - 1) or diagnostic["boundary_count"] != len(rows)
    ):
        raise ValueError("Boundary count must equal chain count minus one")
    endpoints = [
        (data["nodes"][left["end"] - 1], data["nodes"][right["start"] - 1])
        for left, right in zip(chains, chains[1:])
    ]
    widths = [number(node["width"]) for pair in endpoints for node in pair]
    if any(value <= 0 for value in widths):
        raise ValueError("Boundary widths must be positive")
    with localcontext() as context:
        if widths:
            context.prec = max(
                28,
                max(value.adjusted() for value in widths)
                - min(value.as_tuple().exponent for value in widths)
                + len(str(len(endpoints)))
                + 2,
            )
        total = D(0)
        for index, (left, right) in enumerate(endpoints, 1):
            gap = abs(number(left["width"]) - number(right["width"]))
            if not has_supplement:
                rows.append(
                    dict(
                        boundary_index=str(index),
                        **{
                            f"{side}_{field}": node[source]
                            for side, node in (("left", left), ("right", right))
                            for field, source in (
                                ("chain_id", "chain_id"),
                                ("period", "assigned_period"),
                                ("node_id", "node_id"),
                                ("role", "material_role"),
                                ("width", "width"),
                            )
                        },
                        absolute_gap=str(gap),
                        cross_period=str(left["assigned_period"] != right["assigned_period"]),
                    )
                )
            row = rows[index - 1]
            if set(row) != set(BOUNDARY_FIELDS) or row["boundary_index"] != str(index):
                raise ValueError("Boundary rows must preserve consecutive exported order")
            for side, node in (("left", left), ("right", right)):
                if any(
                    row[f"{side}_{field}"] != node[source]
                    for field, source in (
                        ("chain_id", "chain_id"),
                        ("period", "assigned_period"),
                        ("node_id", "node_id"),
                        ("role", "material_role"),
                    )
                ) or number(row[f"{side}_width"]) != number(node["width"]):
                    raise ValueError(
                        f"Boundary {index} {side} endpoint differs from released order"
                    )
            if number(row["absolute_gap"]) != gap or row["cross_period"] != str(
                left["assigned_period"] != right["assigned_period"]
            ):
                raise ValueError(f"Boundary {index} width gap or cross-period flag differs")
            total += gap
    quality = report["observations"]["quality_key"]
    if len(quality) != 7:
        raise ValueError("Boundary objective requires seven quality values")
    values = [report["observations"]["metrics"][GAP_METRIC], quality[gap_index]]
    if diagnostic is not None:
        values += [diagnostic[key] for key in ("raw_metric", "quality_value", "absolute_gap_sum")]
    if any(number(value) != total for value in values):
        raise ValueError("Boundary sum differs from raw metric or declared quality value")
    data.update(boundaries=rows, boundary_total=total, boundary_quality_position=gap_index + 1)
    if has_supplement:
        data["observation_manifest_sha256"] = sha256(manifest_path)


def hover(row):
    kind = ROLES[row["material_role"]] + (" · 拆分片段" if row["partition_id"] else "")
    text = (
        f"序号 {row['sequence']} · 小辊期 {row['small_roll']} · 链内 {int(row['position']) + 1}"
        f"<br>节点：{escape(row['node_id'])}<br>{kind} · {row['weight']} 吨"
        f"<br>大辊期：{escape(row['assigned_period'])} · 交货日期：{row['delivery_date']}"
        f"<br>宽 {row['width']} mm · 厚 {row['thickness']} mm"
        f"<br>允许温度：{row['min_temperature'] or '未提供'}～{row['max_temperature'] or '未提供'} °C"
    )
    return text + (
        "<br>虚拟材温区为生成时派生值，非实测炉温" if row["material_role"] == VIRTUAL else ""
    )


def style(figure, height):
    figure.update_layout(
        template="plotly_white",
        height=height,
        font=dict(family="PingFang SC, Microsoft YaHei, sans-serif", size=13, color="#26374b"),
        margin=dict(l=75, r=40, t=65, b=65),
        legend=dict(orientation="h", y=1.06, x=0),
        hoverlabel=dict(font_size=13),
    )
    figure.update_xaxes(showgrid=False, automargin=True)
    figure.update_yaxes(gridcolor="#e6ebf0", zeroline=False, automargin=True)


def build_figures(data):
    # Plotting is an optional report dependency, never a production solver dependency.
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    trend = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.095,
        subplot_titles=["宽度变化", "厚度变化", "允许温度区间变化（非实际炉温）"],
    )
    for chain_index, chain in enumerate(data["chains"]):
        group = data["nodes"][chain["start"] - 1 : chain["end"]]
        x, tips = [row["sequence"] for row in group], [hover(row) for row in group]
        for field, panel, color, label in (
            ("width", 1, BLUE, "宽度"),
            ("thickness", 2, PURPLE, "厚度"),
            ("min_temperature", 3, GREEN, "温度下限"),
            ("max_temperature", 3, GREEN, "温度上限"),
        ):
            trend.add_trace(
                go.Scatter(
                    x=x,
                    y=[float(row[field]) if row[field] else None for row in group],
                    name=label,
                    legendgroup=field,
                    showlegend=chain_index == 0,
                    mode="lines+markers",
                    marker_size=3,
                    connectgaps=False,
                    line=dict(
                        color=color,
                        width=1.5,
                        dash="dot" if field == "min_temperature" else "solid",
                    ),
                    fill="tonexty" if field == "max_temperature" else None,
                    fillcolor="rgba(35,133,109,0.13)",
                    text=tips,
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=panel,
                col=1,
            )
        if chain_index:
            trend.add_vline(x=chain["start"] - 0.5, line_width=1, line_color="#cbd5df")
    virtuals = [row for row in data["nodes"] if row["material_role"] == VIRTUAL]
    for field, panel in (
        ("width", 1),
        ("thickness", 2),
        ("min_temperature", 3),
        ("max_temperature", 3),
    ):
        trend.add_trace(
            go.Scatter(
                x=[row["sequence"] for row in virtuals],
                y=[float(row[field]) if row[field] else None for row in virtuals],
                mode="markers",
                marker=dict(color=ORANGE, size=7, symbol="diamond"),
                name="虚拟材（菱形）",
                legendgroup="virtual",
                showlegend=field == "width",
                text=[hover(row) for row in virtuals],
                hovertemplate="%{text}<extra></extra>",
            ),
            row=panel,
            col=1,
        )
    for panel, unit in ((1, "宽度 / mm"), (2, "厚度 / mm"), (3, "温度 / °C")):
        trend.update_yaxes(title_text=unit, row=panel, col=1)
    trend.update_xaxes(title_text="全部排程节点顺序（小辊期边界处断线）", row=3, col=1)
    trend.update_xaxes(range=[0.5, len(data["nodes"]) + 0.5])
    style(trend, 1050)

    stats = make_subplots(
        rows=3,
        cols=2,
        specs=[[{"colspan": 2}, None], [{}, {}], [{}, {}]],
        vertical_spacing=0.14,
        subplot_titles=[
            "每个小辊期总重量（真实＋虚拟）",
            "每个小辊期虚拟单个数",
            "每个小辊期虚拟单重量",
            "交货日期分布 · 原订单数量（去重）",
            "交货日期分布 · 真实重量",
        ],
    )
    names = [row["small_roll"] for row in data["chains"]]
    for key, label, color in (
        ("real_weight", "真实重量", BLUE),
        ("virtual_weight", "虚拟重量", ORANGE),
    ):
        stats.add_trace(
            go.Bar(
                x=names,
                y=[float(row[key]) for row in data["chains"]],
                name=label,
                marker_color=color,
                hovertemplate="小辊期 %{x}<br>" + label + "：%{y:.2f} 吨<extra></extra>",
            ),
            row=1,
            col=1,
        )
    for key, label in (("min_weight", "下限"), ("max_weight", "上限")):
        value = data["weight_limits"][key]
        stats.add_hline(
            y=value,
            line_dash="dot",
            line_color="#7b8999",
            row=1,
            col=1,
            annotation_text=f"{label} {value:g} 吨",
            annotation_position="top left",
        )
    for key, col, unit in (("virtual_count", 1, "个"), ("virtual_weight", 2, "吨")):
        values = [float(row[key]) for row in data["chains"]]
        stats.add_trace(
            go.Bar(
                x=names,
                y=values,
                marker_color=ORANGE,
                showlegend=False,
                text=[f"{value:g}" if value else "" for value in values],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="小辊期 %{x}<br>%{y:g} " + unit + "<extra></extra>",
            ),
            row=2,
            col=col,
        )
        stats.update_yaxes(title_text=unit, rangemode="tozero", row=2, col=col)
        stats.update_xaxes(title_text="小辊期", tickangle=-45, type="category", row=2, col=col)
    for key, col, unit, color in (
        ("source_count", 1, "个原订单", BLUE),
        ("weight", 2, "吨", GREEN),
    ):
        values = [float(row[key]) for row in data["due"]]
        stats.add_trace(
            go.Bar(
                x=[row["date"] for row in data["due"]],
                y=values,
                marker_color=color,
                showlegend=False,
                text=[f"{value:,.2f}" if key == "weight" else f"{value:g}" for value in values],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{x}<br>%{y} " + unit + "<extra></extra>",
            ),
            row=3,
            col=col,
        )
        stats.update_xaxes(type="category", tickangle=-20, row=3, col=col)
        stats.update_yaxes(title_text=unit, rangemode="tozero", row=3, col=col)
    stats.update_yaxes(title_text="总重量 / 吨", row=1, col=1)
    stats.update_xaxes(title_text="小辊期（按结果导出顺序编号）", type="category", row=1, col=1)
    stats.update_layout(barmode="stack", bargap=0.22)
    style(stats, 1350)

    days = [row["date"] for row in data["due"]]
    heat = go.Figure(
        go.Heatmap(
            x=days,
            y=names,
            z=[[float(row["due_weights"].get(day, 0)) for day in days] for row in data["chains"]],
            customdata=[[row["due_counts"].get(day, 0) for day in days] for row in data["chains"]],
            colorscale="Blues",
            colorbar=dict(title="真实重量 / 吨"),
            texttemplate="%{z:.2f}",
            textfont_size=12,
            hovertemplate="小辊期 %{y}<br>交货日期 %{x}<br>真实重量 %{z:.2f} 吨<br>本链原订单 %{customdata} 个<extra></extra>",
        )
    )
    heat.update_xaxes(type="category", side="top", title_text="交货日期")
    heat.update_yaxes(type="category", autorange="reversed", title_text="小辊期")
    style(heat, 850)
    if "boundaries" not in data:
        return trend, stats, heat
    boundaries = data["boundaries"]
    tips = [
        f"边界 {row['boundary_index']} · 绝对宽差 {row['absolute_gap']} mm"
        + "".join(
            f"<br>{label}：{escape(row[f'{side}_chain_id'])} · {escape(row[f'{side}_period'])}"
            f"<br>{escape(row[f'{side}_node_id'])} · {ROLES[row[f'{side}_role']]}"
            f" · {row[f'{side}_width']} mm"
            for side, label in (("left", "前链末节点"), ("right", "后链首节点"))
        )
        for row in boundaries
    ]
    gaps = go.Figure(
        go.Bar(
            x=[f"{index:02d}→{index + 1:02d}" for index in range(1, len(boundaries) + 1)],
            y=[float(row["absolute_gap"]) for row in boundaries],
            marker_color=[ORANGE if row["cross_period"] == "True" else BLUE for row in boundaries],
            text=[row["absolute_gap"] for row in boundaries],
            textposition="outside",
            cliponaxis=False,
            customdata=tips,
            hovertemplate="%{customdata}<extra></extra>",
        )
    )
    gaps.update_xaxes(type="category", title_text="已发布生产链序中的相邻小辊期", tickangle=-45)
    gaps.update_yaxes(title_text="绝对宽差 / mm", rangemode="tozero")
    style(gaps, 550)
    return trend, stats, heat, gaps


def write_outputs(data, output_dir):
    figures = build_figures(data)
    names = ("trends", "statistics", "delivery-heatmap")
    if "boundaries" in data:
        names += ("chain-boundaries",)
    if len(figures) != len(names):
        raise ValueError("Every report figure must have its own HTML container")
    output_dir.mkdir(parents=True, exist_ok=False)
    config = dict(
        responsive=True,
        displaylogo=False,
        scrollZoom=False,
        toImageButtonOptions=dict(format="png", scale=2),
    )
    plots = [
        figure.to_html(full_html=False, include_plotlyjs=index == 0, config=config, div_id=name)
        for index, (figure, name) in enumerate(zip(figures, names))
    ]
    fields = [
        "small_roll",
        "chain_id",
        "assigned_period",
        "start",
        "end",
        "node_count",
        "real_node_count",
        "source_count",
        "total_weight",
        "real_weight",
        "virtual_count",
        "virtual_weight",
    ]
    with (output_dir / "chain_statistics.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(data["chains"])
    with (output_dir / "delivery_distribution.csv").open(
        "x", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["date", "source_count", "weight"], lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(data["due"])
    with (output_dir / "all_order_details.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(data["nodes"][0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(data["nodes"])
    table = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(str(row[key]))}</td>"
            for key in (
                "small_roll",
                "assigned_period",
                "node_count",
                "source_count",
                "total_weight",
                "virtual_count",
                "virtual_weight",
            )
        )
        + "</tr>"
        for row in data["chains"]
    )
    options = '<option value="all">全部小辊期</option>' + "".join(
        f'<option value="{i}">小辊期 {row["small_roll"]} · {row["assigned_period"]}</option>'
        for i, row in enumerate(data["chains"])
    )
    ranges = json.dumps([[row["start"] - 0.5, row["end"] + 0.5] for row in data["chains"]])
    boundary_nav, boundary_section = "", ""
    order_note = (
        f"小辊期按结果中的 {len(data['chains'])} 条链编号，保留导出顺序；这不是跨大辊期的生产日历。"
        "竖线为链边界，边界处不连线。可框选放大、双击复原，悬停查看每个节点。"
    )
    source_note = (
        "来源：20 万次配置质量预检结果与冻结 input_orders.csv。只展示结果，不重新求解或更改规则。"
    )
    if "boundaries" in data:
        with (output_dir / "chain_boundary_detail.csv").open(
            "x", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=BOUNDARY_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(data["boundaries"])
        boundary_nav = '<a href="#boundaries">相邻链宽差</a>'
        boundary_section = f"""
<section id="boundaries"><h2>相邻小辊期首尾宽度差</h2>
<p>共 {len(data["boundaries"])} 处边界，绝对宽差合计 {data["boundary_total"]} mm，与原始指标及第{"一二三四五六七"[data["boundary_quality_position"] - 1]}项评分一致。</p>
<p class="note">按已发布生产链序逐对展示，包含虚拟端点与跨大辊期边界，不首尾闭环。橙色为跨大辊期，蓝色为同大辊期；合计降低不代表每一处都降低。</p>
<p><a href="chain_boundary_detail.csv">下载全部相邻链首尾宽差明细</a></p>{plots[3]}</section>"""
        order_note = (
            f"小辊期按已发布生产链序编号，共 {len(data['chains'])} 条链；保留期序、链序及链内节点顺序，不二次排序。"
            "横轴不是时钟或生产日历。竖线为链边界，边界处不连线。可框选放大、双击复原，悬停查看每个节点。"
        )
        source_note = "来源：新七级目标的已发布排程结果与冻结 input_orders.csv。只展示结果，不重新求解或更改规则。"
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GQGA4 排程结果可视化</title><style>
body{{margin:0;color:#26374b;background:#f5f7fa;font:15px/1.65 "PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1440px;margin:auto;padding:32px 28px}}h1{{font-size:28px;margin:0 0 8px}}h2{{font-size:21px;margin:0 0 8px}}
section{{background:white;padding:24px;margin:24px 0}}p{{margin:8px 0}}.note{{color:#556779}}nav{{display:flex;gap:20px;flex-wrap:wrap}}
a{{color:#2166ac}}select{{font:inherit;padding:7px 12px;margin-left:10px;max-width:100%}}
.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;text-align:right}}th,td{{padding:9px 12px;border-bottom:1px solid #e6ebf0;white-space:nowrap}}
th{{background:#f0f4f8}}td:first-child,th:first-child{{text-align:left}}footer{{font-size:13px;color:#556779;overflow-wrap:anywhere}}
@media(max-width:640px){{main{{padding:16px 8px}}section{{padding:16px 4px}}h1{{font-size:23px}}}}
</style></head><body><main>
<header><h1>GQGA4 排程结果</h1><p>{data["source_count"]} 个原订单 · {len(data["nodes"])} 个排程节点 · {len(data["chains"])} 个小辊期</p>
<p>真实重量 {data["real_weight"]:,.2f} 吨　虚拟单 {data["virtual_count"]} 个 / {data["virtual_weight"]:,.2f} 吨　总重量 {data["real_weight"] + data["virtual_weight"]:,.2f} 吨</p>
<nav><a href="#sequence">规格变化</a><a href="#totals">重量与交期统计</a><a href="#due">各小辊期交期分布</a><a href="#table">小辊期明细</a>{boundary_nav}</nav></header>
<section id="sequence"><h2>全部订单的规格变化</h2>
<p class="note">{order_note}</p>
<label>查看范围<select id="chain-range">{options}</select></label>
<p class="note">橙色菱形为虚拟材。温度展示允许下限与上限，不是实际炉温；虚拟温区为生成时派生值。缺温度节点 {data["missing_temperature_count"]} 个，不用零值代替缺失。</p>
{plots[0]}</section>
<section id="totals"><h2>小辊期重量、虚拟材与全局交期</h2>
<p class="note">链重包含真实订单片段与虚拟材。交期数量按原订单去重，重量按排程片段累加；虚拟材无交货日期，不计入交期分布。交期是合同日期，不推算完工或延期。</p>{plots[1]}</section>
<section id="due"><h2>各小辊期的交货日期分布</h2><p class="note">单元格为该日期真实订单的排程重量（吨）；悬停可查看本链去重后的原订单数。跨链数量不直接相加作为全局原订单数量。</p>{plots[2]}</section>{boundary_section}
<section id="table"><h2>每个小辊期的统计明细</h2>
<p><a href="chain_statistics.csv">下载小辊期统计</a> · <a href="delivery_distribution.csv">下载交期分布</a> · <a href="all_order_details.csv">下载全部 {len(data["nodes"])} 个排程节点明细</a></p>
<div class="table-wrap"><table><thead><tr><th>小辊期</th><th>所属大辊期</th><th>节点数</th><th>原订单数</th><th>总重量（吨）</th><th>虚拟个数</th><th>虚拟重量（吨）</th></tr></thead><tbody>{table}</tbody></table></div></section>
<footer><p>{source_note}</p>
<p>公开结果身份：{data["result_fingerprint"]}<br>质量报告 SHA-256：{data["report_sha256"]}</p>
<p>Python 图形库：Plotly；绘图库已内嵌，可离线查看。<a href="https://plotly.com/python/interactive-html-export/">交互报告导出说明</a></p></footer>
</main><script>
const ranges={ranges};document.getElementById('chain-range').addEventListener('change',function(){{
const range=this.value==='all'?[0.5,{len(data["nodes"]) + 0.5}]:ranges[Number(this.value)];
Plotly.relayout('trends',{{'xaxis.range':range,'xaxis2.range':range,'xaxis3.range':range}});}});
</script></body></html>"""
    (output_dir / "report.html").write_text(html, encoding="utf-8")
    manifest = dict(
        result_fingerprint=data["result_fingerprint"],
        report_sha256=data["report_sha256"],
        node_count=len(data["nodes"]),
        chain_count=len(data["chains"]),
        source_count=data["source_count"],
        real_weight=str(data["real_weight"]),
        virtual_count=data["virtual_count"],
        virtual_weight=str(data["virtual_weight"]),
        artifact_sha256={path.name: sha256(path) for path in sorted(output_dir.iterdir())},
    )
    if "boundaries" in data:
        manifest.update(
            boundary_count=len(data["boundaries"]),
            boundary_total=str(data["boundary_total"]),
        )
        if "observation_manifest_sha256" in data:
            manifest["observation_manifest_sha256"] = data["observation_manifest_sha256"]
        else:
            manifest["boundary_source"] = "audited_schedule_exports"
            manifest["boundary_quality_position"] = data["boundary_quality_position"]
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", type=Path, default=HERE.parent / "run_shared")
    parser.add_argument(
        "--input-orders", type=Path, default=ROOT / "tests/baselines/gqga4/inputs/input_orders.csv"
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=ROOT / "tests/baselines/gqga4/gqga4_rule_set_spec_six_level_historical.json",
    )
    parser.add_argument("--output-dir", type=Path, default=HERE / "report")
    args = parser.parse_args()
    if args.output_dir.exists() or args.output_dir.is_symlink():
        parser.error("output directory already exists; choose a new directory")
    data = load_data(args.result_dir, args.input_orders, args.rules)
    manifest = write_outputs(data, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
