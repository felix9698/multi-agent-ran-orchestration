"""Phase B Operator Console - export.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/DESIGN.md`` section 4.5 and
``session-store.1.0.0.schema.json`` ``FigureMetadata``.

Two exporters, one rule: **an export must be enough to redo the analysis**.

* :mod:`gui.operator.export.data_export` writes raw artifacts byte-identically
  and normalized/processed data as CSV and JSON, with ``mode`` and ``runId`` in
  the header of every single file.
* :mod:`gui.operator.export.figure_export` writes paper-grade figures - PDF and
  SVG vector plus 300 dpi PNG - each with its exact source CSV and a mandatory
  metadata sidecar.

Both run on a worker thread and touch no widget.  Neither imports ``tkinter``:
the export path has to work headless, because that is how a figure gets into a
paper.
"""

from __future__ import annotations

from typing import Final

#: Every file an export writes carries these two fields, so a CSV that has been
#: separated from its run directory still says what it is and where it came
#: from.  A figure that loses its mode is the failure this design guards against.
BANNER_FIELDS: Final[tuple] = ("mode", "runId")

__all__ = ["BANNER_FIELDS"]
