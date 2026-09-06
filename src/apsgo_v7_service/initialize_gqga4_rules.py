"""One-time command for initializing the GQGA4 monthly active rule version."""

from apsgo_scheduler.api.json_codec import dumps_exact_json

from .rule_management import initialize_gqga4_rules
from .rule_store import configured_database_path


def main() -> int:
    result = initialize_gqga4_rules(configured_database_path())
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
