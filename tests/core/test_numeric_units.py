"""New numerical contract, deliberately independent of the old float evaluator."""

from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from fractions import Fraction

import pytest

from apsgo_scheduler.core._numeric_units import (
    INT64_MIN, INT64_MAX, NumericUnits, NumericValueError, allocate_piece_milliseconds,
    checked_product, checked_sum, choose_scale, due_milliseconds, from_ticks,
    hours_to_milliseconds, int64, ratio_parts, round_half_up_ratio, score_seconds,
    start_milliseconds, to_ticks, underweight_score,
)


def test_exact_scales_ignore_context_and_preserve_raw_values():
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_HALF_EVEN
        values = [Decimal('1.005'), Decimal('2000.000'), None, Decimal('-0')]
        scale = choose_scale(values, 'weights', minimum_scale=100)
        assert scale == 1000
        assert to_ticks(values[0], scale, 'weight') == 1005
        assert from_ticks(1005, scale, 'weight') == Decimal('1.005')
        assert underweight_score(1005, scale, 'gap') == 101
        assert to_ticks(Decimal('0E+999999999'), scale, 'zero') == 0
        assert choose_scale([Decimal('0E-999999999')], 'zero') == 1


@pytest.mark.parametrize('value', [Decimal('NaN'), Decimal('Infinity'), 1.2, True, '1.2'])
def test_invalid_boundary_types_rejected(value):
    with pytest.raises(NumericValueError, match='field'):
        to_ticks(value, 100, 'field')


def test_scale_and_intermediate_overflow_are_not_normal_rejections():
    for value in [Decimal('0.0000000000000000001'), Decimal('1E+999999999')]:
        with pytest.raises(NumericValueError):
            choose_scale([value], 'width')
    with pytest.raises(NumericValueError):
        to_ticks(Decimal('1.005'), 100, 'weight')
    with pytest.raises(NumericValueError):
        choose_scale([Decimal(INT64_MAX), Decimal('0.1')], 'width')
    with pytest.raises(NumericValueError):
        checked_sum([INT64_MAX, 1, -1], 'prefix')
    with pytest.raises(NumericValueError):
        checked_product(INT64_MIN, -1, 'absolute')
    assert int64(INT64_MIN, 'minimum') == INT64_MIN
    assert checked_sum([INT64_MAX, -1], 'sum') == INT64_MAX - 1


def test_rational_parameters_reduced_before_native_bounds():
    assert ratio_parts(Decimal('0.0500'), 'ratio') == (1, 20)
    assert ratio_parts(Decimal('2E-19'), 'ratio') == (1, 5_000_000_000_000_000_000)
    for value in [Decimal('1E-19'), Decimal('1E-100000000')]:
        with pytest.raises(NumericValueError):
            ratio_parts(value, 'ratio')


@pytest.mark.parametrize('n,d,expected', [(5,2,3),(-5,2,-3),(7,2,4),(4,2,2),
                                        (-4,3,-1),(4,3,1),(0,2,0),
                                        (INT64_MIN,1,INT64_MIN),
                                        (INT64_MAX*10,10,INT64_MAX)])
def test_rounding_signed_ties_and_wide_boundary_intermediates(n,d,expected):
    assert round_half_up_ratio(n,d,'ratio') == expected


def test_rounding_seconds_and_severity():
    assert score_seconds(2500,'seconds') == 3
    assert score_seconds(-2500,'seconds') == -3
    assert round_half_up_ratio(10000005,10,'severity') == 1000001
    with pytest.raises(NumericValueError):
        round_half_up_ratio(1,0,'ratio')
    with pytest.raises(NumericValueError):
        round_half_up_ratio(INT64_MAX*2+1,2,'ratio')


def test_duration_quantized_once_and_context_independent():
    with localcontext() as ctx:
        ctx.prec = 2
        assert hours_to_milliseconds(Decimal('0.00000125'),'duration') == 5
        assert hours_to_milliseconds(Decimal('0.00000125'),'rate',weight=Decimal(20)) == 90
        assert hours_to_milliseconds(Decimal('1.123456789123456789'),'duration') == 4044444
    for value in [Decimal('1E-999999999'), Decimal('1E+999999999'), Decimal(0), Decimal(-1)]:
        with pytest.raises(NumericValueError):
            hours_to_milliseconds(value,'duration')


def test_piece_allocation_stable_order_conservation_and_zero_piece():
    durations=allocate_piece_milliseconds(6,2,[1,1,4],'split')
    assert durations == (0,1,1)
    assert sum(durations)==2
    assert allocate_piece_milliseconds(2,1,[1,1],'split')==(1,0)
    assert allocate_piece_milliseconds(INT64_MAX,INT64_MAX,[1,INT64_MAX-1],'split')==(1,INT64_MAX-1)
    for pieces in ([1,4],[0,6],[6],[-1,7]):
        with pytest.raises(NumericValueError):
            allocate_piece_milliseconds(6,2,pieces,'split')


def test_fraction_reference_over_many_split_boundaries():
    for weight in range(2,35):
        for duration in range(1,12):
            for first in range(1,weight):
                left,right=allocate_piece_milliseconds(weight,duration,[first,weight-first],'split')
                ideal=Fraction(duration*first,weight)
                assert abs(left-ideal)<=Fraction(1,2)
                assert left+right==duration


def test_millisecond_start_and_due_day_end_in_shanghai():
    start=start_milliseconds('2026-06-01T00:00:00+08:00','start')
    assert start==start_milliseconds('2026-05-31T16:00:00Z','start')
    assert due_milliseconds('2026-05-31',start,'due')==0
    assert due_milliseconds('2026-06-01',start,'due')==86400000
    assert due_milliseconds('2026-05-30',start,'due')==-86400000
    assert start_milliseconds('2026-06-01T00:00:00.000499999999999+08:00','start')==start
    assert start_milliseconds('2026-06-01T00:00:00.000500000000001+08:00','start')==start+1
    assert start_milliseconds('1969-12-31T23:59:59.9995Z','start')==-1
    assert start_milliseconds('1969-12-31T23:59:59.99950001Z','start')==0
    with pytest.raises(NumericValueError):
        start_milliseconds('2026-06-01T00:00:00','start')
    with pytest.raises(NumericValueError):
        due_milliseconds('9999-12-31',start,'due')


def test_unit_identity_distinct_and_validated():
    assert NumericUnits(1,100,1,100).fingerprint==NumericUnits(1,100,1,100).fingerprint
    assert NumericUnits(1,100,1,100).fingerprint!=NumericUnits(1,100,1,1000).fingerprint
    for values in [(1,100,1,1),(2,100,1,100),(1,True,1,100)]:
        with pytest.raises(NumericValueError):
            NumericUnits(*values)
