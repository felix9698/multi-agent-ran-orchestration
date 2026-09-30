"""The falsification suite.  Every entry proves a real, non-zero, fail-closed exit.

The list of falsifiers is **read** from
``docs/upper-live-o1-harness/release-gates.1.0.0.json#/falsifiers`` at run time,
not restated here, so a falsifier the design declares and this module does not
implement is a coverage failure rather than a silent gap.

Each falsifier is a sequence of *steps*, and each step is a real subprocess:

* a **control** step establishes that the clean substrate passes (exit 0).
  Without it, "always exits non-zero" would pass trivially;
* one or more **defect** steps inject exactly one defect and require a non-zero
  exit, and where the check is specific they also require the named finding.

Nothing here asserts about intent.  ``FalsifierOutcome.steps`` carries the
argv and the observed exit code of every process that ran, and the report is
the evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import EXIT_OK, SELF_TEST_STATE_LABEL
from .frozen import BUNDLE_RELATIVE, repository_bundle, sha256_bytes
from .scratch import (
    copy_bundle,
    mutate_expected_counter,
    mutate_expected_status,
    mutate_golden_netconf_fixture,
    mutate_netconf_profile_capability,
    mutate_pa_file_namespace,
    mutate_step_expected_status,
    mutate_step_id,
    snapshot_digests,
)

MODULE = "oran.release.lo1_selftest"
DESIGN_DIR = Path("docs") / "upper-live-o1-harness"


@dataclass
class Step:
    name: str
    argv: list[str]
    expect: str            # "ZERO" or "NON_ZERO"
    exit_code: int | None = None
    stdout: dict[str, Any] | None = None
    ok: bool = False
    required_findings: tuple[str, ...] = ()
    detail: str = ""

    def as_report(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": self.argv,
            "expect": self.expect,
            "exitCode": self.exit_code,
            "ok": self.ok,
            "requiredFindings": list(self.required_findings),
            "detail": self.detail,
        }


@dataclass
class FalsifierOutcome:
    id: str
    task_clause: str
    falsified: bool
    steps: list[Step] = field(default_factory=list)
    detail: str = ""

    def as_report(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "taskClause": self.task_clause,
            "falsified": self.falsified,
            "steps": [step.as_report() for step in self.steps],
            "detail": self.detail,
        }


class Session:
    """One falsification session: a shared baseline plus per-falsifier scratch."""

    def __init__(self, *, repo_root: Path, work_dir: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.bundle = self.repo_root / BUNDLE_RELATIVE
        self.design = self.repo_root / "spec"
        if not self.design.is_dir():
            self.design = self.repo_root / DESIGN_DIR
        self.gates = self.design / "release-gates.1.0.0.json"
        self.schema = self.design / "capture-schema.2.0.0.json"
        self._baseline: Path | None = None
        self._baseline_b: Path | None = None
        self._counter = 0
        self.contract_digest_before = snapshot_digests(
            self.repo_root / "contracts" / "oran-aic" / "1.0.1")

    # ------------------------------------------------------------- processes

    def run(self, step: Step) -> Step:
        packaged_launcher = self.repo_root / "bin" / "lo1-selftest"
        command = [str(packaged_launcher), *step.argv] \
            if packaged_launcher.is_file() \
            else [sys.executable, "-m", MODULE, *step.argv]
        completed = subprocess.run(
            command,
            cwd=str(self.repo_root), capture_output=True, text=True, check=False)
        step.exit_code = completed.returncode
        try:
            step.stdout = json.loads(completed.stdout)
        except json.JSONDecodeError:
            step.stdout = None
            step.detail = (completed.stderr or completed.stdout)[-400:]
        if step.expect == "ZERO":
            step.ok = completed.returncode == EXIT_OK
        else:
            step.ok = completed.returncode != EXIT_OK
        if step.ok and step.required_findings and step.stdout:
            findings = " ".join(step.stdout.get("findings", []))
            detail = json.dumps(step.stdout)
            missing = [code for code in step.required_findings
                       if code not in findings and code not in detail]
            if missing:
                step.ok = False
                step.detail = f"required finding(s) absent: {missing}"
        return step

    def selftest_run(self, name: str, *extra: str, expect: str = "ZERO") -> Step:
        self._counter += 1
        work = self.work_dir / f"run-{self._counter:03d}"
        return self.run(Step(name, ["selftest-run", "--work", str(work), *extra],
                             expect))

    def adjudicate(self, name: str, capture: Path, *, expect: str,
                   bundle: Path | None = None, gates: Path | None = None,
                   findings: Sequence[str] = ()) -> Step:
        argv = ["adjudicate", "--capture", str(capture),
                "--bundle", str(bundle or self.bundle),
                "--gates", str(gates or self.gates),
                "--schema", str(self.schema)]
        return self.run(Step(name, argv, expect, required_findings=tuple(findings)))

    def probe(self, name: str, *extra: str, expect: str = "ZERO") -> Step:
        return self.run(Step(name, ["probe", *extra], expect))

    # -------------------------------------------------------------- baseline

    def baseline(self) -> Path:
        if self._baseline is None:
            step = self.selftest_run("baseline")
            if not step.ok or not step.stdout:
                raise RuntimeError(f"the clean baseline run failed: {step.detail}")
            self._baseline = Path(step.stdout["captureRoot"])
        return self._baseline

    def second_baseline(self) -> Path:
        if self._baseline_b is None:
            step = self.selftest_run("baseline-b")
            if not step.ok or not step.stdout:
                raise RuntimeError(f"the second baseline run failed: {step.detail}")
            self._baseline_b = Path(step.stdout["captureRoot"])
        return self._baseline_b

    def copy_capture(self, label: str) -> Path:
        self._counter += 1
        target = self.work_dir / f"capture-{label}-{self._counter:03d}"
        shutil.copytree(self.baseline(), target)
        return target

    def scratch_bundle(self, label: str) -> Path:
        self._counter += 1
        target = self.work_dir / f"bundle-{label}-{self._counter:03d}"
        return copy_bundle(self.bundle, target)

    def scratch_gates(self, label: str, mutate: Callable[[dict], None]) -> Path:
        self._counter += 1
        target = self.work_dir / f"gates-{label}-{self._counter:03d}.json"
        document = json.loads(self.gates.read_text(encoding="utf-8"))
        mutate(document)
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return target

    # ------------------------------------------------------------- utilities

    @staticmethod
    def edit_capture(root: Path, mutate: Callable[[dict], None]) -> None:
        path = Path(root) / "capture.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    @staticmethod
    def edit_report(root: Path, mutate: Callable[[dict], None]) -> None:
        path = Path(root) / "self-test-report.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    @staticmethod
    def pm_artifact(root: Path) -> Path:
        candidates = sorted((Path(root) / "raw" / "o1").glob("*.xml"))
        if not candidates:
            raise RuntimeError("the baseline bundle carries no retrieved PM file")
        return candidates[0]

    def contracts_untouched(self) -> bool:
        after = snapshot_digests(self.repo_root / "contracts" / "oran-aic" / "1.0.1")
        return after == self.contract_digest_before


# --------------------------------------------------------------- falsifiers


def _n01(session: Session) -> FalsifierOutcome:
    steps = [session.adjudicate("control-clean", session.baseline(), expect="ZERO")]
    capture = session.copy_capture("n01")

    def mutate(document: dict) -> None:
        document["coordinator"]["executionMode"] = "NOT_INVOKED"
        document["coordinator"]["processIntentCallsDelta"] = 0
        document["coordinator"]["syntheticTransitionCount"] = 1
        document["coordinator"]["fsmHistory"] = []
        document["coordinator"]["ledgerReferences"] = []
        document["coordinator"]["terminalEvidenceRef"] = None

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "synthetic-transition", capture, expect="NON_ZERO",
        findings=["SYNTHETIC_COORDINATOR"]))
    return _outcome("LO1-ST-N01", steps)


def _n02(session: Session) -> FalsifierOutcome:
    steps = []
    capture = session.copy_capture("n02-pm")
    session.pm_artifact(capture).unlink()
    steps.append(session.adjudicate(
        "raw-pm-omitted", capture, expect="NON_ZERO",
        findings=["RAW_CAPTURE_OMISSION"]))
    capture = session.copy_capture("n02-notify")
    for path in sorted((capture / "raw" / "notify").glob("*.json")):
        path.unlink()
    steps.append(session.adjudicate(
        "raw-notification-omitted", capture, expect="NON_ZERO",
        findings=["RAW_CAPTURE_OMISSION"]))
    return _outcome("LO1-ST-N02", steps)


def _n03(session: Session) -> FalsifierOutcome:
    steps = [
        session.selftest_run(
            "mutable-tag", "--provider-digest", "registry.example/provider:latest",
            expect="NON_ZERO"),
        session.selftest_run(
            "local-image-id", "--provider-digest", "a1b2c3d4e5f6",
            expect="NON_ZERO"),
        session.selftest_run(
            "image-digest-under-self-test-profile",
            "--provider-digest", "sha256:" + "a" * 64, expect="NON_ZERO"),
    ]
    capture = session.copy_capture("n03")

    def mutate(document: dict) -> None:
        document["profile"]["providerKind"] = \
            "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY"
        document["profile"]["emulatorInUse"] = False
        document["profile"]["counterpartKind"] = "LIVE_O1_AUTHORITY_APPROVED_HARNESS"
        document["profile"]["selfTestLabel"] = None
        # one character off a well-formed manifest digest
        document["provider"]["ociImageManifestDigest"] = "sha256:" + "a" * 63 + "z"

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "one-character-off-digest", capture, expect="NON_ZERO",
        findings=["IMMUTABLE_IDENTITY_MISMATCH"]))
    return _outcome("LO1-ST-N03", steps)


def _n04(session: Session) -> FalsifierOutcome:
    steps = [session.selftest_run(
        "authority-collision", "--endpoint-collision", expect="NON_ZERO")]
    capture = session.copy_capture("n04")

    def mutate(document: dict) -> None:
        roots = document["deployment"]["resolvedRoots"]
        roots["a1StatusCallbackRoot"] = roots["r1ApiRoot"]

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "ambiguous-endpoint-in-evidence", capture, expect="NON_ZERO",
        findings=["ENDPOINT_AUTHORITY_COLLISION"]))
    return _outcome("LO1-ST-N04", steps)


def _n05(session: Session) -> FalsifierOutcome:
    steps = [session.selftest_run("stale-capture-root", "--stale-state",
                                  expect="NON_ZERO")]
    capture = session.copy_capture("n05")
    (capture / "raw" / "o1" / "leftover-from-a-previous-run.xml").write_bytes(b"<x/>")
    steps.append(session.adjudicate(
        "unreferenced-artifact", capture, expect="NON_ZERO",
        findings=["STALE_OR_UNREFERENCED_ARTIFACT"]))
    return _outcome("LO1-ST-N05", steps)


def _n06(session: Session) -> FalsifierOutcome:
    baseline_document = json.loads(
        (session.baseline() / "capture.json").read_text(encoding="utf-8"))
    capture = session.copy_capture("n06")
    artifact = session.pm_artifact(capture)
    raw = bytearray(artifact.read_bytes())
    raw[len(raw) // 2] = raw[len(raw) // 2] ^ 0x01
    artifact.write_bytes(bytes(raw))
    steps = [session.adjudicate(
        "pm-byte-flipped-after-digest", capture, expect="NON_ZERO",
        findings=["RAW_DIGEST_MISMATCH"])]
    mutated_document = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
    parser_unchanged = (
        mutated_document["normalization"]["parserInvocations"]
        == baseline_document["normalization"]["parserInvocations"])
    scratch = session.scratch_bundle("n06")
    target = scratch / "scenario-runner-contract.1.0.1.json"
    blob = bytearray(target.read_bytes())
    blob[-2] = blob[-2] ^ 0x01
    target.write_bytes(bytes(blob))
    steps.append(session.adjudicate(
        "packaged-contract-byte-flipped", session.baseline(), expect="NON_ZERO",
        bundle=scratch, findings=["CONTRACT_DIGEST_MISMATCH"]))
    outcome = _outcome("LO1-ST-N06", steps)
    outcome.detail = (
        f"parserInvocations unchanged after quarantine: {parser_unchanged}")
    if not parser_unchanged:
        outcome.falsified = False
    return outcome


def _n07(session: Session) -> FalsifierOutcome:
    steps = [session.selftest_run(
        "unresolvable-credential-ref",
        "--drop-secret", "secret://lo1-selftest/o1/sftp/credential",
        expect="NON_ZERO")]
    capture = session.copy_capture("n07")

    def mutate(document: dict) -> None:
        document["deployment"]["unresolvedSecretRefs"] = [
            "secret://lo1-selftest/o1/sftp/known-hosts"]

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "unresolved-secret-on-admitted-run", capture, expect="NON_ZERO",
        findings=["UNRESOLVED_SECRET_REF_ON_ADMITTED_RUN"]))
    return _outcome("LO1-ST-N07", steps)


def _n08(session: Session) -> FalsifierOutcome:
    steps = [
        session.probe("guard-armed-refuses", "--name", "egress", expect="ZERO"),
        session.probe("guard-disarmed-control", "--name", "egress", "--disarmed",
                      expect="ZERO"),
    ]
    capture = session.copy_capture("n08")

    def mutate(document: dict) -> None:
        external = document["externalCalls"]
        # RFC 5737 TEST-NET-3, a documentation address that is not routable and
        # is never connected to: it only has to be OFF the allowlist.
        external["violations"] = [{
            "kind": "CONNECT",
            "classification": "EXTERNAL_LIVE_TARGET",
            "authority": "203.0.113.7:443",
        }]
        external["forbiddenEgressAttempts"] = 1
        external["hardwareCalls"] = 1
        external["guardMethods"] = ["SOCKET_CONNECT", "DNS_RESOLVE", "SFTP_CLIENT_OPEN"]

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "forbidden-egress-in-evidence", capture, expect="NON_ZERO",
        findings=["FORBIDDEN_EGRESS", "HARDWARE_CALL", "EGRESS_GUARD_UNDER_ARMED"]))
    return _outcome("LO1-ST-N08", steps)


def _n09(session: Session) -> FalsifierOutcome:
    capture = session.copy_capture("n09")

    def mutate(document: dict) -> None:
        document["cleanup"]["residual"] = ["durable/subscription-id"]

    session.edit_capture(capture, mutate)
    steps = [session.adjudicate(
        "residual-after-cleanup", capture, expect="NON_ZERO",
        findings=["INCOMPLETE_CLEANUP"])]
    failure = session.selftest_run(
        "failure-path-cleanup", "--faults", "WITHHOLD_PM_FILE", expect="NON_ZERO")
    steps.append(failure)
    detail = ""
    if failure.stdout and failure.stdout.get("captureRoot"):
        document = json.loads(
            (Path(failure.stdout["captureRoot"]) / "capture.json").read_text("utf-8"))
        complete = document["cleanup"]["complete"]
        path = document["cleanup"]["path"]
        evidence_kept = any(
            (Path(failure.stdout["captureRoot"]) / "raw").rglob("*"))
        detail = (f"failure-path cleanup complete={complete} path={path} "
                  f"evidenceRetained={evidence_kept}")
        if not (complete and path == "FAILURE" and evidence_kept):
            failure.ok = False
            failure.detail = detail
    outcome = _outcome("LO1-ST-N09", steps)
    outcome.detail = detail
    return outcome


def _n10(session: Session) -> FalsifierOutcome:
    steps = [session.selftest_run(
        "emulator-under-live-profile", "--profile", "live-O1", expect="NON_ZERO")]
    capture = session.copy_capture("n10")

    def mutate(document: dict) -> None:
        document["profile"]["providerKind"] = \
            "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY"
        # emulatorInUse and selfTestLabel are deliberately left as the emulator
        # wrote them: the capture schema's profile branch makes this combination
        # unrepresentable, so the claim is schema-invalid as well as wrong.

    session.edit_capture(capture, mutate)
    steps.append(session.adjudicate(
        "capture-claims-live-provider", capture, expect="NON_ZERO",
        findings=["EMULATOR_USED_AS_PRODUCTION"]))
    return _outcome("LO1-ST-N10", steps)


def _o01(session: Session) -> FalsifierOutcome:
    bundle = repository_bundle(session.repo_root)
    pointer = bundle.catalog_pointer()
    assignment_index = int(bundle.assignment_pointer().rsplit("/", 1)[1])
    steps = [session.adjudicate("control-clean", session.baseline(), expect="ZERO")]

    scratch = session.scratch_bundle("o01-committed")
    mutate_expected_counter(scratch, scenario_pointer=pointer,
                            member="committedEvidenceRecords")
    steps.append(session.adjudicate(
        "expected-committed-records-mutated", session.baseline(), expect="NON_ZERO",
        bundle=scratch, findings=["COMMITTED_EVIDENCE_COUNT"]))

    scratch = session.scratch_bundle("o01-status")
    mutate_expected_status(scratch, scenario_pointer=pointer)
    steps.append(session.adjudicate(
        "expected-http-sequence-mutated", session.baseline(), expect="NON_ZERO",
        bundle=scratch, findings=["HTTP_SEQUENCE"]))

    scratch = session.scratch_bundle("o01-calls")
    mutate_expected_counter(scratch, scenario_pointer=pointer,
                            member="processIntentCalls")
    steps.append(session.adjudicate(
        "expected-process-intent-calls-mutated", session.baseline(),
        expect="NON_ZERO", bundle=scratch, findings=["COORDINATOR_CALL_COUNT"]))

    scratch = session.scratch_bundle("o01-step-id")
    mutate_step_id(scratch, scenario_pointer=pointer, index=0)
    steps.append(session.adjudicate(
        "step-id-mutated", session.baseline(), expect="NON_ZERO",
        bundle=scratch, findings=["STEP_ID_NOT_IN_CATALOG"]))
    planned = session.selftest_run(
        "plan-follows-the-mutated-step-vector", "--bundle", str(scratch),
        expect="ZERO")
    steps.append(planned)
    detail = ""
    if planned.stdout and planned.stdout.get("captureRoot"):
        document = json.loads(
            (Path(planned.stdout["captureRoot"]) / "capture.json").read_text("utf-8"))
        planned_ids = [exchange["stepId"] for exchange in document["exchanges"]]
        baseline_ids = [
            exchange["stepId"] for exchange in json.loads(
                (session.baseline() / "capture.json").read_text("utf-8"))["exchanges"]]
        detail = f"plan changed: {planned_ids != baseline_ids}"
        if planned_ids == baseline_ids:
            planned.ok = False
            planned.detail = "the harness plan did not follow the mutated step vector"

    scratch = session.scratch_bundle("o01-step-status")
    mutate_step_expected_status(scratch, scenario_pointer=pointer, index=0)
    steps.append(session.adjudicate(
        "step-expected-status-mutated", session.baseline(), expect="NON_ZERO",
        bundle=scratch, findings=["STEP_EXPECTED_STATUS_MISREAD"]))

    outcome = _outcome("LO1-ST-O01", steps)
    outcome.detail = (f"{detail}; assignmentIndex={assignment_index}; "
                      f"repository bundle untouched: {session.contracts_untouched()}")
    if not session.contracts_untouched():
        outcome.falsified = False
    return outcome


def _o02(session: Session) -> FalsifierOutcome:
    steps = [session.probe("no-oracle-literal-in-the-tree", "--name",
                           "oracle-literals", expect="ZERO")]
    bundle = repository_bundle(session.repo_root)
    expected = bundle.expected()
    literal = next(value for value in expected.values() if isinstance(value, str))
    planted = session.work_dir / "planted"
    planted.mkdir(exist_ok=True)
    (planted / "planted_literal.py").write_text(
        f'OFFENDING = "{literal}"\n', encoding="utf-8")
    steps.append(session.probe(
        "planted-oracle-literal-is-detected", "--name", "oracle-literals",
        "--roots", str(planted), expect="NON_ZERO"))
    return _outcome("LO1-ST-O02", steps)


def _v01(session: Session) -> FalsifierOutcome:
    steps = []
    for label, fault, finding in (
        ("value-out-of-contract-range", "PM_VALUE_OUT_OF_RANGE",
         "LIVE_VALUE_OUT_OF_RANGE"),
        ("value-equals-golden-sample", "PM_VALUE_EQUALS_GOLDEN",
         "LIVE_VALUE_EQUALS_GOLDEN_SAMPLE"),
    ):
        run = session.selftest_run(f"{label}-run", "--faults", fault, expect="ZERO")
        steps.append(run)
        if not run.stdout:
            continue
        capture_root = Path(run.stdout["captureRoot"])
        steps.append(session.adjudicate(
            label, capture_root, expect="NON_ZERO",
            findings=[finding, "COMMITTED_EVIDENCE_COUNT"]))
        document = json.loads((capture_root / "capture.json").read_text("utf-8"))
        published = document["normalization"]["commitEligibleCount"]
        if published >= len(document["normalization"]["records"]):
            steps[-1].ok = False
            steps[-1].detail = (
                f"the out-of-contract record was still commit-eligible ({published})")
    return _outcome("LO1-ST-V01", steps)


def _v02(session: Session) -> FalsifierOutcome:
    steps = [
        session.selftest_run("recovery-unique-candidate-not-unique",
                             "--recovery-probe", "NOT_UNIQUE", expect="NON_ZERO"),
        session.selftest_run("recovery-ambiguous-cardinality-wrong",
                             "--recovery-probe", "WRONG_CARDINALITY",
                             expect="NON_ZERO"),
    ]
    return _outcome("LO1-ST-V02", steps)


def _v03(session: Session) -> FalsifierOutcome:
    run = session.selftest_run("dn-collision-run", "--faults", "PM_DN_COLLISION",
                               expect="ZERO")
    steps = [run]
    detail = ""
    if run.stdout:
        capture_root = Path(run.stdout["captureRoot"])
        steps.append(session.adjudicate(
            "dn-not-bijective", capture_root, expect="NON_ZERO",
            findings=["DN_BIJECTION"]))
        document = json.loads((capture_root / "capture.json").read_text("utf-8"))
        published = document["normalization"]["commitEligibleCount"]
        detail = f"commitEligibleCount under an ambiguous DN = {published}"
        if published != 0:
            steps[-1].ok = False
            steps[-1].detail = detail
    outcome = _outcome("LO1-ST-V03", steps)
    outcome.detail = detail
    return outcome


def _v04(session: Session) -> FalsifierOutcome:
    steps = []
    for label, fault in (
        ("event-time-not-equal-file-ready-time", "NOTIFY_EVENT_TIME_MISMATCH"),
        ("expiration-not-after-ready", "NOTIFY_EXPIRY_BEFORE_READY"),
    ):
        run = session.selftest_run(f"{label}-run", "--faults", fault, expect="ZERO")
        steps.append(run)
        if not run.stdout:
            continue
        steps.append(session.adjudicate(
            label, Path(run.stdout["captureRoot"]), expect="NON_ZERO",
            findings=["FILE_TEMPORAL_ORDER"]))
    return _outcome("LO1-ST-V04", steps)


def _e01(session: Session) -> FalsifierOutcome:
    steps = [session.adjudicate("control-agreement", session.baseline(),
                                expect="ZERO")]

    capture = session.copy_capture("e01-schema")
    session.edit_capture(capture, lambda document: document["profile"].update(
        {"providerKind": "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY"}))
    steps.append(session.adjudicate(
        "schema-branch-disagreement", capture, expect="NON_ZERO",
        findings=["CAPTURE_SCHEMA_INVALID"]))

    capture = session.copy_capture("e01-gate")

    def refuse_a_check(document: dict) -> None:
        for check in document["authority"]["checks"]:
            if check["id"] == "G-ID-10":
                check["outcome"] = "REFUSED"
                check["reasonCode"] = "EMULATOR_REACHABLE_UNDER_LIVE_PROFILE"

    session.edit_capture(capture, refuse_a_check)
    steps.append(session.adjudicate(
        "gate-decision-disagreement", capture, expect="NON_ZERO",
        findings=["GATE_DECISION_DISAGREEMENT"]))

    capture = session.copy_capture("e01-label")
    session.edit_report(capture, lambda document: document.update(
        {"stateLabel": "SOME_OTHER_LABEL"}))
    steps.append(session.adjudicate(
        "capture-label-disagreement", capture, expect="NON_ZERO",
        findings=["SELF_TEST_LABEL_MISSING"]))
    return _outcome("LO1-ST-E01", steps)


def _e03(session: Session) -> FalsifierOutcome:
    control = session.probe("two-runs-diverge-from-golden", "--name", "emulator-pm",
                            "--bundle", str(session.bundle), "--repeat", "2",
                            expect="ZERO")
    steps = [control]
    detail = ""
    if control.stdout:
        digests = control.stdout["observations"]["digests"]
        detail = f"distinct PM digests: {len(set(digests))} of {len(digests)}"
        if len(set(digests)) != len(digests):
            control.ok = False
            control.detail = detail
    run = session.selftest_run("golden-collision-run", "--faults",
                               "PM_VALUE_EQUALS_GOLDEN", expect="ZERO")
    steps.append(run)
    if run.stdout:
        steps.append(session.adjudicate(
            "emitted-value-equals-golden", Path(run.stdout["captureRoot"]),
            expect="NON_ZERO", findings=["LIVE_VALUE_EQUALS_GOLDEN_SAMPLE"]))
    outcome = _outcome("LO1-ST-E03", steps)
    outcome.detail = detail
    return outcome


def _e04(session: Session) -> FalsifierOutcome:
    steps = [session.probe("control-registered-fixtures", "--name", "emulator-rpc",
                           "--bundle", str(session.bundle), expect="ZERO")]
    scratch = session.scratch_bundle("e04")
    note = mutate_golden_netconf_fixture(scratch, name="lock-running.xml")
    steps.append(session.probe(
        "one-byte-drift-in-a-golden-rpc", "--name", "emulator-rpc",
        "--bundle", str(scratch), expect="NON_ZERO"))
    outcome = _outcome("LO1-ST-E04", steps)
    outcome.detail = f"{note}; repository bundle untouched: {session.contracts_untouched()}"
    if not session.contracts_untouched():
        outcome.falsified = False
    return outcome


def _e05(session: Session) -> FalsifierOutcome:
    steps = [session.probe("control-capabilities", "--name", "emulator-capability",
                           "--bundle", str(session.bundle), expect="ZERO")]
    scratch = session.scratch_bundle("e05")
    note = mutate_netconf_profile_capability(scratch)
    steps.append(session.probe(
        "capability-dropped-in-the-profile", "--name", "emulator-capability",
        "--bundle", str(scratch), expect="NON_ZERO"))
    outcome = _outcome("LO1-ST-E05", steps)
    outcome.detail = note
    return outcome


def _e06(session: Session) -> FalsifierOutcome:
    steps = [session.probe("control-pm-profile", "--name", "emulator-pm",
                           "--bundle", str(session.bundle), expect="ZERO")]
    scratch = session.scratch_bundle("e06")
    note = mutate_pa_file_namespace(scratch)
    steps.append(session.probe(
        "pm-file-profile-drift", "--name", "emulator-pm",
        "--bundle", str(scratch), expect="NON_ZERO"))
    outcome = _outcome("LO1-ST-E06", steps)
    outcome.detail = note
    return outcome


def _i01(session: Session) -> FalsifierOutcome:
    import ast

    verifier_source_path = (session.repo_root / "lib" / "oran" / "release" /
                            "lo1_selftest" / "verifier.py")
    if not verifier_source_path.is_file():
        verifier_source_path = (session.repo_root / "oran" / "release" /
                                "lo1_selftest" / "verifier.py")
    verifier_source = verifier_source_path.read_text(encoding="utf-8")
    tree = ast.parse(verifier_source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append("." * (node.level or 0) + (node.module or ""))
    forbidden = [name for name in imported
                 if name.startswith(".") or name.startswith("oran.")]
    independence = Step("verifier-shares-no-runtime-code",
                        ["<static import-graph analysis of verifier.py>"], "ZERO")
    independence.exit_code = 0 if not forbidden else 1
    independence.ok = not forbidden
    independence.detail = f"imports={sorted(set(imported))}"
    steps = [independence]

    capture = session.copy_capture("i01")
    artifact = session.pm_artifact(capture)
    text = artifact.read_text(encoding="utf-8")
    import re as _re

    match = _re.search(r'(<r p="1">)(\d+)(</r>)', text)
    if match is None:
        raise RuntimeError("the baseline PM file carries no positional r value")
    replaced = int(match.group(2))
    substitute = 0 if replaced != 0 else 1
    text = text[:match.start()] + match.group(1) + str(substitute) + match.group(3) \
        + text[match.end():]
    artifact.write_bytes(text.encode("utf-8"))
    new_digest = sha256_bytes(text.encode("utf-8"))

    def relabel(document: dict) -> None:
        # The declared digest is updated to match, so a digest-only check would
        # pass.  Only a verifier that re-derives the VALUE from the raw bytes
        # can still see the substitution.
        for retrieval in document["retrievals"]:
            if retrieval["outcome"] == "RETRIEVED":
                retrieval["byteSha256"] = new_digest
                retrieval["byteCount"] = len(text.encode("utf-8"))
        for record in document["normalization"]["records"]:
            record["sourceFileSha256"] = new_digest

    session.edit_capture(capture, relabel)
    steps.append(session.adjudicate(
        "value-substituted-with-a-consistent-digest", capture, expect="NON_ZERO",
        findings=["NORMALIZED_VALUE_MISMATCH"]))
    outcome = _outcome("LO1-ST-I01", steps)
    outcome.detail = f"substituted RRU.PrbDl {replaced} -> {substitute} in the raw bytes"
    return outcome


def _i02(session: Session) -> FalsifierOutcome:
    steps = [session.adjudicate("control-labelled", session.baseline(),
                                expect="ZERO")]
    capture = session.copy_capture("i02")
    token = b"LO1_SELFTEST_PLANTED_CLAIM_TOKEN"
    (capture / "planted-claim.txt").write_bytes(token)
    gates = session.scratch_gates(
        "i02", lambda document: document["prohibitedClaimDigests"]["digests"].append(
            sha256_bytes(token)))
    steps.append(session.adjudicate(
        "prohibited-claim-string-detected", capture, expect="NON_ZERO",
        gates=gates, findings=["PROHIBITED_CLAIM_STRING"]))
    capture = session.copy_capture("i02-label")
    session.edit_report(capture, lambda document: document.update(
        {"stateLabel": "NOT_THE_SELF_TEST_LABEL"}))
    steps.append(session.adjudicate(
        "self-test-label-absent", capture, expect="NON_ZERO",
        findings=["SELF_TEST_LABEL_MISSING"]))
    return _outcome("LO1-ST-I02", steps)


def _d01_neg(session: Session) -> FalsifierOutcome:
    """``G-DET-1`` plus the ten-pointer negative control the gate spec pins."""
    first = session.baseline()
    second = session.second_baseline()
    steps = [session.run(Step(
        "two-runs-agree-outside-the-allowlist",
        ["determinism", "--a", str(first), "--b", str(second),
         "--gates", str(session.gates), "--negative-control"], "ZERO"))]
    detail = ""
    if steps[0].stdout:
        payload = steps[0].stdout
        detail = (f"compared {payload['comparedSlots']} slots byte-for-byte; "
                  f"{payload['excusedByFrozenAllowlist']} excused by the frozen "
                  f"allowlist; negative control all detected: "
                  f"{payload['negativeControl']['allDetected']}")
    widened = session.scratch_gates(
        "d01", lambda document: document["volatileFieldAllowlist"].extend([
            "/exchanges/*", "/normalization/*", "/cleanup/*", "/externalCalls/*",
            "/deterministicStubs/*", "/run/*", "/netconf/*"]))
    steps.append(session.run(Step(
        "an-allowlist-that-is-too-wide-is-caught",
        ["determinism", "--a", str(first), "--b", str(second),
         "--gates", str(widened), "--negative-control"], "NON_ZERO")))
    outcome = _outcome("LO1-ST-D01-NEG", steps)
    outcome.detail = detail
    return outcome


#: Declared in DESIGN.md section 13 and in the gate spec's negativeControl
#: rather than in the 23-entry falsifiers array, so it is reported separately.
ADDITIONAL: dict[str, Callable[[Session], FalsifierOutcome]] = {
    "LO1-ST-D01-NEG": _d01_neg,
}


IMPLEMENTATIONS: dict[str, Callable[[Session], FalsifierOutcome]] = {
    "LO1-ST-N01": _n01,
    "LO1-ST-N02": _n02,
    "LO1-ST-N03": _n03,
    "LO1-ST-N04": _n04,
    "LO1-ST-N05": _n05,
    "LO1-ST-N06": _n06,
    "LO1-ST-N07": _n07,
    "LO1-ST-N08": _n08,
    "LO1-ST-N09": _n09,
    "LO1-ST-N10": _n10,
    "LO1-ST-O01": _o01,
    "LO1-ST-O02": _o02,
    "LO1-ST-V01": _v01,
    "LO1-ST-V02": _v02,
    "LO1-ST-V03": _v03,
    "LO1-ST-V04": _v04,
    "LO1-ST-E01": _e01,
    "LO1-ST-E03": _e03,
    "LO1-ST-E04": _e04,
    "LO1-ST-E05": _e05,
    "LO1-ST-E06": _e06,
    "LO1-ST-I01": _i01,
    "LO1-ST-I02": _i02,
}


def _outcome(identifier: str, steps: list[Step]) -> FalsifierOutcome:
    return FalsifierOutcome(
        id=identifier, task_clause="", falsified=all(step.ok for step in steps),
        steps=steps)


def declared_falsifiers(repo_root: Path) -> list[Mapping[str, Any]]:
    """The 23 the design declares, READ from the frozen gate spec."""
    design = Path(repo_root) / "spec"
    if not design.is_dir():
        design = Path(repo_root) / DESIGN_DIR
    gates = json.loads((design / "release-gates.1.0.0.json").read_text(encoding="utf-8"))
    return list(gates["falsifiers"])


def run_falsifiers(*, repo_root: Path, work_dir: Path | None = None,
                   only: Sequence[str] | None = None) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    declared = declared_falsifiers(repo_root)
    declared_ids = [str(entry["id"]) for entry in declared]
    missing = [identifier for identifier in declared_ids
               if identifier not in IMPLEMENTATIONS]
    owned = work_dir is None
    work_dir = Path(work_dir or tempfile.mkdtemp(prefix="lo1-falsify-"))
    session = Session(repo_root=repo_root, work_dir=work_dir)
    outcomes: list[FalsifierOutcome] = []
    try:
        for entry in declared:
            identifier = str(entry["id"])
            if only and identifier not in only:
                continue
            implementation = IMPLEMENTATIONS.get(identifier)
            if implementation is None:
                outcomes.append(FalsifierOutcome(
                    identifier, str(entry.get("taskClause", "")), False,
                    detail="no implementation for a declared falsifier"))
                continue
            try:
                outcome = implementation(session)
            except Exception as exc:  # a falsifier that cannot run has not falsified
                outcome = FalsifierOutcome(
                    identifier, str(entry.get("taskClause", "")), False,
                    detail=f"{type(exc).__name__}: {exc}")
            outcome.task_clause = str(entry.get("taskClause", ""))
            outcomes.append(outcome)
        additional: list[FalsifierOutcome] = []
        for identifier, implementation in ADDITIONAL.items():
            if only and identifier not in only:
                continue
            try:
                outcome = implementation(session)
            except Exception as exc:
                outcome = FalsifierOutcome(
                    identifier, "determinism negative control", False,
                    detail=f"{type(exc).__name__}: {exc}")
            outcome.task_clause = "two-run determinism negative control"
            additional.append(outcome)
    finally:
        if owned:
            shutil.rmtree(work_dir, ignore_errors=True)
    return {
        "specVersion": "oran-aic-upper-live-o1-harness-falsification-report/1.0.0",
        "stateLabel": SELF_TEST_STATE_LABEL,
        "declaredFalsifierCount": len(declared_ids),
        "implementedFalsifierCount": len(
            [identifier for identifier in declared_ids
             if identifier in IMPLEMENTATIONS]),
        "unimplemented": missing,
        "executed": [outcome.id for outcome in outcomes],
        "allFalsified": bool(outcomes) and not missing
        and all(outcome.falsified for outcome in outcomes)
        and all(outcome.falsified for outcome in additional),
        "additional": [outcome.as_report() for outcome in additional],
        "frozenBundleUntouched": session.contracts_untouched(),
        "results": [outcome.as_report() for outcome in outcomes],
        "notAcceptance": (
            "A falsifier that does not fail is itself a suite failure. This is "
            "upper-readiness evidence only and is never SC-084 acceptance."),
    }
