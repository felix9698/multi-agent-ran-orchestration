"""Dependency-light trajectory integration used by the live coordinator.

The paper metrics module reuses the same trapezoid rule, but importing that
module also imports experiment-wide configuration.  A delivery runtime needs
this numerical primitive without acquiring the experiment runner or hardware
configuration as transitive dependencies.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Tuple


def trajectory_cost(samples: Iterable[Tuple[float, float]] | None,
                    deficit_ref: Optional[float] = 0.0,
                    gain: bool = False) -> Optional[float]:
    """Integrate a finite timestamped throughput deficit by trapezoids."""
    if deficit_ref is None:
        return None
    points = [
        (float(moment), float(value))
        for moment, value in (samples or ())
        if moment is not None and value is not None
        and math.isfinite(float(moment)) and math.isfinite(float(value))
    ]
    points.sort(key=lambda point: point[0])
    if len(points) < 2:
        return None

    def distance(value: float) -> float:
        return max(0.0, ((value - deficit_ref) if gain
                         else (deficit_ref - value)))

    area = 0.0
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        duration = t1 - t0
        if duration > 0:
            area += 0.5 * (distance(v0) + distance(v1)) * duration
    return area
