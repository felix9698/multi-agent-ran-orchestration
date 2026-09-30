"""Build the live ports the Gate 3 vertical path is handed.

Four ports and nothing else:

``policy_port``
    A real :class:`oran.rapp.r1_client.R1Client` over mutual TLS and an OAuth
    authorization header, resolved from the deployment's own frozen
    integration-values document.  It is created here because this is the only
    place allowed to import it.

``policy_builder``
    :func:`oran.rapp.policy_translator.translate_intent`, fed entirely from
    values :func:`assurance.contracts.actuation_request.derive_actuation_request`
    derived from the frozen epoch, plus the *observed* UE identity.  Nothing in
    the body is a convenience default and nothing is a repository constant that
    could go stale: the ``amfUeNgapId`` and its ``guAmI`` are read out of the
    KPM indication the deployment published seconds ago, which is what the
    previous integration's three separate stale-identity failures were.

``read_new_lines``
    A byte-offset tail of the live KPM JSONL.  Read-only, and it never
    enumerates or writes.

the clock ports
    ``now`` / ``monotonic_ms`` / ``sleep_ms``.

Plus two operational duties that are neither Kernel decisions nor equipment
writes, and that the run cannot honestly skip:

* :func:`observe_live_ue` -- read the UE's live identity and serving cell.
* :func:`clear_ue_scope` -- withdraw any *non-terminal* A1-P policy still
  occupying that UE's scope.  The producer admits one policy per scope, so a
  previous attempt's un-enforced policy fences every retry with a 409.  A
  policy whose episode reached ``APPLIED_VERIFIED`` is left alone: it is the
  record of an effect that really happened, and deleting it to make room would
  be erasing evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import ssl
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from assurance.gateway.commands import GatewayOperation
from oran.rapp.r1_client import _problem_text
from assurance.contracts.actuation_request import derive_actuation_request
from assurance.contracts.live_binding import AssuranceLiveBinding
from assurance.core.timebase import format_utc, parse_utc
from assurance.objectives.registry import (
    ASSURANCE_FAMILY_TO_WIRE_KIND,
    ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
)
from assurance.live.pin_to_cell_driver import (
    KpmUeAttributionReader,
    LiveCellTopology,
    LiveDriverError,
    LivePinToCellDeployment,
    LiveUeObservation,
)

from decision.intent_model import IntentPriority, IntentType
from oran.rapp.contract_support import jcs_sha256, load_schema
from oran.rapp.policy_translator import PolicyTranslationContext, translate_intent
from oran.rapp.r1_client import R1Client
from oran.rapp.r1_security import build_r1_security

__all__ = [
    "A1P_OBJECTIVE_KIND",
    "A1P_OBJECTIVE_KIND_VERSION",
    "RecordingPolicyPort",
    "KpmTail",
    "LivePolicyBuilder",
    "WallClockPorts",
    "build_policy_type_discovery",
    "build_r1_policy_port",
    "clear_ue_scope",
    "kpm_slot_occupancy",
    "live_topology",
    "observe_live_ue",
    "producer_episode",
    "scope_occupant_records",
    "scope_occupants",
    "scope_withdrawal_plan",
    "summarise_occupant",
]

#: The two vocabularies this deployment speaks for one objective.  The
#: assurance target contract names a *project* objective family (design section
#: 9: "an objective name is a project contract identifier, not a standard
#: term"); the frozen ``AIC_UECellSteering_1.0.0`` policy body names the
#: standard steering kind.  A composition root is exactly where the two meet,
#: and an objective with no entry here is refused rather than guessed.
#:
#: Read from the registry rather than restated here.  Two copies of a mapping
#: that decides which objective may reach the radio is one copy too many: the
#: registry is what a reader consults and what the tests check, so it is also
#: what the composition root obeys.  Task section 7.8, version carried alongside.
A1P_OBJECTIVE_KIND: Mapping[str, str] = dict(ASSURANCE_FAMILY_TO_WIRE_KIND)
A1P_OBJECTIVE_KIND_VERSION = ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION

#: The frozen ``rollbackPolicy.on`` vocabulary, in the order the schema lists
#: it.  Both members, because the contracted watchdog is
#: ``STOP_AND_ROLLBACK``: a readback that stops matching and an apply that
#: fails are the two ways this deployment can owe a rollback, and asking for
#: only one of them would leave the other unguarded.
ROLLBACK_ON: Tuple[str, ...] = ("READBACK_MISMATCH", "APPLY_FAILED")

_A1P_POLICY_PATH = "/A1-P/v2/policytypes/{policy_type}/policies/{policy_id}"

#: Episode states whose policy is the durable record of an effect that really
#: happened.  Never withdrawn to free a scope.
TERMINAL_SUCCESS_STATES = frozenset({"APPLIED_VERIFIED"})

#: HTTP statuses that mean the producer accepted the withdrawal.  Nothing else
#: counts: a 401 means this consumer may not withdraw, a 500 means the producer
#: did not, and reporting either as ``WITHDRAWN`` would let a run proceed into
#: the 409 it was trying to avoid while its own evidence said the scope was
#: free.  ``404`` is listed separately because it is not an acceptance -- it is
#: the producer saying the policy is not there, which the vacancy re-read below
#: is what actually confirms.
WITHDRAWAL_ACCEPTED = frozenset({200, 202, 204})
WITHDRAWAL_ABSENT = frozenset({404})


# --------------------------------------------------------------------------- #
# clock ports
# --------------------------------------------------------------------------- #


class WallClockPorts:
    """The three time ports a live run needs, in one object."""

    @staticmethod
    def now() -> str:
        moment = datetime.now(timezone.utc)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def monotonic_ms() -> int:
        return int(time.monotonic() * 1000)

    @staticmethod
    def sleep_ms(milliseconds: int) -> None:
        if milliseconds > 0:
            time.sleep(milliseconds / 1000.0)


# --------------------------------------------------------------------------- #
# the KPM tail
# --------------------------------------------------------------------------- #


class KpmTail:
    """A read-only byte-offset tail of the live KPM JSONL.

    The initial offset is set *back* from the end of the file on purpose.  The
    first instant of a trial's observation window has to be answered from an
    indication that arrived before the trial started, so the reader needs a
    little history the moment it is built rather than only what arrives after
    it.  The offset is then advanced past complete lines only: a partially
    written line is left for the next read instead of being parsed as a
    malformed record.
    """

    def __init__(self, path: str | Path, *, prime_bytes: int = 262_144) -> None:
        self._path = Path(path)
        self._offset = 0
        self._identity: Optional[Tuple[int, int]] = None
        #: How many times the file was replaced under the offset -- a rotated
        #: or truncated JSONL.  Counted rather than hidden: a re-sync means the
        #: history before it belongs to a different file and a reader that
        #: needed it has a gap, not a continuous stream.
        self.rotations = 0
        if self._path.is_file():
            status = self._path.stat()
            self._identity = (status.st_dev, status.st_ino)
            self._offset = max(0, status.st_size - int(prime_bytes))
            if self._offset:
                with self._path.open("rb") as stream:
                    stream.seek(self._offset)
                    skipped = stream.readline()
                self._offset += len(skipped)

    @property
    def offset(self) -> int:
        return self._offset

    def read_new_lines(self) -> Sequence[str]:
        if not self._path.is_file():
            return ()
        status = self._path.stat()
        identity = (status.st_dev, status.st_ino)
        rotated = self._identity is not None and identity != self._identity
        self._identity = identity
        if rotated or status.st_size < self._offset:
            # This is not the file the offset described: either it shrank
            # under the offset (truncated in place) or it is a different inode
            # (rotated away).  Both are what a KPM gate restart does to its
            # JSONL, and the two checks together catch a replacement of any
            # size.  Without this the offset stays past the new end of
            # file and the tail answers nothing for the rest of the sitting,
            # which is safe -- silence becomes MISSING_INTERVAL, never a
            # mis-attribution -- and indistinguishable from "the gNB stopped
            # publishing".  Re-syncing to the start of the new file is the only
            # answer that tells those two apart, and it cannot invent a record:
            # what it reads is whatever the new file actually holds.
            self._offset = 0
            self.rotations += 1
        with self._path.open("rb") as stream:
            stream.seek(self._offset)
            raw = stream.read()
        if not raw:
            return ()
        complete, _, remainder = raw.rpartition(b"\n")
        if not complete:
            return ()
        self._offset += len(complete) + 1
        del remainder
        return tuple(
            line.decode("utf-8", "replace") for line in complete.split(b"\n") if line
        )


# --------------------------------------------------------------------------- #
# the deployment's topology, read from its own manifests
# --------------------------------------------------------------------------- #


def live_topology(
    binding: AssuranceLiveBinding, capability_manifest: Mapping[str, Any]
) -> LiveCellTopology:
    """Map E2 node ids onto cells using the deployment's capability manifest.

    The manifest states, per cell, both the ``cellId`` a policy names and the
    ``globalE2NodeId`` that serves it.  The KPM indication reports the node as a
    decimal ``nb_id``; the manifest reports it as the hexadecimal node id, and
    the two are the same number.  Nothing is assumed about which node is
    "source" -- an operator who swaps the cells over gets a different map from
    the same code.
    """
    cells = (capability_manifest.get("topology") or {}).get("cells") or []
    mapping: Dict[int, int] = {}
    for cell in cells:
        try:
            nci = int(cell["cellId"]["cId"]["ncI"])
            node_hex = str(cell["globalE2NodeId"]["nodeId"]["hex"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveDriverError("capability topology cell is incomplete") from exc
        mapping[int(node_hex, 16)] = nci
    if not mapping:
        raise LiveDriverError("the capability manifest advertises no cell")
    return LiveCellTopology(
        plmn=dict(binding.plmn),
        nb_id_to_nci=mapping,
        expected_epochs=dict(binding.kpm_expected_epochs),
    )


# --------------------------------------------------------------------------- #
# the live UE identity
# --------------------------------------------------------------------------- #


def observe_live_ue(
    reader: KpmUeAttributionReader,
    *,
    now: Callable[[], str],
    sleep_ms: Callable[[int], None],
    freshness_ms: int,
    attempts: int = 20,
    amf_ue_ngap_id: Optional[int] = None,
) -> LiveUeObservation:
    """Read the UE's current identity and serving cell off the live stream.

    Fail-closed and *fresh*: a run that cannot see the UE right now must not
    address it from memory.  This is the fix for the identity half of the
    previous integration's blocker chain -- the AMF hands out a new
    ``amfUeNgapId`` on every registration, so the only correct value is the one
    the deployment is publishing at submit time.
    """
    for _ in range(max(1, int(attempts))):
        reader.refresh(amf_ue_ngap_id=amf_ue_ngap_id)
        observation = reader.at_or_before(
            now(), lookback_ms=freshness_ms, amf_ue_ngap_id=amf_ue_ngap_id
        )
        if observation is not None:
            return observation
        sleep_ms(500)
    raise LiveDriverError(
        "no fresh KPM UE attribution indication: the UE is not observable, so "
        "there is no identity to address and no serving cell to move it from"
    )


# --------------------------------------------------------------------------- #
# the A1-P scope, cleared before a submit
# --------------------------------------------------------------------------- #


def _a1p_context(secrets: Mapping[str, str]) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=secrets["mtlsCa"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        secrets["mtlsClientCertificate"], secrets["mtlsClientPrivateKey"]
    )
    return context


def _segment(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _a1p_authorization(secrets: Mapping[str, str], *, scope: str, jti: str) -> str:
    """Mint the consumer assertion the A1-P producer accepts.

    The signing material is read from the file the deployment's binding names
    and is used for this one request; it is never logged, recorded or returned.
    """
    secret = Path(secrets["oauthToken"]).read_bytes().rstrip(b"\r\n")
    der = ssl.PEM_cert_to_DER_cert(
        Path(secrets["mtlsClientCertificate"]).read_text(encoding="utf-8")
    )
    issued = int(time.time())
    header = _segment({"alg": "HS256", "typ": "JWT"})
    claims = _segment(
        {
            "iss": "https://192.168.50.1:9443/oauth2",
            "aud": ["near-rt-ric-a1"],
            "scope": scope,
            "role": "A1_CONSUMER",
            "iat": issued,
            "nbf": issued - 1,
            "exp": issued + 120,
            "jti": jti,
            "cnf": {
                "x5t#S256": base64.urlsafe_b64encode(hashlib.sha256(der).digest())
                .rstrip(b"=")
                .decode("ascii")
            },
        }
    )
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, f"{header}.{claims}".encode("utf-8"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return "Bearer" + " " + f"{header}.{claims}.{signature}"


def _file_path(reference: str) -> str:
    parts = urlsplit(reference)
    if parts.scheme != "file" or not parts.path:
        raise LiveDriverError(f"credential reference is not a local file: {reference}")
    return parts.path


def scope_occupant_records(
    state_database: str | Path, *, amf_ue_ngap_id: int
) -> List[Dict[str, Any]]:
    """Every producer policy row whose scope carries this ``amfUeNgapId``.

    The whole row, read-only: the policy object, the status object and the
    producer's own bookkeeping.  This is what an archive has to hold for a
    withdrawal to be a move rather than a deletion.
    """
    path = Path(state_database)
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    records: List[Dict[str, Any]] = []
    try:
        for row in connection.execute(
            "select policy_id, policy_type_id, policy_json, status_json, digest, "
            "scope_key, revision, idempotency_key, status_seq, producer_epoch, "
            "fenced, updated_at from policies"
        ):
            try:
                policy = json.loads(row["policy_json"])
                identifier = int(
                    policy["scope"]["ueId"]["guAmfUeNgapId"]["amfUeNgapId"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            if identifier != int(amf_ue_ngap_id):
                continue
            records.append(
                {
                    "policyId": row["policy_id"],
                    "policyTypeId": row["policy_type_id"],
                    "policyObject": policy,
                    "status": json.loads(row["status_json"])
                    if row["status_json"]
                    else None,
                    "digest": row["digest"],
                    "scopeKey": row["scope_key"],
                    "revision": row["revision"],
                    "idempotencyKey": row["idempotency_key"],
                    "statusSeq": row["status_seq"],
                    "producerEpoch": row["producer_epoch"],
                    "fenced": row["fenced"],
                    "updatedAt": row["updated_at"],
                }
            )
    finally:
        connection.close()
    return records


def summarise_occupant(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The few fields a scope decision is made on.

    ``policyTypeId`` is the producer's own record of what the policy *is*, and
    it is carried because the A1-P delete path is keyed by policy type: a row
    withdrawn under the type this run happens to submit, rather than the type
    the row records, is a request about a policy that does not exist.
    """
    status = record.get("status") or {}
    aic = status.get("aicStatus") or {}
    return {
        "policyId": record["policyId"],
        "policyTypeId": record.get("policyTypeId"),
        "episodeState": aic.get("episodeState"),
        "episodeTerminal": aic.get("episodeTerminal"),
        "enforceStatus": status.get("enforceStatus"),
        "readback": (aic.get("readback") or {}).get("result"),
    }


def scope_occupants(
    state_database: str | Path, *, amf_ue_ngap_id: int
) -> List[Dict[str, Any]]:
    """Producer policies whose scope carries this ``amfUeNgapId``, read-only."""
    return [
        summarise_occupant(record)
        for record in scope_occupant_records(
            state_database, amf_ue_ngap_id=amf_ue_ngap_id
        )
    ]


def scope_withdrawal_plan(
    occupants: Sequence[Mapping[str, Any]], *, withdraw_verified: bool,
    policy_type_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Decide, per occupant, whether this run may withdraw it.

    Pure, so the rule can be read and tested without a transport.

    The producer admits **one policy per scope**, and it counts a policy that
    has already run its episode and expired as an occupant: the reverse-direction
    attempt at 08:47 was refused ``A1-P returned HTTP 409`` by the very policy
    the *successful* run had created for the same UE
    (``GATE3-OTA-20260824T084714Z-run.json``).  So "never withdraw a verified
    effect" and "run a second episode on the same UE" cannot both hold.

    They are reconciled by what withdrawal *means*: a policy row is a record,
    and a record that has been archived byte-for-byte before it is withdrawn
    has been moved, not erased.  ``withdraw_verified`` is therefore an explicit
    operator decision and :func:`clear_ue_scope` refuses to act on it without an
    archive.  A non-terminal leftover needs no such ceremony -- it is a stuck
    attempt holding a scope, and clearing it is what it is for.

    ``policy_type_id`` is the type *this run* submits.  An occupant of another
    type is marked ``FOREIGN`` and never withdrawn: the delete path is keyed by
    policy type, so withdrawing it under this run's type would address a policy
    that does not exist, and withdrawing it under its own would be this run
    deleting another objective's policy to make room for itself.  Naming it is
    the right answer -- an operator can then clear it deliberately, or discover
    that the deployment is running something they did not expect.
    """
    plan: List[Dict[str, Any]] = []
    for occupant in occupants:
        verified = occupant.get("episodeState") in TERMINAL_SUCCESS_STATES
        recorded_type = occupant.get("policyTypeId")
        if (policy_type_id is not None and recorded_type is not None
                and str(recorded_type) != str(policy_type_id)):
            decision, reason = "FOREIGN", (
                f"policy type {recorded_type} is not the {policy_type_id} this "
                "run submits")
        elif verified and not withdraw_verified:
            decision, reason = "KEEP", "verified effect"
        elif verified:
            decision, reason = "WITHDRAW", "verified effect, archived before withdrawal"
        else:
            decision, reason = "WITHDRAW", "non-terminal policy holding the scope"
        plan.append({**dict(occupant), "decision": decision, "reason": reason})
    return plan


def clear_ue_scope(
    binding: AssuranceLiveBinding,
    *,
    state_database: str | Path,
    amf_ue_ngap_id: int,
    policy_type_id: str,
    timeout_s: float = 10.0,
    withdraw_verified: bool = False,
    archive: Optional[Callable[[Sequence[Mapping[str, Any]]], None]] = None,
    verify_attempts: int = 5,
    verify_interval_s: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
) -> List[Dict[str, Any]]:
    """Free this UE's A1-P scope, without losing what was there.

    Returns one entry per policy considered, so the evidence says what was
    withdrawn, what was kept, and why.

    Every occupant is handed to *archive* -- complete rows, policy object and
    status object verbatim -- **before** the first DELETE leaves this process.
    Withdrawing a verified effect without one is refused rather than done
    quietly: the archive is what makes the difference between moving a record
    and destroying it, and a run that could skip it would eventually skip it.

    A withdrawal is only reported as one when the producer says so.  This used
    to record every DELETE as ``WITHDRAWN`` whatever came back, so a 401 from an
    expired credential, a 500, or a policy of a type this consumer may not touch
    all read as "scope cleared" -- and the run then walked into the HTTP 409 the
    clearing existed to prevent, with its own evidence saying the scope was
    free.  Now: a foreign policy type is refused rather than deleted, only
    :data:`WITHDRAWAL_ACCEPTED` counts as acceptance, anything else raises with
    the status and body, and the producer's own rows are re-read afterwards to
    confirm the scope is actually vacant before a caller composes anything.
    """
    records = scope_occupant_records(state_database, amf_ue_ngap_id=amf_ue_ngap_id)
    plan = scope_withdrawal_plan(
        [summarise_occupant(record) for record in records],
        withdraw_verified=withdraw_verified,
        policy_type_id=policy_type_id,
    )
    foreign = [entry for entry in plan if entry["decision"] == "FOREIGN"]
    if foreign:
        raise LiveDriverError(
            "this UE's A1-P scope is held by a policy of another type: "
            + "; ".join(f"{entry['policyId']} ({entry.get('policyTypeId')})"
                        for entry in foreign)
            + f". This run submits {policy_type_id} and will not delete another "
              "objective's policy to make room for itself. Withdraw it "
              "deliberately, or run against a UE whose scope is free.")
    withdrawing_verified = any(
        entry["decision"] == "WITHDRAW"
        and entry.get("episodeState") in TERMINAL_SUCCESS_STATES
        for entry in plan
    )
    if withdrawing_verified and archive is None:
        raise LiveDriverError(
            "withdrawing a verified effect needs an archive of it first; "
            "refusing to free a scope by destroying the record of what happened"
        )
    if records and archive is not None:
        archive(records)

    secrets = {
        key: _file_path(value) for key, value in binding.a1p.secret_refs.items()
    }
    parts = urlsplit(binding.a1p.base_url)
    host, port = parts.hostname, parts.port or 443
    actions: List[Dict[str, Any]] = []
    for index, entry in enumerate(plan, 1):
        if entry["decision"] == "KEEP":
            actions.append({**entry, "action": "KEPT"})
            continue
        # Under the type the *row* records, not the one this run submits: the
        # two are equal here (a foreign type was refused above) and saying so
        # in the request keeps that true if the rule ever loosens.
        recorded_type = str(entry.get("policyTypeId") or policy_type_id)
        connection = http.client.HTTPSConnection(
            host, port, timeout=timeout_s, context=_a1p_context(secrets)
        )
        try:
            connection.request(
                "DELETE",
                _A1P_POLICY_PATH.format(
                    policy_type=recorded_type, policy_id=entry["policyId"]
                ),
                headers={
                    "Authorization": _a1p_authorization(
                        secrets,
                        scope="a1.policy.write",
                        jti=f"g3ota-scope-clear-{index:08d}",
                    )
                },
            )
            response = connection.getresponse()
            # 자르기 **전에** 푼다.  프로듀서의 사유는 본문 뒤쪽(RFC7807 의
            # ``detail``, 또는 ``{"error": ...}``)에 있어서, 먼저 자르면 남는 것은
            # ``type``/``title`` 뿐이다 -- 2026-09-17 에 조종 503 네 건이 정확히
            # 그렇게 ``'status': 503, 'deta`` 에서 끊겨 하루 종일 원인을 몰랐다.
            raw = response.read().decode("utf-8", "replace")
            try:
                body = _problem_text(json.loads(raw))
            except (ValueError, TypeError):
                body = raw
            body = body[:200]
        finally:
            connection.close()
        if response.status in WITHDRAWAL_ACCEPTED:
            action = "WITHDRAWN"
        elif response.status in WITHDRAWAL_ABSENT:
            # Not an acceptance: the producer says it is not there, and the
            # vacancy re-read below is what decides whether that is true.
            action = "NOT_FOUND"
        else:
            raise LiveDriverError(
                f"withdrawing {entry['policyId']} from this UE's A1-P scope "
                f"returned HTTP {response.status}: {body!r}. The scope is not "
                "free and this run will not proceed as though it were.")
        actions.append({**entry, "action": action, "status": response.status,
                        "detail": body})

    expected_gone = {entry["policyId"] for entry in actions
                     if entry["action"] in ("WITHDRAWN", "NOT_FOUND")}
    if expected_gone:
        remaining = _surviving_occupants(
            state_database, amf_ue_ngap_id=amf_ue_ngap_id,
            expected_gone=expected_gone, attempts=verify_attempts,
            interval_s=verify_interval_s, sleep=sleep)
        if remaining:
            raise LiveDriverError(
                "the A1-P producer still records "
                + ", ".join(sorted(remaining))
                + f" on this UE's scope after withdrawal. One policy per scope: "
                  "composing now would be refused HTTP 409 by a policy this run "
                  "believes it removed.")
    return actions


def _surviving_occupants(
    state_database: str | Path, *, amf_ue_ngap_id: int,
    expected_gone: Sequence[str] | frozenset, attempts: int,
    interval_s: float, sleep: Callable[[float], None],
) -> List[str]:
    """Which policies this run withdrew are still on the scope, if any.

    Bounded and re-read rather than assumed: the producer commits its own row
    on its own schedule, so a single immediate read can see a policy that is
    already on its way out.  A few short retries tell that apart from a
    withdrawal that did not happen.
    """
    wanted = set(expected_gone)
    remaining: List[str] = []
    for attempt in range(max(1, int(attempts))):
        remaining = sorted(
            wanted.intersection(
                str(record["policyId"]) for record in scope_occupant_records(
                    state_database, amf_ue_ngap_id=amf_ue_ngap_id)))
        if not remaining:
            return []
        if attempt + 1 < max(1, int(attempts)):
            sleep(max(0.0, float(interval_s)))
    return remaining


# --------------------------------------------------------------------------- #
# ground-truth evidence: an acknowledgement is not an effect
# --------------------------------------------------------------------------- #


def producer_episode(database: str | Path, policy_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """The A1-P producer's own record for the policies this run created."""
    path = Path(database)
    if not path.is_file() or not policy_ids:
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows: List[Dict[str, Any]] = []
    try:
        for policy_id in policy_ids:
            row = connection.execute(
                "select policy_id, policy_json, status_json, revision, "
                "not_before_ns, expires_at_ns, fenced from policies where policy_id = ?",
                (policy_id,),
            ).fetchone()
            if row is None:
                rows.append({"policyId": policy_id, "present": False})
                continue
            rows.append(
                {
                    "policyId": row["policy_id"],
                    "present": True,
                    "revision": row["revision"],
                    "fenced": row["fenced"],
                    "policyObject": json.loads(row["policy_json"]),
                    "status": json.loads(row["status_json"]) if row["status_json"] else None,
                }
            )
    finally:
        connection.close()
    return rows


def kpm_slot_occupancy(path: str | Path, *, tail_lines: int = 400) -> Dict[str, Any]:
    """RRC connection occupancy per E2 node, from the tail of the live stream.

    The inversion of this -- source node 1 -> 0 and target node 0 -> 1 -- is the
    RAN's own statement that a UE moved, taken from a counter no policy writes.
    """
    source = Path(path)
    if not source.is_file():
        return {"path": str(source), "present": False}
    with source.open("rb") as stream:
        raw = stream.read()
    lines = raw.decode("utf-8", "replace").splitlines()[-int(tail_lines):]
    latest: Dict[str, Any] = {}
    ues: Dict[str, Any] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("event") != "kpm_indication":
            continue
        node = str(record.get("e2_node"))
        if record.get("kpm_msg_format") == 1:
            for metric in record.get("measurements") or []:
                if metric.get("name") == "RRC.ConnMean":
                    latest[node] = {
                        "rrcConnMean": metric.get("value"),
                        "recvUnixUs": record.get("recv_unix_us"),
                        "connectionEpoch": record.get("connection_epoch"),
                        "slot": record.get("slot"),
                    }
        elif record.get("kpm_msg_format") == 3:
            ues[node] = {
                "amfUeNgapIds": [
                    ue.get("amf_ue_ngap_id") for ue in (record.get("ues") or [])
                ],
                "recvUnixUs": record.get("recv_unix_us"),
                "slot": record.get("slot"),
            }
    return {
        "path": str(source),
        "present": True,
        "linesExamined": len(lines),
        "rrcConnMean": latest,
        "ueAttribution": ues,
    }


# --------------------------------------------------------------------------- #
# the R1 consumer
# --------------------------------------------------------------------------- #


def build_r1_policy_port(
    values: Mapping[str, Any], *, state_path: str | Path
) -> R1Client:
    """The real R1 consumer, mutually authenticated and authorized.

    ``R1Client`` refuses to exist without both halves and
    :func:`oran.rapp.r1_security.build_r1_security` names the reference a
    deployment still owes rather than downgrading the connection, so a missing
    credential fails here and never reaches the equipment.
    """
    security = build_r1_security(values)
    return R1Client(
        # 20 s, not the class default of 5.  An A1 write on this deployment is
        # not an HTTP round trip: the producer hands it to the Campaign-5 worker,
        # which sends the RC control and waits for the KPM readback before it
        # answers.  Measured over 174 writes of 2026-09-16/17 (the adapters' own
        # operations journals, IN_FLIGHT -> outcome):
        #
        #     CREATE  median 6.77 s  p90 8.32 s  max 15.39 s
        #     UPDATE  median 6.83 s  p90 7.28 s  max 15.78 s
        #     DELETE  median 5.29 s  p90 6.87 s  max 13.20 s
        #
        # Every one of them is longer than the 5 s default *at the median*.
        # CREATE survived only because a retry of an idempotent create finds the
        # work already done; UPDATE could not -- a PUT retry has to carry a
        # higher revision than the attempt that actually landed -- and 12 of 30
        # updates failed, each one reaching the Kernel as UNKNOWN -> PARTIAL_APPLY
        # and, on the retention trial of episode c995e62e, as an incident
        # lockdown with RECOVERY_FAILURE.  20 s clears the observed maximum, so
        # the bounded retries go back to being for lost packets rather than for
        # an operation that was always going to take longer than we allowed.
        #
        # Attempts drop 3 -> 2 in the same breath, and for a reason worth
        # stating: the Kernel's permit carries a lease (DEFAULT_LEASE_MS is
        # 30 s; COMMIT gets the contracted hold plus two action deadlines), and
        # an answer that arrives after the lease has expired is refused anyway.
        # 3 x 20 s would put the worst case at 60 s, past that default; 2 x 20 s
        # is 40 s and, with the first attempt now clearing the observed maximum,
        # the second is for a lost packet rather than for a slow producer.
        timeout_s=float(values.get("r1.timeoutSeconds", 20.0)),
        max_attempts=int(values.get("r1.maxAttempts", 2)),
        api_root=values["r1.apiRoot"],
        r_app_id=values["r1.rAppId"],
        policy_evidence_push_base_uri=values["r1.dme.policyEvidencePushBaseUri"],
        state_path=str(state_path),
        ssl_context=security.ssl_context,
        oauth_header_provider=security.oauth_header_provider,
    )


class RecordingPolicyPort:
    """Wrap the R1 consumer so a run can say what the transport actually did.

    The gateway adapter is right to reduce a transport failure to ``UNKNOWN``
    with a type name -- a permit-bound component has no business quoting an
    HTTP body into a Kernel event.  But an OTA run that could not create its
    policy has to be able to say *why*, and the composition root is the place
    that may know.  So every call is recorded here, outside the Kernel's event
    stream and outside the gateway's evidence refs, and the exception is
    re-raised unchanged.

    Recorded, never interpreted: this class does not retry, does not translate
    an error into a verdict and does not decide that a failure was benign.
    """

    def __init__(self, port: Any, *, detail_limit: int = 600) -> None:
        self._port = port
        self._detail_limit = int(detail_limit)
        self.calls: List[Dict[str, Any]] = []

    def _record(self, method: str, subject: Any, outcome: str, detail: str = "") -> None:
        self.calls.append(
            {
                "method": method,
                "subject": subject,
                "outcome": outcome,
                "detail": detail[: self._detail_limit],
            }
        )

    def _call(self, method: str, subject: Any, action: Callable[[], Any]) -> Any:
        try:
            result = action()
        except Exception as exc:
            self._record(method, subject, "RAISED", f"{type(exc).__name__}: {exc}")
            raise
        self._record(method, subject, "OK")
        return result

    # -- the R1PolicyPort surface -----------------------------------------

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        return self._call(
            "get_policy_type", policy_type_id,
            lambda: self._port.get_policy_type(policy_type_id),
        )

    def create_policy(
        self, near_rt_ric_id: str, policy_type_id: str, policy_object: Dict[str, Any]
    ) -> Mapping[str, Any]:
        return self._call(
            "create_policy", near_rt_ric_id,
            lambda: self._port.create_policy(
                near_rt_ric_id, policy_type_id, policy_object
            ),
        )

    def update_policy(self, policy_id: str, policy_object: Dict[str, Any]) -> Mapping[str, Any]:
        return self._call(
            "update_policy", policy_id,
            lambda: self._port.update_policy(policy_id, policy_object),
        )

    def delete_policy(self, policy_id: str) -> None:
        return self._call(
            "delete_policy", policy_id, lambda: self._port.delete_policy(policy_id)
        )

    def get_policy(self, policy_id: str) -> Mapping[str, Any]:
        # Forwarded so the adapter can ask the producer what fencingToken it
        # currently holds.  A retention case restarts the Kernel's fence at
        # zero while the producer still holds the search case's, and the PUT is
        # then refused ("policy update requires a newer revision and
        # fencingToken") -- three live episodes lost their retention to it.
        # The producer is the authority on its own fence, so read it.
        return self._call(
            "get_policy", policy_id, lambda: self._port.get_policy(policy_id),
        )

    def get_policy_status(self, policy_id: str) -> Mapping[str, Any]:
        return self._call(
            "get_policy_status", policy_id,
            lambda: self._port.get_policy_status(policy_id),
        )

    def declare_policy_type(self, policy_id: str, policy_type_id: str) -> None:
        # Forwarded because the adapter looks it up with ``getattr`` and skips
        # it quietly when absent.  Without it an adopted cap/priority policy --
        # one the *previous* case's client created -- was validated against the
        # steering schema by the new client: 2026-09-19 board 115202, the
        # identity rebind's first trial took over pfWeight@ue2 and died at
        # "Additional properties are not allowed ('config' was unexpected)",
        # then every status read of it failed the same way and the trial
        # locked down.
        declare = getattr(self._port, "declare_policy_type", None)
        if callable(declare):
            declare(policy_id, policy_type_id)

    # -- discovery, forwarded so the composition root sees one object ------

    def bootstrap_info(self) -> Any:
        return self._call("bootstrap_info", None, self._port.bootstrap_info)

    def discover_services(self, **kwargs: Any) -> Any:
        return self._call(
            "discover_services", kwargs.get("api_name"),
            lambda: self._port.discover_services(**kwargs),
        )

    def discover_policy_types(self) -> Any:
        return self._call(
            "discover_policy_types", None, self._port.discover_policy_types
        )


def build_policy_type_discovery(
    policy_port: Any, *, policy_type_id: str,
    capability_manifest: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Discover the policy type the translator validates against.

    Discovery is real: bootstrap, service discovery, the policy-type list and
    the type detail all come off the live R1 endpoint, and the translator then
    refuses unless the discovered schemas agree with both the capability
    manifest's digests and this tree's frozen copies.

    This endpoint advertises the schema *by digest* (``schemaSha256``) rather
    than inline, which is the profile's own compact form.  Resolving it to the
    pinned local copy is therefore a lookup, not a substitution -- and it is
    gated on all three digests agreeing first, so a deployment that had drifted
    could not be papered over by this step.  The same resolution the preserved
    consumer performs (``oran/rapp/coordinator_adapter.py``); done here rather
    than reused so the seam stays one-directional.
    """
    for step in ("bootstrap_info", "discover_services"):
        probe = getattr(policy_port, step, None)
        if callable(probe):
            probe()
    detail = dict(policy_port.get_policy_type(policy_type_id))
    if not isinstance(detail.get("policySchema"), Mapping):
        digest = detail.get("schemaSha256")
        if not isinstance(digest, str):
            raise LiveDriverError(
                "R1 advertised neither the policy schema nor its digest"
            )
        policy_schema = load_schema(f"{policy_type_id}.policy")
        status_schema = load_schema(f"{policy_type_id}.status")
        advertised = (capability_manifest or {}).get("schemaDigests", {})
        if (
            digest != jcs_sha256(policy_schema)
            or advertised.get("policy") != jcs_sha256(policy_schema)
            or advertised.get("status") != jcs_sha256(status_schema)
        ):
            raise LiveDriverError(
                "R1, capability manifest and pinned policy schemas disagree; "
                "refusing rather than translating against one of three"
            )
        detail["policySchema"] = policy_schema
        detail["statusSchema"] = status_schema
    listed = getattr(policy_port, "discover_policy_types", None)
    identifiers = listed() if callable(listed) else [policy_type_id]
    return {"policyTypeIds": identifiers, "policyTypeObject": detail}


# --------------------------------------------------------------------------- #
# the policy builder
# --------------------------------------------------------------------------- #


#: Validity of a steering restore body: long enough for the producer to enact
#: one handover (its actionDeadlineMs is 10 s), after which the UE stays put.
RESTORE_WINDOW_MS = 60_000
#: Added to the trial's policy revision for a restore write.
RESTORE_REVISION_OFFSET = 1000


@dataclass
class LivePolicyBuilder:
    """Turn one gateway command into a schema-validated A1 policy body.

    Every behaviour-bearing number comes from
    :func:`~assurance.contracts.actuation_request.derive_actuation_request`,
    which derives it from the frozen epoch and *refuses* where the contracts
    are silent.  The two things it cannot know are supplied here and are both
    observations rather than settings: the UE identity, read from the live
    indication stream, and the permit's own instants, read from the Kernel's
    record of the token it issued for this transaction.

    The identity is re-checked against the command's scope on every call.  A
    builder that quietly addressed a different UE than the one the plan is
    scoped to would produce a perfectly valid policy for the wrong subscriber.
    """

    kernel: Any
    contracts: Mapping[str, Any]
    deployment: LivePinToCellDeployment
    identity: LiveUeObservation
    case_id: str
    policy_type_discovery: Mapping[str, Any]
    capability_manifest: Mapping[str, Any]
    objective_kind: str = A1P_OBJECTIVE_KIND["UeCellSteeringPinToCell"]
    priority: IntentPriority = IntentPriority.MEDIUM
    #: The frozen translator reads only the priority off the intent for a
    #: PIN_TO_CELL; the type is carried because the signature requires one.
    intent_type: IntentType = IntentType.COVERAGE_GOAL
    bodies: List[Dict[str, Any]] = None
    #: The configuration axis this builder serves.  ``servingCell`` for every
    #: single-UE case; a joint case scopes it (``servingCell@<ue>``) so several
    #: UEs' steering axes can share one surface.
    axis: str = "servingCell"
    #: Where the command scope carries this UE.  ``None`` reads the plan
    #: scope's own ``ueId``; a joint case names each UE under its own key.
    scope_key: Optional[str] = None
    #: Restore writes drafted so far per trial: each one needs its own, higher revision.
    restores: Dict[int, int] = None

    def __post_init__(self) -> None:
        if self.bodies is None:
            self.bodies = []
        if self.restores is None:
            self.restores = {}
        if str(self.identity.amf_ue_ngap_id) != self.deployment.ue_scope_id:
            raise LiveDriverError(
                "the observed UE identity is not the one this deployment is scoped to"
            )

    def _next_restore(self, trial_index: int) -> int:
        n = self.restores.get(trial_index, 0)
        self.restores[trial_index] = n + 1
        return n

    def _scoped_ue(self, scope: Mapping[str, Any]) -> Any:
        if self.scope_key is None:
            return scope.get("ueId")
        entry = scope.get(self.scope_key)
        return entry.get("ueId") if isinstance(entry, Mapping) else None

    def _candidate_for(self, command: Mapping[str, Any]) -> Any:
        """The frozen candidate this command realises.

        The trial's own candidate when the command names a trial the Kernel
        has opened -- a joint catalog holds many -- and otherwise the target's
        first candidate, which is the single-candidate case as it always was.
        """
        catalog = self.kernel.current_catalog()
        state = self.kernel.reduced_state()
        trial = state["trials"].get(str(command.get("trialId") or ""))
        if trial is not None:
            wanted = trial.get("candidateId")
            lookup = getattr(catalog.candidates, "get", None)
            if callable(lookup):            # a frozen domain: decode, never walk
                found = lookup(wanted)
                if found is not None:
                    return found
            for item in (() if callable(lookup) else catalog.candidates):
                if item.candidate_id == wanted:
                    return item
        return next(
            item
            for item in catalog.candidates
            if item.target_ref == self.contracts["target"].contract_id
        )

    def __call__(self, command: Mapping[str, Any]) -> Dict[str, Any]:
        scope = command.get("scope") or {}
        named = self._scoped_ue(scope)
        # Two id spaces name the same UE and both are legitimate here.  A
        # numeric-labelled sitting scopes by the amfUeNgapId, and this check was
        # written for that.  A joint agent sitting scopes by the ROLE label:
        # ``assurance/objectives/joint.py`` writes
        # ``scope["ue@ue1"] = {"ueId": "ue1"}`` and the composition root keys this
        # builder ``scope_key="ue@ue1"`` (agent.py), so the value that comes back
        # is "ue1" while ``identity.amf_ue_ngap_id`` is 1.  Comparing only against
        # the numeric id therefore refused every steering command a joint sitting
        # ever issued -- which is why the steering axis has never moved a cell in
        # the v4 condition at all.  (An earlier count said "one cell change"; that
        # counted the REQUESTED configuration of a trial the same guard refused.
        # Counted by the OBSERVED ``servingCell`` it is zero: episode …1843 trial
        # 2, c4630d27 trial 4 and 0039b54c trial 6 all ended
        # "prepare: REJECTED ... names a different UE".)  Accept either
        # spelling; the guard's job is "this scope names MY UE", not "the scope
        # uses the numeric space".
        role = self.scope_key.split("@", 1)[1] if self.scope_key and "@" in self.scope_key else None
        if str(named) not in {str(self.identity.amf_ue_ngap_id),
                              *( {str(role)} if role is not None else set() )}:
            # Say what was compared.  The bare sentence fired twice over the air
            # on 2026-09-17 (episodes c4630d27 and 0039b54c, both on the only
            # steering proposal the search ever reached) and it was not
            # diagnosable from the record: this same branch is taken when the
            # scope carries no entry under THIS builder's key at all, in which
            # case ``named`` is None and no UE was named by anybody.  Those are
            # different faults and the message could not tell them apart.
            raise LiveDriverError(
                "the command's scope names a different UE than the observed "
                f"identity: scope key {self.scope_key!r} yielded {named!r}, "
                f"the observed identity is {self.identity.amf_ue_ngap_id!r}, "
                f"and the scope carried {sorted(scope)!r}"
            )
        axis, value = command.get("axis"), command.get("value")
        if axis != self.axis or value is None:
            raise LiveDriverError(
                f"this builder's configuration axis is {self.axis}, not {axis!r}"
            )
        candidate = self._candidate_for(command)
        # A handover is not undone by withdrawing the policy that made it, so a
        # restore pins the transaction's BASELINE cell (R1Adapter, steering
        # restore).  That value is the staged baseline, not the candidate's.
        restoring = str(command.get("operation")) in (
            GatewayOperation.UNDO.value, GatewayOperation.HALT.value)
        if not restoring and candidate.parameters.get(self.axis) != str(value):
            raise LiveDriverError(
                "the staged value is not the frozen candidate's parameter"
            )
        state = self.kernel.reduced_state()
        permit = state["transactions"].get(command["transactionId"]) or {}
        issued_at, lease_expiry = permit.get("issuedAt"), permit.get("leaseExpiry")
        if restoring and issued_at:
            # The applied policy's lease has usually run out by the time a
            # restore is written (2026-09-19 board 110539: expired 20 s before
            # HALT), and a body valid only in the past is stored EXPIRED and
            # never enacted.  The restore gets its own short window; expiry
            # after the handover does not move the UE again.
            now = datetime.now(timezone.utc)
            issued_at = format_utc(now)
            lease_expiry = format_utc(now + timedelta(milliseconds=RESTORE_WINDOW_MS))
        if not issued_at or not lease_expiry:
            raise LiveDriverError(
                "no permit is on record for this transaction; a policy must not "
                "outlive an authorisation it cannot name"
            )
        trial_id = str(command["trialId"])
        trial_index = self._trial_index(state, trial_id)
        # The policy's validity is the permit's lease and not a second clock,
        # so this reads the Kernel-recorded expiry and never rewrites it: a
        # policy that outlived the authorisation it was issued under would be
        # enforcing on nobody's authority.  What this *can* do is refuse.
        #
        # Only the command that actually installs the policy is held to the
        # contracted window.  ``VALIDATE`` composes a body to check it and
        # throws it away, and its permit is a short one precisely because
        # nothing survives it; requiring a hold-length lease there would
        # refuse every long-hold objective before it began.  ``APPLY`` leaves
        # the change standing through the hold and the reread that follows, so
        # its permit has to cover that window -- and the window is priced
        # where the permit is granted
        # (``assurance.kernel.kernel.contracted_lease_ms``), never stretched
        # here.
        if str(command.get("operation")) == GatewayOperation.APPLY.value:
            hold_ms = max(
                int(self.contracts[name].hold_ms)
                for name in ("measurement_min", "measurement_max")
            )
            finalize_margin_ms = 2 * min(
                int(bound.enforced_timeout_ms)
                for bound in self.contracts["harm"].bounds
                if int(bound.enforced_timeout_ms) > 0
            )
            required_expiry = parse_utc(issued_at) + timedelta(
                milliseconds=hold_ms + finalize_margin_ms
            )
            if parse_utc(lease_expiry) < required_expiry:
                raise LiveDriverError(
                    "the permit's lease expires at %s but this trial's frozen "
                    "contracts hold until %s; a policy must not outlive its "
                    "authorisation, so the lease is issued for the window or "
                    "the trial does not run"
                    % (lease_expiry, format_utc(required_expiry))
                )
        request = derive_actuation_request(
            target=self.contracts["target"],
            candidate=candidate,
            actuator=self.contracts["actuator"],
            harm=self.contracts["harm"],
            measurements=(
                self.contracts["measurement_min"],
                self.contracts["measurement_max"],
            ),
            case_policy=self.contracts["case_policy"],
            case_id=self.case_id,
            trial_id=trial_id,
            # A restore is a newer revision of the same policy (and so a new
            # idempotency key); the offset keeps it clear of every trial's own.
            # 2026-09-29 board 885: the HALT hand-back and the UNDO re-write both went out at
            # trial+1000 with different validity instants; R1 refused the second one
            # AIC_STALE_REVISION, the DELETE never followed, and the policy kept ue2's scope
            # (every later steer 409 AIC_POLICY_CONFLICT).  Each restore write rises by one.
            trial_index=trial_index + (RESTORE_REVISION_OFFSET + self._next_restore(trial_index)
                                       if restoring else 0),
            issued_at=issued_at,
            lease_expiry=lease_expiry,
            policy_scope=self.axis if self.scope_key is not None else None,
        )
        context = PolicyTranslationContext(
            ue_id=self.identity.ue_id(),
            allowed_cells=[self.deployment.topology.cell_object(int(value))],
            forbidden_cells=[],
            objective_kind=self.objective_kind,
            improvement_threshold_prb=None,
            min_seconds_between_actuations=request.min_seconds_between_actuations,
            required_kpi_freshness_ms=request.required_kpi_freshness_ms,
            action_deadline_ms=request.action_deadline_ms,
            not_before=request.not_before,
            expires_at=request.expires_at,
            rollback_on=list(ROLLBACK_ON),
            rollback_timeout_ms=request.rollback_timeout_ms,
            intent_revision=request.intent_revision,
            policy_revision=request.policy_revision,
            correlation_id=request.correlation_id,
            producer_id=request.producer_id,
            intent_id=request.intent_id,
        )
        body = translate_intent(
            {"type": self.intent_type, "priority": self.priority,
             "id": request.intent_id},
            policy_type_discovery=self.policy_type_discovery,
            capability_manifest=self.capability_manifest,
            context=context,
        )
        self.bodies.append(
            {
                "operation": command["operation"],
                "transactionId": command["transactionId"],
                "actuationRequest": request.to_canonical_dict(),
                "policyObject": json.loads(json.dumps(body)),
            }
        )
        return json.loads(json.dumps(body))

    @staticmethod
    def _trial_index(state: Mapping[str, Any], trial_id: str) -> int:
        """This trial's 1-based position in its case.

        The policy revision, so a retry is a new revision downstream rather
        than an indistinguishable repeat of the first attempt.
        """
        trial = state["trials"][trial_id]
        siblings = sorted(
            item["trialId"]
            for item in state["trials"].values()
            if item.get("caseId") == trial.get("caseId")
        )
        return siblings.index(trial_id) + 1
