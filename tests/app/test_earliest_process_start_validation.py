"""Unsupported clocks are rejected at the public input boundary."""
from dataclasses import replace

import pytest

from apsgo_scheduler.app.input_normalizer import InputNormalizationError, normalize_input
from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec, load_rule_set, RuleSetLoadError
from tests.core.test_earliest_process_start_rule import early_request


def test_earliest_rule_without_delivery_clock_is_input_error():
    request = early_request()
    spec = replace(request.rule_set_spec, rules=tuple(
        r for r in request.rule_set_spec.rules if r.rule_type != "DeliveryDuePerformanceRule"),
        quality_spec=tuple(q for q in request.rule_set_spec.quality_spec if q.metric_key not in
            {"old_backlog_last_completion_hours", "delivery_wait_tardiness_tonne_hours"}))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    with pytest.raises(InputNormalizationError) as error:
        normalize_input(replace(request, rule_set_spec=spec))
    assert any(i.code == "unsupported_earliest_start_clock" for i in error.value.issues)


def test_earliest_rule_rejects_parameters():
    request = early_request()
    spec = replace(request.rule_set_spec, rules=(*request.rule_set_spec.rules[:-1],
        replace(request.rule_set_spec.rules[-1], parameters={"wait": True})))
    spec = replace(spec, fingerprint=fingerprint_rule_set_spec(spec))
    with pytest.raises(RuleSetLoadError, match="no parameters"):
        load_rule_set(spec)
