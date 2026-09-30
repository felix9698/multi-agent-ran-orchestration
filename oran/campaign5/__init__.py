"""Campaign 5 official-path coordination for four RAN action families.

This package is the main-repo Python coordination layer that lifts the ``cap``,
``priority``, ``mcs`` and ``power`` action families from the telnet lab-knob
shortcut onto the real O-RAN path, exactly like the Traffic Steering xApp::

    coordinator -> A1 Policy(type) -> A1-P Producer -> FlexRIC xApp(subscribes)
    -> E2SM-RC Control -> gNB -> readback

It owns the A1 policy types (policy + status schemas under
``contracts/oran-aic/campaign5/``), the per-family policy builders and readback
ports injected into the Write Gateway's ``R1Adapter``, and the in-repo A1-P v2
producer that advertises the four new types beside quota (Option B in
``docs/architecture/CAMPAIGN5-OFFICIAL-PATH-DESIGN.md`` section 4.6).

It does **not** speak a transport.  The E2SM-RC encoding stays in the native
FlexRIC xApp; a gNB RAN-function definition, working encoder, working readback
counter and OTA evidence do not exist hardware-free, so nothing here removes a
manifest blocker or makes any objective live-submittable.  Everything the tests
prove is a mapping, never a live call.
"""

from __future__ import annotations

from .families import (
    CAMPAIGN5_FAMILIES,
    CAMPAIGN5_POLICY_TYPES,
    Campaign5Family,
    campaign5_capability_manifest,
    family_by_policy_type,
    load_campaign5_schema,
    local_schema_digest,
    validate_campaign5,
    verify_campaign5_discovery,
)

__all__ = [
    "CAMPAIGN5_FAMILIES",
    "CAMPAIGN5_POLICY_TYPES",
    "Campaign5Family",
    "campaign5_capability_manifest",
    "family_by_policy_type",
    "load_campaign5_schema",
    "local_schema_digest",
    "validate_campaign5",
    "verify_campaign5_discovery",
]
