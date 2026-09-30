"""Read, and on request unlock, the O1 PerfMetricJob over the frozen NETCONF client.

Gate 5 stage 1 judged ``RRU.PrbDl`` "not delivered" because the PM directory
holds no TS 32.435 ``measCollecFile`` at all.  That judgement was about
*delivery*, and the dispatcher's re-examination asks the right follow-up: the
8/19 session did emit real 32.435 files with Provider 1.0.4 and returned the
``PerfMetricJob`` to ``LOCKED`` at shutdown.  A job that is locked emits
nothing.  If that is why the directory is empty, the missing counter is an
**operational state**, not an absent capability -- and QoSTarget and QoSandTSP
would be blocked by a switch rather than by physics.

This module answers that with a real NETCONF read, and can perform the unlock,
but keeps the two strictly apart:

``probe``
    ``<get>`` the job and report its administrative and operational state.
    Read-only; safe to run against a deployment with no gNB.

``unlock``
    ``<edit-config>`` the administrative state to ``UNLOCKED`` and read it
    back.  Refuses unless ``--i-am-unlocking`` is passed, because turning a
    measurement job on is a configuration change to the managed element and
    should not be a side effect of asking what state it is in.

Neither touches the RAN.  The O1 provider is a container and is independent of
the USRP; with no gNB running the job will report no measurements, which is the
expected and uninteresting answer -- the interesting answer is the state field.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from xml.etree import ElementTree

from oran.rapp.headless import load_integration_values
from oran.release.lo1.egress import EgressGuard
from oran.release.lo1.o1_netconf import PinnedNetconfSession, parse_netconf_endpoint

__all__ = ["JobState", "probe_job", "unlock_job", "main"]

DEFAULT_VALUES = (
    "/opt/ran-lab/controller/oran-deploy/session-20260819/deployment/integration-values.json"
)

#: The frozen contract bundle whose golden RPCs this tool replays.  Writing the
#: XML by hand would make this a second opinion about what an unlock is; the
#: release already froze the sequence and the request bodies, so the tool's job
#: is to send them in order and report what came back.
BUNDLE = Path("contracts/oran-aic/1.0.1/shared-contract-bundle")
PROFILE = BUNDLE / "o1-netconf-yang-profile.1.0.0.json"

#: ``#/lifecycle`` ordinals 1..8 -- schema mount, lock running, create the job
#: LOCKED, read it back, the durable subscription, unlock the job, read it back,
#: unlock running.
#:
#: Ordinal 5 carries an ``externalPrecondition`` rather than a
#: ``requestFixture`` because it is not NETCONF at all: it is the
#: FileDataReportingMnS subscription, over HTTPS.  It was skipped in the first
#: version of this tool, and the managed element said no -- the Provider's
#: sysrepo agent runs with ``--subscription-ready`` and refuses the unlock while
#: that witness reads ``0``.  The ordering the profile writes down is enforced
#: by the thing being configured, so ordinal 5 is now performed here, by
#: :mod:`tools.g5ota.filedatasub`, and its durable readback is the recorded
#: precondition for ordinal 6.
LIFECYCLE = "lifecycle"
TEARDOWN = "teardown"

#: Ordinal 3 creates with ``nc:operation="create"``, which is not idempotent by
#: design: replaying it against an existing job is a ``data-exists`` refusal,
#: correctly.  A lifecycle that aborted at ordinal 6 therefore cannot simply be
#: re-run from the top.  Rather than weaken the frozen fixture, the tool reads
#: the job first and treats an already-present job that reads back ``LOCKED``
#: with the frozen metrics as ordinal 3 already satisfied -- which is what it
#: is.  A job present in any other shape is not resumed; it is reported.
RESUMABLE_CREATE_ORDINAL = 3


class JobState(dict):
    """What the managed element said about the job, plus how we asked."""


def _identifiers(values: Mapping[str, Any]) -> Dict[str, str]:
    """Split the configured distinguished name into its two identifiers."""
    dn = str(values["o1.perfMetricJob.managedObjectDn"])
    parts = dict(
        piece.split("=", 1) for piece in dn.split(",") if "=" in piece
    )
    return {
        "dn": dn,
        "element": parts.get("ManagedElement", ""),
        "job": parts.get("PerfMetricJob", ""),
    }


def _file_path(reference: str) -> Path:
    parts = urlsplit(str(reference))
    if parts.scheme != "file" or not parts.path:
        raise ValueError(f"credential reference is not a local file: {reference}")
    return Path(parts.path)


#: The provider's NETCONF account.  The integration-values endpoint is recorded
#: without one (``ssh://192.168.50.1:830``) and the frozen client refuses a
#: non-loopback endpoint that carries no username, so it is supplied here and
#: read back from the container's own passwd rather than guessed:
#: ``o1netconf`` uid 999 is the only NETCONF login the provider image defines.
NETCONF_USERNAME = "o1netconf"

#: The base capabilities this client advertises in its hello.
NETCONF_BASE_CAPABILITIES = (
    "urn:ietf:params:netconf:base:1.0",
    "urn:ietf:params:netconf:base:1.1",
)


def _open_session(
    values: Mapping[str, Any], *, username: str = NETCONF_USERNAME
) -> PinnedNetconfSession:
    raw = str(values["o1.netconf.endpoint"])
    if "@" not in raw:
        scheme, _, rest = raw.partition("://")
        raw = f"{scheme}://{username}@{rest}"
    endpoint = parse_netconf_endpoint(raw)
    guard = EgressGuard(allowlist=(endpoint.authority,))
    session = PinnedNetconfSession(
        endpoint=endpoint,
        known_hosts_path=_file_path(values["o1.netconf.knownHostsRef"]),
        credential_path=_file_path(values["o1.netconf.credentialRef"]),
        guard=guard,
        allowed_authorities=(endpoint.authority,),
        # What this CLIENT advertises, which is also what the session
        # negotiates framing from.  An empty list makes a hello with no
        # capabilities at all -- malformed, and netopeer2 closes the channel on
        # the first RPC rather than answering it.  Both base versions are
        # advertised so the session settles on RFC 6242 chunked framing, which
        # is what a peer advertising base:1.1 will insist on.
        required_capabilities=NETCONF_BASE_CAPABILITIES,
    )
    session.open()
    return session


def _text(reply: bytes, tag: str) -> Optional[str]:
    try:
        root = ElementTree.fromstring(reply.decode("utf-8", "replace"))
    except ElementTree.ParseError:
        return None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == tag and (element.text or "").strip():
            return element.text.strip()
    return None


def _fixture(reference: str) -> bytes:
    """One golden RPC, as bytes, with its message-id left exactly as frozen."""
    return (BUNDLE / reference).read_bytes()


def _steps(section: str) -> Sequence[Mapping[str, Any]]:
    """Every ordinal of a section, fixture-bearing or not.

    Ordinal 5 has no fixture and used to be filtered out here, which is exactly
    how it came to be skipped.  The filter now lives at the point of use, where
    a step without a fixture is dispatched rather than dropped.
    """
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    return list(profile.get(section, []))


def probe_job(values: Mapping[str, Any]) -> JobState:
    """Read the job.  One RPC, no configuration change, safe with no gNB."""
    session = _open_session(values)
    try:
        reply = session.exchange(_fixture("golden/o1/netconf/get-perfmetricjob.xml"))
    finally:
        session.close()
    body = reply.decode("utf-8", "replace")
    ids = _identifiers(values)
    return JobState(
        {
            "action": "probe",
            "dn": ids["dn"],
            "jobPresent": "PerfMetricJob" in body,
            "administrativeState": _text(reply, "administrativeState"),
            "operationalState": _text(reply, "operationalState"),
            "rpcError": _text(reply, "error-message"),
            "raw": body[:900],
        }
    )


def _external_step(entry: Mapping[str, Any],
                   values: Mapping[str, Any]) -> Dict[str, Any]:
    """Perform an ordinal that is a precondition rather than an RPC.

    Only ``SUBSCRIPTION_DURABLE`` exists today.  It is answered by reading the
    persisted identifier back off the filesystem -- not by remembering that a
    subscription was created earlier in this process -- because "durable" is
    the whole content of the precondition.
    """
    from tools.g5ota.filedatasub import confirm_subscription

    state = str(entry.get("state") or "")
    if state != "SUBSCRIPTION_DURABLE":
        return {"ordinal": entry.get("ordinal"), "state": state, "ok": False,
                "error": "unknown external precondition; refusing to continue"}
    try:
        confirmed = confirm_subscription(values)
    except Exception as exc:  # noqa: BLE001 - the reason is the finding
        return {"ordinal": entry.get("ordinal"), "state": state, "ok": False,
                "error": "%s: %s" % (type(exc).__name__, exc)}
    return {
        "ordinal": entry.get("ordinal"),
        "state": state,
        "externalPrecondition": str(entry.get("externalPrecondition") or ""),
        "ok": bool(confirmed["durable"]),
        "error": None,
        "subscriptionId": confirmed["subscriptionId"],
        "durableStatePath": confirmed["durableStatePath"],
    }


def _job_already_created(values: Mapping[str, Any],
                         session: PinnedNetconfSession) -> Dict[str, Any]:
    """Is ordinal 3's postcondition already true?

    True only for the frozen shape: the job present, ``LOCKED``, carrying both
    performance metrics the golden create declares.  Anything else is reported
    as not-satisfied so the create runs and fails loudly.
    """
    reply = session.exchange(_fixture("golden/o1/netconf/get-perfmetricjob.xml"))
    body = reply.decode("utf-8", "replace")
    administrative = _text(reply, "administrativeState")
    satisfied = (
        "PerfMetricJob" in body
        and administrative == "LOCKED"
        and "RRU.PrbDl" in body
        and "DRB.UEThpDl" in body
    )
    return {"satisfied": satisfied, "administrativeState": administrative}


def _rpc_step(entry: Mapping[str, Any], session: PinnedNetconfSession, *,
              readback_timeout_ms: int) -> Dict[str, Any]:
    """One golden RPC, and the frozen readback window if the ordinal declares one.

    ``eventualReadbackTimeoutMs`` is the profile's own statement that a state
    is allowed to settle -- ordinal 7 expects ``UNLOCKED`` immediately but
    ``ENABLED`` only eventually, because the agent has to start the job.  The
    window is bounded and every attempt is counted, so "it settled" stays a
    measurement rather than an assumption; outside a declared window there is
    no retry at all.
    """
    reference = str(entry["requestFixture"])
    request = _fixture(reference)
    window_ms = int(entry.get("eventualReadbackTimeoutMs") or 0)
    expected = {
        "administrativeState": entry.get("expectedAdministrativeState"),
        "operationalState": entry.get("expectedOperationalState"),
    }
    expected = {name: value for name, value in expected.items() if value}
    deadline = time.monotonic() + min(window_ms, readback_timeout_ms) / 1000.0
    attempts = 0
    while True:
        attempts += 1
        reply = session.exchange(request)
        body = reply.decode("utf-8", "replace")
        observed = {name: _text(reply, name) for name in expected}
        matched = all(observed[name] == value for name, value in expected.items())
        if matched or not expected or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    record: Dict[str, Any] = {
        "ordinal": entry.get("ordinal"),
        "fixture": reference,
        "ok": ("<ok" in body or "<data" in body) and (matched or not expected),
        "error": _text(reply, "error-message"),
        "administrativeState": _text(reply, "administrativeState"),
        "reply": body[:400],
    }
    if expected:
        record["expected"] = expected
        record["observed"] = observed
        record["readbackAttempts"] = attempts
        if not matched:
            record["error"] = record["error"] or (
                "the readback never reached the state ordinal %s declares"
                % entry.get("ordinal"))
    return record


def unlock_job(values: Mapping[str, Any], *,
               readback_timeout_ms: int = 30000) -> JobState:
    """Drive the frozen readiness lifecycle: create LOCKED, then unlock.

    The job is *deleted* at teardown, not merely locked, so bringing PM back is
    the whole of ``#/lifecycle`` rather than a single edit.  Each golden RPC is
    replayed in its frozen order and every reply is recorded, so a step that
    fails names itself instead of leaving a half-configured managed element.

    Two ordinals are not plain replays.  Ordinal 3 is skipped when the job it
    would create is already present in the frozen shape, because
    ``nc:operation="create"`` is deliberately not idempotent.  Ordinal 5 is the
    durable subscription, which is HTTPS rather than NETCONF, and is confirmed
    from the filesystem before ordinal 6 is allowed to run at all.
    """
    steps = _steps(LIFECYCLE)
    session = _open_session(values)
    performed: list[Dict[str, Any]] = []
    aborted: Optional[str] = None
    try:
        preexisting = _job_already_created(values, session)
        for entry in steps:
            ordinal = entry.get("ordinal")
            reference = entry.get("requestFixture")
            if reference is None:
                performed.append(_external_step(entry, values))
            elif (ordinal == RESUMABLE_CREATE_ORDINAL
                  and preexisting["satisfied"]):
                performed.append({
                    "ordinal": ordinal,
                    "fixture": str(reference),
                    "ok": True,
                    "error": None,
                    "skipped": "the job is already present and reads back "
                               "LOCKED with the frozen metrics",
                    "administrativeState": preexisting["administrativeState"],
                })
                continue
            else:
                performed.append(_rpc_step(
                    entry, session,
                    readback_timeout_ms=readback_timeout_ms))
            if performed[-1]["error"] or not performed[-1]["ok"]:
                aborted = "ordinal %s did not reach %s" % (
                    ordinal, entry.get("state"))
                break
        final = session.exchange(_fixture("golden/o1/netconf/get-perfmetricjob.xml"))
    finally:
        session.close()
    return JobState(
        {
            "action": "unlock",
            "dn": _identifiers(values)["dn"],
            "aborted": aborted,
            "steps": performed,
            "administrativeStateAfter": _text(final, "administrativeState"),
            "operationalStateAfter": _text(final, "operationalState"),
            "rawFinal": final.decode("utf-8", "replace")[:600],
        }
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--integration-values", default=DEFAULT_VALUES)
    parser.add_argument("--unlock", action="store_true",
                        help="set the job UNLOCKED (a configuration change)")
    parser.add_argument("--i-am-unlocking", action="store_true",
                        help="required alongside --unlock; turning a measurement "
                             "job on must be deliberate, not a side effect")
    args = parser.parse_args(argv)
    values = load_integration_values(args.integration_values)
    try:
        if args.unlock:
            if not args.i_am_unlocking:
                json.dump({"refused": "pass --i-am-unlocking to change configuration"},
                          sys.stdout, indent=2, sort_keys=True)
                sys.stdout.write("\n")
                return 2
            result: Mapping[str, Any] = unlock_job(values)
        else:
            result = probe_job(values)
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason
        import traceback

        json.dump({"error": type(exc).__name__, "detail": str(exc),
                   "traceback": traceback.format_exc().splitlines()[-6:]},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 3
    json.dump(dict(result), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
