"""Machine-readable capture for the upper-bilateral-mock release (D-5).

The capture document is an *observation* record.  It never carries a verdict:
the oracle is the lower frozen runner and lives in the scenario catalog.  This
module is the single writer seam (DESIGN.md 6.4); raw-byte digests, attempt
counts and before/after state only exist inside the request path, so nothing
else may append to the document.

Every string that reaches the document passes the schema's ``noSecretMaterial``
guard, and header *values* are never recorded - only redacted header names and
the digest of the redacted canonical header block.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from oran.contract.jcs import CanonicalizationError, canonicalize_bytes

SCHEMA_ID = "urn:oran-aic:upper-bilateral-mock:capture:1.0.0"
SCHEMA_FILE_NAME = "capture-schema.1.0.0.json"
REDACTION_POLICY = "oran-aic-upper-bilateral-mock-redaction/1.0.0"

#: Header names that are never captured, not even as names.
REDACTED_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token",
})
#: Any header whose name matches this pattern is redacted as well.
REDACTED_HEADER_PATTERN = re.compile(
    r"(?i)(authorization|cookie|token|secret|credential|password|api[-_]?key)")

_CAPTURE_NAMESPACE = uuid.UUID("2b1f6f1c-0c1e-4f2a-9a4f-2b5f0c9d7a11")


class CaptureError(RuntimeError):
    """A capture fragment was rejected; the request must fail closed."""


def _schema_search_roots() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    # <repo>/oran/release/ubm/capture.py -> <repo>/docs/upper-bilateral-mock
    repo_root = here.parents[3]
    # <release>/lib/oran/release/ubm/capture.py -> <release>/spec
    release_root = here.parents[4]
    return (
        repo_root / "docs" / "upper-bilateral-mock",
        release_root / "spec",
        here.parent / "spec",
    )


def default_schema_path() -> Path:
    """Locate the frozen capture schema in a repo or in an extracted release."""
    for root in _schema_search_roots():
        candidate = root / SCHEMA_FILE_NAME
        if candidate.is_file():
            return candidate
    raise CaptureError("capture schema %s was not found" % SCHEMA_FILE_NAME)


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}
_VALIDATOR_CACHE: dict[tuple[str, str], Draft202012Validator] = {}
_CACHE_LOCK = threading.RLock()


def load_schema(schema_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(schema_path) if schema_path is not None else default_schema_path()
    key = str(path.resolve())
    with _CACHE_LOCK:
        cached = _SCHEMA_CACHE.get(key)
        if cached is None:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("$id") != SCHEMA_ID:
                raise CaptureError("capture schema $id mismatch: %s" % cached.get("$id"))
            _SCHEMA_CACHE[key] = cached
        return cached


def _validator(schema_path: Path, fragment: str) -> Draft202012Validator:
    key = (str(schema_path.resolve()), fragment)
    with _CACHE_LOCK:
        cached = _VALIDATOR_CACHE.get(key)
        if cached is None:
            schema = load_schema(schema_path)
            registry = Registry().with_resource(
                SCHEMA_ID, Resource.from_contents(schema))
            target = schema if not fragment else {"$ref": SCHEMA_ID + fragment}
            cached = Draft202012Validator(
                target, registry=registry, format_checker=FormatChecker())
            _VALIDATOR_CACHE[key] = cached
        return cached


# -- Response composition: closed exactly once, at the use site -----------
#
# ``#/$defs/exchange/properties/response`` is
#   allOf[ {$ref: #/$defs/messageDigest}, {required: [status], properties: {...}} ]
# with ``unevaluatedProperties: false`` on the composed object.  An earlier
# revision of the schema instead put ``additionalProperties: false`` inside
# ``messageDigest``; under Draft 2020-12 that keyword sees only the ``properties``
# of the schema object declaring it, so the second branch required exactly what
# the first forbade and *no* exchange record could validate.  The schema was
# corrected (see ``docs/upper-bilateral-mock/capture-schema.1.0.0.json``
# ``$defs.messageDigest.description``), so validation here is unconditional:
# there is no tolerated error class, and every error fails the request closed.
RESPONSE_ONLY_MEMBERS = ("status", "locationHeader", "locationLastPathSegment")


def response_composition_is_satisfiable(schema: Mapping[str, Any]) -> bool:
    """True when the response composition can actually be satisfied.

    Structural check on the schema bytes the runtime was pointed at: the
    composed object must be closed with ``unevaluatedProperties`` (which sees
    both ``allOf`` branches) and ``messageDigest`` must not close itself with
    ``additionalProperties``, which would see only its own members.
    """
    try:
        response = schema["$defs"]["exchange"]["properties"]["response"]
        branches = response["allOf"]
        digest = schema["$defs"]["messageDigest"]
    except (KeyError, TypeError):
        return False
    if len(branches) != 2 or branches[0].get("$ref") != "#/$defs/messageDigest":
        return False
    if digest.get("additionalProperties") is False:
        return False
    if response.get("unevaluatedProperties") is not False:
        return False
    return set(RESPONSE_ONLY_MEMBERS) == set(branches[1].get("properties", {}))


def validate_capture(document: Mapping[str, Any], *, schema_path: Path) -> None:
    """Validate a whole capture document against the frozen schema bytes."""
    path = Path(schema_path)
    errors = sorted(_validator(path, "").iter_errors(document),
                    key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        raise CaptureError("capture document is invalid at %s: %s" % (
            "/" + "/".join(str(part) for part in first.absolute_path), first.message))


def validate_fragment(fragment: Mapping[str, Any], *, schema_path: Path,
                      pointer: str) -> None:
    path = Path(schema_path)
    errors = sorted(_validator(path, pointer).iter_errors(fragment),
                    key=lambda error: list(error.absolute_path))
    if errors:
        first = errors[0]
        raise CaptureError("capture fragment %s is invalid at %s: %s" % (
            pointer, "/" + "/".join(str(part) for part in first.absolute_path),
            first.message))


def redact_headers(headers: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Split headers into the retained set and the redacted *names*.

    The retained mapping still carries values so a digest can be taken over the
    redacted canonical block; it is never written into the document.
    """
    retained: dict[str, str] = {}
    redacted: list[str] = []
    for name, value in headers.items():
        text = str(name)
        lowered = text.lower()
        if lowered in REDACTED_HEADER_NAMES or REDACTED_HEADER_PATTERN.search(text):
            if text not in redacted:
                redacted.append(text)
            continue
        retained[text] = str(value)
    return retained, sorted(redacted)


def canonical_header_block(headers: Mapping[str, str]) -> bytes:
    retained, _ = redact_headers(headers)
    lines = sorted("%s: %s\n" % (str(name).lower(), str(value))
                   for name, value in retained.items())
    return "".join(lines).encode("utf-8")


def header_block_sha256(headers: Mapping[str, str]) -> str:
    """SHA-256 over the REDACTED canonical header block."""
    return hashlib.sha256(canonical_header_block(headers)).hexdigest()


def header_names_present(headers: Mapping[str, str]) -> list[str]:
    retained, _ = redact_headers(headers)
    seen: dict[str, None] = {}
    for name in retained:
        seen.setdefault(str(name), None)
    return sorted(seen)


def body_digests(raw: bytes, *, content_type: str | None) -> dict[str, Any]:
    """Raw-byte digest plus, when the body parses as JSON, its JCS digest."""
    payload = bytes(raw or b"")
    result: dict[str, Any] = {
        "contentType": content_type if content_type else None,
        "bodyByteCount": len(payload),
        "bodyRawSha256": hashlib.sha256(payload).hexdigest(),
    }
    if payload:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return result
        try:
            result["bodyJcsSha256"] = hashlib.sha256(
                canonicalize_bytes(decoded)).hexdigest()
        except CanonicalizationError:
            return result
    return result


#: Endpoint templates whose final path segment IS an assigned identifier.  The
#: plan matcher expands templates with the catalog's literal, so it cannot match
#: a path carrying a freshly assigned one; reading the segment off the resolved
#: URI is what makes registration reliable for both directions.
TERMINAL_SEGMENT_IDENTIFIERS = {
    "#/endpointTemplates/a1Policy": "policyId",
    "#/endpointTemplates/r1Policy": "policyId",
    "#/endpointTemplates/r1DmeDataJob": "dataJobId",
    "#/endpointTemplates/r1DmePushDestination": "deliveryBindingId",
}
#: Same, one segment further in: ``.../{id}/status``.
STATUS_SEGMENT_IDENTIFIERS = {
    "#/endpointTemplates/a1PolicyStatus": "policyId",
    "#/endpointTemplates/r1PolicyStatus": "policyId",
    "#/endpointTemplates/r1DmeDataJobStatus": "dataJobId",
}


def register_path_identifiers(recorder: Any, endpoint_ref: str | None,
                              path: str) -> None:
    """Register the identifier a resolved path carries, before digesting."""
    if not endpoint_ref or recorder is None:
        return
    segments = [token for token in str(path).split("/") if token]
    if not segments:
        return
    member = TERMINAL_SEGMENT_IDENTIFIERS.get(endpoint_ref)
    if member:
        recorder.register_assigned_identifier(segments[-1], member)
        return
    member = STATUS_SEGMENT_IDENTIFIERS.get(endpoint_ref)
    if member and len(segments) >= 2:
        recorder.register_assigned_identifier(segments[-2], member)



def supplied_values(vector: Any, constants: Any = None,
                    provenance: Any = None) -> frozenset[str]:
    """Every identifier the DEPLOYMENT supplies, so it is never tokenised.

    ``binding-provenance.1.0.0.json`` already classifies each identifier as
    CATALOG_LITERAL / CATALOG_CONSTANT / BUNDLE_FIXTURE / VECTOR_SUPPLIED /
    SERVER_ASSIGNED.  Only the last class is minted per run, and only the last
    class may be replaced by a role token: canonicalising a *supplied* value --
    the fixed ``deployment.r1.dme.activeDeliveryBindingId``, say -- would make a
    counterpart that forged it compare equal to one that did not, hiding the
    tamper.  Everything this function returns therefore stays under byte
    comparison.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= 8:
            found.add(node)

    walk(vector)
    walk(constants)
    if isinstance(provenance, Mapping):
        for binding in provenance.get("bindings", ()):
            if not isinstance(binding, Mapping):
                continue
            if str(binding.get("class")) == "SERVER_ASSIGNED":
                continue
            walk(binding.get("value"))
    return frozenset(found)

def canonicalise_identifiers(raw: bytes,
                             identifiers: Sequence[tuple[str, str]]) -> bytes:
    """Replace each assigned identifier with its role token, in raw bytes."""
    text = bytes(raw or b"")
    for value, token in identifiers:
        if value:
            text = text.replace(value.encode("utf-8"), token.encode("utf-8"))
    return text


def message_digest(headers: Mapping[str, str], raw: bytes,
                   *, content_type: str | None = None,
                   pointer_digests: Mapping[str, Any] | None = None,
                   identifiers: Sequence[tuple[str, str]] = (),
                   ) -> dict[str, Any]:
    """Assemble one ``$defs/messageDigest`` object from wire material.

    When ``identifiers`` is supplied, two extra digests are computed over the
    same material with every assigned identifier replaced by its role token.
    Those canonical digests are the ones a two-run comparison can actually use:
    a raw digest whose pre-image embeds a freshly minted identifier can never
    match across runs, so it would have to be *excluded*, and an excluded slot
    detects nothing.  Canonicalising instead keeps every other byte of the body
    and header block under comparison.  The raw digests stay, unchanged, because
    they are what proves byte-faithful forwarding within one run.
    """
    if content_type is None:
        for name, value in headers.items():
            if str(name).lower() == "content-type":
                content_type = str(value)
                break
    record: dict[str, Any] = {
        "headerNamesPresent": header_names_present(headers),
        "headerBlockSha256": header_block_sha256(headers),
    }
    record.update(body_digests(raw, content_type=content_type))
    if pointer_digests:
        record["bodyJsonPointerDigests"] = dict(pointer_digests)
    if identifiers:
        retained, _redacted = redact_headers(headers)
        canonical_headers = {
            name: canonicalise_identifiers(
                str(value).encode("utf-8"), identifiers).decode("utf-8", "replace")
            for name, value in retained.items()
        }
        record["headerBlockCanonicalSha256"] = header_block_sha256(canonical_headers)
        canonical_body = canonicalise_identifiers(raw, identifiers)
        record["bodyCanonicalSha256"] = hashlib.sha256(canonical_body).hexdigest()
    return record


def json_pointer_jcs_digest(raw: bytes, pointer: str) -> str | None:
    """JCS digest of a sub-document, used to prove byte-faithful forwarding."""
    try:
        document = json.loads(bytes(raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    current: Any = document
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, Mapping) and token in current:
            current = current[token]
        else:
            return None
    try:
        return hashlib.sha256(canonicalize_bytes(current)).hexdigest()
    except CanonicalizationError:
        return None


class LogicalClock:
    """Fixed logical time.  It never reads the wall clock (G-DET-1/G-DET-2)."""

    def __init__(self, *, origin_iso: str) -> None:
        if not origin_iso.endswith("Z"):
            raise CaptureError("logical clock origin must be a UTC instant")
        self._origin_iso = origin_iso
        self._origin_ms = _instant_to_epoch_ms(origin_iso)
        self._now_ms = 0
        self._lock = threading.RLock()

    @property
    def origin_iso(self) -> str:
        return self._origin_iso

    def at(self, ms: int) -> str:
        if int(ms) < 0:
            raise CaptureError("logical offsets are non-negative")
        return _epoch_ms_to_instant(self._origin_ms + int(ms))

    def now_ms(self) -> int:
        with self._lock:
            return self._now_ms

    def set_now_ms(self, ms: int) -> int:
        """Move the logical clock forward to a declared instant."""
        value = int(ms)
        if value < 0:
            raise CaptureError("logical offsets are non-negative")
        with self._lock:
            if value > self._now_ms:
                self._now_ms = value
            return self._now_ms

    def offset_of(self, instant_iso: str) -> int:
        return _instant_to_epoch_ms(instant_iso) - self._origin_ms

    def reset(self) -> None:
        with self._lock:
            self._now_ms = 0


def _instant_to_epoch_ms(instant: str) -> int:
    text = instant.strip()
    if not text.endswith("Z"):
        raise CaptureError("only UTC instants are accepted: %s" % instant)
    date_part, _, time_part = text[:-1].partition("T")
    try:
        year, month, day = (int(part) for part in date_part.split("-"))
        clock, _, fraction = time_part.partition(".")
        hour, minute, second = (int(part) for part in clock.split(":"))
    except ValueError as exc:
        raise CaptureError("malformed UTC instant: %s" % instant) from exc
    milliseconds = int((fraction + "000")[:3]) if fraction else 0
    days = _days_from_civil(year, month, day)
    return ((days * 86400) + hour * 3600 + minute * 60 + second) * 1000 + milliseconds


def _epoch_ms_to_instant(value: int) -> str:
    seconds, milliseconds = divmod(int(value), 1000)
    days, remainder = divmod(seconds, 86400)
    year, month, day = _civil_from_days(days)
    hour, remainder = divmod(remainder, 3600)
    minute, second = divmod(remainder, 60)
    base = "%04d-%02d-%02dT%02d:%02d:%02d" % (year, month, day, hour, minute, second)
    return base + (".%03dZ" % milliseconds if milliseconds else "Z")


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def http_date_from_instant(instant: str) -> str:
    """RFC 7231 IMF-fixdate for a logical instant; never reads the wall clock."""
    epoch_ms = _instant_to_epoch_ms(instant)
    seconds, _ = divmod(epoch_ms, 1000)
    days, remainder = divmod(seconds, 86400)
    year, month, day = _civil_from_days(days)
    hour, remainder = divmod(remainder, 3600)
    minute, second = divmod(remainder, 60)
    weekday = _WEEKDAYS[(days + 3) % 7]
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        weekday, day, _MONTHS[month - 1], year, hour, minute, second)


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146097 + day_of_era - 719468


def _civil_from_days(days: int) -> tuple[int, int, int]:
    days += 719468
    era = (days if days >= 0 else days - 146096) // 146097
    day_of_era = days - era * 146097
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524
                   - day_of_era // 146096) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    monthish = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * monthish + 2) // 5 + 1
    month = monthish + (3 if monthish < 10 else -9)
    return year + (month <= 2), month, day


_EMPTY_SNAPSHOT: dict[str, Any] = {
    "jcsSha256": hashlib.sha256(canonicalize_bytes({})).hexdigest(),
    "policyCount": 0,
    "statusCount": 0,
    "dmeDataJobCount": 0,
    "deliveryBindingCount": 0,
    "acceptedPushPayloadCounts": {},
    "subscriptionCount": 0,
    "evidenceCommitCount": 0,
    "policyIds": [],
}


class CaptureRecorder:
    """The single capture writer.  ``emit`` validates before it appends."""

    def __init__(self, *, run_id: str, scenario_id: str,
                 envelope: Mapping[str, Any], clock: LogicalClock) -> None:
        self._lock = threading.RLock()
        self._run_id = str(run_id)
        self._scenario_id = str(scenario_id)
        self._envelope = json.loads(json.dumps(dict(envelope)))
        self._clock = clock
        self._schema_path = Path(self._envelope.pop(
            "schemaPath", str(default_schema_path())))
        self._sequence = 0
        self._exchanges: list[dict[str, Any]] = []
        self._harness_operations: list[dict[str, Any]] = []
        self._state = {"before": dict(_EMPTY_SNAPSHOT), "after": dict(_EMPTY_SNAPSHOT),
                       "diffPointers": []}
        self._coordinator: dict[str, Any] = {
            "processIntentCallsBefore": 0,
            "processIntentCallsAfter": 0,
            "processIntentCallsDelta": 0,
            "executionMode": "NOT_INVOKED",
            "fsmHistory": [],
            "terminalOutcome": None,
            "terminalOutcomeCount": 0,
            "terminalEvidenceRef": None,
            "ledgerReferences": [],
            "requiresRealCoordinatorExecution": bool(
                self._envelope.get("requiresRealCoordinatorExecution", False)),
        }
        self._simulated_writes: dict[str, Any] = {
            "normalRanWrites": 0, "rollbackRanWrites": 0, "ledger": []}
        self._applied_rules: list[dict[str, Any]] = []
        self._attempted_authorities: list[str] = []
        #: Set by the service to the running :class:`~oran.release.ubm.egress.
        #: EgressGuard`.  When it is absent the capture says so rather than
        #: printing a zero it did not measure.
        self._egress: Any = None
        #: value -> role token, for the identifier-canonical digests.  Roles are
        #: named by what the identifier IS, never by the order it was seen in.
        self._assigned_identifiers: dict[str, str] = {}
        #: Values the deployment supplies.  Registering one as "assigned" would
        #: canonicalise a fixed identifier and hide a counterpart that forged
        #: it, so registration refuses them outright.
        self._supplied_values: frozenset[str] = frozenset()
        self._secret_references: list[str] = []
        self._redacted_header_names: list[str] = []
        self._redacted_body_pointers: list[str] = []
        self._started_at = clock.at(0)

    # -- identity ---------------------------------------------------------
    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def scenario_id(self) -> str:
        return self._scenario_id

    @property
    def clock(self) -> LogicalClock:
        return self._clock

    @property
    def schema_path(self) -> Path:
        return self._schema_path

    def set_supplied_values(self, values: Any) -> None:
        """Values the deployment supplies; never treated as server-assigned."""
        with self._lock:
            self._supplied_values = frozenset(str(item) for item in values)

    def register_assigned_identifier(self, value: Any, role: str) -> None:
        """Declare an identifier this run assigned, so digests can canonicalise."""
        text = str(value or "")
        if len(text) < 8:
            return
        with self._lock:
            if text in self._supplied_values:
                # Supplied, not minted: leave it under byte comparison.
                return
            self._assigned_identifiers.setdefault(text, "{ASSIGNED:%s}" % role)

    def assigned_identifiers(self) -> list[tuple[str, str]]:
        """Longest-first, so one identifier cannot mask a prefix of another."""
        with self._lock:
            return sorted(self._assigned_identifiers.items(),
                          key=lambda item: len(item[0]), reverse=True)

    def bind_egress_guard(self, guard: Any) -> None:
        """Attach the running egress guard; its counters become the capture's."""
        with self._lock:
            self._egress = guard

    # -- writers ----------------------------------------------------------
    def next_sequence(self) -> int:
        with self._lock:
            value = self._sequence
            self._sequence += 1
            return value

    def emit(self, record: Mapping[str, Any]) -> int:
        fragment = json.loads(json.dumps(dict(record)))
        validate_fragment(fragment, schema_path=self._schema_path,
                          pointer="#/$defs/exchange")
        with self._lock:
            self._exchanges.append(fragment)
            authority = fragment.get("transport", {}).get("peerAuthority")
            if authority and authority not in self._attempted_authorities:
                self._attempted_authorities.append(authority)
            return int(fragment["sequence"])

    def emit_harness_operation(self, record: Mapping[str, Any]) -> int:
        fragment = json.loads(json.dumps(dict(record)))
        validate_fragment(fragment, schema_path=self._schema_path,
                          pointer="#/$defs/harnessOperation")
        with self._lock:
            self._harness_operations.append(fragment)
            return int(fragment["sequence"])

    def note_redacted_headers(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:
                if name not in self._redacted_header_names:
                    self._redacted_header_names.append(str(name))

    def note_redacted_body_pointer(self, pointer: str) -> None:
        with self._lock:
            if pointer not in self._redacted_body_pointers:
                self._redacted_body_pointers.append(str(pointer))

    def note_secret_reference(self, reference: str) -> None:
        with self._lock:
            if reference not in self._secret_references:
                self._secret_references.append(str(reference))

    def note_attempted_authority(self, authority: str) -> None:
        with self._lock:
            if authority not in self._attempted_authorities:
                self._attempted_authorities.append(str(authority))

    def set_state(self, *, when: str, snapshot: Mapping[str, Any]) -> None:
        if when not in {"before", "after"}:
            raise CaptureError("state snapshots are 'before' or 'after'")
        fragment = json.loads(json.dumps(dict(snapshot)))
        validate_fragment(fragment, schema_path=self._schema_path,
                          pointer="#/$defs/stateSnapshot")
        with self._lock:
            self._state[when] = fragment
            self._state["diffPointers"] = _diff_pointers(
                self._state["before"], self._state["after"])

    def set_coordinator(self, observations: Mapping[str, Any]) -> None:
        with self._lock:
            merged = dict(self._coordinator)
            merged.update(json.loads(json.dumps(dict(observations))))
            self._coordinator = merged

    def set_simulated_writes(self, ledger: Sequence[Mapping[str, Any]],
                             *, normal: int, rollback: int) -> None:
        with self._lock:
            self._simulated_writes = {
                "normalRanWrites": int(normal),
                "rollbackRanWrites": int(rollback),
                "ledger": json.loads(json.dumps([dict(item) for item in ledger])),
            }

    def add_applied_rule(self, rule_id: str, *, inputs: Sequence[str],
                         observations: Mapping[str, Any]) -> None:
        record = {
            "ruleId": str(rule_id),
            "declaredInCatalog": bool(
                rule_id in self._envelope.get("declaredRuleIds", [])),
            "inputs": [str(item) for item in inputs],
            "observations": json.loads(json.dumps(dict(observations))),
        }
        with self._lock:
            self._applied_rules = [item for item in self._applied_rules
                                   if item["ruleId"] != record["ruleId"]]
            self._applied_rules.append(record)
            self._applied_rules.sort(key=lambda item: item["ruleId"])

    # -- readers ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            exchanges = sorted(self._exchanges, key=lambda item: item["sequence"])
            harness = sorted(self._harness_operations,
                             key=lambda item: item["sequence"])
            high_water = max(
                [item["sequence"] for item in exchanges]
                + [item["sequence"] for item in harness] + [0])
            document = {
                "schemaVersion": "oran-aic-upper-bilateral-mock-capture/1.0.0",
                "captureId": str(uuid.uuid5(
                    _CAPTURE_NAMESPACE, self._run_id + "|" + self._scenario_id)),
                "notAVerdict": True,
                "oracleOwnership": (
                    "LOWER_FROZEN_RUNNER_"
                    "51d73ca098743b25fe074d184904e695af37fd95"),
                "run": {
                    "runId": self._run_id,
                    "scenarioId": self._scenario_id,
                    "startedAtLogical": self._started_at,
                    "endedAtLogical": self._clock.at(self._clock.now_ms()),
                    "sequenceHighWaterMark": high_water,
                    "orderingRule": "ATMS_THEN_ARRAY_ORDER",
                    "stepIndexBase": 0,
                },
                "revisions": self._envelope["revisions"],
                "contract": self._envelope["contract"],
                "deployment": self._envelope["deployment"],
                "scenario": self._envelope["scenario"],
                "logicalClock": {
                    "mode": "FIXED_LOGICAL",
                    "origin": self._clock.origin_iso,
                    "evaluationNow": self._envelope.get(
                        "evaluationNow", self._clock.origin_iso),
                    "wallClockReadsOnRequestPath": 0,
                    "hiddenSleepCount": 0,
                },
                "exchanges": exchanges,
                "harnessOperations": harness,
                "state": self._state,
                "coordinator": self._coordinator,
                "simulatedWrites": self._simulated_writes,
                "externalCalls": self._external_calls(),
                "appliedRules": self._applied_rules,
                "redaction": {
                    "policy": REDACTION_POLICY,
                    "redactedHeaderNames": sorted(self._redacted_header_names),
                    "redactedBodyPointers": sorted(self._redacted_body_pointers),
                    "secretReferencesObserved": sorted(self._secret_references),
                    "secretValuesCaptured": False,
                    "credentialMaterialCaptured": False,
                },
            }
            return document

    def _external_calls(self) -> dict[str, Any]:
        """Read the counters off the guard; never write a literal zero.

        With no guard bound the document reports ``guardInstalled: false`` and
        carries the counters it can actually justify -- the authorities seen on
        the capture's own exchanges -- so a reader can tell an unmeasured run
        from a measured clean one.
        """
        declared = list(self._envelope["authorityAllowlist"])
        guard = self._egress
        if guard is None:
            return {
                "externalLiveTargetCalls": 0,
                "hardwareCalls": 0,
                "attemptedAuthorities": sorted(self._attempted_authorities),
                "authorityAllowlist": declared,
                "guardInstalled": False,
                "guardArmedNow": False,
                "guardMethod": "no egress guard was bound to this recorder; the "
                               "counters below are not measurements",
                "scope": "scenario",
                "hardwareDefinitionSource": "not declared",
                "connectionAttempts": 0,
                "approvedConnectionAttempts": 0,
                "violations": [],
                "liveOutboundConnections": 0,
                "peerAttributionAmbiguities": [],
            }
        observed = guard.observations(scope="scenario")
        attempted = sorted(set(self._attempted_authorities)
                           | set(observed["attemptedAuthorities"]))
        return {
            "externalLiveTargetCalls": int(observed["externalLiveTargetCalls"]),
            "hardwareCalls": int(observed["hardwareCalls"]),
            "attemptedAuthorities": attempted,
            "authorityAllowlist": declared,
            "guardInstalled": bool(observed["guardInstalled"]),
            "guardArmedNow": bool(observed["guardArmedNow"]),
            "scope": "scenario",
            "guardMethod": str(observed["guardMethod"]),
            "hardwareDefinitionSource": str(observed["hardwareDefinitionSource"]),
            "connectionAttempts": int(observed["connectionAttempts"]),
            "approvedConnectionAttempts": int(observed["approvedConnectionAttempts"]),
            "violations": [dict(item) for item in observed["violations"]],
            "liveOutboundConnections": int(observed["liveOutboundConnections"]),
            "peerAttributionAmbiguities": [
                dict(item) for item in observed["peerAttributionAmbiguities"]],
        }

    def export(self, path: Path) -> str:
        """Validate, write deterministically and return the byte digest."""
        document = self.snapshot()
        validate_capture(document, schema_path=self._schema_path)
        payload = (json.dumps(document, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return hashlib.sha256(payload).hexdigest()


def _diff_pointers(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    pointers: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            pointers.append("/" + str(key).replace("~", "~0").replace("/", "~1"))
    return pointers
