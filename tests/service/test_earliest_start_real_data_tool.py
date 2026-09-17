"""The acceptance importer must not count exported split pieces twice."""
import csv
from decimal import Decimal

import pytest

from tools.verify_earliest_start_real_data import plan_differences, read_export


def files(tmp_path, mutation=None):
    row = dict.fromkeys(("交货日期", "牌号", "热轧牌号", "执行标准", "表面等级", "客户等级", "钢种大类"), "same")
    row.update({key: "100" for key in ("连镀欠交", "宽度", "厚度", "均热段温度最小值", "均热段温度最大值", "炉区速度")})
    row.update({"合同完全号": "001", "source_order_id": "001", "source_period": "BR_6",
        "战略客户名称": "customer", "客户": "customer", "虚拟材": "否", "排产重量 / t": "40",
        "最早连镀时间": "'2026-06-02 00:00:00", "连镀开始时间": "'2026-06-01 00:00:00"})
    baseline = dict(row)
    second = {**row, "排产重量 / t": "60"}
    if mutation:
        mutation(second)
    virtual = {**row, "合同完全号": "virtual", "虚拟材": "是", "最早连镀时间": ""}
    source, frozen = tmp_path / "export.csv", tmp_path / "baseline.csv"
    for path, rows in ((source, [row, second, virtual]), (frozen, [baseline])):
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerows(rows)
    return source, frozen


def test_split_original_restored_once_and_virtual_excluded(tmp_path):
    selected, report = read_export(*files(tmp_path))
    assert len(selected) == 1
    assert selected[0][0]["source_period"] == "BR_6"
    assert selected[0][2] == "2026-06-02T00:00:00+08:00"
    assert report["real_nodes"] == 2 and report["virtual_nodes"] == 1
    assert report["original_weight"] == Decimal(100)
    assert report["historical_early_nodes"] == 2


@pytest.mark.parametrize("field,value", [("排产重量 / t", "61"), ("最早连镀时间", ""),
    ("宽度", "101"), ("合同完全号", "002"), ("虚拟材", "?")])
def test_conflicting_export_is_not_silently_repaired(tmp_path, field, value):
    with pytest.raises(ValueError):
        read_export(*files(tmp_path, lambda row: row.update({field: value})))


def test_plan_diff_retains_order_values_lengths_and_identity_changes():
    assert plan_differences({"x": [1, 2]}, {"x": [2, 1]}) == [
        {"path": "plan.x[0]", "old": 1, "new": 2},
        {"path": "plan.x[1]", "old": 2, "new": 1}]
    assert plan_differences([1], [1, 2]) == [{"path": "plan", "old_length": 1, "new_length": 2}]
    assert plan_differences({"partition_id": "a"}, {"partition_id": "b"})
    assert plan_differences({"same": [1, None]}, {"same": [1, None]}) == []
