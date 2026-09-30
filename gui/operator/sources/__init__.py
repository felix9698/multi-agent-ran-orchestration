"""Headless data sources for the Operator Console.

Source modules contain no toolkit code.  They run on worker threads and publish
plain, frozen view models to the shared state bus.
"""

from .live import (
    IntentSubmission,
    IntentSubmissionResult,
    IntentWorker,
    build_llm_switch_confirmation,
    build_submit_confirmation,
    build_withdraw_confirmation,
    project_calibration,
    project_decision,
    project_intent_row,
    submit_intent,
    withdraw_intent,
)

__all__ = [
    "IntentSubmission", "IntentSubmissionResult", "IntentWorker",
    "build_llm_switch_confirmation", "build_submit_confirmation",
    "build_withdraw_confirmation", "project_calibration", "project_decision",
    "project_intent_row", "submit_intent", "withdraw_intent",
]
