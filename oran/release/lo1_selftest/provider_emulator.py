"""The contract-faithful Provider emulator -- seam **S4**, published first.

This module is the acceptance precondition for W1 and W2
(``work-split.1.0.0.json#/sequencing`` order 2,
``#/frozenInterfaces['oran/release/lo1_selftest/provider_emulator.py']``).  The
signatures below are frozen: behaviour may be extended behind them, the shape
may not change.

What makes this an *emulator* and not a stand-in for the Provider:

* **Built from frozen bytes only.**  Every input is one of the eight files
  ``emulator-boundary.1.0.0.json#/emulatorConstruction/permittedInputs`` names.
  Every NETCONF request it accepts must be byte-equal to one of the eight
  registered golden fixtures; a one-byte difference is an ``rpc-error`` and an
  unknown route.
* **It must diverge from golden where the live rule says it must.**
  ``RULE-O1-LIVE-VALUE-INVARIANTS`` forbids equality to a golden sample value
  for live input, so the emitted PM values are generated inside the ranges the
  frozen profile declares and are never equal to a golden sample.  Two
  consecutive runs therefore produce different PM digests.
* **It can never be production.**  ``EMULATOR_KIND`` is the only provider kind
  it reports and ``SELF_TEST_STATE_LABEL`` is the only label its artefacts may
  carry; the capture schema's ``profile`` branch, the runtime gate and this
  label must all agree, and ``LO1-ST-E01`` forces a disagreement in each.
* **Loopback only.**  Every listener binds ``127.0.0.1`` on an ephemeral port
  and exists only for the duration of a self-test run.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .frozen import FrozenBundle, PERMITTED_EMULATOR_INPUTS, json_pointer
from .mns_server import MnsRoutes, MnsServer
from .pm import PM_FAULTS, PmGenerator
from .ssh_provider import (
    NetconfObservations,
    NetconfSubsystem,
    SshProviderListener,
    generate_key_material,
    require_paramiko,
)
from .tls import mint_loopback_tls

#: Frozen: the only provider kind this package can ever present itself as.
EMULATOR_KIND: str = "CONTRACT_FAITHFUL_EMULATOR"

#: Frozen: the only state label any self-test artefact may carry.
SELF_TEST_STATE_LABEL: str = "UPPER_LIVE_O1_HARNESS_SELF_TEST"

#: Provider-side faults the falsifiers drive.  None is reachable on a clean run.
FAULT_NOTIFY_EVENT_TIME_MISMATCH = "NOTIFY_EVENT_TIME_MISMATCH"
FAULT_NOTIFY_EXPIRY_BEFORE_READY = "NOTIFY_EXPIRY_BEFORE_READY"
FAULT_NOTIFY_SUBSCRIPTION_MISMATCH = "NOTIFY_SUBSCRIPTION_MISMATCH"
#: A10: a sender that omits the notification boundary header.  The receiver
#: binds a notification to an active subscription through that header and never
#: mints it for itself, so a notification without it must be refused.  This
#: fault is what demonstrates the refusal over a real socket.
FAULT_NOTIFY_WITHOUT_SUBSCRIPTION_HEADER = "NOTIFY_WITHOUT_SUBSCRIPTION_HEADER"
FAULT_SUPPRESS_NOTIFICATION = "SUPPRESS_NOTIFICATION"
FAULT_WITHHOLD_PM_FILE = "WITHHOLD_PM_FILE"

PROVIDER_FAULTS = frozenset({
    FAULT_NOTIFY_EVENT_TIME_MISMATCH,
    FAULT_NOTIFY_EXPIRY_BEFORE_READY,
    FAULT_NOTIFY_SUBSCRIPTION_MISMATCH,
    FAULT_NOTIFY_WITHOUT_SUBSCRIPTION_HEADER,
    FAULT_SUPPRESS_NOTIFICATION,
    FAULT_WITHHOLD_PM_FILE,
}) | PM_FAULTS


class EmulatorError(RuntimeError):
    """The emulator refused to act; it never guesses and never degrades."""


@dataclass(frozen=True)
class EmulatorEndpoints:
    """Frozen seam S4.  Everything here is a loopback runtime value."""

    netconf: str
    mns_root: str
    sftp_authority: str
    host_key_sha256: str


@dataclass(frozen=True)
class EmulatorResult:
    """Frozen seam S4.  Provider-side observations, never a verdict."""

    scenario_id: str
    http_sequence: tuple[int, ...]
    observations: Mapping[str, Any]
    unknown_routes: int
    emitted_pm_sha256: tuple[str, ...]


def _utc_now() -> _datetime.datetime:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0)


def _instant(moment: _datetime.datetime) -> str:
    return moment.astimezone(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProviderEmulator:
    """Contract-faithful Provider emulator for the upper self-test."""

    def __init__(
        self,
        *,
        bundle_path: Path,
        vector: Mapping[str, Any],
        work_dir: Path,
        seed: int,
        consumer_notification_uri: str,
    ) -> None:
        require_paramiko()
        self.bundle = FrozenBundle(Path(bundle_path))
        self.vector = dict(vector)
        self.work_dir = Path(work_dir)
        self.seed = int(seed)
        self.consumer_notification_uri = consumer_notification_uri
        self._lock = threading.Lock()
        self._faults: set[str] = set()
        self._started = False
        self._stopped = False
        self._emitted: list[dict[str, Any]] = []
        self._collisions: list[str] = []
        self._netconf_observations = NetconfObservations()
        self._sftp_events: list[str] = []
        self._endpoints: EmulatorEndpoints | None = None
        #: PerfMetricJob datastore state, advanced only by the mutating fixtures.
        self._job_present = False
        self._job_administrative_state = str(
            self.bundle.netconf_profile["instance"]["configuration"]
            ["createAdministrativeState"])

        self._assert_permitted_inputs()

        self._pm_root = self.work_dir / "pm"
        self._key_root = self.work_dir / "keys"
        self._tls_root = self.work_dir / "tls"
        self._generator = PmGenerator(self.bundle, seed=self.seed)

        self._key_material = None
        self._netconf_listener: SshProviderListener | None = None
        self._sftp_listener: SshProviderListener | None = None
        self._mns: MnsServer | None = None
        self._tls = None

    # ------------------------------------------------------------- construction

    def _assert_permitted_inputs(self) -> None:
        """Refuse to start unless every permitted input is present and readable."""
        for name in PERMITTED_EMULATOR_INPUTS:
            self.bundle.raw(name)
        fixtures = self.bundle.golden_netconf_fixtures()
        if not fixtures:
            raise EmulatorError("no golden NETCONF fixture is registered")

    def _vector(self, pointer: str) -> Any:
        return json_pointer(self.vector, pointer)

    # ------------------------------------------------------------------ faults

    def inject(self, fault: str) -> None:
        """Arm one Provider-side fault.  Additive to the frozen seam."""
        if fault not in PROVIDER_FAULTS:
            raise EmulatorError(f"unknown Provider fault {fault!r}")
        self._faults.add(fault)

    @property
    def armed_faults(self) -> tuple[str, ...]:
        return tuple(sorted(self._faults))

    # --------------------------------------------------------------- lifecycle

    def start(self) -> EmulatorEndpoints:
        if self._started:
            raise EmulatorError("the emulator is already started")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._pm_root.mkdir(parents=True, exist_ok=True)
        self._key_material = generate_key_material(self._key_root)
        self._tls = mint_loopback_tls(self._tls_root, common_name="lo1-selftest-provider")

        fixtures = {}
        replies = {}
        for fixture in self.bundle.golden_netconf_fixtures():
            fixtures[fixture.raw] = (fixture.relative_path, fixture.sha256)
            replies[fixture.relative_path] = self._reply_for(fixture.relative_path)
        netconf = NetconfSubsystem(
            capabilities=self.bundle.required_netconf_capabilities(),
            fixtures=fixtures,
            replies=replies,
            observations=self._netconf_observations,
            lock=self._lock,
            reply_selector=self._select_reply,
        )
        self._netconf_listener = SshProviderListener(
            key_material=self._key_material, netconf=netconf)
        self._sftp_listener = SshProviderListener(
            key_material=self._key_material,
            sftp_root=self._pm_root,
            sftp_observer=self._sftp_events.append,
        )
        self._netconf_listener.start()
        self._sftp_listener.start()

        routes = MnsRoutes.derive(
            self.bundle.pa_file_profile,
            mns_version=str(self._vector("/o1/fileDataReporting/mnsVersion")),
        )
        self._mns = MnsServer(
            routes=routes, tls=self._tls, files_provider=self._files_listing)
        self._mns.start()

        self._started = True
        self._endpoints = EmulatorEndpoints(
            netconf=f"ssh://{self._netconf_listener.authority}",
            mns_root=self._mns.root,
            sftp_authority=self._sftp_listener.authority,
            host_key_sha256=self._key_material.host_key_sha256,
        )
        return self._endpoints

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for listener in (self._netconf_listener, self._sftp_listener):
            if listener is not None:
                listener.stop()
        if self._mns is not None:
            self._mns.stop()
        if self._key_material is not None:
            self._key_material.remove()
        # The run-scoped directory carries the ephemeral keypair and the minted
        # TLS material; it is removed whole so no key byte survives the run.
        shutil.rmtree(self._tls_root, ignore_errors=True)
        shutil.rmtree(self._key_root, ignore_errors=True)

    @property
    def endpoints(self) -> EmulatorEndpoints:
        if self._endpoints is None:
            raise EmulatorError("the emulator has not been started")
        return self._endpoints

    # ------------------------------------------------------------------ trust

    def trust_consumer(self, context: Any) -> None:
        """Bind the trust anchor the notification hop verifies against."""
        if self._mns is None:
            raise EmulatorError("the emulator has not been started")
        self._mns.trust_consumer(context)

    def tls_reference_map(self) -> dict[str, str]:
        if self._tls is None:
            raise EmulatorError("the emulator has not been started")
        return self._tls.reference_map(prefix="lo1-selftest/provider-tls")

    def client_tls_context(self) -> Any:
        if self._tls is None:
            raise EmulatorError("the emulator has not been started")
        return self._tls.client_context()

    def client_private_key(self) -> Any:
        if self._key_material is None:
            raise EmulatorError("the emulator has not been started")
        return self._key_material.client_key

    def advertised_capabilities(self) -> tuple[str, ...]:
        return self.bundle.required_netconf_capabilities()

    def secret_map(self) -> dict[str, str]:
        """``secretRef`` from the vector -> run-scoped path.  Never material."""
        if self._key_material is None or self._tls is None:
            raise EmulatorError("the emulator has not been started")
        return {
            str(self._vector("/o1/netconf/knownHostsRef")):
                str(self._key_material.known_hosts_path),
            str(self._vector("/o1/netconf/credentialRef")):
                str(self._key_material.client_key_path),
            str(self._vector("/o1/sftp/knownHostsRef")):
                str(self._key_material.known_hosts_path),
            str(self._vector("/o1/sftp/credentialRef")):
                str(self._key_material.client_key_path),
            str(self._vector("/security/truststoreRef")): str(self._tls.certificate),
        }

    # ------------------------------------------------------------------ NETCONF

    def _reply_for(self, relative_path: str) -> bytes:
        """Reply derived from the frozen lifecycle declaration, never invented."""
        name = relative_path.rsplit("/", 1)[-1]
        if name == "get-schema-mount.xml":
            return self._wrap(self._schema_mount_body())
        if name == "get-perfmetricjob.xml":
            return self._wrap(self._perf_metric_job_body())
        return self._wrap("  <ok/>\n")

    @staticmethod
    def _wrap(body: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">\n'
            f"{body}"
            "</rpc-reply>\n"
        ).encode("utf-8")

    def _schema_mount_body(self) -> str:
        """The RFC 8528 mount point and mounted YANG library the RPC asks for.

        ``get-schema-mount.xml`` filters both ``ietf-yang-schema-mount`` and
        ``ietf-yang-library:yang-library`` under the concrete ``SubNetwork``
        instance, and lifecycle ordinal 1 requires the mounted library to
        advertise the pinned module revisions and the required feature.  Both
        halves are therefore read out of ``moduleSet`` and
        ``schemaMount.requiredModuleFeatures``; nothing here is typed.
        """
        mount = self.bundle.netconf_profile["schemaMount"]
        features: dict[str, list[str]] = {}
        for entry in mount.get("requiredModuleFeatures", ()):
            features.setdefault(str(entry["module"]), []).append(str(entry["feature"]))
        modules = []
        for entry in self.bundle.netconf_profile["moduleSet"]:
            lines = [f"        <name>{entry['name']}</name>"]
            if entry.get("revision") is not None:
                lines.append(f"        <revision>{entry['revision']}</revision>")
            for feature in features.get(str(entry["name"]), ()):
                lines.append(f"        <feature>{feature}</feature>")
            modules.append("      <module>\n" + "\n".join(lines) + "\n      </module>\n")
        library = (
            '    <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library">\n'
            "     <module-set>\n"
            f"      <set-name>{mount['label']}</set-name>\n"
            + "".join(modules)
            + "     </module-set>\n"
            "    </yang-library>\n"
        )
        return (
            "  <data>\n"
            '   <SubNetwork xmlns="urn:3gpp:sa5:_3gpp-common-subnetwork">\n'
            "    <id>oran-lab</id>\n"
            f"{library}"
            "   </SubNetwork>\n"
            '   <schema-mounts xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-schema-mount">\n'
            "    <mount-point>\n"
            f"     <module>{mount['parentModule']}</module>\n"
            f"     <label>{mount['label']}</label>\n"
            f"     <config>{str(bool(mount.get('configRequired'))).lower()}</config>\n"
            f"     <{mount['schemaReferenceKind']}/>\n"
            "    </mount-point>\n"
            "   </schema-mounts>\n"
            "  </data>\n"
        )

    def _perf_metric_job_body(self) -> str:
        """The job as the datastore currently holds it.

        The frozen lifecycle declares ``get-perfmetricjob.xml`` twice -- LOCKED
        at ordinal 4 and UNLOCKED/ENABLED at ordinal 7 -- so a single static
        reply cannot be faithful to both.  The state is tracked from the
        mutating fixtures and every value still comes from the frozen bytes.
        """
        instance = self.bundle.netconf_profile["instance"]["configuration"]
        with self._lock:
            present = self._job_present
            administrative = self._job_administrative_state
        if not present:
            return "  <data/>\n"
        operational = next(
            (str(entry["expectedOperationalState"])
             for entry in self.bundle.netconf_lifecycle()
             if entry.get("expectedOperationalState")), None)
        lines = [
            "  <data>",
            f"    <jobId>{instance['jobId']}</jobId>",
            f"    <administrativeState>{administrative}</administrativeState>",
        ]
        if operational and administrative == str(instance["activeAdministrativeState"]):
            lines.append(f"    <operationalState>{operational}</operationalState>")
        lines.append("  </data>")
        return "\n".join(lines) + "\n"

    def _select_reply(self, relative_path: str, default: bytes) -> bytes:
        """Advance the declared job state, then answer from it."""
        name = relative_path.rsplit("/", 1)[-1]
        instance = self.bundle.netconf_profile["instance"]["configuration"]
        with self._lock:
            if name == "create-perfmetricjob-locked.xml":
                self._job_present = True
                self._job_administrative_state = str(
                    instance["createAdministrativeState"])
            elif name == "unlock-perfmetricjob.xml":
                self._job_administrative_state = str(
                    instance["activeAdministrativeState"])
            elif name == "lock-perfmetricjob.xml":
                self._job_administrative_state = str(
                    instance["createAdministrativeState"])
            elif name == "delete-perfmetricjob.xml":
                self._job_present = False
        if name == "get-perfmetricjob.xml":
            return self._wrap(self._perf_metric_job_body())
        return default

    @property
    def netconf_observations(self) -> NetconfObservations:
        return self._netconf_observations

    @property
    def unknown_route_count(self) -> int:
        with self._lock:
            mns = self._mns.observations.unknown_routes if self._mns else 0
            return mns + self._netconf_observations.unknown_routes

    @property
    def golden_value_collisions(self) -> tuple[str, ...]:
        return tuple(self._collisions)

    # ------------------------------------------------------------------ PM path

    def _granularity_seconds(self) -> int:
        return int(self.bundle.pa_file_profile["perfMetricJob"]["granularityPeriodSeconds"])

    def observed_window(self) -> tuple[str, str]:
        """Measurement window derived from the run's observed clock."""
        granularity = self._granularity_seconds()
        end = _utc_now()
        start = end - _datetime.timedelta(seconds=granularity)
        return _instant(start), _instant(end)

    def cell_dns(self) -> tuple[str, ...]:
        mappings = self._vector("/topology/cellMappings")
        return tuple(str(entry["managedObjectDn"]) for entry in mappings)

    def emit_pm_file(self, *, window_start: str, window_end: str) -> tuple[str, bytes]:
        if not self._started:
            raise EmulatorError("the emulator has not been started")
        generated = self._generator.generate(
            window_start=window_start,
            window_end=window_end,
            cell_dns=self.cell_dns(),
            job_id=str(self._vector("/o1/perfMetricJob/jobId")),
            faults=frozenset(self._faults & PM_FAULTS),
        )
        self._collisions.extend(generated.golden_value_collisions)
        target = self._pm_root / generated.file_name
        if FAULT_WITHHOLD_PM_FILE not in self._faults:
            target.write_bytes(generated.raw)
        digest = hashlib.sha256(generated.raw).hexdigest()
        record = {
            "fileName": generated.file_name,
            "sha256": digest,
            "byteCount": len(generated.raw),
            "windowStart": window_start,
            "windowEnd": window_end,
            "readyAt": _instant(_utc_now()),
            "present": FAULT_WITHHOLD_PM_FILE not in self._faults,
            "samples": [
                {
                    "measuredObjectDn": sample.measured_object_dn,
                    "measurementName": sample.measurement_name,
                    "position": sample.position,
                    "text": sample.text,
                }
                for sample in generated.samples
            ],
        }
        with self._lock:
            self._emitted.append(record)
        return generated.file_name, generated.raw

    def emitted_files(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(dict(entry) for entry in self._emitted)

    def _file_info(self, record: Mapping[str, Any]) -> dict[str, Any]:
        schema_file_info = self.bundle.vector_schema["$defs"]["fileInfo"]["properties"]
        ready = record["readyAt"]
        expires_at = _datetime.datetime.strptime(ready, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_datetime.timezone.utc) + _datetime.timedelta(days=1)
        expiration = _instant(expires_at)
        if FAULT_NOTIFY_EXPIRY_BEFORE_READY in self._faults:
            expiration = _instant(
                _datetime.datetime.strptime(ready, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_datetime.timezone.utc) - _datetime.timedelta(seconds=1))
        return {
            "fileLocation": f"sftp://{self.endpoints.sftp_authority}/pm/{record['fileName']}",
            "fileSize": int(record["byteCount"]),
            "fileReadyTime": ready,
            "fileExpirationTime": expiration,
            "fileCompression": "",
            "fileFormat": schema_file_info["fileFormat"]["const"],
            "fileDataType": schema_file_info["fileDataType"]["const"],
            "jobId": str(self._vector("/o1/perfMetricJob/jobId")),
        }

    def _files_listing(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._emitted)
        return [self._file_info(record) for record in records]

    # --------------------------------------------------------------- notification

    def notification_document(self) -> dict[str, Any]:
        """``notifyFileReady`` shaped from the golden document, valued from this run."""
        golden = self.bundle.golden_notification()
        with self._lock:
            records = list(self._emitted)
        if not records:
            raise EmulatorError("no PM file has been emitted yet")
        file_infos = [self._file_info(record) for record in records]
        event_time = file_infos[-1]["fileReadyTime"]
        if FAULT_NOTIFY_EVENT_TIME_MISMATCH in self._faults:
            skewed = _datetime.datetime.strptime(
                event_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_datetime.timezone.utc)
            event_time = _instant(skewed + _datetime.timedelta(seconds=31))
        system_dn = str(self._vector("/o1/fileDataReporting/mnsAgentDn"))
        if FAULT_NOTIFY_SUBSCRIPTION_MISMATCH in self._faults:
            system_dn = system_dn + ",MnsAgent=not-subscribed"
        document = {
            "href": str(self._vector("/o1/perfMetricJob/managedObjectUri")),
            "notificationId": len(records),
            "notificationType": str(
                self.bundle.pa_file_profile["delivery"]["notificationType"]),
            "eventTime": event_time,
            "systemDN": system_dn,
            "fileInfoList": file_infos[-1:],
        }
        missing = sorted(set(golden) - set(document))
        if missing:
            raise EmulatorError(
                f"the notification lost golden members {missing}; the shape is frozen")
        return document

    def send_file_ready(self) -> int:
        if self._mns is None:
            raise EmulatorError("the emulator has not been started")
        if FAULT_SUPPRESS_NOTIFICATION in self._faults:
            return 0
        return self._mns.send_notification(
            self.consumer_notification_uri, self.notification_document(),
            carry_subscription_header=(
                FAULT_NOTIFY_WITHOUT_SUBSCRIPTION_HEADER not in self._faults))

    # ------------------------------------------------------------------ scenario

    def run_scenario(self, scenario_id: str) -> EmulatorResult:
        """Drive the Provider side of ``scenario_id``.

        The scenario's shape is read from the frozen catalog: the emulator
        refuses a scenario whose assignment does not resolve to the live-O1
        execution profile, so it cannot be pointed at an arbitrary scenario.
        """
        if not self._started:
            raise EmulatorError("the emulator has not been started")
        profile = self.bundle.execution_profile(scenario_id)
        if profile != "live-O1":
            raise EmulatorError(
                f"{scenario_id} is assigned to execution profile {profile!r}; the "
                "emulator serves the live-O1 profile only")
        declared_ops = {step.get("op") for step in self.bundle.steps(scenario_id)}
        if "O1_NOTIFY" not in declared_ops or "O1_RETRIEVE" not in declared_ops:
            raise EmulatorError(
                f"{scenario_id} declares no O1 notify/retrieve pair; nothing for the "
                "Provider emulator to serve")

        window_start, window_end = self.observed_window()
        self.emit_pm_file(window_start=window_start, window_end=window_end)
        notification_status = self.send_file_ready()

        with self._lock:
            mns_requests = list(self._mns.observations.requests) if self._mns else []
            sftp_events = list(self._sftp_events)
            netconf = {
                "advertisedCapabilities": list(
                    self._netconf_observations.advertised_capabilities),
                "exchangeCount": len(self._netconf_observations.exchanges),
                "unknownRoutes": self._netconf_observations.unknown_routes,
                "acceptedFixtures": [
                    exchange.fixture_ref
                    for exchange in self._netconf_observations.exchanges
                    if exchange.fixture_ref
                ],
                # RFC 6242: which framing the session actually settled on, how
                # many chunks each message took, and every framing refusal.
                # Measured on the wire, so "the peers agreed" is checkable
                # rather than assumed.
                "framingMode": self._netconf_observations.framing_mode,
                "requestChunkCounts": list(
                    self._netconf_observations.request_chunk_counts),
                "replyChunkCounts": list(
                    self._netconf_observations.reply_chunk_counts),
                "framingViolations": list(
                    self._netconf_observations.framing_violations),
            }
            emitted = list(self._emitted)

        http_sequence = tuple(
            [int(entry["status"]) for entry in mns_requests]
            + ([notification_status] if notification_status else [])
        )
        observations = {
            "providerKind": EMULATOR_KIND,
            "selfTestLabel": SELF_TEST_STATE_LABEL,
            "scenarioId": scenario_id,
            "executionProfile": profile,
            "endpoints": {
                "netconf": self.endpoints.netconf,
                "mnsRoot": self.endpoints.mns_root,
                "sftpAuthority": self.endpoints.sftp_authority,
                "hostKeySha256": self.endpoints.host_key_sha256,
            },
            "mnsRequests": mns_requests,
            "sftpEvents": sftp_events,
            "netconf": netconf,
            "emittedFiles": emitted,
            "notificationStatus": notification_status,
            "armedFaults": self.armed_faults,
            "goldenValueCollisions": list(self._collisions),
            "permittedInputDigests": self.bundle.permitted_input_digests(),
        }
        return EmulatorResult(
            scenario_id=scenario_id,
            http_sequence=http_sequence,
            observations=observations,
            unknown_routes=self.unknown_route_count,
            emitted_pm_sha256=tuple(record["sha256"] for record in emitted),
        )

    # ---------------------------------------------------------------- reporting

    def interface_declaration(self) -> dict[str, Any]:
        """Machine-readable publication of seam S4, for W1 and W2 to code against."""
        return {
            "seam": "S4",
            "module": "oran/release/lo1_selftest/provider_emulator.py",
            "emulatorKind": EMULATOR_KIND,
            "selfTestStateLabel": SELF_TEST_STATE_LABEL,
            "endpoints": {
                "netconf": ("ssh://<loopback>:<ephemeral>  (subsystem 'netconf', "
                            "RFC 6242 framing: ']]>]]>' for the hello exchange, "
                            "chunked once both peers advertise base:1.1)"),
                "mnsRoot": "https://<loopback>:<ephemeral>",
                "sftpAuthority": "<loopback>:<ephemeral>  (subsystem 'sftp', read-only)",
                "hostKeySha256": "sha256 of the ephemeral host key, a runtime pin",
            },
            "authentication": {
                "ssh": "publickey only; the credential resolves through the vector secretRef",
                "https": "TLS 1.2+; the trust anchor resolves through security.truststoreRef",
            },
            "netconfContract": {
                "acceptedRequests": "byte-equal to a registered golden fixture, only",
                "onMismatch": "rpc-error operation-not-supported + unknown_route_count += 1",
                "registeredFixtures": [
                    fixture.relative_path
                    for fixture in self.bundle.golden_netconf_fixtures()
                ],
            },
            "pmContract": {
                "valuesDivergeFromGolden": True,
                "windowSource": "the run's observed clock",
                "digestStability": "different on every run, by design",
            },
            "permittedInputs": list(PERMITTED_EMULATOR_INPUTS),
            "faults": sorted(PROVIDER_FAULTS),
        }


def publish_interface(bundle_path: Path) -> dict[str, Any]:
    """Publish seam S4 without starting a listener (W1/W2 read this first)."""
    bundle = FrozenBundle(Path(bundle_path))
    return {
        "seam": "S4",
        "module": "oran/release/lo1_selftest/provider_emulator.py",
        "emulatorKind": EMULATOR_KIND,
        "selfTestStateLabel": SELF_TEST_STATE_LABEL,
        "frozenSymbols": [
            "EMULATOR_KIND", "SELF_TEST_STATE_LABEL",
            "EmulatorEndpoints(netconf, mns_root, sftp_authority, host_key_sha256)",
            "EmulatorResult(scenario_id, http_sequence, observations, unknown_routes, "
            "emitted_pm_sha256)",
            "ProviderEmulator(bundle_path, vector, work_dir, seed, consumer_notification_uri)",
            "ProviderEmulator.start() -> EmulatorEndpoints",
            "ProviderEmulator.secret_map() -> dict[str, str]",
            "ProviderEmulator.emit_pm_file(window_start, window_end) -> (str, bytes)",
            "ProviderEmulator.send_file_ready() -> int",
            "ProviderEmulator.run_scenario(scenario_id) -> EmulatorResult",
            "ProviderEmulator.stop() -> None",
            "ProviderEmulator.unknown_route_count -> int",
            "ProviderEmulator.golden_value_collisions -> tuple[str, ...]",
        ],
        "registeredNetconfFixtures": {
            fixture.relative_path: fixture.sha256
            for fixture in bundle.golden_netconf_fixtures()
        },
        "permittedInputDigests": bundle.permitted_input_digests(),
        "requiredNetconfCapabilities": list(bundle.required_netconf_capabilities()),
        "goldenSampleValuesForbiddenOnTheWire": sorted(bundle.golden_sample_values()),
        "notAcceptance": (
            "The emulator can never stand in for the lower Provider in an SC-084 "
            "acceptance run and can never satisfy the Provider acceptance contract."
        ),
    }


# ---------------------------------------------------------------------------
# Additive seam helpers.  These do NOT change any frozen signature; they close
# the bootstrap loop between the emulator's own runtime endpoints and the
# deployment vector that names them.  A vector cannot carry the emulator's
# ephemeral ports before the emulator has bound them, so the final vector is
# adopted once both sides exist.
# ---------------------------------------------------------------------------

def _adopt_vector(self: ProviderEmulator, vector: Mapping[str, Any]) -> None:
    if not self._started:
        raise EmulatorError("adopt_vector is only meaningful after start()")
    self.vector = dict(vector)


def _adopt_consumer_uri(self: ProviderEmulator, uri: str) -> None:
    self.consumer_notification_uri = uri


ProviderEmulator.adopt_vector = _adopt_vector  # type: ignore[attr-defined]
ProviderEmulator.adopt_consumer_uri = _adopt_consumer_uri  # type: ignore[attr-defined]
