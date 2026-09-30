"""Projection and audited selection of existing LLM backends."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from gui.operator.viewmodel.types import LlmBackendView


CONSTRUCTION_ONLY = "CONSTRUCTION_ONLY"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _backend_model(backend: Any) -> Optional[str]:
    for name in ("model", "model_name", "model_version"):
        value = getattr(backend, name, None)
        if value:
            return str(value)
    kind = getattr(backend, "backend_type", None)
    return str(getattr(kind, "value", kind)) if kind is not None else None


class LlmRegistry:
    """Use ``LLMBackendManager`` discovery without claiming reachability.

    ``is_available`` means construction succeeded, not that a request can reach
    the provider.  A backend remains ``UNKNOWN / CONSTRUCTION_ONLY`` until this
    registry observes a successful real call.
    """

    def __init__(self, manager: Any, *, credential_refs: Optional[Mapping[str, Iterable[str]]] = None) -> None:
        # Accept either the manager itself or an IntentCoordinator.  Discovery
        # belongs to the existing manager; switching through the coordinator is
        # preferred because it audits/queues a change at the next proposal
        # boundary when an episode is in flight.
        self._switch_owner = manager
        self.manager = getattr(manager, "llm_manager", manager)
        self._observations: dict[str, tuple[str, Optional[float]]] = {}
        self._credential_refs = {
            str(k): tuple(str(v) for v in values)
            for k, values in (credential_refs or {}).items()
        }

    def names(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self.manager.get_available_names())

    def refresh(self) -> tuple[str, ...]:
        # Manager-owned discovery is the existing LiteLLM/Ollama mechanism.
        refresh = getattr(self._switch_owner, "refresh_llm_backends", None)
        if callable(refresh):
            refresh()
        else:
            self.manager.refresh_dynamic()
        return self.names()

    def views(self) -> tuple[LlmBackendView, ...]:
        try:
            active = self.manager.active_backend_name()
        except Exception:
            active = None
        result = []
        for name in self.names():
            backend = self.manager.backend_by_name(name)
            observed = self._observations.get(name)
            try:
                constructed = bool(backend is not None and backend.is_available())
            except Exception:
                constructed = False
            if observed is not None:
                status, reason = "OK", None
                last_success, latency = observed
            elif constructed:
                status, reason = "UNKNOWN", CONSTRUCTION_ONLY
                last_success, latency = None, None
            else:
                status, reason = "UNAVAILABLE", "BACKEND_NOT_CONSTRUCTED"
                last_success, latency = None, None
            refs = self._credential_refs.get(name, ())
            result.append(LlmBackendView(
                name=name, model_version=_backend_model(backend),
                availability=status, availability_reason=reason,
                last_success_at=last_success, last_latency_ms=latency,
                is_active=(name == active), credential_refs=refs,
                credential_configured=(constructed if refs else None)))
        return tuple(result)

    def select(self, name: str) -> bool:
        """Select through the coordinator audit when it is available."""
        switch = getattr(self._switch_owner, "set_llm_backend", None)
        if not callable(switch):
            switch = self.manager.set_backend
        return bool(switch(str(name)))

    def record_success(self, name: str, latency_ms: Optional[float], *,
                       observed_at: Optional[str] = None) -> None:
        latency = None if latency_ms is None else float(latency_ms)
        self._observations[str(name)] = (observed_at or _utc_now(), latency)


def project_backends(manager: Any) -> tuple[LlmBackendView, ...]:
    return LlmRegistry(manager).views()


__all__ = ["CONSTRUCTION_ONLY", "LlmRegistry", "project_backends"]
