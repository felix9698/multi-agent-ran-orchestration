"""UTC timebase for every assurance record.

Complete module: pure functions over ``datetime``, no owner.

Design section 6.1 requires a creation timestamp on every runtime-relevant
object, and section 6.12 requires expiry, staleness and reordering to be
decidable from the record alone.  Both only work if every timestamp in the
system is (a) unambiguous and (b) canonical, so two components that observe the
same instant write the same bytes and therefore the same content hash.

The rules, enforced rather than documented:

* A timestamp is UTC.  A naive string, a local offset or a ``+00:00`` spelling
  is rejected on the way in and normalised on the way out to a single ``Z``
  form with microsecond precision.  Truncating to seconds would make two
  distinct events inside one measurement cadence collide.
* Parsing is strict.  Missing-telemetry handling in section 8 says an absent or
  unusable observation stays unusable; silently coercing an unparseable
  timestamp to "now" is exactly the substitution that rule forbids.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Final

__all__ = [
    "TimestampError",
    "UTC_TIMESTAMP_PATTERN",
    "format_utc",
    "is_utc_timestamp",
    "parse_utc",
    "utc_now",
    "utc_now_text",
]


class TimestampError(ValueError):
    """A timestamp is not a canonical UTC instant."""


#: The single accepted spelling: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.
UTC_TIMESTAMP_PATTERN: Final[re.Pattern] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


def utc_now() -> datetime:
    """The current instant as a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    """Render *moment* in the one canonical form.

    A naive ``datetime`` is rejected rather than assumed to be UTC: the whole
    point of the canonical form is that the reader never has to guess which
    clock produced it.
    """
    if moment.tzinfo is None:
        raise TimestampError("a naive datetime has no instant; attach timezone.utc")
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_utc(value: str) -> datetime:
    """Parse a canonical UTC timestamp, or raise :class:`TimestampError`.

    ``+00:00`` and second-precision inputs are accepted here because upstream
    O-RAN and O1 payloads legitimately produce them; they are normalised, and
    :func:`is_utc_timestamp` remains the strict check used when sealing a
    record.  Any other offset is refused: a non-UTC offset in an evidence
    record is a correlation bug, not a formatting preference.
    """
    if not isinstance(value, str) or not value:
        raise TimestampError(f"not a timestamp string: {value!r}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampError(f"not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise TimestampError(f"timestamp has no timezone: {value!r}")
    if parsed.utcoffset().total_seconds() != 0:
        raise TimestampError(f"timestamp is not UTC: {value!r}")
    return parsed.astimezone(timezone.utc)


def is_utc_timestamp(value: object) -> bool:
    """True only for the canonical ``...Z`` microsecond form."""
    return isinstance(value, str) and bool(UTC_TIMESTAMP_PATTERN.match(value))


def utc_now_text() -> str:
    """The current instant in the canonical form."""
    return format_utc(utc_now())
