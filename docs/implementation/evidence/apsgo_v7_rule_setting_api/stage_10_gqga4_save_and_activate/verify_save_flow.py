#!/usr/bin/env python3
"""Static checks for the GQGA4 V7 save-and-activate flow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

EXPECTED_RULE_COUNT = 17
USER_GITIGNORE_SHA256 = "8d83935bf7d0b0cefa4909e338427b956c6db20dc360204f32421da3ea97cfc9"


def read_source(path: Path) -> str:
    source = path.read_bytes()
    if source.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"changed source must be UTF-8 without BOM: {path}")
    return source.decode("utf-8")


def method_body(source: str, method_name: str) -> str:
    declaration = re.search(
        rf"(?:private|public|internal)\s+(?:static\s+)?(?:async\s+)?[^\s]+\s+"
        rf"{re.escape(method_name)}\s*\(",
        source,
    )
    if declaration is None:
        raise AssertionError(f"missing C# method: {method_name}")
    brace_index = source.index("{", declaration.start())
    depth = 0
    for index in range(brace_index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[declaration.start() : index + 1]
    raise AssertionError(f"unterminated C# method: {method_name}")


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


def require_all(source: str, snippets: tuple[str, ...], message: str) -> None:
    missing = [snippet for snippet in snippets if snippet not in source]
    if missing:
        raise AssertionError(f"{message}: {missing}")


def verify(client_root: Path) -> dict[str, object]:
    page_path = client_root / "SchedApp/Forms/RuleConf/RuleConfFormGQGA4.cs"
    designer_path = client_root / "SchedApp/Forms/RuleConf/RuleConfFormGQGA4.designer.cs"
    client_path = client_root / "SchedApp/ApsgoV7RuleApiClient.cs"
    page = read_source(page_path)
    designer = read_source(designer_path)
    client = read_source(client_path)

    save = method_body(page, "rule_config_save")
    if page.count(".SetActiveRulesAsync(") != 1 or save.count(".SetActiveRulesAsync(") != 1:
        raise AssertionError("the page save path must issue exactly one V7 POST")
    if not re.search(r"\.SetActiveRulesAsync\(\s*request\s*,", save):
        raise AssertionError("the V7 POST must send the retained request instance")
    forbidden_legacy = (
        "PipelineV3",
        "pipelineV3",
        "SaveRuleDraft",
        "ActivateRule",
        "PreviewRule",
        "FormSpecCatalog",
        "GetCurrentRule",
        "saveRuleDraftBTN",
        "activateRuleBTN",
        "保存草稿",
        "启用草稿",
    )
    legacy_hits = [item for item in forbidden_legacy if item in page or item in designer]
    if legacy_hits:
        raise AssertionError(f"legacy V3 rule flow remains reachable: {legacy_hits}")

    expected_block = re.search(r"ExpectedV7RuleIds\s*=\s*\{(?P<body>.*?)\};", page, re.DOTALL)
    if expected_block is None:
        raise AssertionError("missing ExpectedV7RuleIds")
    expected_ids = re.findall(r'"([a-z0-9_]+)"', expected_block.group("body"))
    if len(expected_ids) != EXPECTED_RULE_COUNT or len(set(expected_ids)) != EXPECTED_RULE_COUNT:
        raise AssertionError("the page must declare 17 unique GQGA4 rule identifiers")

    build_request = method_body(page, "TryBuildV7SaveRequest")
    require_all(
        build_request,
        (
            "activeRulesSnapshot.Rules.Count != ExpectedV7RuleIds.Length",
            ".Select(CloneV7EditableRule)",
            ".ToList()",
            "Rules = rules",
            "ExpectedActiveVersionId = activeRulesSnapshot.ActiveVersionId",
            'SaveOperationId = Guid.NewGuid().ToString("D")',
        ),
        "save request must clone the complete active snapshot",
    )
    if ".OrderBy(" in build_request:
        raise AssertionError("save request must preserve the active snapshot rule order")
    if not re.search(
        r"activeRulesSnapshot\.Rules\s*\.Select\(CloneV7EditableRule\)\s*\.ToList\(\)",
        build_request,
    ):
        raise AssertionError("all editable rules must be cloned directly from the snapshot list")
    clone_rule = method_body(page, "CloneV7EditableRule")
    require_all(
        clone_rule,
        (
            "RuleId = source.RuleId",
            "Enabled = source.Enabled",
            "Parameters = (JObject)source.Parameters.DeepClone()",
        ),
        "editable rules must deep-clone identity, state, and parameters",
    )

    require_all(
        page,
        (
            "private ApsgoV7SetActiveRulesRequest pendingSaveRequest;",
            "ApsgoV7SetActiveRulesRequest request = pendingSaveRequest;",
            "pendingSaveRequest = request;",
        ),
        "uncertain retries must retain the exact request",
    )
    if save.index("pendingSaveRequest") > save.index("TryBuildV7SaveRequest"):
        raise AssertionError("retry must reuse a pending request before building a new UUID")
    generic_failure = block_after(save, "catch (Exception ex)")
    if "pendingSaveRequest = null" in generic_failure:
        raise AssertionError("an uncertain transport result must retain the request for replay")
    api_failure = block_after(save, "catch (ApsgoV7RuleApiException ex)")
    require_all(
        api_failure,
        ("(int)ex.StatusCode >= 500", "if (!retrySameOperation)"),
        "server failures must retain the pending request for exact replay",
    )

    conflict = block_after(save, "ex.StatusCode == HttpStatusCode.Conflict")
    require_all(
        conflict,
        ("MessageBoxButtons.YesNo", "MessageBoxDefaultButton.Button2"),
        "409 handling must preserve edits unless reload is explicitly confirmed",
    )
    invalid = block_after(save, "(int)ex.StatusCode == 422")
    if "LoadV7ActiveRulesAsync" in invalid or "ApplyV7ActiveRules" in invalid:
        raise AssertionError("422 handling must retain the current editor values")
    require_all(
        invalid,
        ("FocusV7RuleIssue(ex);", "当前编辑值已保留"),
        "422 handling must expose field errors without replacing editor values",
    )

    inactive = block_after(save, "if (!response.SavedVersionIsActive)")
    if "return;" not in inactive or "DialogResult.OK" in inactive:
        raise AssertionError("a non-active idempotent replay must refresh but not advance")
    if "!response.SavedVersionIsActive && !response.IdempotentReplay" not in save:
        raise AssertionError("a non-active save result is valid only for an idempotent replay")
    if "response.PreviousActiveVersionId != request.ExpectedActiveVersionId" not in save:
        raise AssertionError("the save response must match the request base version")
    if page.count("DialogResult = DialogResult.OK;") != 1:
        raise AssertionError("the form must have one successful completion point")
    dialog_index = save.index("DialogResult = DialogResult.OK;")
    if save.index("if (!response.SavedVersionIsActive)") > dialog_index:
        raise AssertionError("non-active replay guard must occur before successful completion")

    cancel = method_body(page, "cancelRuleChangesBTN_Click")
    require_all(
        cancel,
        ("pendingSaveRequest = null;", "AcceptV7ActiveRulesResponse(activeRulesSnapshot)"),
        "cancel changes must restore the last accepted activity snapshot",
    )
    if "Async(" in cancel or "v7RuleApiClient" in cancel:
        raise AssertionError("cancel changes must remain a local-only operation")
    require_all(
        designer,
        (
            'this.ruleConfigBTN.Text = "保存并启用";',
            "this.ruleConfigBTN.Click += new System.EventHandler(this.rule_config_save);",
            'this.cancelRuleChangesBTN.Text = "取消修改";',
            "this.cancelRuleChangesBTN.Click += new System.EventHandler(this.cancelRuleChangesBTN_Click);",
        ),
        "the page must expose only save-and-activate and cancel-changes actions",
    )

    build_prototypes = method_body(page, "BuildV7VirtualPrototypes")
    disabled_prototypes = block_after(build_prototypes, 'if (!setting.Value<bool>("enabled"))')
    if "return new List<ApsgoV7VirtualPrototype>();" not in disabled_prototypes:
        raise AssertionError("disabling virtual material must save an empty prototype collection")
    require_all(
        build_prototypes,
        (
            'string prototypeId = "virtual_sphc:"',
            'width.ToString("G29", CultureInfo.InvariantCulture)',
            'thickness.ToString("G29", CultureInfo.InvariantCulture)',
            'Grade = "SPHC"',
            "MinTemperature = null",
            "MaxTemperature = null",
            "RuleAttributes = new JObject()",
        ),
        "new virtual prototypes must use the confirmed canonical defaults",
    )
    clone_prototype = method_body(page, "CloneV7VirtualPrototype")
    require_all(
        clone_prototype,
        (
            "PrototypeId = source.PrototypeId",
            "Width = source.Width",
            "Thickness = source.Thickness",
            "MinTemperature = source.MinTemperature",
            "MaxTemperature = source.MaxTemperature",
            "Grade = source.Grade",
            "RuleAttributes = (JObject)source.RuleAttributes.DeepClone()",
        ),
        "existing virtual prototype metadata must be cloned",
    )

    validate_request = method_body(client, "ValidateRequest")
    if "operationId == Guid.Empty" not in validate_request:
        raise AssertionError("the V7 client must reject Guid.Empty save_operation_id")
    validate_response = method_body(client, "ValidateResponse")
    if "saved.BasedOnVersionId != saved.PreviousActiveVersionId" not in validate_response:
        raise AssertionError("an active save response must match its previous version")

    gitignore_path = client_root / ".gitignore"
    gitignore_status = "absent_in_export"
    if gitignore_path.exists():
        digest = hashlib.sha256(gitignore_path.read_bytes()).hexdigest()
        if digest != USER_GITIGNORE_SHA256:
            raise AssertionError("the user-owned C# .gitignore changed")
        gitignore_status = "preserved"

    return {
        "client_root": str(client_root),
        "v7_post_calls": page.count(".SetActiveRulesAsync("),
        "rules_cloned_in_snapshot_order": len(expected_ids),
        "expected_active_version_forwarded": True,
        "response_base_version_correlated": True,
        "uncertain_retry_reuses_request": True,
        "conflict_and_validation_preserve_edits": True,
        "inactive_replay_does_not_advance": True,
        "cancel_changes_is_local": True,
        "virtual_prototype_semantics": "confirmed",
        "guid_empty_rejected": True,
        "active_save_lineage_validated": True,
        "user_gitignore": gitignore_status,
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
