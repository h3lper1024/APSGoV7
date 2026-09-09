"""Exact JSON output for public contracts and persisted rule snapshots."""

import json
from collections.abc import Mapping
from decimal import Decimal


def _decimal_json(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("JSON Decimal must be finite")
    sign, raw_digits, exponent = value.as_tuple()
    if not any(raw_digits):
        return "0.0"
    raw = "".join(str(digit) for digit in raw_digits)
    digits = raw.rstrip("0")
    exponent += len(raw) - len(digits)
    prefix = "-" if sign else ""
    point = len(digits) + exponent
    if point <= 0 and -point <= 64:
        return f"{prefix}0.{('0' * -point)}{digits}"
    if point >= len(digits) and point <= 64:
        return f"{prefix}{digits}{'0' * (point - len(digits))}.0"
    if 0 < point < len(digits):
        return f"{prefix}{digits[:point]}.{digits[point:]}"
    fraction = f".{digits[1:]}" if len(digits) > 1 else ""
    return f"{prefix}{digits[0]}{fraction}e{point - 1}"


def dumps_exact_json(value) -> str:
    """Serialize supported values without routing Decimal through float."""

    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        return _decimal_json(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be text")
        return (
            "{"
            + ",".join(
                f"{dumps_exact_json(key)}:{dumps_exact_json(value[key])}" for key in sorted(value)
            )
            + "}"
        )
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(dumps_exact_json(item) for item in value) + "]"
    raise ValueError(f"unsupported JSON value type: {type(value).__name__}")


__all__ = ["dumps_exact_json"]
