"""One-time command for initializing the GQGA4 monthly active rule version."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from apsgo_scheduler.api.json_codec import dumps_exact_json

from .configuration import DEFAULT_CONFIGURATION_PATH, load_service_configuration
from .migrate_gqga4_grade_dictionary import (
    load_gqga4_grade_dictionary_from_v3_sqlite,
)
from .rule_management import RuleManagementServiceError, initialize_gqga4_rules


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the GQGA4 monthly active rule version."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIGURATION_PATH, type=Path)
    parser.add_argument(
        "--source-database-path",
        type=Path,
        help="Absolute path to the existing V3 SQLite source; required for a new V7 database.",
    )
    arguments = parser.parse_args(argv)
    configuration = load_service_configuration(arguments.config)
    source = None
    if arguments.source_database_path is None:
        if not configuration.database_path.is_file():
            parser.error(
                "--source-database-path is required when initializing a new V7 database"
            )
    else:
        try:
            source = load_gqga4_grade_dictionary_from_v3_sqlite(
                arguments.source_database_path,
                timeout_seconds=configuration.database_timeout_seconds,
            )
        except (OSError, RuleManagementServiceError, sqlite3.Error, ValueError) as error:
            parser.error(f"--source-database-path: {error}")
    result = initialize_gqga4_rules(
        configuration.database_path,
        initial_grade_dictionary=None if source is None else source.snapshot,
        timeout_seconds=configuration.database_timeout_seconds,
    )
    active = result.active_rules
    print(
        dumps_exact_json(
            {
                "status": "initialized" if result.created else "already_initialized",
                "product_line_code": active.rule_set_spec.product_line_code,
                "process_code": active.rule_set_spec.process_code,
                "scenario": active.rule_set_spec.scenario,
                "active_version_id": active.active_version_id,
                "version": active.rule_set_spec.version,
                "fingerprint": active.rule_set_spec.fingerprint,
                "rule_count": len(active.rule_set_spec.rules),
                "virtual_prototype_count": len(active.virtual_prototypes),
                "grade_dictionary_fingerprint": result.grade_dictionary_fingerprint,
                "grade_dictionary_entry_count": result.grade_dictionary_entry_count,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
