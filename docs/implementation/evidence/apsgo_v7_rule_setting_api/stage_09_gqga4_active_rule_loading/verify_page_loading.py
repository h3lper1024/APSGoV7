#!/usr/bin/env python3
"""Static checks for the GQGA4 V7 active-rule page loading flow."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def section(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def method_body(source: str, signature: str) -> str:
    start_index = source.index(signature)
    brace_index = source.index("{", start_index)
    depth = 0
    for index in range(brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start_index : index + 1]
    raise AssertionError(f"unterminated C# method: {signature}")


def named_method(source: str, method_name: str) -> str:
    declaration = re.search(
        rf"private\s+(?:static\s+)?[^\s]+\s+{re.escape(method_name)}\s*\(",
        source,
    )
    if declaration is None:
        raise AssertionError(f"missing C# method: {method_name}")
    return method_body(source, declaration.group())


def block_after(source: str, marker: str) -> str:
    marker_index = source.index(marker)
    brace_index = source.index("{", marker_index)
    depth = 0
    for index in range(brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace_index : index + 1]
    raise AssertionError(f"unterminated C# block after: {marker}")


def verify(client_root: Path) -> dict[str, object]:
    page_path = client_root / "SchedApp/Forms/RuleConf/RuleConfFormGQGA4.cs"
    command_path = (
        client_root / "SchedApp/Forms/SchedPage/Test/GQGA4/CalcRollPosCommandGQGA4SetRules.cs"
    )
    page_bytes = page_path.read_bytes()
    command_bytes = command_path.read_bytes()
    if page_bytes.startswith(b"\xef\xbb\xbf") or command_bytes.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("changed C# sources must be UTF-8 without BOM")
    page = page_bytes.decode("utf-8")
    command = command_bytes.decode("utf-8")
    prototype_editor = (
        client_root / "SchedApp/Forms/RuleConf/VirtualSphcSpecificationsEditor.cs"
    ).read_text(encoding="utf-8-sig")

    shown = section(
        page,
        "private async void RuleConfFormGQGA4_Shown",
        "private void RuleConfFormGQGA4_FormClosed",
    )
    if page.count(".GetActiveRulesAsync(") != 1:
        raise AssertionError("the page must have exactly one V7 active-rule GET call")
    if "LoadV7ActiveRulesAsync(formLifetimeCancellation.Token)" not in shown:
        raise AssertionError("Shown must pass the form-lifetime cancellation token")
    for legacy_call in ("LoadRuleFormSpecCatalogAsync", "LoadCurrentRuleAsync"):
        if legacy_call in shown:
            raise AssertionError(f"Shown still reaches legacy loader: {legacy_call}")

    load = section(
        page,
        "private async Task LoadV7ActiveRulesAsync",
        "private void ConfigureV7RulePage",
    )
    get_index = load.index(".GetActiveRulesAsync(")
    for reset in (
        "ResetV7ActiveRuleState();",
        "SetRuleEditorEnabled(false);",
        "SetRuleOperationButtonsEnabled(false);",
    ):
        if load.index(reset) > get_index:
            raise AssertionError(f"load precondition occurs after GET: {reset}")
    apply_index = load.index("ApplyV7ActiveRules(")
    for commit in (
        "activeRulesSnapshot = response;",
        "activeRulesById = rules;",
        "currentRuleValidated = true;",
    ):
        if load.index(commit) < apply_index:
            raise AssertionError(f"successful state committed before mapping: {commit}")
    if "SetRuleOperationButtonsEnabled(true)" in load:
        raise AssertionError("stage 9 must not enable the legacy V3 save path")

    project_root = Path(__file__).resolve().parents[5]
    baseline = json.loads(
        (project_root / "tests/baselines/gqga4/gqga4_rule_set_spec.json").read_text()
    )
    expected_ids = [rule["rule_id"] for rule in baseline["rules"]]
    expected_block = re.search(r"ExpectedV7RuleIds\s*=\s*\{(?P<body>.*?)\};", page, re.DOTALL)
    if expected_block is None:
        raise AssertionError("missing ExpectedV7RuleIds")
    actual_ids = re.findall(r'"([a-z0-9_]+)"', expected_block.group("body"))
    if actual_ids != expected_ids:
        raise AssertionError("page rule identities/order differ from the V7 baseline")
    if "new Dictionary<string, ApsgoV7RuleView>(StringComparer.Ordinal)" not in page:
        raise AssertionError("rule lookup must use exact case-sensitive identifiers")
    if not re.search(
        r"BuildV7VirtualPrototypeSetting\(\s*response\.VirtualPrototypes(?:\s*,|\s*\))",
        load,
    ):
        raise AssertionError("the complete prototype response is not validated for projection")
    for cache in ("ApsgoV7ActiveRulesResponse activeRulesSnapshot", "activeRulesById"):
        if cache not in page:
            raise AssertionError(f"missing complete page cache: {cache}")

    prototype_projection = method_body(
        page, "private static JObject BuildV7VirtualPrototypeSetting"
    )
    for unsupported_shape in (
        "prototype.UnitWeight != unitWeight",
        "!pairs.Add(",
        "pairs.Count != widths.Count * thicknesses.Count",
    ):
        unsupported_block = block_after(prototype_projection, unsupported_shape)
        if "throw new" in unsupported_block or "return null;" not in unsupported_block:
            raise AssertionError(
                "a valid prototype snapshot that the legacy editor cannot represent must not fail the page"
            )

    active_rule_validation = method_body(
        page, "private static Dictionary<string, ApsgoV7RuleView> ValidateV7ActiveRules"
    )
    if "RequireV7Array(rules" in active_rule_validation:
        raise AssertionError("disabled rules must not unconditionally reject empty arrays")
    enabled_array_validation = method_body(page, "private static void ValidateV7ArrayWhenEnabled")
    if ".Enabled" not in enabled_array_validation:
        raise AssertionError("array content validation must be conditional on rule enabled state")
    thickness_validation = method_body(page, "private static void ValidateV7ThicknessRule")
    if "ranges.Count == 0" in thickness_validation and "!rule.Enabled" not in thickness_validation:
        raise AssertionError("a disabled thickness rule must allow an empty ranges array")

    apply_v7 = method_body(page, "private void ApplyV7ActiveRules")
    numeric_apply_methods = re.findall(r"\b(ApplyV7[A-Za-z0-9_]+)\(", apply_v7)
    for method_name in dict.fromkeys(numeric_apply_methods):
        if method_name == "ApplyV7FixedLineRules":
            continue
        apply_method = named_method(page, method_name)
        for forbidden in ("ReadDouble(", "Math.Round(", 'ToString("0.###"'):
            if forbidden in apply_method:
                raise AssertionError(
                    f"V7 loading converts or truncates a business decimal in {method_name}: {forbidden}"
                )
        if re.search(r"\bdouble\??\b", apply_method):
            raise AssertionError(
                f"V7 loading converts a business decimal to double in {method_name}"
            )
    decimal_reader = named_method(page, "ReadV7NullableDecimal")
    if re.search(r"\bdouble\b", decimal_reader) or "ReadDouble(" in decimal_reader:
        raise AssertionError("the V7 decimal reader must retain Decimal precision")
    thickness_row_writer = named_method(page, "AddThicknessRuleRow")
    if re.search(r"\bdouble\b", thickness_row_writer):
        raise AssertionError("the V7 thickness grid must retain Decimal precision")
    for column_name in ("min", "max", "tolerance"):
        if f'thicknessRuleTable.Columns.Add("{column_name}", typeof(decimal))' not in page:
            raise AssertionError(f"the thickness {column_name} column must store Decimal values")

    if "virtualSphcSpecificationsEditor.ApplySetting(" in apply_v7:
        if re.search(r"\bdouble\b", prototype_editor):
            raise AssertionError(
                "V7 prototype loading must not pass Decimal values through the legacy double editor path"
            )

    thickness_call = re.search(
        r"(?P<method>ApplyV7[A-Za-z0-9_]+)\(\s*"
        r'rules\["thickness_jump_limit"\](?:\.Parameters)?\s*\)',
        apply_v7,
    )
    if thickness_call is None:
        raise AssertionError("missing thickness rule control mapping")
    thickness_apply = named_method(page, thickness_call.group("method"))
    basis_read = re.search(
        r"(?:string|var)\s+(?P<value>\w+)\s*=\s*"
        r'(?:rule\.Parameters|parameters)\["basis"\][^;]*;',
        thickness_apply,
    )
    if basis_read is None or not re.search(
        rf"\w*[Bb]asis\w*\.(?:EditValue|Text)\s*=\s*[^;]*\b{basis_read.group('value')}\b",
        thickness_apply[basis_read.end() :],
    ):
        raise AssertionError("thickness basis is not accurately echoed from the V7 response")
    if not re.search(
        r"ApplyV7DecimalText\(\s*\w*[Ff]allback\w*\s*,\s*"
        r'(?:rule\.Parameters|parameters)\["fallback_tolerance"\]\s*\)',
        thickness_apply,
    ):
        raise AssertionError(
            "thickness fallback_tolerance is not accurately echoed from the V7 response"
        )

    reverse_width_call = re.search(
        r"(?P<method>ApplyV7[A-Za-z0-9_]+)\(\s*"
        r'rules\["reverse_width_limit"\]\.Parameters\s*\)',
        apply_v7,
    )
    if reverse_width_call is None:
        raise AssertionError("missing reverse-width rule control mapping")
    reverse_width_apply = named_method(page, reverse_width_call.group("method"))
    if not re.search(
        r"ApplyV7DecimalText\(\s*\w*[Vv]irtual\w*[Ww]idth\w*[Tt]olerance\w*\s*,\s*"
        r'(?:rule\.Parameters|parameters)\["virtual_width_tolerance"\]\s*\)',
        reverse_width_apply,
    ):
        raise AssertionError("virtual_width_tolerance has no explicit control mapping")

    for lifecycle in (
        "FormClosing += RuleConfFormGQGA4_FormClosing;",
        "formLifetimeCancellation.Cancel();",
        "v7RuleApiClient?.Dispose();",
        "formLifetimeCancellation.Dispose();",
    ):
        if lifecycle not in page:
            raise AssertionError(f"missing form lifecycle action: {lifecycle}")
    for disabled_action in (
        "saveRuleDraftBTN.Visible = false;",
        "activateRuleBTN.Visible = false;",
        "SetRuleOperationButtonsEnabled(false);",
    ):
        if disabled_action not in page:
            raise AssertionError(f"legacy save action remains reachable: {disabled_action}")

    dialog_index = command.index("dialog.ShowDialog() != DialogResult.OK")
    database_index = command.index("DatabaseService.CreateDbClient()")
    insert_index = command.index("db.Insertable(schedRecords).ExecuteCommand();")
    completed_index = command.index("changesSchedRecords = true;")
    if not dialog_index < database_index < insert_index < completed_index:
        raise AssertionError("schedule-step records can change before a successful dialog result")
    for command_guard in (
        "public override bool ChangesSchedRecords => changesSchedRecords;",
        "changesSchedRecords = false;",
    ):
        if command_guard not in command:
            raise AssertionError(f"missing command completion guard: {command_guard}")

    return {
        "client_root": str(client_root),
        "v7_get_calls": page.count(".GetActiveRulesAsync("),
        "expected_rule_count": len(actual_ids),
        "complete_snapshot_cached": True,
        "prototype_snapshot_preserved": True,
        "unsupported_prototype_projection_is_nonfatal": True,
        "disabled_rule_empty_arrays_supported": True,
        "thickness_basis_and_fallback_echoed": True,
        "v7_decimal_projection_is_exact": True,
        "virtual_width_tolerance_mapped": True,
        "close_cancels_request": True,
        "cancel_does_not_advance_step": True,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-root", required=True, type=Path)
    print(
        json.dumps(
            verify(parser.parse_args().client_root.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
