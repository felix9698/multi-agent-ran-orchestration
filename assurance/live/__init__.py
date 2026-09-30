"""The live half of the Gate 3 vertical path, assembled from injected ports.

Everything in this package obeys the same seam as the rest of ``assurance/**``:
no transport, no toolkit, no model client and no clock of its own.  What makes
it *live* is not that it opens a socket -- it cannot -- but that every port it
is handed is backed by the real deployment: a real R1 policy consumer, a real
KPM indication stream, a real wall clock.  The composition root that supplies
those ports lives outside this package (``tools/g3ota``), because that is where
``oran.rapp`` and an HTTPS client may be imported.

The one thing this package owns is the *shape* of a live PIN_TO_CELL run: the
contract family the epoch freezes, the counter the UE's serving cell travels
on, and the readback that refuses to call an acknowledgement an effect.
"""

from assurance.live.pin_to_cell_driver import (
    PIN_TO_CELL_GRAMMAR,
    CorroboratedServingCellReadback,
    InjectedClock,
    KpmUeAttributionReader,
    LiveCellTopology,
    LiveDriverError,
    LivePinToCellDeployment,
    LivePinToCellRuntime,
    LiveTiming,
    LiveUeObservation,
    ServingCellCollector,
    build_live_pin_to_cell_runtime,
    live_contract_set,
    serving_cell_projection,
)

__all__ = [
    "PIN_TO_CELL_GRAMMAR",
    "CorroboratedServingCellReadback",
    "InjectedClock",
    "KpmUeAttributionReader",
    "LiveCellTopology",
    "LiveDriverError",
    "LivePinToCellDeployment",
    "LivePinToCellRuntime",
    "LiveTiming",
    "LiveUeObservation",
    "ServingCellCollector",
    "build_live_pin_to_cell_runtime",
    "live_contract_set",
    "serving_cell_projection",
]
