#!/usr/bin/env python3
"""Static cross-check for the V7 rule client on hosts without .NET Framework."""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

GET_PATH = "/api/v1/rule-sets/GQGA4/default/month/getActiveRules"
POST_PATH = "/api/v1/rule-sets/GQGA4/default/month/setActiveRules"
ACTIVE_FIELDS = {
    "product_line_code",
    "process_code",
    "scenario",
    "active_version_id",
    "version_no",
    "based_on_version_id",
    "fingerprint",
    "rules",
    "quality_spec",
    "allowed_final_deviation_codes",
    "virtual_prototypes",
    "remark",
    "activated_at",
    "activated_by",
}
SAVE_RESULT_FIELDS = {
    "save_operation_id",
    "previous_active_version_id",
    "saved_version_id",
    "saved_version_is_active",
    "idempotent_replay",
}
SAVE_REQUEST_FIELDS = {
    "save_operation_id",
    "expected_active_version_id",
    "rules",
    "virtual_prototypes",
    "remark",
}


def class_body(source: str, name: str) -> str:
    match = re.search(
        rf"\binternal (?:sealed )?class {re.escape(name)}"
        rf"(?:\s*:\s*[^\r\n{{]+)?\s*{{",
        source,
    )
    if match is None:
        raise AssertionError(f"missing class: {name}")
    depth = 1
    index = match.end()
    while depth and index < len(source):
        depth += (source[index] == "{") - (source[index] == "}")
        index += 1
    if depth:
        raise AssertionError(f"unclosed class: {name}")
    return source[match.end() : index - 1]


def json_fields(source: str, name: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2) or ""
        for match in re.finditer(r'\[JsonProperty\("([^"]+)"([^\]]*)\)\]', class_body(source, name))
    }


def require_exact_fields(source: str, name: str, expected: set[str]) -> dict[str, str]:
    actual = json_fields(source, name)
    if set(actual) != expected:
        raise AssertionError(
            f"{name} fields differ: expected={sorted(expected)}, actual={sorted(actual)}"
        )
    return actual


def verify(client_root: Path) -> dict[str, object]:
    source_path = client_root / "SchedApp/ApsgoV7RuleApiClient.cs"
    project_path = client_root / "SchedApp/SchedApp.csproj"
    config_path = client_root / "SchedApp/App.config"
    source_bytes = source_path.read_bytes()
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        raise AssertionError("new C# source must be UTF-8 without BOM")
    source = source_bytes.decode("utf-8")

    if GET_PATH not in source or POST_PATH not in source:
        raise AssertionError("fixed endpoint path differs from the V7 contract")
    if "FloatParseHandling = FloatParseHandling.Decimal" not in source:
        raise AssertionError("Json.NET must parse floating numbers as decimal")
    if re.search(r"\bdouble\b", source):
        raise AssertionError("V7 rule client must not route business decimals through double")
    if "ValidateJsonNumbers" not in source:
        raise AssertionError("outgoing JObject values must reject binary floating numbers")
    if "缺少 BACKEND_ALGORITHM_URL 配置" not in source:
        raise AssertionError("missing backend address must fail clearly")

    require_exact_fields(source, "ApsgoV7SetActiveRulesRequest", SAVE_REQUEST_FIELDS)
    require_exact_fields(
        source,
        "ApsgoV7EditableRuleRequest",
        {"rule_id", "enabled", "parameters"},
    )
    require_exact_fields(source, "ApsgoV7ActiveRulesResponse", ACTIVE_FIELDS)
    require_exact_fields(source, "ApsgoV7SetActiveRulesResponse", SAVE_RESULT_FIELDS)
    for name in (
        "ApsgoV7RuleView",
        "ApsgoV7QualityCriterionView",
        "ApsgoV7ActiveRulesResponse",
        "ApsgoV7SetActiveRulesResponse",
        "ApsgoV7RuleApiIssue",
        "ApsgoV7RuleApiError",
        "ApsgoV7RuleApiErrorEnvelope",
    ):
        if not all(
            "Required = Required." in options for options in json_fields(source, name).values()
        ):
            raise AssertionError(f"{name} has a response field that can disappear silently")

    prototype = json_fields(source, "ApsgoV7VirtualPrototype")
    for field in ("width", "thickness", "min_temperature", "max_temperature"):
        if "Required.AllowNull" not in prototype[field]:
            raise AssertionError(f"{field} must be present but may be null")
    if "public decimal UnitWeight" not in source or source.count("public decimal?") < 4:
        raise AssertionError("virtual prototype numeric DTO fields must use decimal")

    project = ET.parse(project_path)
    namespace = {"msbuild": "http://schemas.microsoft.com/developer/msbuild/2003"}
    includes = [
        item.attrib.get("Include") for item in project.findall(".//msbuild:Compile", namespace)
    ]
    if includes.count("ApsgoV7RuleApiClient.cs") != 1:
        raise AssertionError("old-style C# project must compile the client exactly once")

    config = ET.parse(config_path)
    addresses = [
        item.attrib.get("value")
        for item in config.findall(".//appSettings/add")
        if item.attrib.get("key") == "BACKEND_ALGORITHM_URL"
    ]
    if addresses != ["http://127.0.0.1:8001"]:
        raise AssertionError("BACKEND_ALGORITHM_URL deployment default differs")

    return {
        "client_root": str(client_root),
        "endpoints": [GET_PATH, POST_PATH],
        "active_response_fields": len(ACTIVE_FIELDS),
        "save_request_fields": len(SAVE_REQUEST_FIELDS),
        "save_response_fields": len(ACTIVE_FIELDS | SAVE_RESULT_FIELDS),
        "decimal_transport": "required",
        "project_compile_items": includes.count("ApsgoV7RuleApiClient.cs"),
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-root", required=True, type=Path)
    result = verify(parser.parse_args().client_root.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
