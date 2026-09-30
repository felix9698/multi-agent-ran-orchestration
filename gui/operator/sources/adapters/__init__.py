"""Phase B Operator Console - Replay source adapters.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/replay-sources.1.0.0.json``.

An adapter *writes* a run directory that validates against
``session-store.1.0.0.schema.json`` and reads nothing else afterwards.  Every
workspace then consumes the normalized run directory, never a source-specific
shape - which is what makes "a workspace that renders a replayed run renders a
live one with no code path of its own" true rather than aspirational.

Four invariants bind every adapter in this package:

* **Never invent a value.**  Where a source lacks a field the record carries
  ``None`` and the metric index carries ``UNAVAILABLE`` or ``UNSUPPORTED`` with a
  reason.
* **Never re-timestamp.**  A measurement keeps the observation time its source
  declared.  Ordering without time becomes ``origin=DERIVED`` with its derivation
  stated.
* **Never write to the source.**  A capture directory, an evidence bundle and an
  ``experiment_results`` directory are all read-only inputs.
* **The mode is evidence, not a setting.**  ``manifest.mode`` is set from source
  evidence and there is no code path, and no UI control, that overrides it.
"""

from __future__ import annotations

from typing import Final

#: Adapter ids, matching ``replay-sources.1.0.0.json``.
LO1_CAPTURE: Final[str] = "lo1-capture"
EXPERIMENT_RUN: Final[str] = "experiment-run"
LIVECONSOLE_RUN: Final[str] = "liveconsole-run"

ADAPTER_IDS: Final[tuple] = (LO1_CAPTURE, EXPERIMENT_RUN, LIVECONSOLE_RUN)

__all__ = ["ADAPTER_IDS", "EXPERIMENT_RUN", "LIVECONSOLE_RUN", "LO1_CAPTURE"]
