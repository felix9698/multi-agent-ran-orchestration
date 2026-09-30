"""RFC 8785 JSON Canonicalization Scheme (JCS).

Python 3.10+ uses a shortest-round-trip representation for binary64 values.
This implementation applies ECMAScript's fixed/exponent thresholds and exponent
spelling to it.  It deliberately rejects NaN, infinities, and lone surrogate
code points because they are not valid I-JSON values.  Decimal is rejected: JCS
numbers are IEEE-754 binary64 values, so accepting Decimal would hide rounding.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from json.encoder import encode_basestring as _encode_basestring
from typing import Any

#: 2026-09-22: `_quote` 가 원시 샘플 하나하나의 문자열마다 (1) 파이썬 루프로 전 글자
#: 서로게이트 검사를 돌고 (2) `json.dumps` 를 불러 `JSONEncoder` 를 새로 만들었다.
#: 판의 시팅이 한 시행에서 코어를 9분 넘게 태운 정체가 여기였다 -- SIGUSR1 스택이
#: `_run_trial -> ingest_raw_sample -> canonical_bytes -> canonicalize -> _quote ->
#: json.dumps -> JSONEncoder.__init__` 를 그대로 가리켰다.
#: 출력은 같다: 문자열 하나에 대해 `json.dumps(v, ensure_ascii=False)` 와
#: `encode_basestring(v)` 는 같은 바이트를 낸다(separators 는 스칼라에 영향이 없다).
_SURROGATE = re.compile('[\ud800-\udfff]')


class CanonicalizationError(ValueError):
    """The supplied value cannot be represented as canonical I-JSON."""


def _quote(value: str) -> str:
    # ASCII 문자열에는 서로게이트가 있을 수 없으므로 검사를 건너뛴다(대부분이 여기다).
    if not value.isascii() and _SURROGATE.search(value) is not None:
        raise CanonicalizationError("JCS forbids lone UTF-16 surrogate code points")
    return _encode_basestring(value)


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise AssertionError("bool is handled before number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError("JCS forbids non-finite numbers")
    if value == 0:
        return "0"

    # CPython's repr is the correctly rounded shortest binary64 round trip.
    rendered = repr(value).lower()
    absolute = abs(value)
    if "e" not in rendered:
        # ECMAScript selects exponential notation at [1e21, infinity), but
        # CPython renders an integer-looking fixed string below that boundary.
        if absolute >= 1e21:
            rendered = format(value, ".15e")
            coefficient, exponent = rendered.split("e")
            coefficient = coefficient.rstrip("0").rstrip(".")
            rendered = coefficient + "e" + exponent
        else:
            return rendered[:-2] if rendered.endswith(".0") else rendered

    coefficient, exponent = rendered.split("e")
    exponent_int = int(exponent)
    # ECMAScript prints fixed notation for 1e-6 <= |n| < 1e21.
    if 1e-6 <= absolute < 1e21:
        sign = ""
        if coefficient.startswith("-"):
            sign, coefficient = "-", coefficient[1:]
        digits = coefficient.replace(".", "")
        decimal_pos = 1 + exponent_int
        if decimal_pos <= 0:
            return sign + "0." + "0" * (-decimal_pos) + digits
        if decimal_pos >= len(digits):
            return sign + digits + "0" * (decimal_pos - len(digits))
        return sign + digits[:decimal_pos] + "." + digits[decimal_pos:]
    return coefficient + "e" + ("+" if exponent_int >= 0 else "") + str(exponent_int)


def canonicalize(value: Any) -> str:
    """Return RFC 8785 canonical JSON text for an I-JSON-compatible value."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("JCS object keys must be strings")
        ordered = sorted(value, key=lambda key: key.encode("utf-16be"))
        return "{" + ",".join(
            _quote(key) + ":" + canonicalize(value[key]) for key in ordered
        ) + "}"
    raise CanonicalizationError(f"JCS does not support {type(value).__name__}")


def canonicalize_bytes(value: Any) -> bytes:
    """Return UTF-8 encoded JCS text."""
    return canonicalize(value).encode("utf-8")


def jcs_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 digest of the RFC 8785 bytes."""
    return hashlib.sha256(canonicalize_bytes(value)).hexdigest()
