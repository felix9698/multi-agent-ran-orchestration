"""Preflight: everything the console can check without changing anything.

Preflight exists to answer one question before a run starts - *would this
experiment be able to record what it claims to record?* - and it answers it
using only read paths.  ``boundary-map.1.0.0.json`` F-PREFLIGHT states the rule
plainly: no check starts, stops or configures anything.

Three properties are worth stating because they are easy to erode:

* **A check that cannot be evaluated reports ``UNSUPPORTED`` or ``UNKNOWN``,
  never ``OK``.**  Absence of evidence is not a pass.
* **An unsupported check does not block a start.**  Most of this testbed's
  boundaries are genuinely not exposed (GAP-01); refusing to run because of that
  would make the console useless.  A real failure - an unwritable runs root, an
  invalid profile - does block.
* **Every check names the boundary it read.**  An operator reading a green row
  should be able to say which interface produced it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .. import status as st
from ..viewmodel.types import PreflightCheckView
from .profile import ExperimentProfile

#: A blocking check is one whose failure means the run could not be recorded
#: honestly.  Everything else is informational.
BLOCKING_CHECKS: Tuple[str, ...] = ("PF-PROFILE", "PF-RUNS-ROOT")


@dataclass(frozen=True)
class _Check:
    check_id: str
    label: str
    boundary: str
    run: Callable[[], PreflightCheckView]


def _ok(check_id: str, label: str, boundary: str,
        detail: Optional[str] = None) -> PreflightCheckView:
    return PreflightCheckView(check_id=check_id, label=label, status=st.OK,
                              boundary=boundary, detail=detail)


def _fail(check_id: str, label: str, boundary: str, reason: str,
          status: str = st.ERROR,
          detail: Optional[str] = None) -> PreflightCheckView:
    return PreflightCheckView(check_id=check_id, label=label, status=status,
                              reason=reason, boundary=boundary, detail=detail)


class PreflightRunner:
    """Runs the read-only checks for one profile.

    Every collaborator is injected and optional.  A console with no R1 client
    still gets a complete, honest preflight - the R1 rows say ``Unsupported: no
    R1 client is configured`` instead of quietly disappearing.
    """

    def __init__(self, profile: Optional[ExperimentProfile], *,
                 r1_client: Any = None,
                 capability_manifest: Optional[Mapping[str, Any]] = None,
                 llm_backend_names: Sequence[str] = (),
                 readiness_ladder: Optional[Mapping[str, Any]] = None,
                 runs_root: Optional[str] = None) -> None:
        self.profile = profile
        self.r1_client = r1_client
        self.capability_manifest = capability_manifest
        self.llm_backend_names = tuple(llm_backend_names)
        self.readiness_ladder = readiness_ladder
        self.runs_root = runs_root or (profile.runs_root if profile else None)

    # -- individual checks -------------------------------------------------- #

    def check_profile(self) -> PreflightCheckView:
        boundary = "SESSION_STORE"
        if self.profile is None:
            return _fail("PF-PROFILE", "Experiment profile", boundary,
                         "no profile is loaded")
        issues = self.profile.validate()
        errors = [i for i in issues if i.severity == "ERROR"]
        warnings = [i for i in issues if i.severity != "ERROR"]
        if errors:
            return _fail("PF-PROFILE", "Experiment profile", boundary,
                         "; ".join(issue.as_text() for issue in errors))
        detail = "; ".join(issue.as_text() for issue in warnings) or None
        return _ok("PF-PROFILE", "Experiment profile", boundary,
                   detail or f"profile {self.profile.profile_id}")

    def check_runs_root(self) -> PreflightCheckView:
        """Is the run directory writable?  Probed by creating the directory only.

        The probe deliberately stops at ``mkdir``: creating a scratch file to
        test writability would leave debris in a directory whose contents are
        research evidence.
        """
        boundary = "SESSION_STORE"
        if not self.runs_root:
            return _fail("PF-RUNS-ROOT", "Run directory", boundary,
                         "no runs root configured")
        root = Path(self.runs_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail("PF-RUNS-ROOT", "Run directory", boundary,
                         f"cannot create {root}: {exc}")
        if not os.access(root, os.W_OK):
            return _fail("PF-RUNS-ROOT", "Run directory", boundary,
                         f"{root} is not writable")
        return _ok("PF-RUNS-ROOT", "Run directory", boundary, str(root))

    def check_capability_manifest(self) -> PreflightCheckView:
        boundary = "CAPABILITY_MANIFEST"
        if not self.capability_manifest:
            return _fail("PF-CAPABILITY", "RAN capability manifest", boundary,
                         "no capability manifest is declared; topology "
                         "elements and KPI availability render Unsupported",
                         status=st.UNSUPPORTED)
        manifest_id = self.capability_manifest.get("manifestId")
        ric_id = self.capability_manifest.get("nearRtRicId")
        if not manifest_id:
            return _fail("PF-CAPABILITY", "RAN capability manifest", boundary,
                         "manifest carries no manifestId", status=st.DEGRADED)
        return _ok("PF-CAPABILITY", "RAN capability manifest", boundary,
                   f"{manifest_id} / {ric_id or 'no nearRtRicId'}")

    def check_r1_bootstrap(self) -> PreflightCheckView:
        boundary = "R1_POLICY"
        reader = getattr(self.r1_client, "bootstrap_info", None)
        if reader is None:
            return _fail("PF-R1-BOOTSTRAP", "R1 service discovery", boundary,
                         "no R1 client is configured",
                         status=st.UNSUPPORTED)
        try:
            info = reader()
        except Exception as exc:
            return _fail("PF-R1-BOOTSTRAP", "R1 service discovery", boundary,
                         f"{type(exc).__name__}: {exc}", status=st.UNAVAILABLE)
        return _ok("PF-R1-BOOTSTRAP", "R1 service discovery", boundary,
                   str(sorted(info)) if isinstance(info, Mapping) else "reachable")

    def check_policy_types(self) -> PreflightCheckView:
        boundary = "R1_POLICY"
        reader = getattr(self.r1_client, "discover_policy_types", None)
        if reader is None:
            return _fail("PF-POLICY-TYPE", "A1 policy type availability",
                         boundary, "no R1 client is configured",
                         status=st.UNSUPPORTED)
        try:
            types = reader()
        except Exception as exc:
            return _fail("PF-POLICY-TYPE", "A1 policy type availability",
                         boundary, f"{type(exc).__name__}: {exc}",
                         status=st.UNAVAILABLE)
        declared = tuple((self.capability_manifest or {}).get("policyTypes") or ())
        found = _policy_type_ids(types)
        missing = [name for name in declared if name not in found]
        if missing:
            return _fail("PF-POLICY-TYPE", "A1 policy type availability",
                         boundary,
                         f"declared but not discovered: {', '.join(missing)}")
        return _ok("PF-POLICY-TYPE", "A1 policy type availability", boundary,
                   ", ".join(found) or "none discovered")

    def check_dme_type(self) -> PreflightCheckView:
        boundary = "R1_DME"
        reader = getattr(self.r1_client, "discover_dme_type", None)
        if reader is None:
            return _fail("PF-DME-TYPE", "DME evidence type", boundary,
                         "no R1 client is configured", status=st.UNSUPPORTED)
        try:
            reader()
        except Exception as exc:
            return _fail("PF-DME-TYPE", "DME evidence type", boundary,
                         f"{type(exc).__name__}: {exc}", status=st.UNAVAILABLE)
        return _ok("PF-DME-TYPE", "DME evidence type", boundary,
                   "aic:policy-evidence:1.0.0 discoverable")

    def check_llm_backend(self) -> PreflightCheckView:
        """Presence only.  Reachability is GAP-11 and is not claimed here."""
        boundary = "LOCAL_PROCESS"
        if not self.llm_backend_names:
            return _fail("PF-LLM", "LLM proposer backend", boundary,
                         "no backend is registered", status=st.UNAVAILABLE)
        wanted = self.profile.llm_backend if self.profile else None
        if wanted and wanted not in self.llm_backend_names:
            return _fail("PF-LLM", "LLM proposer backend", boundary,
                         f"profile asks for {wanted!r}, which is not registered")
        return PreflightCheckView(
            check_id="PF-LLM", label="LLM proposer backend", status=st.UNKNOWN,
            reason="registered, but availability is construction-only "
                   "[GAP-11]; no reachability check exists",
            boundary=boundary,
            detail=f"{len(self.llm_backend_names)} backend(s): "
                   + ", ".join(self.llm_backend_names[:8]))

    def check_readiness(self) -> PreflightCheckView:
        boundary = "O1_ASSURANCE"
        if not self.readiness_ladder:
            return _fail("PF-READINESS", "End-to-end readiness ladder", boundary,
                         "no readiness resource is exposed over R1 [GAP-01]; "
                         "the readiness strip renders Unknown outside a harness "
                         "run", status=st.UNSUPPORTED)
        unmet = [name for name, entry in self.readiness_ladder.items()
                 if not _confirmed(entry)]
        if unmet:
            return _fail("PF-READINESS", "End-to-end readiness ladder", boundary,
                         f"unmet segment(s): {', '.join(sorted(unmet))}",
                         status=st.BLOCKED)
        return _ok("PF-READINESS", "End-to-end readiness ladder", boundary,
                   ", ".join(sorted(self.readiness_ladder)))

    def check_boundary_declaration(self) -> PreflightCheckView:
        """The console's own status read path still declares itself read-only.

        Cheap, and it catches the one regression that would matter most: a
        projection module that quietly grew a write.
        """
        boundary = "R1_POLICY"
        try:
            from oran.rapp import status_projection
        except Exception as exc:
            return _fail("PF-BOUNDARY", "Status read path", boundary,
                         f"projection unavailable: {exc}", status=st.ERROR)
        if not getattr(status_projection, "READ_ONLY", False):
            return _fail("PF-BOUNDARY", "Status read path", boundary,
                         "status projection no longer declares READ_ONLY")
        return _ok("PF-BOUNDARY", "Status read path", boundary,
                   "oran.rapp.status_projection READ_ONLY")

    # -- the run ------------------------------------------------------------ #

    def checks(self) -> Tuple[Callable[[], PreflightCheckView], ...]:
        return (self.check_profile, self.check_runs_root,
                self.check_capability_manifest, self.check_r1_bootstrap,
                self.check_policy_types, self.check_dme_type,
                self.check_llm_backend, self.check_readiness,
                self.check_boundary_declaration)

    def run(self) -> Tuple[PreflightCheckView, ...]:
        """Run every check.  A raising check becomes an ERROR row, not a crash."""
        results: List[PreflightCheckView] = []
        for check in self.checks():
            try:
                results.append(check())
            except Exception as exc:                      # pragma: no cover
                results.append(PreflightCheckView(
                    check_id=getattr(check, "__name__", "PF-UNKNOWN"),
                    label="Preflight check", status=st.ERROR,
                    reason=f"check raised {type(exc).__name__}: {exc}"))
        return tuple(results)


def _policy_type_ids(payload: Any) -> Tuple[str, ...]:
    """Pull policy type ids out of whatever shape discovery returned."""
    if isinstance(payload, Mapping):
        payload = payload.get("policyTypeIds") or payload.get("items") or []
    if not isinstance(payload, (list, tuple)):
        return ()
    out = []
    for item in payload:
        name = item if isinstance(item, str) else (
            item.get("policyTypeId") if isinstance(item, Mapping) else None)
        if name and name not in out:
            out.append(str(name))
    return tuple(out)


def _confirmed(entry: Any) -> bool:
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, Mapping):
        return bool(entry.get("confirmed") or entry.get("ready"))
    return bool(entry)


def run_preflight(profile: Optional[ExperimentProfile], **kwargs: Any
                  ) -> Tuple[PreflightCheckView, ...]:
    """Convenience wrapper used by the workspace and by the ten-step scenario."""
    return PreflightRunner(profile, **kwargs).run()


def blocking_failures(results: Sequence[PreflightCheckView]
                      ) -> Tuple[PreflightCheckView, ...]:
    """The subset that must stop a session start."""
    return tuple(view for view in results
                 if view.check_id in BLOCKING_CHECKS and view.status != st.OK)


def preflight_summary(results: Sequence[PreflightCheckView]) -> str:
    """One line for the header and for the timeline audit record."""
    counts: dict = {}
    for view in results:
        counts[view.status] = counts.get(view.status, 0) + 1
    parts = [f"{st.resolve(key).glyph}{key}:{value}"
             for key, value in sorted(counts.items())]
    return "  ".join(parts) or "no checks run"


__all__ = ["BLOCKING_CHECKS", "PreflightRunner", "blocking_failures",
           "preflight_summary", "run_preflight"]
