"""Campaign 5 composition root: the official write path for four action families.

``assurance/**`` may not import a transport (the KGW seam test enforces it), so
the Write Gateway takes *ports*.  This package is the sanctioned place outside
``assurance/`` that wires the real ones together for ``cap``/``priority``/
``mcs``/``power``: it injects each family's ``policy_builder`` and corroborated
``readback_port`` into the gateway's ``R1Adapter`` (built through
``assurance.gateway.live.build_live_r1_adapter``) and registers it under the
family's ``adapter_name`` -- exactly how ``tools/g3ota`` wires steering.

Nothing here opens a socket.  The ``policy_port`` (an ``R1Client``-shaped
object) and the ``kpm_reader`` are injected; tests pass fakes, and the same
shape carries a live client in a deployment that has the radio.
"""

from __future__ import annotations

from .route import (
    RC_STYLE2_FAMILY_KEYS,
    build_official_adapter,
    build_official_gateway,
    campaign5_live_binding,
    family_for_rc_style2,
    official_plan,
    rc_style2_official_plan,
    run_official_route,
)

__all__ = [
    "RC_STYLE2_FAMILY_KEYS",
    "build_official_adapter",
    "build_official_gateway",
    "campaign5_live_binding",
    "family_for_rc_style2",
    "official_plan",
    "rc_style2_official_plan",
    "run_official_route",
]
