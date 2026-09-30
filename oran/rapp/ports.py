"""Dependency-injection ports for the rApp's S3 and S4 boundaries."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Iterable, Protocol


class AssuranceDecision(Enum):
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


class PolicyDispatchPort(Protocol):
    """S3 port.  Implementations dispatch only through R1 policy management."""

    def bootstrap_info(self) -> Dict[str, Any]: ...

    def discover_services(self, *, api_name: str | None = None) -> Any: ...

    def discover_policy_types(self) -> Any: ...

    def get_policy_type(self, policy_type_id: str) -> Dict[str, Any]: ...

    def create_policy(self, near_rt_ric_id: str, policy_type_id: str,
                      policy_object: Dict[str, Any]) -> Dict[str, Any]: ...

    def update_policy(self, policy_id: str,
                      policy_object: Dict[str, Any]) -> Dict[str, Any]: ...

    def get_policy_status(self, policy_id: str) -> Dict[str, Any]: ...

    def create_continuous_job(self, *, policy_id: str, policy_revision: int,
                              near_rt_ric_id: str) -> Dict[str, Any]: ...


class AssurancePort(Protocol):
    """S4 port.  A1 lifecycle and DME performance evidence stay separate."""

    def assess(self, policy_object: Dict[str, Any], policy_id: str,
               policy_status: Dict[str, Any],
               evidence_records: Iterable[Dict[str, Any]], *,
               now: Any) -> AssuranceDecision: ...
