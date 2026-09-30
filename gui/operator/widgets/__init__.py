"""Reusable render widgets.

Every widget here follows the console's two structural rules:

* **No module-scope toolkit import.**  The toolkit is imported inside ``build``,
  so a widget module can be imported - and its pure projection functions
  asserted - in a hermetic test with no display.
* **Composition, not inheritance.**  A widget *holds* a frame instead of being
  one.  That is what makes the first rule possible, and it keeps the render
  layer swappable, which section 3 of the design promises.
"""

from __future__ import annotations

__all__: list = []
