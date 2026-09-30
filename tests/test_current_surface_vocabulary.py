"""Guard the shipped surface against retired deployment-tier vocabulary."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# These are the operator-facing and runtime surfaces that must use the
# single-system deployment vocabulary.  B-01-owned entries stay in this list:
# this guard intentionally fails until its cutover wording lands.
CURRENT_SURFACES = (
    "assurance",
    "gui",
    "tools",
    "oran/integration",
    "oran/slice_actuator",
    "oran/rapp",
    "main.py",
    "README.md",
    "docs/architecture",
    "docs/release",
    "docs/traceability",
    "docs/user",
)

# Retained archives and evidence are digest-checked historical records.  They
# are explicitly outside this terminology migration rather than silently
# overlooked by a broad glob.
HISTORY_ONLY_PREFIXES = (
    "oran/release/",
    "docs/integration/evidence/",
    "docs/baseline/",
    "standard/",
    "tools/labctl/assets/lower-1.0.0/",
)

# The B-01 cutover made these two unreachable from the final runtime (see
# docs/architecture/FINAL-ARCHITECTURE.md: neither module is in ``sys.modules``
# after the default entry point runs).  They are the same category as
# ``oran/release/**`` - preserved byte for byte, not shipped - so they are
# named here rather than quietly swept in by the ``oran/rapp`` glob above.
HISTORY_ONLY_FILES = (
    "oran/rapp/gui_entry.py",
    "oran/rapp/coordinator_adapter.py",
)

# The one compatibility shim that exists to keep the retired module and class
# names importable.  Its whole purpose is to spell them, so the file is
# excluded; nothing else may re-export them.
COMPATIBILITY_ALIAS_FILES = (
    "oran/integration/lower_release.py",
)

# Artifact ids are published names, not deployment-tier prose.  A match that
# falls entirely inside one of these ids is ignored; descriptive prose
# elsewhere on the same line is still reported.
PUBLISHED_ARTIFACT_IDS = re.compile(
    r"(?:oran-aic-lower-integration|upper-live-o1-harness|lower)-"
    r"?(?:integration/)?\d+\.\d+\.\d+"
)

# A historical explanation may quote the retired boundary only in this form;
# current-system prose must use the neutral vocabulary instead.
HISTORICAL_QUOTE = "구 Upper/Lower 경계"

# The authority documents have to be able to *state* the ban, and stating it
# means enumerating the banned words.  Exactly one shape is exempt: a line that
# says what is forbidden and lists the words in backticks.  Prose that merely
# mentions `Upper` or `Lower` does not match, so this cannot be used to keep
# descriptive use of the retired terms.
RULE_DEFINITION = re.compile(r"forbids[^`]*`Upper`.*`Lower`")

# Prose use of the retired tier words.
RETIRED_VOCABULARY = re.compile(r"\b(?:Upper|Lower)\b|앞단|뒷단")

# Identifier use of the same words, which the prose pattern above cannot see
# because ``\b`` does not fire inside ``lowerReleaseBinding`` or
# ``lower_release_binding``.  Covers snake_case, camelCase, PascalCase and
# SCREAMING_CASE, in leading and trailing position.
#
# ``front``/``back`` are deliberately absent.  A sweep of every surface listed
# above found only legitimate technical uses - LLM/decision *backend*,
# ``backend.*`` deployment-values keys, the matplotlib Agg backend, the xApp
# backend, "USRP / RF front end", ``rollback``, ``feedback``, ``background`` -
# and no organisational front/back split at all.  A pattern broad enough to
# catch a split that does not exist would only produce false positives.
# ``\\b`` is deliberately not used: it does not fire next to an underscore, so
# it would miss ``_lower_release_binding_label`` and
# ``..._NOT_LOWER_BOUND`` - exactly the shapes that slipped through before.
RETIRED_IDENTIFIERS = re.compile(
    r"(?<![A-Za-z0-9])(?:[Uu]pper|[Ll]ower)(?:_[a-z0-9]|[A-Z][a-z0-9])"
    r"|(?<![A-Za-z0-9])(?:UPPER|LOWER)_[A-Z0-9]"
    r"|[A-Za-z0-9]_(?:upper|lower)(?![A-Za-z0-9])"
    r"|[a-z0-9](?:Upper|Lower)(?![A-Za-z0-9])"
    r"|[A-Za-z0-9]_(?:UPPER|LOWER)(?![A-Za-z0-9])"
)

# ``upper``/``lower`` also mean the two ends of a numeric interval, which is
# not a product tier and was never part of this migration.
NEUTRAL_INTERVAL_IDENTIFIERS = frozenset({
    "lower_bound", "upper_bound", "lowerBound", "upperBound",
    "lower_limit", "upper_limit", "lowerLimit", "upperLimit",
    "lower_observed", "upper_observed",
})

# Retired spellings the shipped surface must keep, enumerated per file with the
# reason.  This list is exhaustive and exact: a retired identifier that is not
# spelled here - including a second one in one of these same files - still
# fails this guard.
PRESERVED_COMPATIBILITY_IDENTIFIERS = {
    # On-disk deployment binding documents and already-exported identity
    # records spell these; each is emitted or read beside its neutral name
    # (``composedComponentRelease``, ``composedReleaseRoot``), never instead
    # of it.  ``LOWER_PROVIDED`` is a value read out of the release receipt,
    # and ``verify_lower_integration_handoff`` is a file *inside* the published
    # composed release, matched by name under its digest.
    "oran/integration/deployment_binding.py": frozenset({
        "upper_release", "upperRelease", "lowerReleaseRoot", "LOWER_PROVIDED",
        "verify_lower_integration_handoff",
    }),
    # Retired keyword aliases kept because the byte-preserved history-only GUI
    # entry passes them by name; the neutral parameters are the documented ones.
    "oran/rapp/headless.py": frozenset({
        "lower_release_root", "lower_contracts",
    }),
    # A key of the published capture schema
    # ``oran-aic-upper-live-o1-harness-capture/2.0.0``.
    "oran/rapp/http_servers.py": frozenset({"upperOutcome"}),
    # Read-side compatibility for the identity dict the history-only
    # integration entry emits; the neutral key is tried first.
    "gui/operator/sources/live.py": frozenset({"lowerReleaseBinding"}),
    # A persisted timeline lane id, shared with ``runstore/records.py`` and
    # present in existing run stores; renaming it is a record migration, not a
    # wording change.
    "gui/operator/widgets/timeline.py": frozenset({"LOWER_FEEDBACK"}),
    # The same persisted lane id, on the writing side.  The LIVE-console replay
    # adapter maps the Kernel's ``RawSampleIngested`` -- a real event kind it
    # must consume by name -- onto the lane ``runstore.records.LANES``
    # enumerates, and ``timeline_event`` refuses any value not in that tuple.
    # There is no non-retired spelling to use instead: the neutral name would
    # have to be added to the persisted vocabulary first, and that is the same
    # record migration the entry above defers.
    "gui/operator/sources/adapters/liveconsole_run.py": frozenset({
        "LOWER_FEEDBACK",
    }),
    # The refusal code the packaged LO1 self-test raises verbatim
    # (``oran/release/lo1_selftest/driver.py``); the documents quote it.
    "docs/architecture/GATE2-ACCEPTANCE.md": frozenset({"UPPER_IDENTITY_UNRESOLVED"}),
    "docs/architecture/GATE4-ACCEPTANCE.md": frozenset({"UPPER_IDENTITY_UNRESOLVED"}),
    "docs/architecture/gate2-acceptance.1.0.0.json": frozenset({"UPPER_IDENTITY_UNRESOLVED"}),
}

TEXT_SUFFIXES = {".md", ".json", ".csv", ".py", ".txt"}


def _is_excluded(relative: str) -> bool:
    return (any(relative.startswith(prefix) for prefix in HISTORY_ONLY_PREFIXES)
            or relative in HISTORY_ONLY_FILES
            or relative in COMPATIBILITY_ALIAS_FILES
            or "/archive/" in f"/{relative}")


def _files_to_scan():
    for surface in CURRENT_SURFACES:
        path = REPOSITORY_ROOT / surface
        if path.is_file():
            candidates = (path,)
        else:
            candidates = sorted(candidate for candidate in path.rglob("*")
                                if candidate.is_file())
        for candidate in candidates:
            relative = candidate.relative_to(REPOSITORY_ROOT).as_posix()
            if (candidate.suffix not in TEXT_SUFFIXES or _is_excluded(relative)
                    or "__pycache__" in candidate.parts):
                continue
            yield relative, candidate


def _published_id_spans(line: str):
    return tuple(match.span() for match in PUBLISHED_ARTIFACT_IDS.finditer(line))


def _allowed_identifier(line: str, match: "re.Match[str]", allowed) -> bool:
    """Whether *match* is one of the spellings this file may still carry."""
    start = match.start()
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
        start -= 1
    end = match.end()
    while end < len(line) and (line[end].isalnum() or line[end] == "_"):
        end += 1
    word = line[start:end]
    return word in allowed or word in NEUTRAL_INTERVAL_IDENTIFIERS


class CurrentSurfaceVocabularyTests(unittest.TestCase):
    def test_current_surface_contains_no_retired_deployment_tier_terms(self):
        offenders = []
        for relative, path in _files_to_scan():
            allowed = PRESERVED_COMPATIBILITY_IDENTIFIERS.get(
                relative, frozenset())
            for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if HISTORICAL_QUOTE in line or RULE_DEFINITION.search(line):
                    continue
                spans = _published_id_spans(line)

                def preserved(match):
                    return any(low <= match.start() and match.end() <= high
                               for low, high in spans)

                hits = [match for match in RETIRED_VOCABULARY.finditer(line)
                        if not preserved(match)]
                hits += [match for match in RETIRED_IDENTIFIERS.finditer(line)
                         if not preserved(match)
                         and not _allowed_identifier(line, match, allowed)]
                if hits:
                    offenders.append(f"{relative}:{line_number}: {line.strip()}")
        self.assertEqual(offenders, [], "retired tier vocabulary:\n" +
                         "\n".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
