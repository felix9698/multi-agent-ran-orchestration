"""§9 ProblemDetails generator."""

from __future__ import annotations

from typing import Any

from .errors import ERROR_CODES


def problem_details(code: str, status: int, detail: str, instance: str) -> dict[str, Any]:
    """Create the exact AIC ProblemDetails machine-readable shape."""
    if code not in ERROR_CODES:
        raise ValueError(f"unknown AIC error code: {code}")
    if not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError("ProblemDetails status must be an HTTP status")
    return {
        "type": f"urn:oran-aic:problem:{code}",
        "title": code,
        "status": status,
        "detail": detail,
        "instance": instance,
    }
