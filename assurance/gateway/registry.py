"""The gateway's private adapter registry.

Owner lane: **KGW**.

:class:`~assurance.gateway.write_gateway.AdapterRegistry` is the frozen seam:
its signatures and its contract are fixed by
``docs/architecture/SEAMS-GATE2.md`` and the seam test pins its bodies as
raising, so every lane reading a traceback learns who owns them.  The body
lives here, in a subclass, which keeps the seam intact while giving the KGW
lane a real implementation to dispatch through.

What the class enforces is design section 9's boundary, at the one place it can
be enforced structurally:

* an adapter whose
  :attr:`~assurance.contracts.capability.ActuatorBinding.path` is
  ``LAB_SETUP_PREPARATION`` is refused at registration.  Direct SSH, Telnet,
  OAI CLI, PRB/MCS/scheduler commands, process control and USRP power belong to
  Lab Setup preparation, shutdown or recovery, and "must not be catalog
  candidates and must not be counted as an objective effect".  Refusing them
  here rather than filtering them at dispatch is the difference between a
  boundary and a habit;
* re-registering a name already in use is refused.  A silently replaced adapter
  would change where every subsequent command for every in-flight transaction
  went, and nothing downstream would report anything unusual;
* :meth:`resolve` raises :class:`~assurance.gateway.write_gateway.GatewayRefusal`
  for an unknown name instead of returning ``None``, so an absent adapter is
  one failure at one place rather than a check every call site has to remember.

The registry is not exported from :mod:`assurance.gateway` and the gateway
keeps its instance private.  Agents, the GUI and the collector have no
reference to it and therefore no path to an adapter; the KGW boundary test
asserts that no module outside ``assurance/gateway/`` imports one.
"""

from __future__ import annotations

from typing import Dict, Mapping

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.write_gateway import (
    AdapterRegistry,
    GatewayRefusal,
    WriteGatewayAdapter,
)

__all__ = ["GatewayAdapterRegistry"]


class GatewayAdapterRegistry(AdapterRegistry):
    """The concrete set of adapters one gateway may dispatch through."""

    def __init__(self) -> None:
        self._adapters: Dict[str, WriteGatewayAdapter] = {}

    def register(self, name: str, adapter: WriteGatewayAdapter) -> None:
        """Register *adapter* under *name*; refuse anything off the path."""
        if not isinstance(name, str) or not name.strip():
            raise GatewayRefusal(f"adapter name must be a non-empty string, got {name!r}")
        if name in self._adapters:
            raise GatewayRefusal(
                f"adapter {name!r} is already registered; replacing it would redirect "
                "every in-flight transaction silently"
            )
        if not isinstance(adapter, WriteGatewayAdapter):
            raise GatewayRefusal(
                f"adapter {name!r} does not implement the WriteGatewayAdapter protocol"
            )
        path = getattr(adapter, "actuator_path", None)
        if not isinstance(path, ActuatorPath):
            raise GatewayRefusal(
                f"adapter {name!r} does not declare an ActuatorPath"
            )
        if path is not ActuatorPath.OFFICIAL_ORAN_DYNAMIC:
            raise GatewayRefusal(
                f"adapter {name!r} declares {path.value}; only OFFICIAL_ORAN_DYNAMIC "
                "may be registered on the objective path (design section 9)"
            )
        self._adapters[name] = adapter

    def resolve(self, name: str) -> WriteGatewayAdapter:
        """Return the adapter registered under *name*, or refuse."""
        try:
            return self._adapters[name]
        except KeyError:
            raise GatewayRefusal(
                f"no adapter registered as {name!r}; nothing reached the equipment"
            ) from None

    def registered_paths(self) -> Mapping[str, ActuatorPath]:
        """Adapter name to actuator path, for the boundary tests."""
        return {name: adapter.actuator_path for name, adapter in sorted(self._adapters.items())}
