"""Application chrome: the window, the always-visible header and footer, and
the confirmation flow.

Every module in this package follows one rule that the rest of the console
depends on: **the toolkit is imported inside ``build()``, never at module
scope.**  That keeps each module importable in a hermetic test on a machine with
no display - and, more importantly, it keeps the pure chrome functions
(``header_fields``, ``footer_fields``, ``evaluate_confirmation``) testable
without constructing a single widget.  The render classes are therefore plain
objects that *hold* a frame rather than subclassing one.
"""

from .confirm import ConfirmationDialog, ConfirmationOutcome, evaluate_confirmation
from .footer import FooterBar, FooterField, footer_fields
from .header import HeaderBar, HeaderField, format_duration, header_fields
from .window import ConsoleWindow

__all__ = [
    "ConfirmationDialog", "ConfirmationOutcome", "ConsoleWindow", "FooterBar",
    "FooterField", "HeaderBar", "HeaderField", "evaluate_confirmation",
    "footer_fields", "format_duration", "header_fields",
]
