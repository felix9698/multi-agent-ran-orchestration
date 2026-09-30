"""keeper 가 epoch drift 와 A1-P 인벤토리 노후를 구분해 다룬다.

둘은 **다른 고장**이다.  KPM 게이트는 매 주기 live epoch 으로 다시 만들어지므로 스스로
낫지만(`conductor.GATE_RUN` 이 `_current_epochs()` 로 조립된다), A1-P 인벤토리·capability·
binding 은 `repin_a1p.sh` 만 고친다.  후자가 낡으면 프로듀서가 기동을 거부하고, 조종 정책이
`AIC_E2_NOT_READY` 로 서며 gNB 에는 아무 흔적도 안 남는다.
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

KEEPER = (Path(__file__).resolve().parents[1]
          / "experiment_results/ota-20260911/ops/keeper.py")


def _load():
    spec = importlib.util.spec_from_file_location("keeper_under_test", KEEPER)
    module = importlib.util.module_from_spec(spec)
    argv = sys.argv
    sys.argv = ["keeper"]
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass
    finally:
        sys.argv = argv
    return module


class TestGatePinnedEpochs(unittest.TestCase):
    """env 는 순서 있는 두 목록으로만 말한다 -- topology 가 든 gNB_ID 로 짝지어야 한다."""

    def setUp(self):
        self.keeper = _load()

    def _with_inspect(self, stdout):
        def run(args, timeout=60, stdin=None):
            return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        self.keeper.run = run

    def test_pairs_by_the_gnb_id_inside_the_topology_entry(self):
        self._with_inspect(
            "KPM_GATE_TOPOLOGY=208-95-2-0xe00-32-20895-12345678,"
            "208-95-2-0xb00-32-20895-87654321\n"
            "KPM_GATE_CONNECTION_EPOCHS=756,766\n")
        self.assertEqual(self.keeper._gate_pinned_epochs(), {3584: 756, 2816: 766})

    def test_a_length_mismatch_yields_nothing_rather_than_a_guess(self):
        self._with_inspect(
            "KPM_GATE_TOPOLOGY=208-95-2-0xe00-32-20895-12345678,"
            "208-95-2-0xb00-32-20895-87654321\n"
            "KPM_GATE_CONNECTION_EPOCHS=756\n")
        self.assertEqual(self.keeper._gate_pinned_epochs(), {})

    def test_an_unparseable_topology_yields_nothing(self):
        self._with_inspect("KPM_GATE_TOPOLOGY=nonsense\n"
                           "KPM_GATE_CONNECTION_EPOCHS=756\n")
        self.assertEqual(self.keeper._gate_pinned_epochs(), {})

    def test_drift_names_only_the_nodes_that_differ(self):
        self._with_inspect(
            "KPM_GATE_TOPOLOGY=208-95-2-0xe00-32-20895-12345678,"
            "208-95-2-0xb00-32-20895-87654321\n"
            "KPM_GATE_CONNECTION_EPOCHS=756,750\n")
        self.keeper._live_epochs = lambda: {3584: 756, 2816: 766}
        self.assertEqual(self.keeper._gate_epoch_drift(), {2816: [750, 766]})

    def test_no_drift_when_the_pins_match(self):
        self._with_inspect(
            "KPM_GATE_TOPOLOGY=208-95-2-0xe00-32-20895-12345678,"
            "208-95-2-0xb00-32-20895-87654321\n"
            "KPM_GATE_CONNECTION_EPOCHS=756,766\n")
        self.keeper._live_epochs = lambda: {3584: 756, 2816: 766}
        self.assertEqual(self.keeper._gate_epoch_drift(), {})


class TestInventoryEpochs(unittest.TestCase):
    """인벤토리는 같은 노드를 witness 와 **다른 철자**로 적는다."""

    def setUp(self):
        self.keeper = _load()

    def _inventory(self, tmp, connections):
        import json
        path = Path(tmp) / "e2-capability-inventory.json"
        path.write_text(json.dumps({"connections": connections}))
        self.keeper.A1P_INVENTORY = path

    def test_reads_the_hex_node_id_not_nbId(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._inventory(tmp, [
                {"active": True, "connectionEpoch": 756,
                 "globalE2NodeId": {"nodeId": {"hex": "0x00000e00", "bitLength": 32}}},
                {"active": True, "connectionEpoch": 762,
                 "globalE2NodeId": {"nodeId": {"hex": "0x00000b00", "bitLength": 32}}}])
            self.assertEqual(self.keeper._inventory_epochs(), {3584: 756, 2816: 762})

    def test_an_inactive_connection_is_not_a_pin(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._inventory(tmp, [
                {"active": False, "connectionEpoch": 700,
                 "globalE2NodeId": {"nodeId": {"hex": "0x00000e00"}}}])
            self.assertEqual(self.keeper._inventory_epochs(), {})

    def test_drift_is_named_per_node(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._inventory(tmp, [
                {"active": True, "connectionEpoch": 756,
                 "globalE2NodeId": {"nodeId": {"hex": "0x00000e00"}}},
                {"active": True, "connectionEpoch": 762,
                 "globalE2NodeId": {"nodeId": {"hex": "0x00000b00"}}}])
            self.keeper._live_epochs = lambda: {3584: 756, 2816: 766}
            self.assertEqual(self.keeper._inventory_drift(), {2816: [762, 766]})

    def test_an_unreadable_inventory_makes_no_claim(self):
        self.keeper.A1P_INVENTORY = Path("/nonexistent/inventory.json")
        self.assertEqual(self.keeper._inventory_epochs(), {})
        self.assertEqual(self.keeper._inventory_drift(), {})


class TestA1pInventoryGuard(unittest.TestCase):
    """프로듀서가 **그 서명으로** 죽었을 때만 재핀한다."""

    def setUp(self):
        import tempfile
        self.keeper = _load()
        self.calls = []
        self.keeper.log = lambda event, **kw: self.calls.append((event, kw))
        self.keeper.budget = lambda target: True
        self.keeper._a1p_repin_at = 0.0
        # **테스트는 실제 베드 디렉터리에 쓰면 안 된다.**  2026-09-23 에 이 테스트가
        # ops/overnight/ 에 0바이트 repin-a1p-*.log 다섯 개를 남겼고, 그것을 운영 중인
        # keeper 가 남긴 것으로 오독해 한참을 헤맸다.  `ensure_a1p_inventory` 는 HERE 로
        # 로그 자리를 정하므로 그것부터 임시로 돌린다.
        self._tmp = tempfile.TemporaryDirectory()
        self.keeper.HERE = Path(self._tmp.name)
        (Path(self._tmp.name) / "overnight").mkdir()
        self.addCleanup(self._tmp.cleanup)
        # 기본은 drift 없음 -- 각 테스트가 필요할 때만 켠다 (실제 베드 파일을 읽지 않는다)
        self.keeper._inventory_drift = lambda: {}

    def _bed(self, status, logs):
        def run(args, timeout=60, stdin=None):
            if args[:2] == ["docker", "inspect"]:
                return types.SimpleNamespace(stdout=status, stderr="", returncode=0)
            if args[:2] == ["docker", "logs"]:
                return types.SimpleNamespace(stdout=logs, stderr="", returncode=0)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)
        self.keeper.run = run

    def test_a_running_producer_with_a_matching_inventory_is_left_alone(self):
        self._bed("running\n", "")
        self.keeper.subprocess = None      # 부르면 터지도록
        self.keeper.ensure_a1p_inventory()
        self.assertEqual(self.calls, [])

    def test_a_running_producer_with_a_stale_inventory_is_still_repinned(self):
        """**프로세스 상태로 판정하면 안 된다.**  거부는 기동할 때만 하므로, 기동 뒤에 생긴
        drift 는 프로듀서를 살려 둔 채 조종 쓰기만 조용히 실패시킨다 (2026-09-23 02:36)."""
        self._bed("running\n", "")
        self.keeper._inventory_drift = lambda: {2816: [762, 766]}
        self.keeper._gate_epoch_drift = lambda: {}
        self.keeper._live_epochs = lambda: {3584: 756, 2816: 766}
        seen = {}

        def call(args, stdout=None, stderr=None, timeout=None, cwd=None):
            seen["args"] = args
            seen["cwd"] = cwd
            return 0
        self.keeper.subprocess = types.SimpleNamespace(call=call)
        self.keeper.ensure_a1p_inventory()
        self.assertEqual([e for e, _ in self.calls], ["A1P_INVENTORY_STALE", "A1P_REPINNED"])
        self.assertTrue(str(seen["args"][1]).endswith("repin_a1p.sh"))

    def test_a_producer_down_for_another_reason_is_reported_not_repinned(self):
        self._bed("dead\n", "some other crash\n")
        self.keeper.ensure_a1p_inventory()
        self.assertEqual([e for e, _ in self.calls], ["A1P_PRODUCER_DOWN"])

    def test_an_exited_producer_with_a_current_inventory_is_started_not_repinned(self):
        # 2026-09-25 00:52: it exits when its xApp worker stops; the next board waited 10 min.
        self._bed("exited\n", "xApp worker stopped: 'x'\n")
        self.keeper.subprocess = None      # a repin would call it
        self.keeper.ensure_a1p_inventory()
        self.assertEqual([e for e, _ in self.calls], ["A1P_PRODUCER_STARTED"])

    def test_the_stale_inventory_signature_triggers_a_repin(self):
        self._bed("exited\n", "refusing startup: invalid live xApp binding: "
                              "connection witness differs from the active E2 inventory\n")
        self.keeper._live_epochs = lambda: {3584: 756, 2816: 766}
        self.keeper._gate_epoch_drift = lambda: {2816: [750, 766]}
        seen = {}

        def call(args, stdout=None, stderr=None, timeout=None, cwd=None):
            seen["args"] = args
            seen["cwd"] = cwd
            return 0
        self.keeper.subprocess = types.SimpleNamespace(call=call)
        self.keeper.ensure_a1p_inventory()
        self.assertEqual([e for e, _ in self.calls], ["A1P_INVENTORY_STALE", "A1P_REPINNED"])
        self.assertTrue(str(seen["args"][1]).endswith("repin_a1p.sh"))
        # **레포 루트에서 돌려야 한다.**  repin_a1p.sh 는 deployment/... 를 상대경로로
        # 찾으므로 ops/ 에서 부르면 rc=1 로 죽는다 (2026-09-23 03:45 실제 발생,
        # `cp: 'deployment/assurance-live-binding.1.0.0.json' ... 없습니다`).
        self.assertEqual(str(seen["cwd"]), str(Path(seen["args"][1]).parents[2]))

    def test_the_cooldown_stops_a_repin_loop(self):
        self._bed("exited\n", "refusing startup: invalid live xApp binding\n")
        self.keeper._a1p_repin_at = self.keeper.time.time()
        self.keeper.subprocess = None
        self.keeper.ensure_a1p_inventory()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()


class AnExitedProducerIsStartedMidBoard(unittest.TestCase):
    """2026-09-25 08:31: it exited mid-board and stayed down 7 min -> the UE rebind hit ConnectionRefused."""

    def test_started_in_any_phase_when_the_inventory_is_current(self):
        k = _load()
        calls, logs = [], []
        k.log = lambda e, **kw: logs.append(e)
        k.budget = lambda t: True
        k._inventory_drift = lambda: {}
        k.episode_in_sitting = lambda: True
        k.run = lambda args, timeout=60, stdin=None: (calls.append(args) or types.SimpleNamespace(
            stdout='exited\n' if args[:2] == ['docker', 'inspect'] else '', stderr='', returncode=0))
        k.start_exited_a1p_producer()
        self.assertIn(['docker', 'start', 'oran-aic-a1p-producer'], calls)
        self.assertEqual(['A1P_PRODUCER_STARTED'], logs)

    def test_a_drifted_inventory_is_left_to_the_repin(self):
        k = _load()
        calls = []
        k.log = lambda e, **kw: None
        k.budget = lambda t: True
        k._inventory_drift = lambda: {2816: [1, 2]}
        k.run = lambda args, timeout=60, stdin=None: (calls.append(args) or types.SimpleNamespace(
            stdout='exited\n', stderr='', returncode=0))
        k.start_exited_a1p_producer()
        self.assertNotIn(['docker', 'start', 'oran-aic-a1p-producer'], calls)
