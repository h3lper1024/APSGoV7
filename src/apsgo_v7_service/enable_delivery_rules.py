"""Explicit, backed-up activation of GQGA4 delivery rules; no schema migration."""

import argparse
from pathlib import Path
import sqlite3
from uuid import uuid4

from apsgo_scheduler.api.rule_management import EditableRuleInput, SetActiveRulesRequest
from .gqga4 import GQGA4_DELIVERY_RULE_SET_TEMPLATE
from .rule_management import get_active_gqga4_rules, set_active_gqga4_rules


def enable_delivery_rules(database_path, backup_path, *, expected_active_version_id):
    database = Path(database_path).resolve(strict=True)
    backup = Path(backup_path).absolute()
    if not database.is_file() or backup.exists() or not backup.parent.is_dir():
        raise ValueError("an existing database and a new backup file in an existing directory are required")
    # Reserve the new file without ever overwriting an earlier backup.
    with backup.open("xb"):
        pass
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("database backup integrity check failed")
    active = get_active_gqga4_rules(database)
    if active.active_version_id != expected_active_version_id:
        raise ValueError("active version changed; backup retained, no rules changed")
    if any(item.rule_id == "delivery_due_performance" for item in active.rule_set_spec.rules):
        raise ValueError("delivery rules already exist; use the normal rule editor")
    delivery = next(item for item in GQGA4_DELIVERY_RULE_SET_TEMPLATE.rules
                    if item.rule_id == "delivery_due_performance")
    request = SetActiveRulesRequest(
        save_operation_id=str(uuid4()), expected_active_version_id=active.active_version_id,
        rules=tuple(EditableRuleInput(item.rule_id, item.enabled, item.parameters)
                    for item in (*active.rule_set_spec.rules, delivery)),
        virtual_prototypes=active.virtual_prototypes,
        remark="启用月计划开始时间、九级交期目标与统一数值计算；保留已有业务规则和字典。",
    )
    return set_active_gqga4_rules(request, database, audit_actor="v7-delivery-upgrade")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path, required=True)
    parser.add_argument("--expected-active-version-id", type=int, required=True)
    args = parser.parse_args()
    result = enable_delivery_rules(args.database_path, args.backup_path,
                                   expected_active_version_id=args.expected_active_version_id)
    print(f"active_version_id={result.active_rules.active_version_id}; backup={args.backup_path}")


if __name__ == "__main__":
    main()
