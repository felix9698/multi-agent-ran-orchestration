"""Two fixed-cell workers behind one in-process producer; no live transports."""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from oran.campaign5 import live_worker
from oran.campaign5.producer import A1Conflict, Campaign5PolicyProducer
from tests.oran.test_campaign5_live_worker import CAP, CELL, NB, NOW, TEST_ENV, Harness, policy

TARGET = "87654321"
TARGET_NB = 2816


class MultiCellLifecycle(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", TEST_ENV, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("source", "target"):
            (self.root / name).mkdir()
        self.source = Harness(self.root / "source", [])
        self.target = Harness(self.root / "target", [], cell_id=TARGET,
                              nb_id=TARGET_NB, epoch=274)
        self.network = patch("socket.socket", side_effect=AssertionError("no network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def compose(self):
        self.workers = [self.source.worker(), self.target.worker()]
        self.producer = Campaign5PolicyProducer(clock=lambda: self.source.now)
        self.router = live_worker.Campaign5WorkerDispatcher(self.workers)
        self.router.bind(self.producer)
        return self.producer

    @staticmethod
    def target_policy(value=12):
        body = policy(value)
        body["config"]["cellId"] = TARGET
        body["trace"]["traceId"] = "target-trace"
        return body

    def apply_both(self):
        self.source.actions = [12]
        self.target.actions = [12]
        producer = self.compose()
        producer.put_policy(CAP, "source", policy())
        producer.put_policy(CAP, "target", self.target_policy())
        return producer

    def test_target_create_and_status_do_not_touch_source(self):
        producer = self.compose()
        self.target.actions = [12]
        source_before = self.source.ledger.read_bytes()
        producer.put_policy(CAP, "target", self.target_policy())
        self.assertEqual([], self.source.calls)
        self.assertEqual(source_before, self.source.ledger.read_bytes())
        self.assertEqual("APPLIED_VERIFIED",
                         producer.get_status(CAP, "target")["aicStatus"]["episodeState"])
        env = self.target.calls[0][1]
        self.assertEqual(str(TARGET_NB), env["RC_SOURCE_NB_ID"])
        self.assertEqual("274", env["RC_SOURCE_CONNECTION_EPOCH"])
        producer.put_policy(CAP, "target", self.target_policy())
        self.assertEqual(1, len(self.target.calls))

    def test_unbound_and_malformed_cell_never_claim_policy_or_scope(self):
        producer = self.compose()
        for value in ("unknown", "", "  ", None, [], 123):
            with self.subTest(cell=value):
                body = self.target_policy()
                body["config"]["cellId"] = value
                result = producer.handle("PUT", f"/A1-P/v2/policytypes/{CAP}/policies/p", body)
                self.assertEqual(400, result.status)
                self.assertEqual([], producer.list_policies(CAP))
                self.assertEqual({}, producer._scope_owner)
                self.assertEqual({}, producer._statuses)
        self.assertEqual([], self.source.calls + self.target.calls)

    def test_update_keeps_id_scope_and_fence_guards(self):
        producer = self.apply_both()
        before = producer.policy_record("source")
        moving = self.target_policy(6)
        moving["trace"].update(revision=2, fencingToken=2)
        with self.assertRaises(A1Conflict):
            producer.put_policy(CAP, "source", moving)
        self.assertEqual(before, producer.policy_record("source"))
        update = policy(6)
        with self.assertRaises(A1Conflict):
            producer.put_policy(CAP, "source", update)
        update["trace"].update(revision=2, fencingToken=2)
        self.source.actions = [6]
        producer.put_policy(CAP, "source", update)
        self.assertEqual(2, len(self.source.calls))
        self.assertEqual(1, len(self.target.calls))

    def test_delete_restores_only_original_owner_baseline(self):
        producer = self.apply_both()
        source_before = self.source.ledger.read_bytes()
        self.target.actions = [0]
        result = producer.handle("DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/target")
        self.assertEqual(204, result.status)
        self.assertEqual(["source"], producer.list_policies(CAP))
        self.assertEqual(source_before, self.source.ledger.read_bytes())
        self.assertEqual("0", self.target.calls[-1][1]["RC_CAP_MAX_DL_PRBS"])

    def test_moved_ue_delete_is_retained_not_rerouted(self):
        producer = self.apply_both()
        self.source.now += timedelta(seconds=10)
        self.target.now = self.source.now
        self.target.append(12)
        result = producer.handle("DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/source")
        self.assertEqual(409, result.status)
        self.assertEqual(["source", "target"], producer.list_policies(CAP))
        self.assertEqual(1, len(self.source.calls))
        self.assertEqual(1, len(self.target.calls))
        state = json.loads(self.source.ledger.read_text())
        self.assertIn("source", state["owners"].values())

    def test_expiry_uses_original_workers_and_retains_failed_restore(self):
        producer = self.compose()
        for harness, name, body in ((self.source, "source", policy()),
                                    (self.target, "target", self.target_policy())):
            harness.actions = [12]
            body["validity"]["notAfter"] = "2026-09-04T12:00:01Z"
            producer.put_policy(CAP, name, body)
            harness.now = NOW + timedelta(seconds=10)
        self.target.actions = [0]
        self.target.append(12)
        with self.assertLogs("oran.campaign5.live_worker", level="ERROR"):
            self.assertEqual(("target",), self.router.expire_due())
        self.assertEqual(["source"], producer.list_policies(CAP))
        self.assertEqual(1, len(self.source.calls))
        # The failed restore backs off (2 s after its first failure) before it is retried.
        self.source.now = NOW + timedelta(seconds=13)
        self.source.append(12)
        self.source.actions = [0]
        self.assertEqual(("source",), self.router.expire_due())

    def test_restart_rehydrates_both_without_target_replay(self):
        self.apply_both()
        self.compose()
        self.assertEqual(["source", "target"], self.producer.list_policies(CAP))
        for name in ("source", "target"):
            self.assertEqual("APPLIED_VERIFIED",
                             self.producer.get_status(CAP, name)["aicStatus"]["episodeState"])
        self.assertEqual(1, len(self.source.calls))
        self.assertEqual(1, len(self.target.calls))

    def test_ambiguous_target_restart_restores_without_reapplying(self):
        producer = self.compose()
        self.target.actions = ["crash", 0]
        with self.assertRaises(KeyboardInterrupt):
            producer.put_policy(CAP, "target", self.target_policy())
        self.compose()
        self.assertEqual([], self.source.calls)
        self.assertEqual(["12", "0"],
                         [env["RC_CAP_MAX_DL_PRBS"] for _, env in self.target.calls])

    def test_all_ledgers_checked_before_any_recovery_or_mutation(self):
        self.apply_both()
        # An ambiguous first ledger would emit rollback during recovery. The
        # invalid second ledger must stop startup before reaching that control.
        first = json.loads(self.source.ledger.read_text())
        entry = first["entries"]["source"]
        entry["attempts"][entry["currentDigest"]]["phase"] = "WRITE_STARTED"
        self.source.ledger.write_text(json.dumps(first))
        second = json.loads(self.target.ledger.read_text())
        second["deploymentBinding"]["nbId"] = NB
        self.target.ledger.write_text(json.dumps(second))
        before = [h.ledger.read_bytes() for h in (self.source, self.target)]
        with self.assertRaisesRegex(live_worker.LiveWorkerError, "binding"):
            self.compose()
        self.assertEqual(before, [h.ledger.read_bytes() for h in (self.source, self.target)])
        self.assertEqual([], self.producer.list_policies(CAP))
        self.assertEqual(1, len(self.source.calls))
        self.assertEqual(1, len(self.target.calls))

    def test_duplicate_ids_wrong_cells_and_ledger_reuse_refuse_startup(self):
        self.apply_both()
        state = json.loads(self.target.ledger.read_text())
        state["entries"]["source"] = state["entries"].pop("target")
        state["owners"] = {key: "source" for key in state["owners"]}
        self.target.ledger.write_text(json.dumps(state))
        with self.assertRaisesRegex(live_worker.LiveWorkerError, "policy id"):
            self.compose()
        state["entries"]["target"] = state["entries"].pop("source")
        state["owners"] = {key: "target" for key in state["owners"]}
        entry = state["entries"]["target"]
        entry["attempts"][entry["currentDigest"]]["policy"]["config"]["cellId"] = CELL
        self.target.ledger.write_text(json.dumps(state))
        with self.assertRaisesRegex(live_worker.LiveWorkerError, "cell|digest"):
            self.compose()
        first, second = self.source.worker(), self.target.worker()
        second._ledger.path = first._ledger.path
        with self.assertRaisesRegex(live_worker.LiveWorkerError, "ledger path"):
            live_worker.Campaign5WorkerDispatcher([first, second]).bind(Campaign5PolicyProducer())
        self.assertEqual(1, len(self.source.calls))
        self.assertEqual(1, len(self.target.calls))

    def test_legacy_single_cell_is_not_silently_migrated_to_multi_cell(self):
        self.source.actions = [12]
        self.source.worker().apply(CAP, "source", policy())
        state = json.loads(self.source.ledger.read_text())
        state.pop("deploymentBinding", None)
        self.source.ledger.write_text(json.dumps(state))
        single = Campaign5PolicyProducer()
        self.source.worker().bind(single)
        self.assertEqual(["source"], single.list_policies(CAP))
        self.assertNotIn("deploymentBinding", json.loads(self.source.ledger.read_text()))
        with self.assertRaisesRegex(live_worker.LiveWorkerError, "legacy.*binding.*migration"):
            self.compose()
        self.assertEqual(1, len(self.source.calls))

    def test_foreign_node_stale_and_changed_epoch_do_not_write(self):
        producer = self.compose()
        # 2026-09-21: 매 회차가 **앞 회차가 고쳐 쓴** 파일의 첫 줄을 다시 읽고 있었다.
        # 첫 `node` 회차가 바꾼 `nb_id` 가 그대로 남아 `stale`·`ue` 회차는 자기 변조가
        # 아니라 node 오류로 거절됐다 -- 세 경우 중 둘이 아무것도 증명하지 않았다.
        original = self.target.jsonl.read_text().splitlines()[0]
        for bad in ("node", "stale", "ue"):
            with self.subTest(bad=bad):
                row = json.loads(original)
                if bad == "node":
                    row["nb_id"] = NB
                elif bad == "stale":
                    row["recv_unix_us"] -= 10_000_000
                else:
                    row["ues"][0]["amf_ue_ngap_id"] += 1
                self.target.jsonl.write_text(json.dumps(row) + "\n")
                with self.assertRaises(A1Conflict):
                    producer.put_policy(CAP, bad, self.target_policy())
                # 2026-09-21: 예전엔 워커가 거절을 삼켜 정책이 `APPLY_FAILED` 상태로
                # 남았고, 그 정책이 scope 를 **영구히** 들고 있어 테스트조차 매번 새
                # 프로듀서를 세워야 했다.  이제 거절은 생성 자체를 되감는다.
                self.assertNotIn(bad, producer._records)
                self.assertEqual({}, producer._scope_owner)
                producer = self.compose()   # 다음 회차는 고쳐 쓴 KPM 을 새로 읽어야 한다
        self.assertEqual([], self.source.calls + self.target.calls)
        self.target.append(0)
        self.target.actions = [12]
        producer.put_policy(CAP, "target", self.target_policy())
        self.target.epoch += 1
        self.target.append(12)
        self.target.actions = []
        update = self.target_policy(6)
        update["trace"].update(revision=2, fencingToken=2)
        producer.put_policy(CAP, "target", update)
        self.assertEqual(1, len(self.target.calls))
        result = producer.handle("DELETE", f"/A1-P/v2/policytypes/{CAP}/policies/target")
        self.assertEqual(409, result.status)

    def test_forged_or_malformed_node_key_is_not_a_bound_node_observation(self):
        original = json.loads(self.target.jsonl.read_text().splitlines()[0])
        for node in (f"type=2;nb={NB}/0", "gnb2", "type=2;nb=2816/x",
                     "type=2;nb=2816/0;nb=3584/0", None):
            with self.subTest(node=node):
                self.target.jsonl.write_text(json.dumps({**original, "e2_node": node}) + "\n")
                producer = self.compose()
                self.target.actions = [12]
                with self.assertRaises(A1Conflict):
                    producer.put_policy(CAP, "target", self.target_policy())
                self.assertEqual([], self.target.calls)
                self.assertEqual({}, producer._scope_owner)
        self.assertEqual([], self.source.calls)

    def test_r1_and_a1_share_one_store_and_new_cell_needs_new_create_id(self):
        producer = self.compose()
        self.source.actions, self.target.actions = [12], [12, 0]
        root = "/r1/a1-policy-management/v1/policies"
        headers = {"Version": "1.0.0"}
        def create(body):
            return producer.handle("POST", root, {"nearRtRicId": "ric", "policyTypeId": CAP,
                                                  "policyObject": body}, headers=headers)
        first = create(policy())
        self.assertEqual(201, first.status)
        moved = self.target_policy()
        moved["trace"]["traceId"] = policy()["trace"]["traceId"]
        self.assertEqual(409, create(moved).status)
        target = create(self.target_policy())
        self.assertEqual(201, target.status)
        self.assertEqual(2, len(producer.list_policies(CAP)))
        location = target.headers["Location"]
        self.assertEqual(200, producer.handle("GET", location + "/status", headers=headers).status)
        self.assertEqual(204, producer.handle("DELETE", location, headers=headers).status)
        self.assertEqual(1, len(producer.list_policies(CAP)))

    def test_one_transaction_writes_both_cells_through_the_builder(self):
        """2026-09-20: one trial, one type, two cells -> two ids, each on its worker."""
        from oran.campaign5.builders import fixed_validity, make_policy_builder
        from oran.campaign5.families import CAMPAIGN5_FAMILIES
        producer = self.compose()
        self.source.actions, self.target.actions = [12], [12]
        fam = CAMPAIGN5_FAMILIES["cap"]
        root = "/r1/a1-policy-management/v1/policies"
        headers = {"Version": "1.0.0"}
        locations = []
        ue = str(policy()["config"]["ueId"])   # 각 셀의 하니스가 같은 UE 를 붙여 둔다
        for cell in (CELL, TARGET):
            # One builder per participant, as the composition makes them.
            build = make_policy_builder(fam, validity_provider=fixed_validity(
                "2026-09-04T00:00:00Z", "2026-09-05T00:00:00Z"))
            body = build({"operation": "APPLY", "transactionId": "tx-joint", "trialId": "t",
                          "fencingToken": 1, "commandSequence": 2, "commandIndex": 0,
                          "idempotencyKey": "k", "scope": {"cellId": cell, "ueId": ue},
                          "axis": fam.axis, "value": 12})
            answer = producer.handle("POST", root, {"nearRtRicId": "ric", "policyTypeId": CAP,
                                                    "policyObject": body}, headers=headers)
            self.assertEqual(201, answer.status, answer.body)
            locations.append(answer.headers["Location"])
        self.assertEqual(2, len(set(locations)))
        owners = [self.router._owners[location.rsplit("/", 1)[-1]]  # noqa: SLF001
                  for location in locations]
        self.assertEqual(self.workers, owners)             # each id on its own cell's worker

    def test_cells_serialize_separately_and_one_scope_never_writes_twice_at_once(self):
        """2026-09-23 audit: one producer-wide lock held across the worker's
        18 s control stalled every other cell's PUT and status GET past the R1
        client's 20 s timeout.  Each cell now has its own writer lock."""
        producer = self.compose()
        self.assertIs(self.workers[0]._lifecycle_lock, producer.cell_lock(CELL))
        self.assertIs(self.workers[1]._lifecycle_lock, producer.cell_lock(TARGET))
        self.assertIsNot(producer.cell_lock(CELL), producer.cell_lock(TARGET))
        self.assertIsNot(producer.cell_lock(CELL), producer.lifecycle_lock)
        self.source.actions, self.target.actions = [12], [12]
        entered, release = threading.Event(), threading.Event()
        original = self.workers[0]._runner
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release fake runner")
            return original(*args, **kwargs)
        self.workers[0]._runner = blocked
        results = {}
        def run(name, call):
            try:
                results[name] = call()
            except BaseException as exc:
                results[name] = exc
        rival = policy()
        rival["trace"]["traceId"] = "rival"
        first = threading.Thread(target=run, args=(
            "source", lambda: producer.put_policy(CAP, "source", policy())))
        other = threading.Thread(target=run, args=(
            "target", lambda: producer.put_policy(CAP, "target", self.target_policy())))
        same = threading.Thread(target=run, args=(
            "rival", lambda: producer.put_policy(CAP, "rival", rival)))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            # Another cell writes while this cell's control is in flight ...
            other.start()
            other.join(2)
            self.assertFalse(other.is_alive())
            self.assertEqual(201, results["target"].http_status)
            # ... and status reads do not wait for it either.
            answer = producer.handle(
                "GET", f"/A1-P/v2/policytypes/{CAP}/policies/target/status")
            self.assertEqual(200, answer.status)
            # The same scope does wait: no second write while one is in flight.
            same.start()
            same.join(0.05)
            self.assertTrue(same.is_alive())
            self.assertEqual([], self.source.calls)
        finally:
            release.set()
            first.join(5)
            if same.ident is not None:
                same.join(5)
        self.assertEqual(201, results["source"].http_status)
        self.assertIsInstance(results["rival"], A1Conflict)
        self.assertEqual(1, len(self.source.calls))
        owners = json.loads(self.source.ledger.read_text())["owners"]
        self.assertEqual(["source"], list(owners.values()))
        self.assertEqual(["source", "target"], producer.list_policies(CAP))

    def test_one_cells_expiry_fault_does_not_stop_the_other_cell(self):
        # 2026-09-23 audit: an exception in one worker's pass escaped the
        # dispatcher, so every later cell's expiry and rollback was skipped.
        self.compose()
        def broken():
            raise RuntimeError("ledger is unreadable")
        self.workers[0].expire_due = broken
        self.workers[1].expire_due = lambda: ("target",)
        with self.assertLogs("oran.campaign5.live_worker", level="ERROR"):
            self.assertEqual(("target",), self.router.expire_due())

if __name__ == "__main__":
    unittest.main()


class TheProducerRunAsAModuleHasOneSetOfExceptions(unittest.TestCase):
    """2026-09-15 attempt 74: a live-worker A1Conflict escaped as HTTP 500."""

    def test_a_relative_import_under_dash_m_yields_the_running_classes(self):
        import subprocess, sys, textwrap
        code = textwrap.dedent("""
            import runpy, sys
            sys.argv = ["producer", "--help"]
            try:
                runpy.run_module("oran.campaign5.producer", run_name="__main__", alter_sys=True)
            except SystemExit:
                pass
            from oran.campaign5.producer import A1Conflict
            running = sys.modules["oran.campaign5.producer"]
            print(running.__name__, A1Conflict is running.A1Conflict)
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=60, cwd=str(Path(__file__).resolve().parents[2]))
        self.assertEqual(out.stdout.strip().splitlines()[-1], "__main__ True", out.stderr[-800:])
