"""Hardware-free proofs for the Campaign-5 ``our_rc_xapp`` worker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oran.campaign5.live_worker import (
    Campaign5LiveWorker,
    IdentityRefusal,
    ScopeConflict,
)
from oran.campaign5.producer import A1Conflict, Campaign5PolicyProducer

CAP = "AIC_UeDlPrbCap_1.0.0"
CELL = "12345678"
AMF = 130
RAN = 41
AMF2 = 131
RAN2 = 42
NB = 3584
EPOCH = 273
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

# Never inherit credentials, endpoints or session tokens into fake controls.
TEST_ENV = {"PATH": os.defpath, "HOME": "/nonexistent/campaign5-test", "LANG": "C.UTF-8",
            # v5 power spacing off: no test waits on another test's (or the bed's) RF write.
            "AIC_POWER_WRITE_STAMP": ""}
CONTROL_ENV_FIELDS = frozenset({
    "RC_HEADER_RRC_UE_ID", "RC_HEADER_AMF_UE_NGAP_ID",
    "RC_UE_GUAMI_MCC", "RC_UE_GUAMI_MNC", "RC_UE_GUAMI_MNC_LEN",
    "RC_UE_AMF_REGION_ID", "RC_UE_AMF_SET_ID", "RC_UE_AMF_POINTER",
    "RC_SOURCE_CONNECTION_EPOCH", "RC_SOURCE_NB_ID", "RC_ACTION",
    "RC_CAP_MAX_DL_PRBS", "RC_PF_WEIGHT", "RC_MCS_MIN", "RC_MCS_MAX",
    "RC_TX_ATTEN_DB", "RC_SLICE_MIN_RATIO", "RC_SLICE_MAX_RATIO",
    "RC_SLICE_DEDICATED_RATIO", "RC_SLICE_MCC", "RC_SLICE_MNC",
    "RC_SLICE_MNC_LEN", "RC_SLICE_SST", "RC_SLICE_HAS_SD", "RC_SLICE_SD",
})


def policy(value: int = 12) -> dict:
    return {
        "config": {"cellId": CELL, "ueId": str(AMF), "maxDlPrbs": value},
        "validity": {
            "notBefore": "2026-09-04T00:00:00Z",
            "notAfter": "2026-09-05T00:00:00Z",
        },
        "trace": {"traceId": "tx-1", "revision": 1, "fencingToken": 1},
    }


def scoped_policy(policy_type: str, values: dict) -> dict:
    common = {
        "validity": {
            "notBefore": "2026-09-04T00:00:00Z",
            "notAfter": "2026-09-05T00:00:00Z",
        },
        "trace": {"traceId": "tx-cell", "revision": 1, "fencingToken": 1},
    }
    if policy_type == "AIC_SliceSLATarget_1.0.0":
        return {
            **common,
            "scope": {
                "plmnId": {"mcc": "208", "mnc": "95"},
                "snssai": {"sst": 1, "sd": "00007b"},
            },
            "quota": dict(values),
        }
    config = {"cellId": CELL, **values}
    if policy_type == "AIC_CellDlTxPower_1.0.0":
        config["gnbId"] = "gnb1"
    return {**common, "config": config}



import contextlib as _contextlib
import logging as _logging


@_contextlib.contextmanager
def _quiet():
    logger = _logging.getLogger('oran.campaign5.live_worker')
    previous = logger.disabled
    logger.disabled = True
    try:
        yield
    finally:
        logger.disabled = previous


class Harness:
    def __init__(self, root: Path, actions, *, ran_ue_id: int = RAN,
                 cell_id: str = CELL, nb_id: int = NB, epoch: int = EPOCH):
        self.root = root
        self.cell_id, self.nb_id, self.epoch = cell_id, nb_id, epoch
        self.jsonl = root / "a1-live-kpm.jsonl"
        self.ledger = root / "worker.json"
        self.actions = list(actions)
        self.calls = []
        self.invocation_headers = []
        self.now = NOW
        self.active_ue = "ue1"
        self.active_amf = AMF
        self.active_ran = RAN
        self.write_header("ue1", AMF, RAN)
        self.append(0, ran_ue_id=ran_ue_id)

    def write_header(self, ue_tag: str, amf_id: int, ran_ue_id: int) -> None:
        (self.root / f"{ue_tag}-hdr.env").write_text(
            "\n".join((
                f"RC_HEADER_RRC_UE_ID={ran_ue_id}",
                f"RC_HEADER_AMF_UE_NGAP_ID={amf_id}",
                "RC_UE_GUAMI_MCC=208",
                "RC_UE_GUAMI_MNC=95",
                "RC_UE_GUAMI_MNC_LEN=2",
                "RC_UE_AMF_REGION_ID=1",
                "RC_UE_AMF_SET_ID=2",
                "RC_UE_AMF_POINTER=3",
                "",
            )),
            encoding="utf-8",
        )

    def append(self, cap: int, *, ran_ue_id: int = RAN) -> None:
        self.append_record(
            ue_measurements=[
                {"name": "RAN.UE.DlPrbCap", "type": "int", "value": cap}
            ],
            ran_ue_id=ran_ue_id,
        )

    def append_record(self, *, measurements=(), ue_measurements=(),
                      ran_ue_id=None, include_ue=True) -> None:
        ues = []
        if include_ue:
            ues.append({
                "ue_id_type": "gNB",
                "has_ran_ue_id": True,
                "amf_ue_ngap_id": self.active_amf,
                "ran_ue_id": self.active_ran if ran_ue_id is None else ran_ue_id,
                "guami": {
                    "mcc": 208, "mnc": 95, "mnc_digit_len": 2,
                    "amf_region_id": 1, "amf_set_id": 2, "amf_pointer": 3,
                },
                "measurements": list(ue_measurements),
            })
        record = {
            "event": "kpm_indication",
            "recv_unix_us": int(self.now.timestamp() * 1_000_000),
            "e2_node": f"type=2;mcc=208;mnc=95;nb={self.nb_id}/0",
            "nb_id": self.nb_id,
            "connection_epoch": self.epoch,
            "ues": ues,
            "measurements": list(measurements),
        }
        with self.jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def append_cell(self, counter: str, values: dict, *, ue_values=None,
                    include_ue=True) -> None:
        value = next(iter(values.values())) if len(values) == 1 else dict(values)
        ue_measurements = []
        if ue_values is not None:
            ue_value = (
                next(iter(ue_values.values()))
                if len(ue_values) == 1 else dict(ue_values)
            )
            ue_measurements.append({"name": counter, "type": "int", "value": ue_value})
        self.append_record(
            measurements=[{"name": counter, "type": "int", "value": value}],
            ue_measurements=ue_measurements,
            include_ue=include_ue,
        )

    def append_ue(self, counter: str, values: dict) -> None:
        value = next(iter(values.values())) if len(values) == 1 else dict(values)
        self.append_record(ue_measurements=[{
            "name": counter, "type": "real", "value": value,
        }])

    def reattach(self) -> None:
        (self.root / f"{self.active_ue}-hdr.env").unlink(missing_ok=True)
        self.active_ue = "ue2"
        self.active_amf = AMF2
        self.active_ran = RAN2
        self.write_header(self.active_ue, self.active_amf, self.active_ran)

    def detach_all(self) -> None:
        (self.root / f"{self.active_ue}-hdr.env").unlink(missing_ok=True)

    def runner(self, argv, **kwargs):
        action = self.actions.pop(0)
        # Assertion diffs must never dump inherited credentials/session env.
        self.calls.append((list(argv), {key: value for key, value in kwargs["env"].items()
                                       if key in CONTROL_ENV_FIELDS}))
        self.assert_authoritative_argv(argv)
        self.invocation_headers.append(
            Path(argv[2]).read_text(encoding="utf-8")
        )
        if action == "crash":
            raise KeyboardInterrupt("simulated process death")
        if isinstance(action, tuple) and action[0] == "cell":
            self.append_cell(action[1], action[2])
        elif isinstance(action, tuple) and action[0] == "ue":
            self.append_ue(action[1], action[2])
        elif action is not None:
            self.append(int(action))
        return SimpleNamespace(
            returncode=0,
            stdout=(f"RC control action={kwargs['env']['RC_ACTION']} success=1\n"
                    "RIC Control ACK\n"),
            stderr="",
        )

    @staticmethod
    def assert_authoritative_argv(argv) -> None:
        if len(argv) != 3 or argv[1] != "--header-file":
            raise AssertionError(f"worker did not pass an authoritative header: {argv!r}")

    def worker(self) -> Campaign5LiveWorker:
        return Campaign5LiveWorker(
            kpm_jsonl=self.jsonl,
            header_dir=self.root,
            fire_xapp=self.root / "fire_xapp.sh",
            ledger_path=self.ledger,
            cell_id=self.cell_id,
            nb_id=self.nb_id,
            freshness_s=5,
            deadline_s=0.01,
            poll_interval_s=0.001,
            clock=lambda: self.now,
            runner=self.runner,
        )


class LiveWorkerProof(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fake_controls_never_capture_unrelated_environment_values(self):
        self.assertEqual(set(TEST_ENV), set(os.environ))
        harness = Harness(self.root, [12])
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)
        # Deliberately fake sentinels; no credential is read from the process.
        sentinels = {"ANTHROPIC_API_KEY": "fake-test-only",
                     "ANTHROPIC_AUTH_TOKEN": "fake-test-only",
                     "ORCA_AGENT_HOOK_TOKEN": "fake-test-only",
                     "CLAUDE_CODE_MESSAGING_TOKEN": "fake-test-only",
                     "RC_UNRELATED_SECRET": "fake-test-only"}
        with patch.dict(os.environ, sentinels):
            producer.put_policy(CAP, "capture-proof", policy())
        self.assertEqual(1, len(harness.calls))
        captured_keys = set(harness.calls[0][1])
        self.assertFalse(captured_keys.intersection(sentinels))
        self.assertLessEqual(captured_keys, CONTROL_ENV_FIELDS)

    def test_ack_without_new_readback_is_unverified_then_restored(self):
        harness = Harness(self.root, [None, 0])
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)

        producer.put_policy(CAP, "p1", policy())

        status = producer.get_status(CAP, "p1")
        self.assertEqual("APPLIED_UNVERIFIED", status["aicStatus"]["episodeState"])
        self.assertEqual("NOT_ENFORCED", status["enforceStatus"])
        self.assertFalse(status["aicStatus"]["control"]["resultIsEffectEvidence"])
        self.assertEqual(2, len(harness.calls))
        self.assertEqual("12", harness.calls[0][1]["RC_CAP_MAX_DL_PRBS"])
        self.assertEqual("0", harness.calls[1][1]["RC_CAP_MAX_DL_PRBS"])

    def test_stale_kpm_to_header_identity_performs_zero_writes(self):
        harness = Harness(self.root, [], ran_ue_id=RAN + 1)

        with self.assertRaises(IdentityRefusal):
            harness.worker().apply(CAP, "p1", policy())

        self.assertEqual([], harness.calls)
        self.assertFalse(harness.ledger.exists())

    def test_duplicate_policy_causes_a_single_requested_write(self):
        harness = Harness(self.root, [12])
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)

        first = producer.put_policy(CAP, "p1", policy())
        second = producer.put_policy(CAP, "p1", policy())

        self.assertEqual(201, first.http_status)
        self.assertEqual(200, second.http_status)
        self.assertEqual(1, len(harness.calls))
        self.assertEqual("APPLIED_VERIFIED",
                         producer.get_status(CAP, "p1")["aicStatus"]["episodeState"])

    def test_durable_writer_slot_refuses_a_second_policy_for_the_same_ue_action(self):
        harness = Harness(self.root, [12])
        worker = harness.worker()
        worker.apply(CAP, "p1", policy())

        with self.assertRaises(ScopeConflict):
            worker.apply(CAP, "p2", policy())

        self.assertEqual(1, len(harness.calls))

    def test_restart_after_ambiguous_write_converges_without_reapplying_target(self):
        harness = Harness(self.root, ["crash", 0])
        with self.assertRaises(KeyboardInterrupt):
            harness.worker().apply(CAP, "p1", policy())

        outcome = harness.worker().apply(CAP, "p1", policy())

        self.assertTrue(outcome.rollback_verified)
        self.assertFalse(outcome.effect_verified)
        self.assertEqual(2, len(harness.calls))
        self.assertEqual("12", harness.calls[0][1]["RC_CAP_MAX_DL_PRBS"])
        self.assertEqual("0", harness.calls[1][1]["RC_CAP_MAX_DL_PRBS"])
        state = json.loads(harness.ledger.read_text(encoding="utf-8"))
        attempt = next(iter(state["entries"]["p1"]["attempts"].values()))
        self.assertEqual("ROLLED_BACK_VERIFIED", attempt["phase"])

    def test_restart_rehydrates_producer_and_reverifies_without_another_write(self):
        harness = Harness(self.root, [12])
        first = Campaign5PolicyProducer()
        harness.worker().bind(first)
        first.put_policy(CAP, "p1", policy())
        self.assertEqual(1, len(harness.calls))

        restarted = Campaign5PolicyProducer()
        harness.worker().bind(restarted)

        self.assertEqual(["p1"], restarted.list_policies(CAP))
        self.assertEqual("APPLIED_VERIFIED",
                         restarted.get_status(CAP, "p1")["aicStatus"]["episodeState"])
        self.assertEqual(1, len(harness.calls))

    def test_delete_restores_persisted_baseline_before_releasing_policy(self):
        harness = Harness(self.root, [12, 0])
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)
        producer.put_policy(CAP, "p1", policy())

        response = producer.handle(
            "DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/p1"
        )

        self.assertEqual(204, response.status)
        self.assertEqual(2, len(harness.calls))
        self.assertEqual("0", harness.calls[-1][1]["RC_CAP_MAX_DL_PRBS"])
        self.assertEqual([], producer.list_policies(CAP))

    def test_expiry_uses_the_same_verified_restore_and_delete_gate(self):
        harness = Harness(self.root, [12, 0])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        expiring = policy()
        expiring["validity"]["notAfter"] = "2026-09-04T12:00:01Z"
        producer.put_policy(CAP, "p1", expiring)
        harness.now = NOW + timedelta(seconds=2)

        expired = worker.expire_due()

        self.assertEqual(("p1",), expired)
        self.assertEqual([], producer.list_policies(CAP))
        self.assertEqual("0", harness.calls[-1][1]["RC_CAP_MAX_DL_PRBS"])

    def test_expiry_failure_is_logged_retained_and_retried(self):
        harness = Harness(self.root, [12, 0])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        expiring = policy()
        expiring["validity"]["notAfter"] = "2026-09-04T12:00:01Z"
        producer.put_policy(CAP, "p1", expiring)
        harness.now = NOW + timedelta(seconds=10)

        with self.assertLogs("oran.campaign5.live_worker", level="ERROR") as captured:
            self.assertEqual((), worker.expire_due())

        self.assertIn("policy_id=p1", "\n".join(captured.output))
        self.assertEqual(["p1"], producer.list_policies(CAP))
        harness.append(12)
        # A failed restore backs off (2 s after the first failure) rather than
        # re-reading the stream every timer tick; it is still retried after that.
        self.assertEqual((), worker.expire_due())
        harness.now = NOW + timedelta(seconds=13)
        harness.append(12)
        self.assertEqual(("p1",), worker.expire_due())
        self.assertEqual([], producer.list_policies(CAP))

    def test_a_restore_that_keeps_failing_backs_off_to_a_bounded_interval(self):
        # 2026-09-15: two policies whose UEs had left were retried every second for
        # hours, each pass reading the whole KPM stream; the producer sat at 100 % CPU.
        harness = Harness(self.root, [12, 0])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        expiring = policy()
        expiring["validity"]["notAfter"] = "2026-09-04T12:00:01Z"
        producer.put_policy(CAP, "p1", expiring)
        attempts = []
        for second in range(10, 700):
            harness.now = NOW + timedelta(seconds=second)
            with _quiet():
                worker.expire_due()
            attempts.append(worker.__dict__.get("_expiry_failures", {}).get("p1", 0))
        self.assertLess(attempts[-1], 15)          # not ~690 retries
        self.assertGreater(attempts[-1], 3)        # but still retried
        self.assertEqual(["p1"], producer.list_policies(CAP))

    def test_a_stuck_policy_stops_reprinting_its_stack_and_says_how_long(self):
        """갇힌 정책은 **읽히는 상태**여야 한다.

        2026-09-17 에 정책 여섯 개가 최대 **119회(약 10시간)** 재시도 중이었는데,
        300초마다 같은 스택을 통째로 찍어 프로듀서 로그가 1 MB 의 동일한 트레이스백
        이었다.  그래서 "정책 하나가 영구히 갇혔다" 는 사실이 아무에게도 안 보였다.
        백오프(위 시험)는 CPU 를 고쳤지 **가독성**을 고치지 않았다.
        """
        import logging

        harness = Harness(self.root, [12, 0])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        expiring = policy()
        expiring["validity"]["notAfter"] = "2026-09-04T12:00:01Z"
        producer.put_policy(CAP, "p1", expiring)

        with self.assertLogs("oran.campaign5.live_worker", level=logging.WARNING) as caught:
            for second in range(10, 2000):
                harness.now = NOW + timedelta(seconds=second)
                worker.expire_due()

        stacks = [r for r in caught.records if r.exc_info]
        lines = [r for r in caught.records if not r.exc_info]
        self.assertLessEqual(len(stacks), 3,
                             "같은 스택을 되풀이해 찍으면 아무도 못 본다")
        self.assertGreater(len(lines), 0, "그 뒤로도 말은 해야 한다")
        said = lines[-1].getMessage()
        self.assertIn("stuck for", said)
        self.assertIn("attempt", said)
        # 사유가 한 줄에 실려야 한다 -- 스택 없이도 무엇이 막혔는지 알아야 한다.
        self.assertRegex(said, r"[A-Za-z]+Error|Refusal|Conflict")

    def test_the_kpm_cache_is_a_window_and_line_numbers_stay_absolute(self):
        """줄 캐시는 창이어야 하고, 그래도 **줄번호는 절대값**이라야 한다.

        모든 독자가 최근 줄만 쓴다 -- 두 곳은 `fresh()`(라이브 5초)로 거르고
        한 곳은 최신부터 거꾸로 걷는다. 그런데 읽은 줄을 전부 기억하고 있었고,
        2026-09-17 에 라이브 액션 프로듀서가 **RSS 1060 MB**, 스트림은 178 MB /
        177,298줄이고 초당 5 KB 씩 계속 자라고 있었다. 09-15 수정은 파일을 다시
        안 읽게 했지 **기억하지 않게** 하지는 않았다.

        창을 씌우면서 `min_line` 마커가 깨지면 안 된다: 마커는 쓰기 직전에
        `line_count()` 로 찍히고, 그 사이 줄이 밀려나도 **같은 줄을 가리켜야** 한다.
        """
        from oran.campaign5 import live_worker as lw

        path = self.root / "window-kpm.jsonl"
        path.write_text("")
        gate = lw._KpmGate(path, clock=lambda: NOW, freshness_s=5.0, nb_id=1)

        original = lw.KPM_LINE_WINDOW
        lw.KPM_LINE_WINDOW = 10
        try:
            with path.open("a") as handle:
                for n in range(4):
                    handle.write(json.dumps({"event": "kpm_indication", "n": n}) + "\n")
            self.assertEqual(4, gate.line_count())
            marker = gate.line_count()

            # 창을 훌쩍 넘겨 채운다.
            with path.open("a") as handle:
                for n in range(4, 30):
                    handle.write(json.dumps({"event": "kpm_indication", "n": n}) + "\n")

            self.assertEqual(30, gate.line_count(),
                             "창을 씌워도 줄 수는 절대값이라야 한다")
            self.assertLessEqual(len(gate._cache["lines"]), 10,
                                 "창보다 많이 들고 있으면 새는 것이다")

            kept = gate.records(min_line=marker)
            self.assertEqual([n for n in range(20, 30)], [r["n"] for _, r in kept],
                             "창 밖은 줄 수 없지만, 준 것은 마커 이후라야 한다")
            self.assertEqual([n for n in range(20, 30)], [no for no, _ in kept],
                             "줄번호가 절대값이 아니면 마커가 딴 줄을 가리킨다")

            # 최신부터 거꾸로 걷는 독자는 창 안에서 답을 얻는다.
            newest = gate.records()[-1]
            self.assertEqual(29, newest[1]["n"])
            self.assertEqual(29, newest[0])
        finally:
            lw.KPM_LINE_WINDOW = original

    def test_cell_and_slice_lifecycle_survives_control_ue_reattach(self):
        cases = (
            (
                "AIC_DlMcsBounds_1.0.0", "RAN.Cell.DlMcsBounds",
                {"minDlMcs": 4, "maxDlMcs": 16},
                {"minDlMcs": 0, "maxDlMcs": 28},
            ),
            (
                "AIC_CellDlTxPower_1.0.0", "RAN.Cell.TxAttenuationDb",
                {"txAttenuationDb": 6}, {"txAttenuationDb": 0},
            ),
            (
                "AIC_SliceSLATarget_1.0.0", "RAN.SlicePrbQuotaMin",
                {
                    "minPrbPolicyRatio": 30, "maxPrbPolicyRatio": 90,
                    "dedicatedPrbPolicyRatio": 10,
                },
                {
                    "minPrbPolicyRatio": 1, "maxPrbPolicyRatio": 100,
                    "dedicatedPrbPolicyRatio": 0,
                },
            ),
        )
        for index, (policy_type, counter, desired, baseline) in enumerate(cases):
            with self.subTest(policy_type=policy_type):
                root = self.root / str(index)
                root.mkdir()
                harness = Harness(root, [
                    ("cell", counter, desired), ("cell", counter, baseline),
                ])
                # A conflicting UE-level leaf must not satisfy a cell readback.
                harness.append_cell(counter, baseline, ue_values=desired)
                worker = harness.worker()

                applied = worker.apply(
                    policy_type, "p-cell", scoped_policy(policy_type, desired)
                )
                self.assertTrue(applied.effect_verified)
                self.assertEqual(str(AMF), harness.calls[0][1][
                    "RC_HEADER_AMF_UE_NGAP_ID"
                ])

                harness.detach_all()
                harness.append_cell(counter, desired, include_ue=False)
                with self.assertRaises(IdentityRefusal):
                    worker.withdraw(policy_type, "p-cell")
                state = json.loads(harness.ledger.read_text(encoding="utf-8"))
                attempt = next(iter(
                    state["entries"]["p-cell"]["attempts"].values()
                ))
                self.assertEqual("APPLIED_VERIFIED", attempt["phase"])
                self.assertEqual(1, len(harness.calls))

                harness.reattach()
                harness.append_cell(counter, desired)
                withdrawn = worker.withdraw(policy_type, "p-cell")

                self.assertTrue(withdrawn.rollback_verified)
                self.assertEqual(str(AMF2), harness.calls[1][1][
                    "RC_HEADER_AMF_UE_NGAP_ID"
                ])
                state = json.loads(harness.ledger.read_text(encoding="utf-8"))
                identity = state["entries"]["p-cell"]["identity"]
                self.assertEqual({
                    "scopeKind": "CELL", "cellId": CELL,
                    "nbId": NB, "epoch": EPOCH,
                }, identity)

    def test_scheduler_priority_uses_the_full_apply_withdraw_lifecycle(self):
        policy_type = "AIC_SchedulerPriority_1.0.0"
        counter = "RAN.UE.PfWeight"
        desired = {"pfWeight": 4.0}
        baseline = {"pfWeight": 1.0}
        harness = Harness(self.root, [
            ("ue", counter, desired), ("ue", counter, baseline),
        ])
        harness.append_ue(counter, baseline)
        worker = harness.worker()

        applied = worker.apply(
            policy_type, "p-priority", {
                **scoped_policy(policy_type, desired),
                "config": {"cellId": CELL, "ueId": str(AMF), **desired},
            },
        )
        withdrawn = worker.withdraw(policy_type, "p-priority")

        self.assertTrue(applied.effect_verified)
        self.assertTrue(withdrawn.rollback_verified)
        self.assertEqual("4.0", harness.calls[0][1]["RC_PF_WEIGHT"])
        self.assertEqual("1.0", harness.calls[1][1]["RC_PF_WEIGHT"])

    def test_renderer_uses_the_operator_wrapper_vocabulary_for_all_five_actions(self):
        harness = Harness(self.root, [None] * 5)
        worker = harness.worker()
        identity = worker._resolve_identity(None, CELL)
        # Reproduce the reviewed race: refresh the shared ue1 file after the
        # worker captures identity.  The child must still receive that capture.
        harness.write_header("ue1", AMF2, RAN2)
        cases = (
            ("AIC_UeDlPrbCap_1.0.0", {"maxDlPrbs": 12},
             {"RC_ACTION": "102", "RC_CAP_MAX_DL_PRBS": "12"}),
            ("AIC_SchedulerPriority_1.0.0", {"pfWeight": 4.0},
             {"RC_ACTION": "103", "RC_PF_WEIGHT": "4.0"}),
            ("AIC_DlMcsBounds_1.0.0", {"minDlMcs": 4, "maxDlMcs": 16},
             {"RC_ACTION": "101", "RC_MCS_MIN": "4", "RC_MCS_MAX": "16"}),
            ("AIC_CellDlTxPower_1.0.0", {"txAttenuationDb": 6},
             {"RC_ACTION": "104", "RC_TX_ATTEN_DB": "6"}),
            ("AIC_SliceSLATarget_1.0.0", {
                "minPrbPolicyRatio": 1,
                "maxPrbPolicyRatio": 100,
                "dedicatedPrbPolicyRatio": 0,
                "_scope": {
                    "plmnId": {"mcc": "208", "mnc": "95"},
                    "snssai": {"sst": 1, "sd": "00007b"},
                },
            }, {
                "RC_ACTION": "6", "RC_SLICE_MIN_RATIO": "1",
                "RC_SLICE_MAX_RATIO": "100", "RC_SLICE_DEDICATED_RATIO": "0",
                "RC_SLICE_MCC": "208", "RC_SLICE_MNC": "95",
                "RC_SLICE_MNC_LEN": "2", "RC_SLICE_SST": "1",
                "RC_SLICE_HAS_SD": "1", "RC_SLICE_SD": "123",
            }),
        )

        for policy_type, values, expected in cases:
            action = worker._action(policy_type)
            ack, _ = worker._run_control(action, identity, values)
            self.assertTrue(ack)
            env = harness.calls[-1][1]
            with self.subTest(policy_type=policy_type):
                for name, value in expected.items():
                    self.assertEqual(value, env[name])
                self.assertEqual(str(RAN), env["RC_HEADER_RRC_UE_ID"])
                self.assertEqual(str(AMF), env["RC_HEADER_AMF_UE_NGAP_ID"])
                self.assertEqual(str(EPOCH), env["RC_SOURCE_CONNECTION_EPOCH"])
                header = dict(
                    line.split("=", 1)
                    for line in harness.invocation_headers[-1].splitlines()
                )
                self.assertEqual({name: env[name] for name in header}, header)
                self.assertEqual([], list(self.root.glob(".campaign5-control-*.env")))

    def test_target_worker_header_carries_its_bound_node_and_epoch(self):
        harness = Harness(self.root, [12], cell_id="87654321", nb_id=2816, epoch=274)
        target = policy()
        target["config"]["cellId"] = harness.cell_id
        self.assertTrue(harness.worker().apply(CAP, "target", target).effect_verified)
        header = dict(line.split("=", 1)
                      for line in harness.invocation_headers[-1].splitlines())
        self.assertEqual("2816", header.get("RC_SOURCE_NB_ID"))
        self.assertEqual("274", header["RC_SOURCE_CONNECTION_EPOCH"])

    def test_operator_wrapper_uses_worker_epoch_instead_of_witness(self):
        wrapper = self.root / "fire_xapp.sh"
        shutil.copy2(
            Path(__file__).resolve().parents[2] / "scripts/hardware/fire_xapp.sh",
            wrapper,
        )
        fake_xapp = self.root / "fake-xapp.sh"
        fake_xapp.write_text(
            "#!/bin/sh\n"
            "echo action=$RC_ACTION success=1 ACK "
            "epoch=$RC_SOURCE_CONNECTION_EPOCH rrc=$RC_HEADER_RRC_UE_ID "
            "node=$RC_SOURCE_NB_ID\n",
            encoding="utf-8",
        )
        fake_xapp.chmod(0o755)
        (self.root / "sm").mkdir()
        witness = self.root / "witness.json"
        witness.write_text(json.dumps({"connections": [{
            "connectionEpoch": 999,
            "globalE2NodeId": {"nbId": NB},
            "active": True,
        }]}), encoding="utf-8")
        (self.root / "env.sh").write_text(
            f"HW_XAPP_BIN='{fake_xapp}'\n"
            f"HW_XAPP_CONF='{self.root / 'xapp.conf'}'\n"
            f"HW_WITNESS='{witness}'\n"
            f"HW_RUNTIME_DIR='{self.root}'\n"
            f"HW_GNB1_NB_ID='{NB}'\n",
            encoding="utf-8",
        )
        header = self.root / "worker.env"
        header.write_text(
            f"RC_HEADER_RRC_UE_ID={RAN}\n"
            f"RC_HEADER_AMF_UE_NGAP_ID={AMF}\n"
            "RC_UE_GUAMI_MCC=208\nRC_UE_GUAMI_MNC=95\n"
            "RC_UE_GUAMI_MNC_LEN=2\nRC_UE_AMF_REGION_ID=1\n"
            "RC_UE_AMF_SET_ID=2\nRC_UE_AMF_POINTER=3\n"
            f"RC_SOURCE_CONNECTION_EPOCH={EPOCH}\n"
            "RC_SOURCE_NB_ID=2816\n",
            encoding="utf-8",
        )

        env = {**TEST_ENV, "RC_ACTION": "102", "RC_CAP_MAX_DL_PRBS": "12"}
        completed = subprocess.run(
            [str(wrapper), "--header-file", str(header)], env=env,
            text=True, capture_output=True, check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"epoch={EPOCH}", completed.stdout)
        self.assertNotIn("epoch=999", completed.stdout)
        self.assertIn("node=2816", completed.stdout)

        # The shared operator branch must reach its explicit fail-closed guard
        # when the witness has no active epoch, despite ``set -e``.
        shutil.copy2(header, self.root / "ue1-hdr.env")
        witness.write_text(json.dumps({"connections": []}), encoding="utf-8")
        operator = subprocess.run(
            [str(wrapper), "ue1"], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(9, operator.returncode)
        self.assertIn(f"FATAL: no active epoch for nbId={NB}", operator.stderr)


if __name__ == "__main__":
    unittest.main()


class KpmGateReadsOnlyWhatIsNew(unittest.TestCase):
    def test_appended_lines_partial_tail_and_a_replaced_file(self):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from oran.campaign5.live_worker import _KpmGate
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kpm.jsonl"
            row = lambda n: json.dumps({"event": "kpm_indication", "n": n}) + "\n"
            path.write_text(row(1) + row(2))
            gate = _KpmGate(path, clock=lambda: datetime.now(timezone.utc), freshness_s=5, nb_id=1)
            self.assertEqual(2, gate.line_count())
            with path.open("a") as handle:
                handle.write(row(3) + '{"event": "kpm_ind')      # a writer mid-line
            self.assertEqual([3], [r["n"] for _, r in gate.records(min_line=2)])
            with path.open("a") as handle:
                handle.write('ication", "n": 4}\n')
            self.assertEqual([3, 4], [r["n"] for _, r in gate.records(min_line=2)])
            replacement = Path(tmp) / "new.jsonl"
            replacement.write_text(row(9))
            replacement.replace(path)                              # rotation
            self.assertEqual([9], [r["n"] for _, r in gate.records()])


class AFailedCreateMustNotStrandTheScope(unittest.TestCase):
    """적용이 거절된 **생성**은 정책이 된 적이 없다 — 등록부가 들고 있으면 안 된다.

    들고 있으면 scope 가 **영구히** 막힌다.  워커는 자기 원장을 쓰기 **전에** 거절할
    수 있고(신원 거절 · 기준선 없음 · scope 충돌), `expire_due()` 는 그 원장을
    순회하므로 그런 정책을 **영영 보지 못한다.**  그동안 프로듀서의 `_scope_owner`
    claim 은 같은 축의 뒤 쓰기를 전부 `target scope already owned by policy <id>`
    로 막는다.

    2026-09-17 실측: 프로듀서가 9개를 들고 있었고 **그중 어느 것도** 두 워커 원장에
    없었다.  그중 하나(`78c1aa67`, ue 3 의 pfWeight)가 **판 넷을 연속으로** 막았고
    그중 셋이 `RECOVERY_FAILURE` 로 끝났다.
    """

    TYPE = "AIC_UeDlPrbCap_1.0.0"
    BODY = {"config": {"cellId": "12345678", "maxDlPrbs": 6, "ueId": "9"},
            "trace": {"fencingToken": 1, "revision": 1, "traceId": "t"},
            "validity": {"notBefore": "2026-09-17T00:00:00Z",
                         "notAfter": "2026-09-18T00:00:00Z"}}

    def producer(self):
        from oran.campaign5.producer import Campaign5PolicyProducer
        return Campaign5PolicyProducer()

    def test_the_scope_is_free_for_the_next_write(self):
        producer = self.producer()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the worker refused before writing its ledger")

        producer._apply_handler = refuse
        with self.assertRaises(RuntimeError):
            producer.put_policy(self.TYPE, "pol-A", self.BODY)

        self.assertNotIn("pol-A", producer._records, "거절된 생성이 남아 있다")
        self.assertEqual({}, producer._scope_owner, "scope claim 이 남아 있다")

        producer._apply_handler = None
        result = producer.put_policy(self.TYPE, "pol-B", self.BODY)
        self.assertEqual(201, result.http_status)

    def test_a_failed_update_keeps_the_policy_that_was_already_there(self):
        """갱신 실패는 되돌리지 않는다 — 그 정책은 실재하고 롤백 의무가 있을 수 있다."""
        producer = self.producer()
        producer.put_policy(self.TYPE, "pol-A", self.BODY)

        def refuse(*_args, **_kwargs):
            raise RuntimeError("the worker refused the update")

        producer._apply_handler = refuse
        newer = json.loads(json.dumps(self.BODY))
        newer["trace"] = {"fencingToken": 2, "revision": 2, "traceId": "t"}
        newer["config"]["maxDlPrbs"] = 12
        with self.assertRaises(RuntimeError):
            producer.put_policy(self.TYPE, "pol-A", newer)

        self.assertIn("pol-A", producer._records, "실재하던 정책을 지우면 안 된다")
        self.assertEqual({(self.TYPE, "cellId=12345678/ueId=9"): "pol-A"},
                         producer._scope_owner)


class UnrestorableRollbackRetirement(unittest.TestCase):
    """33 정책이 16시간 동안 같은 롤백을 재시도하고 있었다 (2026-09-18).

    사유는 전부 ``UE scope has no unique fresh KPM/header identity`` — 그 노브를
    들고 있던 UE 문맥이 사라졌다는 뜻이고, 재부착한 UE 는 기준값에서 시작하므로
    (실측: ``RAN.UE.DlPrbCap`` 0 · ``RAN.UE.PfWeight`` 1.0) 되돌릴 대상이 없다.
    은퇴 판정은 **셋이 함께 성립할 때만** 참이어야 한다 — 하나씩은 전부 거짓말을 한다.
    """

    class _Gate:
        def __init__(self, rows): self._rows = rows
        def records(self): return list(enumerate(self._rows))
        def fresh(self, record): return bool(record.get("_fresh"))

    LIVE = [{"_fresh": True, "ues": [{"amf_ue_ngap_id": 7}]}]

    def worker(self, rows, scope_kind="UE"):
        worker = Campaign5LiveWorker.__new__(Campaign5LiveWorker)
        worker._gate = self._Gate(rows)
        worker._ledger = ASupersededUeIdentityIsRetiredAtOnce._Ledger(
            {"entries": {"pol": {"identity": {"scopeKind": scope_kind}}}})
        return worker

    def test_a_cell_scoped_setting_is_never_retired(self):
        # 2026-09-23 audit: attenuation stays on the radio whatever the UEs do;
        # retiring it would drop the only owner of a value still in force.
        self.assertFalse(self.worker(self.LIVE, scope_kind="CELL")._rollback_target_is_gone(
            self.wrapped(), 99999.0, "pol"))

    def wrapped(self):
        cause = IdentityRefusal("UE scope has no unique fresh KPM/header identity")
        try:
            raise A1Conflict(f"DELETE rollback gate failed: {cause}") from cause
        except A1Conflict as exc:
            return exc

    def test_a_gone_ue_on_a_reporting_cell_is_retired(self):
        self.assertTrue(self.worker(self.LIVE)._rollback_target_is_gone(
            self.wrapped(), 1801.0, "pol"))

    def test_a_momentary_gap_is_never_retired(self):
        # keeper 부활 공백 실측 84건: 중앙 10.0 s · p99 21.0 s
        self.assertFalse(self.worker(self.LIVE)._rollback_target_is_gone(
            self.wrapped(), 21.0, "pol"))

    def test_a_cell_reporting_no_ue_list_is_not_evidence_of_departure(self):
        rows = [{"_fresh": True, "ues": []}]
        self.assertFalse(self.worker(rows)._rollback_target_is_gone(
            self.wrapped(), 99999.0, "pol"))

    def test_a_stale_read_is_not_evidence_of_departure(self):
        rows = [{"_fresh": False, "ues": [{"amf_ue_ngap_id": 7}]}]
        self.assertFalse(self.worker(rows)._rollback_target_is_gone(
            self.wrapped(), 99999.0, "pol"))

    def test_a_transport_fault_never_retires_a_policy_holding_the_radio(self):
        # 프로듀서가 30분 안 보이는 동안 은퇴시키면, 라디오에 남은 캡을 잊는다.
        self.assertFalse(self.worker(self.LIVE)._rollback_target_is_gone(
            A1Conflict("producer unreachable"), 99999.0, "pol"))


class ASupersededUeIdentityIsRetiredAtOnce(unittest.TestCase):
    """v46r8 board 462 (2026-09-23): the steered UE died, re-registered under a new
    AMF UE NGAP ID, and the policy bound to the old id could not be withdrawn --
    the DELETE was refused and the expiry path waited 30 min.  Once the role's
    header names another id and the old id is absent from fresh KPM, there is
    nothing to restore."""

    class _Ledger:
        def __init__(self, state): self.state = state
        def edit(self, **_):
            import contextlib
            return contextlib.nullcontext(self.state)

    def worker(self, *, header_amf, rows, scope_kind="UE"):
        worker = Campaign5LiveWorker.__new__(Campaign5LiveWorker)
        worker._gate = UnrestorableRollbackRetirement._Gate(rows)
        worker._ledger = self._Ledger({"entries": {"pol": {"identity": {
            "scopeKind": scope_kind, "ueTag": "ue2", "amfUeNgapId": 640}}}})
        worker._headers_on_disk = lambda: (
            [] if header_amf is None
            else [("ue2", {"RC_HEADER_AMF_UE_NGAP_ID": header_amf})])
        return worker

    FRESH = [{"_fresh": True, "ues": [{"amf_ue_ngap_id": 645}]}]

    def test_a_new_id_for_the_role_with_the_old_one_gone_is_superseded(self):
        self.assertTrue(self.worker(header_amf=647, rows=self.FRESH)
                        ._identity_superseded("pol"))

    def test_the_same_id_is_a_momentary_gap_not_a_departure(self):
        self.assertFalse(self.worker(header_amf=640, rows=self.FRESH)
                         ._identity_superseded("pol"))

    def test_the_old_id_still_in_fresh_kpm_is_not_superseded(self):
        rows = [{"_fresh": True, "ues": [{"amf_ue_ngap_id": 640}]}]
        self.assertFalse(self.worker(header_amf=647, rows=rows)._identity_superseded("pol"))

    def test_no_readable_ue_list_is_not_evidence(self):
        for rows in ([{"_fresh": False, "ues": [{"amf_ue_ngap_id": 645}]}],
                     [{"_fresh": True, "ues": []}]):
            with self.subTest(rows=rows):
                self.assertFalse(self.worker(header_amf=647, rows=rows)
                                 ._identity_superseded("pol"))

    def test_no_header_for_the_role_or_a_cell_scope_is_not_superseded(self):
        self.assertFalse(self.worker(header_amf=None, rows=self.FRESH)._identity_superseded("pol"))
        self.assertFalse(self.worker(header_amf=647, rows=self.FRESH, scope_kind="CELL")
                         ._identity_superseded("pol"))

    def test_the_same_amf_id_on_a_new_rrc_context_after_a_core_reset_is_superseded(self):
        # 2026-09-27 11:55: AMF numbering restarted; ue2 came back as AMF id 2, RRC id 1 (was 2).
        worker = self.worker(header_amf=640, rows=[{"_fresh": True, "ues": [
            {"amf_ue_ngap_id": 640, "ran_ue_id": 1}]}])
        worker._ledger.state["entries"]["pol"]["identity"]["ranUeId"] = 2
        worker._headers_on_disk = lambda: [("ue2", {"RC_HEADER_AMF_UE_NGAP_ID": 640,
                                                    "RC_HEADER_RRC_UE_ID": 1})]
        self.assertTrue(worker._identity_superseded("pol"))
        worker._gate = UnrestorableRollbackRetirement._Gate([{"_fresh": True, "ues": [
            {"amf_ue_ngap_id": 640, "ran_ue_id": 2}]}])
        self.assertFalse(worker._identity_superseded("pol"))
        worker._headers_on_disk = lambda: [("ue2", {"RC_HEADER_AMF_UE_NGAP_ID": 640,
                                                    "RC_HEADER_RRC_UE_ID": 2})]
        self.assertFalse(worker._identity_superseded("pol"))

    def _deleting(self, superseded):
        worker = self.worker(header_amf=647 if superseded else 640, rows=self.FRESH)
        worker._producer = SimpleNamespace(policy_record=lambda pid: {
            "policyTypeId": "T", "policy": {}})
        def refuse(*a, **k):
            raise IdentityRefusal("UE scope has no unique fresh KPM/header identity")
        worker.withdraw = refuse
        worker.retired = []
        worker._retire_unrestorable = lambda pid, n, s: worker.retired.append(pid)
        return worker

    def test_a_delete_for_a_superseded_ue_retires_instead_of_refusing(self):
        worker = self._deleting(superseded=True)
        worker._delete_from_a1("pol")
        self.assertEqual(worker.retired, ["pol"])

    def test_a_delete_for_a_ue_still_there_is_still_refused(self):
        worker = self._deleting(superseded=False)
        with self.assertRaises(A1Conflict):
            worker._delete_from_a1("pol")
        self.assertEqual(worker.retired, [])


class ACellScopeOutlivesItsNodeEpochNoLonger(unittest.TestCase):
    """2026-09-24: the cell scope key carries no epoch, ``withdraw`` refused any
    epoch change, and both retirement paths were UE-only -- one gNB restart left
    a cell policy owning ``action=X/target=cell`` for good, so every later write
    of that cell action was a ``ScopeConflict``.  The fresh counter under the new
    epoch now decides: back at the conf value -> retire, still ours -> restore."""

    POWER = "AIC_CellDlTxPower_1.0.0"
    COUNTER = "RAN.Cell.TxAttenuationDb"
    DESIRED = {"txAttenuationDb": 6}
    BASELINE = {"txAttenuationDb": 10}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def _applied_then_epoch_moves(self, value_after, actions_after=()):
        harness = Harness(self.root, [("cell", self.COUNTER, self.DESIRED), *actions_after])
        harness.append_cell(self.COUNTER, self.BASELINE)
        worker = harness.worker()
        self.assertTrue(worker.apply(
            self.POWER, "p", scoped_policy(self.POWER, self.DESIRED)).effect_verified)
        harness.now += timedelta(seconds=30)      # the old-epoch records go stale
        harness.epoch += 2                         # gNB / E2 link came back
        harness.append_cell(self.COUNTER, value_after)
        return harness, worker

    def test_a_restarted_gnb_at_its_conf_value_is_retired_and_frees_the_scope(self):
        harness, worker = self._applied_then_epoch_moves(
            self.BASELINE, [("cell", self.COUNTER, self.DESIRED)])
        with self.assertRaises(IdentityRefusal):
            worker.withdraw(self.POWER, "p")
        self.assertTrue(worker._identity_superseded("p"))
        worker._producer = SimpleNamespace(
            policy_record=lambda pid: {"policyTypeId": self.POWER, "policy": {}},
            delete_after_rollback=lambda *a: None)
        worker._delete_capability = None
        worker._retire_unrestorable("p", 1, 0.0)
        self.assertEqual(1, len(harness.calls), "retirement sends no control")
        # The axis is usable again under the new epoch.
        self.assertTrue(worker.apply(
            self.POWER, "p2", scoped_policy(self.POWER, self.DESIRED)).effect_verified)

    def test_an_e2_reconnect_with_our_value_in_force_is_restored_under_the_new_epoch(self):
        harness, worker = self._applied_then_epoch_moves(
            self.DESIRED, [("cell", self.COUNTER, self.BASELINE)])
        self.assertFalse(worker._identity_superseded("p"))
        withdrawn = worker.withdraw(self.POWER, "p")
        self.assertTrue(withdrawn.rollback_verified)
        self.assertEqual("10", harness.calls[1][1]["RC_TX_ATTEN_DB"])
        self.assertEqual(str(harness.epoch), harness.calls[1][1]["RC_SOURCE_CONNECTION_EPOCH"])
        state = json.loads(harness.ledger.read_text(encoding="utf-8"))
        self.assertEqual(harness.epoch, state["entries"]["p"]["identity"]["epoch"])
