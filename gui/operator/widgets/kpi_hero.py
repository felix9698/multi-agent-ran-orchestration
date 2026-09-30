"""Large, accessible KPI cards for Demo View.

The card shows a *measurement* when there is one, and says what is missing when
there is not.  It never shows a bare number: a value on a projector with no unit
and no quality beside it is exactly the kind of figure an audience reads as a
claim the run did not make.
"""

from __future__ import annotations

from typing import Optional, Tuple

from gui.operator.status import PRE_MEASUREMENT, describe, resolve
from gui.operator.viewmodel.types import MetricAvailabilityView


def _measured(value: float, unit: Optional[str]) -> str:
    """The number with the unit the *source* declared, verbatim.

    Deliberately not ``status.format_value``: that maps a quantity *kind* onto
    the console's own unit table, and the metric index carries the unit the
    source stated for this metric.  Substituting the console's spelling for the
    source's would be the console asserting a unit nobody measured in.
    """
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip() if unit else text


def hero_text(metric: MetricAvailabilityView) -> Tuple[str, str]:
    """Label and value for one KPI card.

    Four cases, and the order matters:

    * the metric is not OK - the availability status and its reason, never a
      number.  An UNSUPPORTED metric has nothing to show and says so;
    * OK with no sample yet - the pre-measurement placeholder, not ``0``;
    * OK with a value - the value with its unit, its quality and its scope, so a
      reader on a projector sees what qualifies the number at the same moment
      they see the number;
    * OK, samples counted, but no value carried - the sample count, honestly
      labelled as a count.  This is the pre-integration behaviour, kept for the
      case where a source indexes a metric without a readable last sample.
    """
    if metric.status != "OK":
        return metric.display, describe(resolve(metric.status,
                                                reason=metric.reason))
    if metric.last_value is not None:
        value = _measured(metric.last_value, metric.unit)
        quality = metric.last_value_quality or "quality not stated"
        scope = f" · {metric.last_value_scope}" if metric.last_value_scope else ""
        return metric.display, f"{value}{scope}\n{quality} · n={metric.sample_count}"
    if metric.sample_count == 0:
        return metric.display, PRE_MEASUREMENT
    return metric.display, f"{metric.sample_count} samples · no value carried"


class KpiHero:
    """Small Tk component with text as well as colour status encoding."""

    def __init__(self, parent, *, title: str = "KPI") -> None:
        import tkinter as tk

        self._title = tk.Label(parent, text=title, anchor="w", font=("DejaVu Sans", 14, "bold"))
        self._value = tk.Label(parent, text=PRE_MEASUREMENT, anchor="w", justify="left", font=("DejaVu Sans", 20, "bold"))
        self._title.pack(fill="x", padx=8, pady=(8, 0))
        self._value.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def set_metric(self, metric: Optional[MetricAvailabilityView]) -> None:
        if metric is None:
            self._title.configure(text="KPI")
            self._value.configure(text="⊘ Unsupported\nNo metric declared")
            return
        title, value = hero_text(metric)
        self._title.configure(text=title)
        self._value.configure(text=value)
