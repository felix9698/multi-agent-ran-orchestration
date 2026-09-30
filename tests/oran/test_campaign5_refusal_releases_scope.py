"""거절된 쓰기가 남긴 두 흔적 — 지어낸 기준선과 영구 점유된 scope (2026-09-21).

`AFailedCreateMustNotStrandTheScope` 는 **프로듀서** 쪽 절반만 증명한다: 적용
핸들러가 raise 하면 등록부가 되감긴다.  그런데 실제 핸들러인
`Campaign5LiveWorker._apply_from_a1` 이 `LiveWorkerError` 를 **삼키고 있었으므로**
그 되감기는 한 번도 돌지 않았다.  여기서는 진짜 워커를 물려 둘을 함께 본다.

같은 자리에 기준선 위조도 있었다: cap(action 102)의 기준선을 못 읽으면
`{"maxDlPrbs": 0}` 을 지어냈는데, **0 은 "할당 0" 이 아니라 "무제한"** 이다.
실제 cap 이 12 이던 UE 를 복구할 때 캡을 통째로 풀어 실험 조건을 조용히 바꾼다.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oran.campaign5.producer import A1Conflict, Campaign5PolicyProducer

from oran.campaign5.live_worker import LiveWorkerError

from tests.oran.test_campaign5_live_worker import CAP, TEST_ENV, Harness, policy


class RefusalBeforeTheLedgerReleasesTheScope(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _harness_without_a_cap_baseline(self) -> Harness:
        harness = Harness(self.root, [])
        harness.jsonl.write_text("", encoding="utf-8")   # cap 기준선을 지운다
        harness.append_record(ue_measurements=[])        # UE 신원만 있고 카운터는 없다
        return harness

    def test_a_missing_cap_baseline_is_refused_not_invented(self):
        harness = self._harness_without_a_cap_baseline()
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)

        with self.assertRaisesRegex(A1Conflict, "nothing was applied"):
            producer.put_policy(CAP, "pol-A", policy(6))

        self.assertEqual([], harness.calls, "기준선도 없이 라디오에 썼다")

    def test_the_refused_create_leaves_the_scope_free(self):
        harness = self._harness_without_a_cap_baseline()
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)

        with self.assertRaises(A1Conflict):
            producer.put_policy(CAP, "pol-A", policy(6))

        self.assertNotIn("pol-A", producer._records)
        self.assertEqual({}, producer._scope_owner,
                         "거절된 정책이 축을 영구히 점유한다")

        # 기준선이 돌아오면 같은 scope 에 바로 다시 쓸 수 있어야 한다.
        # 201 하나로 끝내면 "요청이 접수됐다" 만 보는 것이다 -- 실제로 **라디오를
        # 때렸는지**와 그 값까지 본다.
        harness.actions.append(6)
        harness.append(12)
        result = producer.put_policy(CAP, "pol-B", policy(6))
        self.assertEqual(201, result.http_status)
        self.assertEqual(1, len(harness.calls), "적용이 라디오에 닿지 않았다")
        self.assertEqual("6", harness.calls[0][1]["RC_CAP_MAX_DL_PRBS"])
        self.assertEqual({(CAP, "cellId=12345678/ueId=130"): "pol-B"},
                         producer._scope_owner)

    def test_a_durable_ledger_entry_is_never_unwound(self):
        """원장에 남은 정책은 워커가 롤백을 빚고 있다 — 되감으면 그 빚이 사라진다."""
        harness = Harness(self.root, [12])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        producer.put_policy(CAP, "pol-A", policy(12))
        self.assertIn("pol-A", worker._ledger.snapshot()["entries"])

        with patch.object(type(worker), "apply",
                          side_effect=LiveWorkerError("rollback gate failed")):
            worker._apply_from_a1(CAP, "pol-A", policy(12))   # raise 하면 안 된다

        self.assertIn("pol-A", producer._records)
        self.assertEqual({(CAP, "cellId=12345678/ueId=130"): "pol-A"},
                         producer._scope_owner)


if __name__ == "__main__":
    unittest.main()


class ADispatcherMustNotKeepOwnershipOfARefusedCreate(unittest.TestCase):
    """거절된 생성의 소유권 주장이 dispatcher 에 남아 있었다 (2026-09-21, codex 3회차).

    프로듀서 쪽은 되감기는데 `Campaign5WorkerDispatcher._owners` 는 callback 전에
    심어 두고 예외에서 지우지 않았다.  그 id 는 이후 **다른 셀에서 영영 쓸 수 없고**
    (`_validate_route` 가 `policy id cannot move to another cell worker/ledger` 로
    막는다), 맵은 판마다 자란다.
    """

    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_refused_create_leaves_no_owner_behind(self):
        from oran.campaign5.live_worker import Campaign5WorkerDispatcher

        root = self.root / "cell"
        root.mkdir()
        harness = Harness(root, [])
        harness.jsonl.write_text("", encoding="utf-8")
        harness.append_record(ue_measurements=[])          # 기준선 없음 -> 거절
        worker = harness.worker()
        producer = Campaign5PolicyProducer()
        dispatcher = Campaign5WorkerDispatcher([worker])
        dispatcher.bind(producer)

        with self.assertRaises(A1Conflict):
            producer.put_policy(CAP, "pol-A", policy(6))

        self.assertEqual({}, dispatcher._owners, "거절된 id 의 소유권이 남았다")
        self.assertEqual({}, producer._scope_owner)


class AFinishedPolicyIdMustNotLookLikeAnObligation(unittest.TestCase):
    """끝난 이력을 "롤백 빚" 으로 읽으면 영구 점유가 그대로 되살아난다.

    2026-09-21 codex 3회차가 재현한 경로다.  정상 롤백·DELETE 는 `entries[id]` 를
    **남기고** `owners` 만 지운다.  그래서 "원장에 항목이 있나" 로 판정하면, 같은
    id 를 다시 쓰는 순간 끝난 이력이 빚으로 읽혀 거절이 다시 삼켜진다:

        cap 12 관측 → cap 6 적용 → DELETE 로 12 복구 → entries 는 남고 owners 는 빔
        → 카운터 소실 → 같은 id 로 cap 18 생성 → 기준선 없음 → **삼켜져 201**
        → 프로듀서가 scope 점유, 원장 owners 는 비어 `expire_due()` 가 영영 못 본다

    판별자는 `expire_due()` 와 같은 `owners[scopeKey] == policyId` 여야 한다.
    """

    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reusing_a_rolled_back_id_still_releases_the_scope(self):
        harness = Harness(self.root, [6, 0])      # 적용 6 → 롤백 0
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        producer.put_policy(CAP, "pol-A", policy(6))
        self.assertEqual(204, producer.handle(
            "DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/pol-A").status)

        state = worker._ledger.snapshot()
        self.assertIn("pol-A", state["entries"], "이 시험의 전제: 이력은 남는다")
        self.assertEqual({}, state["owners"], "이 시험의 전제: 빚은 사라졌다")

        harness.jsonl.write_text("", encoding="utf-8")   # cap 카운터 소실
        harness.append_record(ue_measurements=[])
        before = len(harness.calls)
        with self.assertRaises(A1Conflict):
            producer.put_policy(CAP, "pol-A", policy(18))

        self.assertEqual(before, len(harness.calls), "기준선도 없이 라디오에 썼다")
        self.assertEqual({}, producer._scope_owner, "끝난 이력이 축을 다시 잠갔다")
        self.assertEqual([], producer.list_policies(CAP))

    def test_a_pre_write_refusal_is_a_4xx_not_a_server_failure(self):
        """500 이면 adapter 가 **ACK 유실**로 읽고 쓰지도 않은 쓰기의 복구를 연다."""
        harness = Harness(self.root, [])
        harness.jsonl.write_text("", encoding="utf-8")
        harness.append_record(ue_measurements=[])
        producer = Campaign5PolicyProducer()
        harness.worker().bind(producer)

        response = producer.handle(
            "PUT", f"/A1-P/v2/policytypes/{CAP}/policies/pol-A", policy(6))

        self.assertEqual(409, response.status, response.body)
        self.assertIn("nothing was applied", str(response.body))
        # 정책이 된 적 없는 요청은 제어 이력도 남기지 않는다.
        self.assertEqual([], [c for c in producer._controls
                              if dict(c).get("policyId") == "pol-A"], producer._controls)


class ANackIsNotEvidenceThatNothingWasWritten(unittest.TestCase):
    """ACK 부재를 쓰기 부재로 번역하면 안 된다 (2026-09-21, codex 4회차 재현).

    제어가 ACK 을 못 받아도 라디오는 이미 값을 받았을 수 있다.  둘을 가르는 것은
    워커의 원장이고(`owners[scopeKey] == policyId`), 그때 계약이 정한 모양은
    **`APPLY_FAILED` + `rollback.state = REQUESTED` + 비종결**이다
    (A1 status 스키마 `allOf` 4·21).

    이 값은 감사용 표시가 아니다: adapter 의 `_refused_without_writing()` 이 그대로
    믿고 정책 되읽기를 건너뛴다.  codex 재현에서는 라디오가 `maxDlPrbs = 6` 을 들고
    있는데 상태는 "쓰지 않았다" 였다.
    """

    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_failure_after_the_write_asks_for_a_rollback_and_names_it(self):
        harness = Harness(self.root, [12])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        producer.put_policy(CAP, "pol-A", policy(12))

        with patch.object(type(worker), "apply",
                          side_effect=LiveWorkerError("readback gate failed")):
            worker._apply_from_a1(CAP, "pol-A", policy(12))

        aic = producer.get_status(CAP, "pol-A")["aicStatus"]
        self.assertEqual("APPLY_FAILED", aic["episodeState"])
        self.assertTrue(aic["control"]["writeMayHaveOccurred"], "쓴 것을 안 썼다고 적었다")
        self.assertTrue(aic["error"]["writeMayHaveOccurred"])
        self.assertFalse(aic["episodeTerminal"], "롤백을 빚진 판이 종결로 적혔다")
        self.assertEqual("REQUESTED", aic["rollback"]["state"])
        # 되돌릴 대상을 **이름까지** 말해야 한다 -- 하니스의 기준선은 cap 0 이다.
        self.assertEqual(0, aic["rollback"]["restoreDlPrbCap"]["maxDlPrbs"])
        self.assertEqual("12345678", aic["rollback"]["restoreDlPrbCap"]["cellId"])

    def test_a_ledger_write_failure_after_the_radio_does_not_erase_the_policy(self):
        """원장 저장 실패는 `OSError` 다 -- 예외의 종류로 책임을 지우면 안 된다."""
        harness = Harness(self.root, [12])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)

        real, calls = worker._ledger.flush, []

        def flaky(state):
            calls.append(1)
            real(state)
            if len(calls) == 2:            # 라디오를 때린 **뒤**의 저장
                raise OSError("disk went away")

        # 정상 경로로 들어가야 프로듀서가 기록을 먼저 만든다 -- 그래야 "되감기는가" 를
        # 물을 수 있다.  핸들러를 직접 부르면 되감을 기록 자체가 없다.
        with patch.object(worker._ledger, "flush", flaky):
            producer.put_policy(CAP, "pol-A", policy(12))

        self.assertIn("pol-A", producer._records, "라디오는 바뀌었는데 정책을 지웠다")
        self.assertEqual(1, len(harness.calls), "제어가 실제로 나갔다는 전제")
        aic = producer.get_status(CAP, "pol-A")["aicStatus"]
        self.assertTrue(aic["control"]["writeMayHaveOccurred"])


class AFinishedPolicyIdIsNotReusable(unittest.TestCase):
    """끝난 id 위에 새 적용을 얹으면 두 가지가 조용히 깨진다 (codex 4회차 재현).

    - **같은 digest**: `_reconcile_existing()` 이 옛 결과(`ROLLED_BACK_VERIFIED`)를
      돌려주며 소유권을 다시 심지 않는다 -> 201 이 나가고 프로듀서에는 정책이 있는데
      원장 `owners` 는 비어 `expire_due()` 가 영영 보지 못한다.
    - **다른 digest**: 새 기준선을 읽고도 기존 entry 는 `notAfter` 만 갱신하므로,
      바깥 상태가 12 -> 18 로 바뀐 뒤에도 **옛 12 로** 되돌린다.

    한 생애의 attempts·baseline 은 그 생애의 것이다.  재사용은 거절한다.
    """

    def setUp(self):
        environment = patch.dict(os.environ, TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def finished(self):
        """적용 -> DELETE 까지 마친 판 하나.  원장에 이력만 남고 owners 는 빈다."""
        harness = Harness(self.root, [6, 0])
        producer = Campaign5PolicyProducer()
        worker = harness.worker()
        worker.bind(producer)
        producer.put_policy(CAP, "pol-A", policy(6))
        self.assertEqual(204, producer.handle(
            "DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/pol-A").status)
        state = worker._ledger.snapshot()
        self.assertIn("pol-A", state["entries"])
        self.assertEqual({}, state["owners"])
        return harness, producer

    def test_the_same_body_is_not_quietly_reconciled(self):
        harness, producer = self.finished()
        before = len(harness.calls)
        with self.assertRaisesRegex(A1Conflict, "completed its lifecycle"):
            producer.put_policy(CAP, "pol-A", policy(6))
        self.assertEqual(before, len(harness.calls))
        self.assertEqual([], producer.list_policies(CAP))

    def test_a_new_body_cannot_inherit_the_old_baseline(self):
        harness, producer = self.finished()
        harness.append(18)                      # 바깥 상태가 바뀌었다
        with self.assertRaisesRegex(A1Conflict, "completed its lifecycle"):
            producer.put_policy(CAP, "pol-A", policy(12))
        self.assertEqual({}, producer._scope_owner)
