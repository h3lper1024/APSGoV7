"""Exact input-boundary conversion for the integer search standard.

Python integers may be wider than int64 here: only checked results enter arrays.
This module is not a second search evaluator and does not change the live solver.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import re
from zoneinfo import ZoneInfo

from .contracts import fingerprint

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
NUMERIC_STANDARD = "integer_physical_ms_half_up_v1"
MILLISECONDS_PER_HOUR = 3_600_000
MILLISECONDS_PER_SECOND = 1000
_MILLISECOND_DIGITS = len(str(MILLISECONDS_PER_SECOND)) - 1
SEVERITY_SCALE = 1_000_000
WEIGHT_SCORE_SCALE = 100


class NumericValueError(ValueError):
    """A located conversion/range error, never an ordinary candidate rejection."""

    def __init__(self, path, reason):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def int64(value, path):
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise NumericValueError(path, "integer is outside signed int64")
    return value


def checked_sum(values, path):
    """Prove every running prefix, not only the final (possibly cancelling) sum."""
    total = 0
    for value in values:
        total = int64(total + int64(value, path), path)
    return total


def checked_product(left, right, path):
    return int64(int64(left, path) * int64(right, path), path)


def round_half_up_ratio(numerator, denominator, path):
    """Round an exact boundary ratio with ties away from zero.

    Wide intermediate integers are intentional during input preparation. Native
    kernels must prove their product/absolute-value bounds before calling their
    integer operations, not rely on Python's arbitrary precision after overflow.
    """
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise NumericValueError(path, "ratio requires integer numerator and positive denominator")
    quotient, remainder = divmod(abs(numerator), denominator)
    if remainder >= denominator - remainder:
        quotient += 1
    return int64(-quotient if numerator < 0 else quotient, path)


def _decimal(value, path):
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise NumericValueError(path, "finite decimal or integer required; floats are not accepted")
    return value


def _parts(value, path):
    value = _decimal(value, path)
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return 0, 0
    stop = len(digits)
    while digits[stop - 1] == 0:
        stop -= 1
        exponent += 1
    coefficient = 0
    for digit in digits[:stop]:
        coefficient = coefficient * 10 + digit
    return -coefficient if sign else coefficient, exponent


def _scale_places(scale, path):
    int64(scale, path)
    if scale < 1:
        raise NumericValueError(path, "scale must be a positive power of ten")
    places, value = 0, scale
    while value > 1 and value % 10 == 0:
        value //= 10
        places += 1
    if value != 1:
        raise NumericValueError(path, "scale must be a power of ten")
    return places


def choose_scale(values, path, *, minimum_scale=1):
    places = _scale_places(minimum_scale, path)
    parts = tuple(_parts(value, f"{path}[{index}]") for index, value in enumerate(values)
                  if value is not None)
    for _, exponent in parts:
        places = max(places, -exponent)
    # 10**19 cannot be represented; reject before constructing a huge power.
    if places > 18:
        raise NumericValueError(path, "exact decimal scale cannot fit int64")
    scale = 10 ** places
    for coefficient, exponent in parts:
        _scaled_parts(coefficient, exponent, places, path)
    return scale


def _scaled_parts(coefficient, exponent, places, path):
    exponent += places
    if exponent < 0:
        raise NumericValueError(path, "value is not exactly representable at this scale")
    if coefficient and exponent > 18:
        raise NumericValueError(path, "scaled value cannot fit int64")
    return int64(coefficient * 10 ** exponent if coefficient else 0, path)


def to_ticks(value, scale, path):
    coefficient, exponent = _parts(value, path)
    return _scaled_parts(coefficient, exponent, _scale_places(scale, path), path)


def from_ticks(value, scale, path):
    """Restore physical decimals exactly, independent of the active Context."""
    int64(value, path)
    places = _scale_places(scale, path)
    digits = tuple(int(digit) for digit in str(abs(value)))
    return Decimal((int(value < 0), digits, -places))


def ratio_parts(value, path):
    coefficient, exponent = _parts(value, path)
    if coefficient == 0:
        return 0, 1
    # After stripping decimal trailing zeroes, at least 2**k or 5**k
    # remains in the denominator. A larger exponent cannot fit either way.
    if exponent < -63 or exponent > 18:
        raise NumericValueError(path, "reduced ratio cannot fit int64")
    ratio = Fraction(coefficient * 10 ** max(exponent, 0), 10 ** max(-exponent, 0))
    return int64(ratio.numerator, path), int64(ratio.denominator, path)


def hours_to_milliseconds(hours, path, *, weight=Decimal(1)):
    """One quantization of an original duration or prototype weight * rate."""
    hours, weight = _decimal(hours, path), _decimal(weight, path)
    if hours <= 0 or weight <= 0:
        raise NumericValueError(path, "original duration and weight must be positive")
    # Avoid constructing enormous powers from inputs already outside any
    # possible rounding boundary. This bound uses exponents, not float math.
    magnitude = hours.adjusted() + weight.adjusted()
    if magnitude > 13:
        raise NumericValueError(path, "duration cannot fit int64 milliseconds")
    if magnitude < -9:
        raise NumericValueError(path, "positive original duration rounds to zero milliseconds")
    value = Fraction(hours) * Fraction(weight) * MILLISECONDS_PER_HOUR
    result = round_half_up_ratio(value.numerator, value.denominator, path)
    if result <= 0:
        raise NumericValueError(path, "positive original duration rounds to zero milliseconds")
    return result


def allocate_piece_milliseconds(parent_weight, duration_ms, piece_weights, path):
    """Stable piece-index allocation; callers must not pass production order."""
    int64(parent_weight, path)
    int64(duration_ms, path)
    weights = tuple(piece_weights)
    if parent_weight <= 0 or duration_ms <= 0 or len(weights) < 2:
        raise NumericValueError(path, "positive parent and at least two pieces required")
    if any(int64(weight, path) <= 0 or weight >= parent_weight for weight in weights):
        raise NumericValueError(path, "piece weight must be positive and smaller than parent")
    if checked_sum(weights, path) != parent_weight:
        raise NumericValueError(path, "piece weights do not conserve parent weight")
    cumulative, previous, durations = 0, 0, []
    for weight in weights:
        cumulative += weight
        allocated = round_half_up_ratio(duration_ms * cumulative, parent_weight, path)
        durations.append(allocated - previous)
        previous = allocated
    return tuple(durations)


def score_seconds(milliseconds, path):
    return round_half_up_ratio(int64(milliseconds, path), MILLISECONDS_PER_SECOND, path)


def underweight_score(gap_ticks, weight_scale, path):
    int64(gap_ticks, path)
    _scale_places(weight_scale, path)
    if gap_ticks < 0 or weight_scale < WEIGHT_SCORE_SCALE:
        raise NumericValueError(path, "nonnegative gap and at least cent-tonne scale required")
    return round_half_up_ratio(gap_ticks * WEIGHT_SCORE_SCALE, weight_scale, path)


_START_PATTERN = re.compile(
    r"(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:[.,](?P<fraction>\d+))?(?P<offset>Z|[+-]\d{2}:\d{2})"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def start_milliseconds(value, path):
    """Parse offset ISO timestamps without losing sub-microsecond fractions."""
    match = _START_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise NumericValueError(path, "offset timestamp with date, hours, minutes and seconds required")
    try:
        base = datetime.fromisoformat(match['base'] + match['offset'].replace('Z', '+00:00'))
        delta = base.astimezone(timezone.utc) - _EPOCH
    except (ValueError, OverflowError) as error:
        raise NumericValueError(path, "invalid timestamp") from error
    # Only the fourth fractional digit can change millisecond rounding. Reading
    # its decimal digits directly avoids ambient precision and huge ratios.
    fraction = match['fraction'] or ''
    millis = int(fraction[:_MILLISECOND_DIGITS].ljust(_MILLISECOND_DIGITS, '0'))
    if len(fraction) > _MILLISECOND_DIGITS and fraction[_MILLISECOND_DIGITS] >= '5':
        millis += 1
    seconds = delta.days * 86400 + delta.seconds
    # For dates before the epoch, rounding must still be away from zero.
    if seconds < 0 and len(fraction) > _MILLISECOND_DIGITS:
        tail = fraction[_MILLISECOND_DIGITS:]
        if tail[0] == '5' and not any(c != '0' for c in tail[1:]):
            millis -= 1
    return int64(seconds * MILLISECONDS_PER_SECOND + millis, path)


def due_milliseconds(value, start_ms, path):
    int64(start_ms, path)
    try:
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError('noncanonical date')
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZoneInfo('Asia/Shanghai'))
        delta = end.astimezone(timezone.utc) - _EPOCH
    except (TypeError, ValueError, OverflowError) as error:
        raise NumericValueError(path, "due date must be valid YYYY-MM-DD with a following day") from error
    return int64((delta.days * 86400 + delta.seconds) * MILLISECONDS_PER_SECOND - start_ms, path)


@dataclass(frozen=True, slots=True)
class NumericUnits:
    width: int
    thickness: int
    temperature: int
    weight: int

    def __post_init__(self):
        for name in ('width', 'thickness', 'temperature', 'weight'):
            _scale_places(getattr(self, name), name)
        if self.weight < WEIGHT_SCORE_SCALE:
            raise NumericValueError('weight', 'scale must express cent-tonne scores exactly')

    @property
    def fingerprint(self):
        return fingerprint({'standard': NUMERIC_STANDARD, 'scales': self,
                            'time_unit': 'millisecond', 'score_time_unit': 'second',
                            'rounding': 'ROUND_HALF_UP', 'comparison_slack': 0,
                            'severity_scale': SEVERITY_SCALE, 'severity_aggregation': 'per_violation',
                            'encoding_version': 1, 'rule_compiler_version': 1})
