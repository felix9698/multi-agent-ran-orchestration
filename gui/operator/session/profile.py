"""The experiment profile: create, load, save, validate.

A profile is **GUI-owned configuration** - intent defaults, the policy context,
which metrics to chart, graph windows, recording options and which LLM backend
to propose with.  ``boundary-map.1.0.0.json`` F-PROFILE-MANAGE draws the line
that matters: a profile never contains RAN configuration, and it never contains
a secret.  Credentials appear as *reference names* such as
``env:ANTHROPIC_API_KEY``, never as values.

That last rule is enforced, not documented: :meth:`ExperimentProfile.from_dict`
refuses a credential entry that does not look like a reference, and refuses any
key whose name suggests a secret carrying a literal value.  A profile is written
to disk and copied into every run's ``config-snapshot.json``, so a leaked value
here would end up in the exported dataset.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

SCHEMA_ID: str = "oran-aic-phase-b-gui-profile/1.0.0"

#: A credential reference: a namespace, a colon, and a name.  Never a value.
CREDENTIAL_REF = re.compile(r"^(env|file|keyring|vault):[A-Za-z0-9_.\-/]{1,128}$")

#: Field names that must never carry a literal in a profile document.
SECRET_NAME = re.compile(r"(?i)(secret|password|passwd|token|api_?key|private_?key)")


class ProfileError(ValueError):
    """A profile document that cannot be trusted, loaded or saved."""


@dataclass(frozen=True)
class ProfileIssue:
    """One validation finding, with the field that produced it."""

    field: str
    message: str
    severity: str = "ERROR"      # ERROR blocks a session start; WARNING does not

    def as_text(self) -> str:
        return f"{self.severity} {self.field}: {self.message}"


@dataclass(frozen=True)
class ExperimentProfile:
    """One reusable experiment configuration."""

    profile_id: str
    label: str = ""
    description: str = ""
    #: Free-text intent seeds the operator starts from.  Not policy, not config.
    intent_defaults: Tuple[str, ...] = ()
    #: Policy context the coordinator already understands (scope, validity,
    #: required KPI freshness).  Passed through untouched; never invented here.
    policy_context: Mapping[str, Any] = field(default_factory=dict)
    metric_selection: Tuple[str, ...] = ()
    graph_window_s: Optional[float] = 300.0
    refresh_interval_ms: int = 100
    recording: bool = True
    runs_root: str = "gui_runs"
    llm_backend: Optional[str] = None
    #: Reference names only, e.g. ``env:ANTHROPIC_API_KEY``.
    credential_refs: Tuple[str, ...] = ()
    capability_manifest_path: Optional[str] = None
    #: The deployment this profile may run Live against: the path of its
    #: integration-values document.  A profile without one is a perfectly good
    #: Replay/Disconnected profile; it simply cannot back a Live session, and
    #: the console says so instead of connecting to something unnamed.
    integration_values_path: Optional[str] = None
    source_path: Optional[str] = None

    # -- serialisation ------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        """A JSON document.  ``source_path`` is local provenance, not content."""
        return {
            "schema": SCHEMA_ID,
            "profileId": self.profile_id,
            "label": self.label,
            "description": self.description,
            "intentDefaults": list(self.intent_defaults),
            "policyContext": dict(self.policy_context),
            "metricSelection": list(self.metric_selection),
            "graphWindowS": self.graph_window_s,
            "refreshIntervalMs": self.refresh_interval_ms,
            "recording": self.recording,
            "runsRoot": self.runs_root,
            "llmBackend": self.llm_backend,
            "credentialRefs": list(self.credential_refs),
            "capabilityManifestPath": self.capability_manifest_path,
            "integrationValuesPath": self.integration_values_path,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any], *,
                  source_path: Optional[str] = None) -> "ExperimentProfile":
        """Build a profile from a JSON document, refusing secret material."""
        if not isinstance(document, Mapping):
            raise ProfileError("profile document must be a JSON object")
        _refuse_secret_values(document)
        refs = tuple(str(ref) for ref in document.get("credentialRefs") or ())
        for ref in refs:
            if not CREDENTIAL_REF.match(ref):
                raise ProfileError(
                    f"credentialRefs entry {ref!r} is not a reference "
                    "(expected env:NAME, file:PATH, keyring:NAME or vault:NAME)")
        profile_id = str(document.get("profileId") or "").strip()
        if not profile_id:
            raise ProfileError("profileId is required")
        window = document.get("graphWindowS", 300.0)
        return cls(
            profile_id=profile_id,
            label=str(document.get("label") or profile_id),
            description=str(document.get("description") or ""),
            intent_defaults=tuple(str(item) for item in
                                  document.get("intentDefaults") or ()),
            policy_context=dict(document.get("policyContext") or {}),
            metric_selection=tuple(str(item) for item in
                                   document.get("metricSelection") or ()),
            graph_window_s=None if window is None else float(window),
            refresh_interval_ms=int(document.get("refreshIntervalMs") or 100),
            recording=bool(document.get("recording", True)),
            runs_root=str(document.get("runsRoot") or "gui_runs"),
            llm_backend=(str(document["llmBackend"])
                         if document.get("llmBackend") else None),
            credential_refs=refs,
            capability_manifest_path=(
                str(document["capabilityManifestPath"])
                if document.get("capabilityManifestPath") else None),
            integration_values_path=(
                str(document["integrationValuesPath"])
                if document.get("integrationValuesPath") else None),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path) -> "ExperimentProfile":
        """Read a profile from disk.

        A malformed document raises rather than degrading into a default: a
        session started from a half-understood profile would record a
        config snapshot that does not describe the experiment that ran.
        """
        file_path = Path(path)
        try:
            document = json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProfileError(f"profile not found: {file_path}") from exc
        except json.JSONDecodeError as exc:
            raise ProfileError(f"profile is not valid JSON: {exc}") from exc
        return cls.from_dict(document, source_path=str(file_path))

    def save(self, path) -> Path:
        """Write the profile atomically and return the path written."""
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True,
                             ensure_ascii=False, allow_nan=False)
        temp = file_path.with_name(file_path.name + ".tmp")
        temp.write_text(payload + "\n", encoding="utf-8")
        temp.replace(file_path)
        return file_path

    # -- validation --------------------------------------------------------- #

    def validate(self) -> Tuple[ProfileIssue, ...]:
        """Every finding, in one pass.

        Returns findings rather than raising, because the configuration summary
        shows all of them at once; an operator fixing a profile one exception at
        a time is a worse experience and a slower experiment.
        """
        issues: List[ProfileIssue] = []
        if not self.profile_id.strip():
            issues.append(ProfileIssue("profileId", "must not be empty"))
        if self.refresh_interval_ms < 20:
            issues.append(ProfileIssue(
                "refreshIntervalMs",
                "below 20 ms the drain loop starves the widget layer",
                "WARNING"))
        if self.refresh_interval_ms > 2000:
            issues.append(ProfileIssue(
                "refreshIntervalMs", "above 2000 ms the console looks frozen",
                "WARNING"))
        if self.graph_window_s is not None and self.graph_window_s <= 0:
            issues.append(ProfileIssue("graphWindowS",
                                       "must be positive, or null for full run"))
        if not self.runs_root.strip():
            issues.append(ProfileIssue("runsRoot", "must not be empty"))
        for ref in self.credential_refs:
            if not CREDENTIAL_REF.match(ref):
                issues.append(ProfileIssue(
                    "credentialRefs", f"{ref!r} is not a reference name"))
        if (self.capability_manifest_path
                and not Path(self.capability_manifest_path).is_file()):
            issues.append(ProfileIssue(
                "capabilityManifestPath",
                f"{self.capability_manifest_path} is not readable; topology "
                "elements will render Unsupported", "WARNING"))
        if (self.integration_values_path
                and not Path(self.integration_values_path).is_file()):
            issues.append(ProfileIssue(
                "integrationValuesPath",
                f"{self.integration_values_path} is not readable; this profile "
                "cannot start a Live session", "WARNING"))
        if not self.metric_selection:
            issues.append(ProfileIssue(
                "metricSelection",
                "no metric selected; charts will start empty", "WARNING"))
        return tuple(issues)

    @property
    def is_valid(self) -> bool:
        """True when nothing blocking was found.  Warnings do not block."""
        return not any(issue.severity == "ERROR" for issue in self.validate())

    def with_overrides(self, **changes: Any) -> "ExperimentProfile":
        return replace(self, **changes)

    def credential_summary(self) -> Tuple[Dict[str, Any], ...]:
        """Reference name and configured yes/no.  Never a value.

        ``configured`` is answered by asking the environment whether the *name*
        resolves, which is the strongest statement that can be made without
        reading a secret into the process's own display path.
        """
        import os

        out = []
        for ref in self.credential_refs:
            namespace, _, name = ref.partition(":")
            if namespace == "env":
                configured: Optional[bool] = bool(os.environ.get(name))
            elif namespace == "file":
                configured = Path(name).is_file()
            else:
                configured = None
            out.append({"reference": ref, "namespace": namespace,
                        "configured": configured})
        return tuple(out)


def _refuse_secret_values(document: Mapping[str, Any], *,
                          path: str = "") -> None:
    """Walk a profile document and refuse a literal in a secret-shaped field.

    ``credentialRefs`` is the sanctioned way to name a credential, and it is
    checked separately.  Anything else that looks like a secret name and carries
    a string is refused outright rather than redacted, because a redaction would
    still have travelled through a file the operator may share.
    """
    for key, value in document.items():
        here = f"{path}.{key}" if path else str(key)
        if isinstance(value, Mapping):
            _refuse_secret_values(value, path=here)
            continue
        if key == "credentialRefs":
            continue
        if SECRET_NAME.search(str(key)) and isinstance(value, str) and value:
            if not CREDENTIAL_REF.match(value):
                raise ProfileError(
                    f"{here} looks like a secret value; a profile may carry "
                    "credential references only")


def default_profile(profile_id: str = "default") -> ExperimentProfile:
    """A minimal usable profile.

    Deliberately thin: it names no cell, no UE and no component count, because
    section 6 forbids forcing this testbed's shape onto a deployment.
    """
    return ExperimentProfile(
        profile_id=profile_id, label="Default operator profile",
        description="Minimal profile: no deployment inventory is assumed.",
        metric_selection=("RRU.PrbDl",))


def profile_summary(profile: Optional[ExperimentProfile]
                    ) -> Tuple[Tuple[str, str], ...]:
    """Label/value pairs for the configuration summary pane."""
    if profile is None:
        return (("Profile", "none loaded"),)
    issues = profile.validate()
    errors = sum(1 for issue in issues if issue.severity == "ERROR")
    warnings = len(issues) - errors
    return (
        ("Profile", profile.profile_id),
        ("Label", profile.label or "-"),
        ("Source", profile.source_path or "in-memory"),
        ("Runs root", profile.runs_root),
        ("Recording", "on" if profile.recording else "off"),
        ("Refresh", f"{profile.refresh_interval_ms} ms"),
        ("Graph window",
         "full run" if profile.graph_window_s is None
         else f"{profile.graph_window_s:g} s"),
        ("Metrics", ", ".join(profile.metric_selection) or "none selected"),
        ("LLM backend", profile.llm_backend or "console default"),
        ("Integration", profile.integration_values_path or "none (no Live)"),
        ("Credentials", ", ".join(entry["reference"]
                                  for entry in profile.credential_summary())
         or "none referenced"),
        ("Validation", f"{errors} error(s), {warnings} warning(s)"),
    )


__all__ = ["CREDENTIAL_REF", "SCHEMA_ID", "ExperimentProfile", "ProfileError",
           "ProfileIssue", "default_profile", "profile_summary"]
