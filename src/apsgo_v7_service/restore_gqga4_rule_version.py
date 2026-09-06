"""Controlled command for restoring historical GQGA4 rule content as a new version."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from apsgo_scheduler.api.json_codec import dumps_exact_json
from apsgo_scheduler.api.rule_management import (
    EditableRuleInput,
    SetActiveRulesRequest,
    SetActiveRulesResponse,
)

from .gqga4 import GQGA4_RULE_SET_TEMPLATE
from .rule_management import (
    RuleManagementServiceError,
    _read_rules_version,
    set_active_gqga4_rules,
)
from .rule_store import RuleStore


def _request_from_verified_version(
    version,
    *,
    save_operation_id: str,
    expected_active_version_id: int,
) -> SetActiveRulesRequest:
    return SetActiveRulesRequest(
        save_operation_id=save_operation_id,
        expected_active_version_id=expected_active_version_id,
        rules=tuple(
            EditableRuleInput(rule.rule_id, rule.enabled, rule.parameters)
            for rule in version.rule_set_spec.rules
        ),
        virtual_prototypes=version.virtual_prototypes,
        remark=version.remark,
    )


def restore_gqga4_rule_version(
    source_version_id: int,
    save_operation_id: str,
    expected_active_version_id: int,
    database_path: str | Path,
) -> SetActiveRulesResponse:
    """Copy one historical editor snapshot into a newly compiled active version."""

    if type(source_version_id) is not int or source_version_id < 1:
        raise ValueError("source_version_id must be a positive integer")
    if type(expected_active_version_id) is not int or expected_active_version_id < 1:
        raise ValueError("expected_active_version_id must be a positive integer")

    path = database_path
    with RuleStore.open(path) as store:
        with store.transaction():
            template = GQGA4_RULE_SET_TEMPLATE
            rule_set = store.find_rule_set(
                template.product_line_code,
                template.process_code,
                template.scenario,
            )
            if rule_set is None or rule_set.active_version_id is None:
                raise RuleManagementServiceError(
                    "rule_set_not_initialized", "GQGA4 月计划规则尚未初始化。"
                )
            if source_version_id == rule_set.active_version_id:
                raise ValueError("source_version_id must identify a historical version")
            source = store.find_version(source_version_id)
            if source is None or source.rule_set_id != rule_set.id:
                raise ValueError("source_version_id does not identify a GQGA4 monthly rule version")

            verified_source = _read_rules_version(store, rule_set, source_version_id)
            request = _request_from_verified_version(
                verified_source,
                save_operation_id=save_operation_id,
                expected_active_version_id=expected_active_version_id,
            )

    return set_active_gqga4_rules(
        request,
        path,
        audit_actor=f"v7-rule-restore:source-version-{source_version_id}",
    )


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _operation_uuid(value: str) -> str:
    try:
        result = UUID(value)
    except (AttributeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be UUID text") from error
    if result.int == 0:
        raise argparse.ArgumentTypeError("must not be the empty UUID")
    return str(result)


def _existing_absolute_database(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError("must identify an existing database file") from error
    if not resolved.is_file():
        raise argparse.ArgumentTypeError("must identify an existing database file")
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore historical GQGA4 rule content as a new active version."
    )
    parser.add_argument("--source-version-id", required=True, type=_positive_int)
    parser.add_argument("--save-operation-id", required=True, type=_operation_uuid)
    parser.add_argument("--expected-active-version-id", required=True, type=_positive_int)
    parser.add_argument("--database-path", required=True, type=_existing_absolute_database)
    arguments = parser.parse_args(argv)

    result = restore_gqga4_rule_version(
        arguments.source_version_id,
        arguments.save_operation_id,
        arguments.expected_active_version_id,
        arguments.database_path,
    )
    active = result.active_rules
    status = (
        "superseded_replay"
        if not result.saved_version_is_active
        else "idempotent_replay"
        if result.idempotent_replay
        else "restored"
    )
    print(
        dumps_exact_json(
            {
                "status": status,
                "source_version_id": arguments.source_version_id,
                "database_path": str(arguments.database_path),
                "save_operation_id": result.save_operation_id,
                "previous_active_version_id": result.previous_active_version_id,
                "saved_version_id": result.saved_version_id,
                "saved_version_is_active": result.saved_version_is_active,
                "active_version_id": active.active_version_id,
                "active_version": active.rule_set_spec.version,
                "fingerprint": active.rule_set_spec.fingerprint,
            }
        )
    )
    return 0 if result.saved_version_is_active else 3


if __name__ == "__main__":
    raise SystemExit(main())
