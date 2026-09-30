"""Experiment session lifecycle: profile, preflight and the session controller.

This package is where ``Start Experiment`` is defined, and the definition is the
point.  It means: validate the profile, verify readiness through the boundaries
the console is allowed to read, open a run directory, and begin the
intent/policy/measurement/evidence workflow.

It does **not** mean starting a network element.  There is no code path here
that starts, stops or configures a Core, a gNB, a UE or a USRP, and
``boundary-map.1.0.0.json`` forbids adding one.
"""

from .controller import SessionController, SessionError
from .preflight import PreflightRunner, run_preflight
from .profile import ExperimentProfile, ProfileError, ProfileIssue

__all__ = [
    "ExperimentProfile", "PreflightRunner", "ProfileError", "ProfileIssue",
    "SessionController", "SessionError", "run_preflight",
]
