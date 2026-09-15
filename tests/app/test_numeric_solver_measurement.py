from apsgo_scheduler.app.rule_set_loader import fingerprint_rule_set_spec
from apsgo_scheduler.core.contracts import INTEGER_NUMERIC_SEMANTICS_KEY
from tests.app.test_input_normalizer import make_request
from tests.core.test_numeric_evaluation import numeric_quality_spec
from tools.profile_numeric_solver import derive_numeric_request


def test_numeric_measurement_derives_a_new_explicit_identity_without_changing_rules():
    source = make_request(rule_set_spec=numeric_quality_spec())
    derived = derive_numeric_request(source)

    assert derived.policy.numeric_semantics_key == INTEGER_NUMERIC_SEMANTICS_KEY
    assert derived.rule_set_spec.rules == source.rule_set_spec.rules
    assert derived.orders == source.orders
    assert derived.periods == source.periods
    assert derived.virtual_prototypes == source.virtual_prototypes
    assert derived.rule_set_spec.fingerprint == fingerprint_rule_set_spec(
        derived.rule_set_spec
    )
    assert {
        item.numeric_projection for item in derived.rule_set_spec.quality_spec
    } == {
        "integer_exact_v1",
        "severity_round_6_half_up_per_violation",
        "underweight_gap_round_2_half_up_per_chain",
        "delivery_second_half_up",
    }
