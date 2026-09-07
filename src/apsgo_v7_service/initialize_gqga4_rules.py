"""One-time command for initializing the GQGA4 monthly active rule version."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from apsgo_scheduler.api.json_codec import dumps_exact_json

from .configuration import DEFAULT_CONFIGURATION_PATH, load_service_configuration
from .rule_management import initialize_gqga4_rules


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the GQGA4 monthly active rule version."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIGURATION_PATH, type=Path)
    arguments = parser.parse_args(argv)
    configuration = load_service_configuration(arguments.config)
    result = initialize_gqga4_rules(
        configuration.database_path,
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
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
