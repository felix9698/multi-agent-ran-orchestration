"""Unified O-RAN OTA assurance system.

Design authority: ``docs/superpowers/specs/2026-08-20-unified-ota-assurance-system-design.md``
and ``task_0820_unified_ota_assurance_system.md``.  Structural authority (which
existing component is reused, adapted, replaced or removed):
``docs/architecture/GATE1-MAP.md``.  Lane ownership of the files below:
``docs/architecture/SEAMS-GATE2.md``.

Five runtime components live under this package, one sub-package each:

``assurance.core``
    Shared typed vocabulary.  Pure data and pure functions, no owner: the trial
    state machine and its transition table, the seven separated decision axes,
    provenance-tagged quantities, append-only event / mailbox envelopes,
    content addressing and the Operator ``ConfirmationRecord``.
``assurance.contracts``
    Versioned, content-addressed contract families and the evidence epoch
    record (design section 6).
``assurance.kernel``
    The deterministic Assurance Kernel: the only component that admits
    contracts, freezes epochs, generates the finite catalog, drives trial
    transitions, accounts harm, closes evidence, releases target vectors and
    terminates cases (design section 4.3).
``assurance.gateway``
    The Write Gateway: the only component that performs dynamic equipment
    changes, and only against a Kernel-issued token (design section 4.4).
``assurance.collector``
    The Measurement Collector: raw counters delivered straight to the Kernel,
    never through an agent (design section 4.5).
``assurance.advisors``
    Intent Agent, xApp Agent and Evidence Coordinator.  Advisory only: they
    emit typed proposals into the Kernel mailbox and own no authority
    (design section 4.2).

Two boundaries hold for every module in this package:

* **No hardware, no network, no LLM.**  Nothing here opens a socket, spawns a
  process or calls a model.  Transport lives in adapters that the Kernel and
  the Write Gateway own; this package only defines what crosses the seam.
* **No human-authority vocabulary.**  There are no signers, signatures, roles,
  thresholds or approval authorities anywhere in the design (section 5).  A
  Kernel token is a deterministic safety permit; an Operator confirmation
  records a content hash and a timestamp, not a person.
"""

from __future__ import annotations

from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION

__all__ = ["ASSURANCE_SCHEMA_VERSION"]
