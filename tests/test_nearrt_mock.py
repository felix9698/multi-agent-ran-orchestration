"""Hermetic contract checks for the deterministic Near-RT mock."""
import copy
import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oran.contract.jcs import jcs_sha256
from oran.mocks.nearrt.producer import NearRtMock, POLICY_TYPE, Problem, TRANSITIONS, TransitionError, validate_status

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "contracts" / "oran-aic" / "1.0.0" / "shared-contract-bundle"
if not ROOT.is_dir():
    raise RuntimeError(f"tracked contract bundle is missing: {ROOT}")
FIXED_NOW = datetime(2026, 8, 4, 0, 0, 2, tzinfo=timezone.utc)

#: The frozen mandatory contract, in both vendored authorities.  The 1.0.0 byte
#: digest is the ``frozenLowerStandard.mandatoryContractSha256`` the Lower
#: recipient's ``CONTRACT-DIGESTS.json`` pins, so a gate derived from these bytes
#: is derived from the same bytes both sides agreed on.
MANDATORY_CONTRACTS = (
    (ROOT.parent / "02-rapp-xapp-backend-mandatory-contract.md",
     "0793b9940273f207211ad63ed51d814f2c5ceda835f97fe86b8ba73d275e07ba"),
    (REPO / "contracts/oran-aic/1.0.1"
     / "02-rapp-xapp-backend-mandatory-contract.1.0.1.md", None),
)

#: §7.1's declaration sentence, verbatim, followed by one bullet per member.
POLICY_TYPE_OBJECT_DECLARATION = "`PolicyTypeObject`를 반환해야 한다"
_MEMBER_BULLET = re.compile(r"^-\s+`([A-Za-z0-9_]+)`$")


def contract_policy_type_members(path):
    """The ``PolicyTypeObject`` member list §7.1 declares, read from the bytes."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if POLICY_TYPE_OBJECT_DECLARATION not in line:
            continue
        members = []
        for candidate in lines[index + 1:]:
            text = candidate.strip()
            if not text:
                if members:
                    return tuple(members)
                continue
            match = _MEMBER_BULLET.match(text)
            if not match:
                break
            members.append(match.group(1))
        if members:
            return tuple(members)
    raise AssertionError(
        f"{path.name} no longer declares the PolicyTypeObject member list; the "
        "gate below would silently stop constraining the mock")


class Clock:
    def __init__(self):
        self.wall = FIXED_NOW
        self.mono = 100.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def policy():
    return copy.deepcopy(json.loads((ROOT / "golden/golden-vectors.1.0.0.json").read_text())["canonicalObjects"]["policy"])


def capability():
    return copy.deepcopy(json.loads((ROOT / "golden/golden-vectors.1.0.0.json").read_text())["canonicalObjects"]["capabilityManifest"])


def load_capability(mock):
    mock.harness_op({
        "op": "LOAD_CAPABILITY",
        "capability": capability(),
        "knownUeScopes": [policy()["scope"]["ueId"]],
    })
    return mock


# Independent transcription of contract 02 §8.4 (the §9 envelope safety sink is separate).
CONTRACT_TRANSITIONS = {
    None: frozenset(("SCHEDULED",)),
    "SCHEDULED": frozenset(("COMPUTING", "ABORTED_NO_WRITE")),
    "COMPUTING": frozenset(("NO_ACTION", "ABORTED_NO_WRITE", "APPLYING")),
    "APPLYING": frozenset(("APPLIED_UNVERIFIED", "APPLY_FAILED", "RECOVERY_PENDING")),
    "APPLIED_UNVERIFIED": frozenset(("APPLIED_VERIFIED", "READBACK_MISMATCH", "RECOVERY_PENDING")),
    "READBACK_MISMATCH": frozenset(("ROLLING_BACK",)),
    "APPLY_FAILED": frozenset(("ROLLING_BACK",)),
    "ROLLING_BACK": frozenset(("ROLLED_BACK_VERIFIED", "ROLLBACK_FAILED", "ROLLBACK_UNKNOWN")),
    "ROLLBACK_FAILED": frozenset(("QUARANTINED",)),
    "ROLLBACK_UNKNOWN": frozenset(("QUARANTINED",)),
    "RECOVERY_PENDING": frozenset(("APPLIED_VERIFIED", "READBACK_MISMATCH", "QUARANTINED")),
}


class NearRtMockTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.mock = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        load_capability(self.mock)
        self.cell1, self.cell2 = policy()["steeringObjective"]["actionEnvelope"]["allowedCells"]
        self.mock.e2.serving = copy.deepcopy(self.cell1)

    def snapshot(self, *, first=80, second=20, when=None, fresh=False):
        when = when or self.clock.wall
        return {"fresh": fresh, "observationWindowEnd": when.isoformat().replace("+00:00", "Z"),
                "servingCell": copy.deepcopy(self.mock.e2.serving),
                "prbByCell": {str(self.cell1["cId"]["ncI"]): first, str(self.cell2["cId"]["ncI"]): second}}

    def seed(self, value=None):
        self.mock.put_policy("p1", value or policy())

    def assert_all_statuses_valid(self, mock=None):
        for entries in (mock or self.mock).statuses.values():
            for status in entries:
                validate_status(status)

    def test_discovery_vends_both_contract_schemas(self):
        self.assertEqual(self.mock.list_policy_types(), [POLICY_TYPE])
        self.assertIn("$id", self.mock.policy_type()["policySchema"])
        self.assertIn("$id", self.mock.policy_type()["statusSchema"])

    def test_policy_type_response_is_no_richer_than_the_frozen_contract(self):
        """The mock's A1 policy-type object matches the contract member set exactly.

        This gate exists because of a real defect, and it is aimed at that
        defect's whole class.  The mock used to return ``nearRtRicId``,
        ``policyTypeId`` and two ``*Sha256`` members that §7.1 does not define on
        this resource; an Upper check then required ``nearRtRicId``, passed every
        hardware-free run against this mock, and refused the first conformant
        Near-RT RIC it ever met.  A mock richer than the contract does not make
        checks stricter - it makes them untested.

        The expected set is *derived from the frozen bytes*, not restated here,
        so the gate cannot drift away from the contract it claims to enforce.
        """
        declared = {path.name: contract_policy_type_members(path)
                    for path, _digest in MANDATORY_CONTRACTS}
        for path, digest in MANDATORY_CONTRACTS:
            if digest is not None:
                self.assertEqual(
                    digest,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    f"{path.name} is not the frozen byte sequence this gate reads")
        self.assertEqual(1, len(set(declared.values())),
                         f"the vendored authorities disagree: {declared}")
        members = next(iter(declared.values()))
        self.assertEqual(("policySchema", "statusSchema"), members,
                         "non-vacuity: §7.1's member list was not parsed")

        observed = self.mock.policy_type()
        self.assertEqual(set(members), set(observed),
                         "the mock's A1 policy-type response is not the frozen "
                         "contract's member set")
        self.assertEqual(tuple(members), NearRtMock.POLICY_TYPE_OBJECT_MEMBERS)

        # The members carry the frozen schema bytes themselves, which is what
        # makes a digest re-anchor against them meaningful.
        golden = json.loads(
            (ROOT / "golden/golden-vectors.1.0.0.json").read_text())["digests"]
        self.assertEqual(golden["policySchemaJcsSha256"],
                         jcs_sha256(observed["policySchema"]))
        self.assertEqual(golden["statusSchemaJcsSha256"],
                         jcs_sha256(observed["statusSchema"]))

    def test_admission_distinguishes_unknown_ue_from_unsupported_cell(self):
        mock = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        load_capability(mock)
        unknown = policy()
        unknown["scope"]["ueId"]["guAmfUeNgapId"]["amfUeNgapId"] = 999999
        mock.put_policy("unknown", unknown)
        unknown_status = mock.get_status("unknown")
        self.assertEqual("AIC_SCOPE_NOT_FOUND", unknown_status["aicStatus"]["error"]["code"])

        mock = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        load_capability(mock)
        unsupported = policy()
        unsupported["steeringObjective"]["actionEnvelope"]["allowedCells"] = [{
            "plmnId": {"mcc": "208", "mnc": "95"}, "cId": {"ncI": 22222222}}]
        mock.put_policy("unsupported", unsupported)
        cell_status = mock.get_status("unsupported")
        self.assertEqual("AIC_CELL_NOT_ALLOWED", cell_status["aicStatus"]["error"]["code"])
        self.assertEqual("STATEMENT_NOT_APPLICABLE", cell_status["enforceReason"])

    def test_transition_matrix_is_independent_contract_transcription(self):
        self.assertEqual(TRANSITIONS, CONTRACT_TRANSITIONS)
        self.seed()
        with self.assertRaises(TransitionError):
            self.mock._episode("p1", "APPLIED_VERIFIED")

    def test_freshness_is_computed_and_caller_boolean_is_ignored(self):
        self.seed()
        status = self.mock.kpm_snapshot(self.snapshot(first=40, second=60, fresh=False))
        self.assertEqual(status["aicStatus"]["episodeState"], "NO_ACTION")
        self.clock.advance(3.001)
        stale = self.snapshot(when=self.clock.wall - timedelta(milliseconds=3001), fresh=True)
        status = self.mock.kpm_snapshot(stale)
        self.assertNotIn("episodeState", status["aicStatus"])
        self.assertEqual(status["aicStatus"]["error"]["code"], "AIC_KPI_STALE")
        self.assertEqual(self.mock.e2.normal_writes, 0)
        self.assert_all_statuses_valid()
        recovered = self.mock.kpm_snapshot(self.snapshot(first=41, second=60))
        self.assertEqual(recovered["aicStatus"]["policyState"], "ACTIVE")
        self.assertEqual(recovered["aicStatus"]["episodeState"], "NO_ACTION")

    def test_golden_status_template_preserves_runtime_policy_id(self):
        dynamic_policy_id = "runtime-assigned-policy-id"
        self.mock.put_policy(dynamic_policy_id, policy())
        status = self.mock.kpm_snapshot(self.snapshot())
        self.assertEqual(dynamic_policy_id, status["aicStatus"]["policyId"])

    def test_cooldown_coalesces_latest_snapshot_until_fresh(self):
        value = policy(); value["constraints"]["minSecondsBetweenActuations"] = 2
        self.seed(value)
        self.mock.kpm_snapshot(self.snapshot())
        self.assertEqual(self.mock.e2.normal_writes, 1)
        self.clock.advance(1)
        older = self.snapshot(first=20, second=80)
        self.assertIsNone(self.mock.kpm_snapshot(older))
        newest = self.snapshot(first=10, second=90)
        self.assertIsNone(self.mock.kpm_snapshot(newest))
        self.assertEqual(self.mock.e2.normal_writes, 1)
        self.clock.advance(1)
        status = self.mock.tick()
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLIED_VERIFIED")
        self.assertEqual(status["aicStatus"]["selectedCell"], self.cell1)
        self.assertEqual(self.mock.e2.normal_writes, 2)

    def test_expiry_is_periodic_terminal_and_blocks_writes(self):
        value = policy(); value["validity"]["expiresAt"] = (self.clock.wall + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        self.seed(value); self.clock.advance(1)
        status = self.mock.tick()
        self.assertEqual((status["aicStatus"]["policyState"], status["aicStatus"]["policyTerminal"]), ("EXPIRED", True))
        self.mock.kpm_snapshot(self.snapshot())
        self.assertEqual(self.mock.e2.normal_writes, 0)

    def test_action_deadline_uses_scheduled_monotonic_commit(self):
        value = policy(); value["constraints"]["requiredKpiFreshnessMs"] = 20000
        self.seed(value)
        self.mock.kpm_snapshot(self.snapshot(), schedule_only=True)
        self.clock.advance(10)
        status = self.mock.continue_episode("p1")
        self.assertEqual(status["aicStatus"]["episodeState"], "ABORTED_NO_WRITE")
        self.assertEqual(status["aicStatus"]["error"]["code"], "AIC_DEADLINE_EXCEEDED")
        self.assertEqual(self.mock.e2.normal_writes, 0)

    def test_partial_nack_rollback_authorization_and_recovery_split(self):
        self.seed(); self.mock.e2.control_result = "NACK"; self.mock.e2.effect_applied = True
        status = self.mock.kpm_snapshot(self.snapshot())
        self.assertEqual((status["aicStatus"]["episodeState"], status["aicStatus"]["rollback"]["state"]),
                         ("APPLY_FAILED", "REQUESTED"))
        self.assertFalse(status["aicStatus"]["episodeTerminal"])

        value = policy(); value["rollbackPolicy"]["on"] = ["READBACK_MISMATCH"]
        other = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
        load_capability(other)
        other.e2.serving = copy.deepcopy(self.cell1); other.put_policy("p1", value)
        other.e2.control_result = "NACK"; other.e2.effect_applied = True
        snapshot = self.snapshot(); snapshot["servingCell"] = copy.deepcopy(self.cell1)
        status = other.kpm_snapshot(snapshot)
        self.assertEqual(status["aicStatus"]["episodeState"], "RECOVERY_PENDING")
        self.assertNotIn("rollback", status["aicStatus"])
        self.assertEqual(other.e2.rollback_writes, 0)

    def test_rollback_unknown_quarantines_and_higher_revision_releases(self):
        self.seed(); self.mock.e2.control_result = "ACK"; self.mock.e2.effect_applied = False
        self.mock.kpm_snapshot(self.snapshot())
        self.mock.e2.control_result = "TIMEOUT"; self.mock.e2.effect_applied = True
        status = self.mock.rollback("p1")
        self.assertEqual(status["aicStatus"]["episodeState"], "QUARANTINED")
        self.assertEqual(status["aicStatus"]["error"]["code"], "AIC_ROLLBACK_UNKNOWN")
        writes = self.mock.e2.normal_writes
        self.mock.kpm_snapshot(self.snapshot())
        self.assertEqual(self.mock.e2.normal_writes, writes)
        replacement = policy(); replacement["trace"]["policyRevision"] = 2
        self.mock.put_policy("p1", replacement)
        self.assertNotIn("p1", self.mock.quarantine)

    def test_fence_rejects_update_and_delete_without_fresh_readback(self):
        self.seed(); self.mock.e2.control_result = "PENDING"; self.mock.e2.readback_quality = "MISSING"
        self.mock.kpm_snapshot(self.snapshot())
        replacement = policy(); replacement["trace"]["policyRevision"] = 2
        with self.assertRaises(Problem) as put_error:
            self.mock.put_policy("p1", replacement)
        self.assertEqual(put_error.exception.status, 409)
        self.assertEqual(self.mock.get_policy("p1")["trace"]["policyRevision"], 1)
        with self.assertRaises(Problem) as delete_error:
            self.mock.delete_policy("p1")
        self.assertEqual(delete_error.exception.status, 409)
        self.assertEqual(self.mock.e2.normal_writes, 1)
        self.assertIn("p1", self.mock.fences)

    def test_fence_resolves_with_fresh_readback_before_update(self):
        self.seed(); self.mock.e2.control_result = "PENDING"; self.mock.e2.effect_applied = True
        self.mock.kpm_snapshot(self.snapshot())
        self.mock.e2.serving = copy.deepcopy(self.cell2); self.mock.e2.readback_quality = "VERIFIED"
        replacement = policy(); replacement["trace"]["policyRevision"] = 2
        code, _ = self.mock.put_policy("p1", replacement)
        self.assertEqual(code, 200)
        self.assertEqual(self.mock.get_policy("p1")["trace"]["policyRevision"], 2)
        self.assertNotIn("p1", self.mock.fences)
        self.assertEqual(self.mock.e2.normal_writes, 1)

    def test_recovery_window_converges_to_zero_write_quarantine(self):
        self.seed(); self.mock.e2.control_result = "TIMEOUT"; self.mock.e2.effect_applied = True
        self.mock.kpm_snapshot(self.snapshot())
        self.clock.advance(30)
        status = self.mock.tick()
        self.assertEqual(status["aicStatus"]["episodeState"], "QUARANTINED")
        self.assertEqual(self.mock.e2.normal_writes, 1)
        self.assertEqual(self.mock.e2.rollback_writes, 0)
        self.assertIn("p1", self.mock.quarantine)

    def test_declared_admission_and_precondition_errors_are_reachable(self):
        unsupported = policy(); unsupported["steeringObjective"]["kind"] = "UNSUPPORTED"
        with self.assertRaises(Problem) as error:
            self.mock.put_policy("bad", unsupported)
        self.assertEqual(error.exception.code, "AIC_UNSUPPORTED_OBJECTIVE")
        invalid = policy(); invalid["validity"]["expiresAt"] = invalid["validity"]["notBefore"]
        with self.assertRaises(Problem) as error:
            self.mock.put_policy("bad", invalid)
        self.assertEqual(error.exception.code, "AIC_VALIDITY_INVALID")

        for dependency, code in (("CELL_ALLOWED", "AIC_CELL_NOT_ALLOWED"),
                                 ("CELL_NEIGHBOR", "AIC_CELL_NOT_NEIGHBOR"),
                                 ("CAPABILITY", "AIC_CAPABILITY_MISMATCH")):
            mock = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
            load_capability(mock)
            mock.e2.serving = copy.deepcopy(self.cell1); mock.put_policy("p1", policy())
            mock.harness_op({"op": "SET_DEPENDENCY", "dependency": dependency, "ready": False})
            self.assertEqual(mock.get_status("p1")["aicStatus"]["error"]["code"], code)
            self.assertEqual(mock.e2.normal_writes, 0)

    def test_e2_association_loss_uses_max_seq_and_one_clean_callback(self):
        golden = json.loads(
            (ROOT / "golden/golden-vectors.1.0.0.json").read_text())[
                "canonicalObjects"]
        callbacks = []
        mock = NearRtMock(
            callback=lambda status: callbacks.append(copy.deepcopy(status)) or 204,
            wall_clock=lambda: self.clock.wall,
            monotonic=lambda: self.clock.mono)
        load_capability(mock)
        policy_id = golden["appliedVerifiedStatus"]["aicStatus"]["policyId"]
        mock.seed_resource(
            policy_id, golden["policy"], golden["appliedVerifiedStatus"])
        mock.harness_op({
            "op": "INSTALL_STATUS_DESTINATION",
            "url": "http://127.0.0.1/status",
        })

        output = mock.harness_op({
            "op": "SET_DEPENDENCY",
            "dependency": "E2_NODE_ASSOCIATION",
            "ready": False,
        })["outputs"]

        self.assertEqual(
            [item["aicStatus"]["statusSeq"]
             for item in mock.statuses[policy_id]], [7, 8])
        current = mock.statuses[policy_id][-1]
        self.assertEqual(current["enforceStatus"], "NOT_ENFORCED")
        self.assertEqual(current["enforceReason"], "OTHER_REASON")
        self.assertEqual(current["aicStatus"]["error"]["code"],
                         "AIC_E2_NOT_READY")
        for forbidden in ("episodeId", "episodeState", "episodeTerminal",
                          "control", "readback", "rollback"):
            self.assertNotIn(forbidden, current["aicStatus"])
        self.assertEqual(output["callbackAttempts"], [204])
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(mock.e2.normal_writes, 0)
        self.assertEqual(mock.e2.rollback_writes, 0)

    def test_golden_terminal_templates_never_regress_status_sequence(self):
        self.seed()
        self.mock.kpm_snapshot(self.snapshot())
        self.mock.policies["p1"]["last_write_mono"] = None
        self.mock.kpm_snapshot(self.snapshot())

        grouped = {}
        for status in self.mock.statuses["p1"]:
            aic = status["aicStatus"]
            grouped.setdefault((aic["policyId"], aic["producerEpoch"]), []).append(
                aic["statusSeq"])
        for sequences in grouped.values():
            self.assertTrue(all(left < right for left, right in zip(
                sequences, sequences[1:])), sequences)

    def test_duplicate_status_emit_fails_fast(self):
        golden = json.loads(
            (ROOT / "golden/golden-vectors.1.0.0.json").read_text())[
                "canonicalObjects"]
        policy_id = golden["appliedVerifiedStatus"]["aicStatus"]["policyId"]
        mock = NearRtMock(wall_clock=lambda: self.clock.wall,
                          monotonic=lambda: self.clock.mono)
        load_capability(mock)
        mock.seed_resource(policy_id, golden["policy"],
                           "ENFORCED_ACTIVE_NO_EPISODE")
        emitted = copy.deepcopy(golden["appliedVerifiedStatus"])

        mock.harness_op({"op": "A1_EMIT_STATUS", "status": emitted})
        with self.assertRaisesRegex(TransitionError, "strictly increase"):
            mock.harness_op({"op": "A1_EMIT_STATUS", "status": emitted})

        self.assertEqual([1, 7], [
            item["aicStatus"]["statusSeq"]
            for item in mock.statuses[policy_id]
        ])

    def test_explicit_status_replay_is_delivered_without_persistence(self):
        golden = json.loads(
            (ROOT / "golden/golden-vectors.1.0.0.json").read_text())[
                "canonicalObjects"]
        policy_id = golden["appliedVerifiedStatus"]["aicStatus"]["policyId"]
        mock = NearRtMock(wall_clock=lambda: self.clock.wall,
                          monotonic=lambda: self.clock.mono)
        load_capability(mock)
        mock.seed_resource(policy_id, golden["policy"],
                           "ENFORCED_ACTIVE_NO_EPISODE")
        emitted = copy.deepcopy(golden["appliedVerifiedStatus"])
        mock.harness_op({"op": "A1_EMIT_STATUS", "status": emitted})

        replay = mock.harness_op({
            "op": "A1_EMIT_STATUS", "status": emitted, "replay": True,
        })

        self.assertTrue(replay["outputs"]["replayed"])
        self.assertEqual([1, 7], [
            item["aicStatus"]["statusSeq"]
            for item in mock.statuses[policy_id]
        ])

    def test_restart_executes_and_status_sequence_restarts_at_one(self):
        with tempfile.TemporaryDirectory() as directory:
            mock = NearRtMock(state_path=Path(directory) / "wal.json", wall_clock=lambda: self.clock.wall,
                              monotonic=lambda: self.clock.mono)
            load_capability(mock)
            mock.e2.serving = copy.deepcopy(self.cell1); mock.put_policy("p1", policy())
            old_epoch = mock.epoch
            output = mock.harness_op({"op": "PROCESS_RESTART", "component": "NEAR_RT_RIC_A1P_PRODUCER"})
            self.assertTrue(output["outputs"]["restartCompleted"])
            latest = mock.get_status("p1")["aicStatus"]
            self.assertNotEqual(latest["producerEpoch"], old_epoch)
            self.assertEqual(latest["statusSeq"], 1)

    def test_restart_does_not_promote_fixed_logical_clock_to_default(self):
        with tempfile.TemporaryDirectory() as directory:
            mock = NearRtMock(state_path=Path(directory) / "wal.json",
                              wall_clock=lambda: self.clock.wall,
                              monotonic=lambda: self.clock.mono)
            logical = "2026-08-04T00:00:00Z"
            mock.harness_op({
                "op": "RESET_SCENARIO_STATE",
                "logicalOrigin": logical,
                "logicalNow": logical,
            })
            mock.harness_op({
                "op": "PROCESS_RESTART",
                "component": "NEAR_RT_RIC_A1P_PRODUCER",
            })
            self.assertEqual(datetime.fromisoformat(logical.replace("Z", "+00:00")),
                             mock._wall_clock())
            mock.harness_op({"op": "RESET_SCENARIO_STATE"})

            self.assertEqual(self.clock.wall, mock._wall_clock())

    def test_restart_marks_uncertain_action_recovery_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            mock = NearRtMock(state_path=Path(directory) / "wal.json", wall_clock=lambda: self.clock.wall,
                              monotonic=lambda: self.clock.mono)
            load_capability(mock)
            mock.e2.serving = copy.deepcopy(self.cell1); mock.put_policy("p1", policy())
            mock.e2.control_result = "PENDING"; mock.kpm_snapshot(self.snapshot())
            recovered = mock.restart(); latest = recovered.statuses["p1"][-1]["aicStatus"]
            self.assertEqual(latest["episodeState"], "RECOVERY_PENDING")
            self.assertEqual(latest["statusSeq"], 1)
            self.assert_all_statuses_valid(recovered)

    def test_ep_001_through_010_match_golden_write_oracle(self):
        golden = json.loads((ROOT / "golden/golden-vectors.1.0.0.json").read_text())
        expectations = {item["id"]: item for item in golden["scenarioExpectations"]}

        def fresh_mock(value=None):
            mock = NearRtMock(wall_clock=lambda: self.clock.wall, monotonic=lambda: self.clock.mono)
            load_capability(mock)
            mock.e2.serving = copy.deepcopy(self.cell1); mock.put_policy("p1", value or policy())
            return mock

        results = {}
        mock = fresh_mock(); results["EP-001-already-target"] = (mock, mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot(first=40, second=60)}) and mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot(), "scheduleOnly": True})
        mock.harness_op({"op": "SET_DEPENDENCY", "dependency": "REQUIRED_KPI_FRESHNESS", "ready": False})
        results["EP-002-stale-after-scheduled"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()})
        results["EP-003-applying"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "ACK", "effectApplied": True})
        mock.harness_op({"op": "READBACK", "quality": "OK", "servingCellNcI": self.cell2["cId"]["ncI"]})
        results["EP-004-verified"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "TIMEOUT", "effectApplied": True})
        results["EP-005-control-timeout"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "NACK", "effectApplied": False, "writeMayHaveOccurred": False})
        results["EP-006-zero-effect-nack"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "ACK", "effectApplied": False})
        mock.harness_op({"op": "READBACK", "quality": "OK", "servingCellNcI": self.cell1["cId"]["ncI"]})
        results["EP-007-mismatch-rollback-requested"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()}); mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "ACK", "effectApplied": False}); mock.harness_op({"op": "READBACK", "quality": "OK", "servingCellNcI": self.cell1["cId"]["ncI"]})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "channel": "ROLLBACK", "result": "ACK", "effectApplied": True})
        results["EP-008-rollback-verified"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "KPM_SNAPSHOT", "snapshot": self.snapshot()}); mock.harness_op({"op": "E2_CONTROL_RESULT", "result": "ACK", "effectApplied": False}); mock.harness_op({"op": "READBACK", "quality": "OK", "servingCellNcI": self.cell1["cId"]["ncI"]})
        mock.harness_op({"op": "E2_CONTROL_RESULT", "channel": "ROLLBACK", "result": "NACK", "effectApplied": True})
        results["EP-009-rollback-failed-then-quarantine"] = (mock, mock.get_status("p1"))

        mock = fresh_mock(); mock.harness_op({"op": "DECISION_RESULT", "selectedCellNcI": 99999999})
        results["EP-010-envelope-quarantine-zero-write"] = (mock, mock.get_status("p1"))

        for expectation_id, (mock, status) in results.items():
            with self.subTest(expectation_id=expectation_id):
                expected = expectations[expectation_id]; aic = status["aicStatus"]
                self.assertEqual(aic["episodeState"], expected["expectedEpisodeState"])
                self.assertEqual(aic["episodeTerminal"], expected["expectedEpisodeTerminal"])
                self.assertEqual(mock.e2.normal_writes, expected["expectedNormalWrites"])
                self.assertEqual(mock.e2.rollback_writes, expected["expectedRollbackWrites"])
                if "expectedErrorCode" in expected: self.assertEqual(aic["error"]["code"], expected["expectedErrorCode"])
                self.assert_all_statuses_valid(mock)

    def test_golden_snapshots_are_schema_valid(self):
        golden = json.loads((ROOT / "golden/golden-vectors.1.0.0.json").read_text())["canonicalObjects"]
        validate_status(golden["appliedVerifiedStatus"]); validate_status(golden["noActionStatus"])


if __name__ == "__main__":
    unittest.main()
