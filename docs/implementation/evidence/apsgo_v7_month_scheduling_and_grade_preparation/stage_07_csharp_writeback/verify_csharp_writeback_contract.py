#!/usr/bin/env python3
"""Verify the C# V7 month-solve writeback and SQLite warning contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def read_utf8(path: Path) -> str:
    payload = path.read_bytes()
    if payload.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"UTF-8 BOM is not allowed: {path}")
    return payload.decode("utf-8")


def require(condition: bool, message: str, checks: list[str]) -> None:
    if not condition:
        raise AssertionError(message)
    checks.append(message)


def require_text(source: str, value: str, label: str, checks: list[str]) -> None:
    require(value in source, f"{label}: missing {value!r}", checks)


def require_order(
    source: str,
    values: list[str],
    label: str,
    checks: list[str],
) -> None:
    positions = [source.find(value) for value in values]
    require(
        all(position >= 0 for position in positions)
        and positions == sorted(positions)
        and len(set(positions)) == len(positions),
        f"{label}: required order not found",
        checks,
    )


def section(source: str, start: str, end: str) -> str:
    start_at = source.find(start)
    end_at = source.find(end, start_at + len(start))
    if start_at < 0 or end_at < 0:
        raise AssertionError(f"cannot locate C# section: {start!r} .. {end!r}")
    return source[start_at:end_at]


def verify_service(service: str, checks: list[str]) -> None:
    for value, label in (
        ("internal sealed class ApsgoV7SchedRecordSolveService", "V7 专属回写服务"),
        ("ApsgoV7RuleApiClient ruleApiClient", "V7 规则客户端依赖"),
        ("ApsgoV7SchedulingApiClient schedulingApiClient", "V7 求解客户端依赖"),
        ("public ApsgoV7MonthSolveResponse RunGqga4Month(", "GQGA4 月计划入口"),
    ):
        require_text(service, value, label, checks)
    require(
        "PipelineV3" not in service and "BackendAlgorithm" not in service,
        "V7 回写服务不依赖 V3 求解服务",
        checks,
    )

    run = section(service, "public ApsgoV7MonthSolveResponse RunGqga4Month(", "private static PreparedSolve BuildPreparedSolve(")
    require_order(
        run,
        [
            ".GetActiveRulesAsync(cancellationToken)",
            "BuildPreparedSolve(records, activeRules.ActiveVersionId)",
            ".SolveMonthAsync(prepared.Request, cancellationToken)",
            "BuildResultRecords(",
            "cancellationToken.ThrowIfCancellationRequested();",
            "ReplaceStepRecords(",
            "return response;",
        ],
        "规则快照、求解、完整转换、取消边界、事务回写依次执行",
        checks,
    )

    prepared = section(service, "private static PreparedSolve BuildPreparedSolve(", "private static List<SchedRecord> BuildResultRecords(")
    for value, label in (
        ("ExpectedActiveVersionId = activeVersionId", "请求绑定当前启用规则版本"),
        ("RequestId = Guid.NewGuid().ToString(\"D\")", "每次求解生成独立请求标识"),
        ("SourceOrderId = sourceOrderId", "合同号映射到来源订单标识"),
        ("SourcePeriod = periodId", "预设大辊期映射到来源计划期"),
        ("IsVirtual = false", "C# 原始输入明确为真实订单"),
        ("Weight = weight", "连镀欠交映射到求解重量"),
        ("Grade = RequiredText(record.Grade", "牌号字段映射"),
        ("GradeClass = TextOrNull(record.GradeClass)", "牌号类别字段映射"),
        ("HotRollGrade = TextOrNull(record.HMGrade)", "热轧牌号字段映射"),
        ("Width = PositiveDecimalOrNull(record.Width", "宽度字段映射"),
        ("Thickness = PositiveDecimalOrNull(record.Thickness", "厚度字段映射"),
        ("MinTemperature = PositiveDecimalOrNull(record.MinTemperature", "温区下限字段映射"),
        ("MaxTemperature = PositiveDecimalOrNull(record.MaxTemperature", "温区上限字段映射"),
        ("CustomerGrade = TextOrNull(record.CustomerGrade)", "客户等级字段映射"),
        ("record.TerminalCustomerName", "终端客户名称候选字段映射"),
        ("record.StrategicCustomer", "战略客户名称候选字段映射"),
        ("record.CustomerName", "客户名称候选字段映射"),
        ("ExecutionStandard = TextOrNull(record.ExecutionStandard)", "执行标准字段映射"),
        ("SurfaceGrade = TextOrNull(record.SurfaceGrade, record.SurfaceQuality)", "表面等级字段映射"),
        ("if (bySourceId.ContainsKey(sourceOrderId))", "重复来源整批拒绝"),
        ("Periods = periods", "有序计划期写入请求"),
        ("Orders = bindings.Select(binding => binding.Order).ToList()", "有效订单写入请求"),
    ):
        require_text(prepared, value, label, checks)
    require(
        re.search(r"\.Distinct\(\)\s*\.OrderBy\(number => number\)", prepared) is not None,
        "预设大辊期按数值去重并升序",
        checks,
    )

    coating_weight = section(service, "private static bool TryReadCoatingWeight(", "private static bool IsVirtualInput(")
    require_text(
        coating_weight,
        "record?.CoatingShortage?.Trim()",
        "仅从连镀欠交读取待排重量",
        checks,
    )
    require(
        all(
            value not in coating_weight
            for value in (
                "record.Weight",
                "record.ActualWeight",
                "record.CastingShortage",
                "record.HotRollShortage",
                "record.TemperShortage",
                "record.PickleShortage",
            )
        ),
        "待排重量不回退到其他重量或欠交字段",
        checks,
    )

    period_parser = section(service, "private static int ParsePresetBigRollNumber(", "private static string FormatPeriodId(")
    for value, label in (
        ("record?.RollPos?.Trim()", "从 RollPos 读取预设大辊期"),
        ("parts.Length == 1 || parts.Length == 3", "兼容单段及三段 RollPos"),
        ("parts[0].Trim()", "使用 RollPos 第一段作为大辊期"),
    ):
        require_text(period_parser, value, label, checks)
    require_text(service, 'return PeriodPrefix + bigRollNumber.ToString("D8"', "大辊期规范化为 BR 八位编号", checks)

    publishable = section(service, "private static void ValidatePublishableResponse(", "private static void ValidateCommonRow(")
    for value, label in (
        ('"success"', "允许成功状态"),
        ('"publishable_with_allowed_deviation"', "允许规则声明的可发布偏差状态"),
        ("!response.Publishable", "结果必须可发布"),
        ("!response.AuditSummary.Passed", "结果审计必须通过"),
        ("response.Rows.Count == 0", "拒绝空结果"),
        ("response.PreparationReport.InputOrderCount != prepared.Request.Orders.Count", "软硬钢准备数量一致"),
    ):
        require_text(publishable, value, label, checks)

    result = section(service, "private static List<SchedRecord> BuildResultRecords(", "private static void ValidatePublishableResponse(")
    for value, label in (
        ("var separatorCountByPartition = new Dictionary<string, int>", "按拆单分区统计隔离材"),
        ("foreach (ApsgoV7MonthSolveRow row in response.Rows)", "按服务返回顺序逐行转换"),
        ("row.ChainSequence != expectedChainSequence", "链序连续性校验"),
        ("row.NodeSequence != expectedNodeSequence", "节点序连续性校验"),
        ("ValidateVirtualRow(row, separatorCountByPartition)", "虚拟材谱系和隔离材数量采集"),
        ("ValidateRealRow(row, source, partitionSources)", "真实材及拆单谱系校验"),
        ("separatorCountByPartition);", "隔离材计数进入来源覆盖守恒校验"),
        ("resultRecords.Count != response.Rows.Count", "结果行无静默丢失校验"),
    ):
        require_text(result, value, label, checks)

    virtual_row = section(service, "private static void ValidateVirtualRow(", "private static void ValidateRealRow(")
    for value, label in (
        ("Dictionary<string, int> separatorCountByPartition", "虚拟材校验接收分区计数器"),
        ('string.Equals(lineage.Purpose, "split_separator"', "仅拆单隔离材参与分区计数"),
        ("lineage.RelatedPartitionId = partitionId", "隔离材分区标识先规范化"),
        ("separatorCountByPartition.TryGetValue(", "同分区隔离材累计计数"),
        ("? currentCount + 1", "已有分区隔离材计数递增"),
        (": 1;", "新分区隔离材计数从一开始"),
    ):
        require_text(virtual_row, value, label, checks)

    source_coverage = section(service, "private static void ValidateSourceCoverage(", "private static SchedRecord CreateRealRecord(")
    for value, label in (
        ("rows.Count == 0", "每个来源订单都必须出现"),
        ("rows.Sum(row => row.Weight) != source.Order.Weight", "拆片重量守恒"),
        ("rows.Select(row => row.SoftHardClass", "拆片软硬钢分类一致"),
        ("rows.Any(row => row.SplitLineage == null)", "禁止拆片和未拆节点混用"),
        ("lineages.Any(lineage => !SameSplitPartition(lineage, firstLineage))", "同分区拆片完整谱系一致"),
        ("SequenceEqual(Enumerable.Range(1, rows.Count))", "拆片序号完整"),
        ("separatorCountByPartition.TryGetValue(", "读取每个拆单分区的隔离材数量"),
        ("separatorCount != rows.Count - 1", "隔离材数量必须等于拆片数量减一"),
        ("!partitionSources.ContainsKey(partitionId)", "虚拟分隔材引用有效拆单分区"),
    ):
        require_text(source_coverage, value, label, checks)
    same_partition = section(service, "private static bool SameSplitPartition(", "private static SchedRecord CreateRealRecord(")
    for field in (
        "PartitionId",
        "ParentNodeId",
        "ParentSourceOrderId",
        "SourceResourceId",
        "SourcePeriod",
        "OriginAssignedPeriod",
        "SplitMode",
        "TargetAssignedPeriod",
        "AcceptedSourceSequence",
        "ParentWeight",
        "PieceCount",
        "AuthorizationRuleId",
        "AuthorizationRuleVersion",
        "AuthorizationDecisionFingerprint",
        "ReasonCode",
    ):
        require_text(
            same_partition,
            f"left.{field}",
            f"同分区拆片比较 {field}",
            checks,
        )

    for value, label in (
        ('classification, "软钢"', "接受软钢分类"),
        ('classification, "硬钢"', "接受硬钢分类"),
        ("result.SoftOrHard = row.SoftHardClass", "软硬钢分类回写"),
        ("ParsePeriodNumber(row.AssignedPeriod)", "分配计划期回写为 RollPos 大辊期"),
        ("result.RollSeq = row.NodeSequence", "节点序回写为 RollSeq"),
        ('AddWarning(warnings, "WidthWarning"', "宽度告警写入"),
        ('AddWarning(warnings, "ThicknessWarning"', "厚度告警写入"),
        ('AddWarning(warnings, "TemperatureWarning"', "温度告警写入"),
        ('AddWarning(warnings, "ChainWarning"', "链告警写入"),
        ('["V7Metadata"] = metadata', "V7 求解身份元数据写入"),
    ):
        require_text(service, value, label, checks)

    transaction = section(service, "private static void ReplaceStepRecords(", "private static string BuildWarningsJson(")
    require_order(
        transaction,
        [
            "db.Ado.BeginTran();",
            "db.Deleteable<SchedRecord>",
            "db.Insertable(records).ExecuteCommand()",
            "db.Queryable<SchedRecord>()",
            "string.Equals(expected, record.Warnings, StringComparison.Ordinal)",
            "db.Ado.CommitTran();",
            "db.Ado.RollbackTran();",
            "throw;",
        ],
        "删除、插入、逐字回读、提交及失败回滚顺序",
        checks,
    )
    require(
        "GetActiveRulesAsync" not in transaction and "SolveMonthAsync" not in transaction,
        "数据库事务内没有远程接口调用",
        checks,
    )
    require(
        "CancellationToken" not in transaction
        and "ThrowIfCancellationRequested" not in transaction,
        "取消只在事务开始前生效，不中断原子替换",
        checks,
    )


def verify_command(command: str, checks: list[str]) -> None:
    for value, label in (
        ("public override bool RunInBackground => true", "GQGA4 V7 求解在后台运行"),
        ("public override bool ChangesSchedRecords => changesSchedRecords", "记录变更标志由结果控制"),
        ('r.Step == "配置规则"', "从配置规则步骤读取整批输入"),
        ("r.Version == Version", "整批输入限定所选版本"),
        ("r.ProductLine == ProductLine", "整批输入限定所选产线"),
        (".OrderBy(r => r.RollSeq)", "整批输入先按 RollSeq 排序"),
        (".ThenBy(r => r.Id)", "整批输入再按 Id 稳定排序"),
        ("new ApsgoV7RuleApiClient()", "使用 V7 规则客户端"),
        ("new ApsgoV7SchedulingApiClient()", "使用 V7 求解客户端"),
        ("new ApsgoV7SchedRecordSolveService(", "使用 V7 专属回写服务"),
    ):
        require_text(command, value, label, checks)
    require(
        "selectedIds.Contains" not in command,
        "选中记录只定位版本，不把求解输入缩减为选中子集",
        checks,
    )
    require(
        "PipelineV3" not in command and "BackendAlgorithm" not in command,
        "GQGA4 命令不调用 V3 求解链",
        checks,
    )
    require_order(
        command,
        [
            "changesSchedRecords = false;",
            ".ToList()",
            ".OrderBy(r => r.RollSeq)",
            ".ThenBy(r => r.Id)",
            "solveService.RunGqga4Month(",
            "changesSchedRecords = true;",
        ],
        "命令仅在 V7 事务回写成功返回后标记记录已变化",
        checks,
    )
    require("catch" not in command, "命令不吞掉服务异常，由共享框架统一处理", checks)


def verify_shared_view(view: str, checks: list[str]) -> None:
    require_order(
        view,
        [
            "List<long> selectedIds = (SelectedIds ?? Enumerable.Empty<long>()).ToList();",
            "if (selectedIds.Count == 0)",
            "return;",
            "selectedIds.First()",
        ],
        "共享命令入口在读取首个选择项前友好拦截空选择",
        checks,
    )
    completion = section(view, "Action commandCompleted = () =>", "if (command.RunInBackground)")
    for value, label in (
        ("if (command.ChangesSchedRecords)", "仅实际变更记录后推进步骤"),
        ("CurrStep = command.Text", "成功后推进到命令步骤"),
        ("SchedRecordChanged?.Invoke(command)", "成功后通知页面刷新"),
    ):
        require_text(completion, value, label, checks)
    require_order(
        view,
        [
            "await Task.Run(executeCommand);",
            "commandCompleted();",
            "catch (Exception ex)",
        ],
        "后台求解成功后推进，异常交给共享页面提示",
        checks,
    )


def verify_summary(summary: str, checks: list[str]) -> None:
    condition = section(summary, "bool shouldParseWarningMessages =", "DateTime? endTime")
    for value, label in (
        ("parseWarningMessages", "保留调用方原告警解析开关"),
        ('string.Equals(productLine, "GQGA4"', "GQGA4 强制解析结构化告警"),
        ('string.Equals(productLine, "四镀锌"', "四镀锌中文名强制解析结构化告警"),
    ):
        require_text(condition, value, label, checks)
    for value, label in (
        ("? orderedRecords.Count(HasWarningMessages)", "目标产线只统计真实告警字段"),
        (": orderedRecords.Count(r => !string.IsNullOrWhiteSpace(r.Warnings))", "其他产线保留非空文本计数"),
        ("return ReadWarningProperties(record).Any();", "告警计数复用统一解析器"),
        ('JObject warnings = root["Warnings"] as JObject;', "V7Metadata 不计为业务告警"),
    ):
        require_text(summary, value, label, checks)


def verify_project(project_xml: str, checks: list[str]) -> None:
    root = ET.fromstring(project_xml)
    namespace = {"msb": "http://schemas.microsoft.com/developer/msbuild/2003"}
    includes = [
        item.attrib["Include"]
        for item in root.findall(".//msb:Compile", namespace)
    ]
    expected = (
        r"Forms\SchedPage\Services\ApsgoV7SchedRecordSolveService.cs",
        r"Forms\SchedPage\Test\GQGA4\CalcRollPosCommandGQGA4RequestSolution.cs",
        r"Forms\SchedPage\Test\SchedPageRollSummaryBuilder.cs",
        r"Forms\SchedPage\SchedPageView.cs",
        "ApsgoV7Configuration.cs",
        "ApsgoV7RuleApiClient.cs",
        "ApsgoV7SchedulingApiClient.cs",
    )
    for include in expected:
        require(
            includes.count(include) == 1,
            f"C# 项目恰好编译一次 {include}",
            checks,
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_warning_round_trip(source_database: Path) -> dict[str, object]:
    if not source_database.is_file():
        raise AssertionError(f"missing source database: {source_database}")
    source_hash_before = sha256(source_database)

    warning_text = "链间宽差与拆单谱系复核：" + "逐字保存，不允许截断。" * 64
    payload = json.dumps(
        {
            "Warnings": {
                "WidthWarning": warning_text,
                "ThicknessWarning": "厚度告警验证",
                "TemperatureWarning": "温区告警验证",
                "ChainWarning": "链级告警验证",
            },
            "V7Metadata": {
                "contract_version": "apsgo-v7-month-solve/1",
                "request_id": "00000000-0000-4000-8000-000000000707",
                "status": "success",
                "bound_result_fingerprint": "a" * 64,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload) <= 255:
        raise AssertionError("warning test payload must exceed 255 characters")

    with tempfile.TemporaryDirectory(prefix="apsgo_v7_stage07_") as directory:
        copied_database = Path(directory) / source_database.name
        shutil.copy2(source_database, copied_database)
        copy_hash_before = sha256(copied_database)

        connection = sqlite3.connect(copied_database)
        try:
            columns = {
                row[1]: row
                for row in connection.execute('PRAGMA table_info("SchedRecord")')
            }
            if "Warnings" not in columns:
                raise AssertionError("SchedRecord.Warnings column is missing")
            declared_type = str(columns["Warnings"][2])
            selected = connection.execute(
                'SELECT "Id" FROM "SchedRecord" ORDER BY "Id" LIMIT 1'
            ).fetchone()
            if selected is None:
                raise AssertionError("SchedRecord has no row for the round-trip check")
            record_id = selected[0]
            cursor = connection.execute(
                'UPDATE "SchedRecord" SET "Warnings" = ? WHERE "Id" = ?',
                (payload, record_id),
            )
            if cursor.rowcount != 1:
                raise AssertionError(f"expected one updated row, got {cursor.rowcount}")
            connection.commit()
        finally:
            connection.close()

        connection = sqlite3.connect(copied_database)
        try:
            persisted = connection.execute(
                'SELECT "Warnings" FROM "SchedRecord" WHERE "Id" = ?',
                (record_id,),
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()

        if persisted is None or persisted[0] != payload:
            raise AssertionError("Warnings did not survive the temporary SQLite round trip exactly")
        if integrity != ("ok",):
            raise AssertionError(f"temporary SQLite integrity check failed: {integrity!r}")
        copy_hash_after = sha256(copied_database)
        if copy_hash_before == copy_hash_after:
            raise AssertionError("temporary database did not record the warning update")

    source_hash_after = sha256(source_database)
    if source_hash_after != source_hash_before:
        raise AssertionError("source SchedDatas.db changed during the temporary-copy test")

    return {
        "source_database": str(source_database),
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "source_unchanged": True,
        "warnings_declared_type": declared_type,
        "payload_characters": len(payload),
        "payload_utf8_bytes": len(payload.encode("utf-8")),
        "round_trip_exact": True,
        "temporary_database_integrity": "ok",
    }


def verify_csharp(csharp_root: Path) -> list[str]:
    project = csharp_root / "SchedApp"
    paths = {
        "service": project / "Forms/SchedPage/Services/ApsgoV7SchedRecordSolveService.cs",
        "command": project / "Forms/SchedPage/Test/GQGA4/CalcRollPosCommandGQGA4RequestSolution.cs",
        "view": project / "Forms/SchedPage/SchedPageView.cs",
        "summary": project / "Forms/SchedPage/Test/SchedPageRollSummaryBuilder.cs",
        "project": project / "SchedApp.csproj",
    }
    checks: list[str] = []
    sources = {name: read_utf8(path) for name, path in paths.items()}
    verify_service(sources["service"], checks)
    verify_command(sources["command"], checks)
    verify_shared_view(sources["view"], checks)
    verify_summary(sources["summary"], checks)
    verify_project(sources["project"], checks)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify APSGo V7 C# month-solve writeback contracts."
    )
    parser.add_argument(
        "--csharp-root",
        type=Path,
        required=True,
        help="Root of the aps-code-0806 checkout.",
    )
    args = parser.parse_args()
    csharp_root = args.csharp_root.resolve()

    try:
        checks = verify_csharp(csharp_root)
        database = verify_warning_round_trip(
            csharp_root / "SchedApp/Data/SchedDatas.db"
        )
    except Exception as error:
        print(
            json.dumps(
                {"status": "fail", "error": str(error)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "status": "pass",
                "csharp_root": str(csharp_root),
                "static_check_count": len(checks),
                "database_check": database,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
