"""v4.7 cell power (owner 2026-09-25): the gnb2 operator's energy intent on cellTxAttenuationDb."""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

from assurance.coordination import concession
from assurance.coordination.intake import DEFAULT_OBSERVATION_RULES, IntakeChecklist
from assurance.coordination.tc import KPI_CELL_TX_ATTENUATION, SUPPORTED_KPIS, Intent
from tools.liveconsole.kpi_observer import KpmCellAttenuationObserver

ENERGY = {"intentId": "I4e", "owner": "operator-gnb2", "priority": 5, "weight": 1.0,
          "requirement": {"reqId": "I4e.r1", "kpi": "cellTxAttenuationDb", "op": ">=",
                          "value": 16.0, "bound": 10.0, "steps": 20, "unit": "dB",
                          "scope": "cell@87654321"}}


def _utc(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Adapter:
    """``parse_lines`` over (node, value, receive-seconds) tuples, the shape the KPM adapter
    yields (observed_at is the host receive time)."""

    def parse_lines(self, lines):
        return SimpleNamespace(samples=[
            SimpleNamespace(counter_id="RAN.Cell.TxAttenuationDb",
                            scope_snapshot={"e2_node": node}, observed_at=_utc(at),
                            value=SimpleNamespace(value=value)) for node, value, at in lines])


class CellPowerIntent(unittest.TestCase):
    def test_kpi_and_rule_exist(self):
        self.assertIn(KPI_CELL_TX_ATTENUATION, SUPPORTED_KPIS)
        # "consistent": a rollback inside the window is UNKNOWN, never the last value.
        self.assertEqual("consistent", DEFAULT_OBSERVATION_RULES[KPI_CELL_TX_ATTENUATION].statistic)

    def test_intake_asks_nothing_for_a_cell_scoped_energy_intent(self):
        intent = Intent.from_record(ENERGY)
        self.assertEqual("cellTxAttenuationDb@87654321", intent.requirement.observation_key)
        self.assertFalse([q for q in IntakeChecklist()._check_intent(intent)
                          if q.get("field") in ("ueId", "kpi", "op", "value")])

    def test_evaluator_bins_the_energy_coordinate(self):
        reqs = concession.requirements_from_intents([Intent.from_record(ENERGY)])
        owners = ["operator-gnb2"]
        key = "cellTxAttenuationDb@87654321"
        self.assertTrue(concession.evaluate(reqs, owners, {key: 16.0}).t0)
        mid = concession.evaluate(reqs, owners, {key: 13.0})
        self.assertEqual((10,), mid.owner_bins)          # halfway = 10 of 20 bins
        self.assertFalse(concession.evaluate(reqs, owners, {key: 9.0}).attained)


class AttenuationObserver(unittest.TestCase):
    def _observer(self, batches, now):
        topology = SimpleNamespace(nb_id_to_nci={2816: 87654321, 3584: 12345678})
        return KpmCellAttenuationObserver(
            read_new_lines=lambda: batches.pop(0) if batches else [], adapter=_Adapter(),
            topology=topology, cells=(87654321,), max_age_ms=5000.0, wall_s=lambda: now[0])

    def test_publishes_fresh_and_drops_stale(self):
        now = [1000.0]
        observer = self._observer([[("nb=2816/1445", 13.0, 1000.0), ("nb=3584/1444", 8.0, 1000.0)]],
                                  now)
        self.assertEqual({"cellTxAttenuationDb@87654321": 13.0}, observer.sample())
        now[0] = 1004.0
        self.assertEqual({"cellTxAttenuationDb@87654321": 13.0}, observer.sample())
        now[0] = 1006.0
        self.assertEqual({}, observer.sample())

    def test_a_delayed_record_is_aged_by_its_own_time(self):
        # Codex review 2026-09-25 #1: a record received 20 s ago and dequeued now is stale.
        now = [1000.0]
        observer = self._observer([[("nb=2816/1445", 13.0, 980.0)]], now)
        self.assertEqual({}, observer.sample())

    def test_an_older_record_never_replaces_a_newer_one(self):
        now = [1000.0]
        observer = self._observer([[("nb=2816/1445", 16.0, 999.0)],
                                   [("nb=2816/1445", 13.0, 990.0)]], now)
        observer.sample()
        self.assertEqual({"cellTxAttenuationDb@87654321": 16.0}, observer.sample())


class CorpusEnergy(unittest.TestCase):
    def _mod(self):
        path = (Path(__file__).resolve().parents[1]
                / "experiment_results/ota-20260911/ops/make_v47_corpus.py")
        spec = importlib.util.spec_from_file_location("make_v47_corpus_power", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_energy_intent_and_ladder(self):
        mod = self._mod()
        ref = {"energy": {"cell": "87654321", "originalDb": 16.0, "boundDb": 10.0}}
        intent = mod.energy_intent(ref, [{"priority": 1}, {"priority": 4}])
        self.assertEqual(5, intent["priority"])
        self.assertEqual("cell@87654321", intent["requirement"]["scope"])
        self.assertEqual(mod.STEPS, intent["requirement"]["steps"])
        self.assertEqual("87654321:13,16", mod.att_cells(ref))
        self.assertEqual("87654321:12,14,15", mod.att_cells(
            {"energy": {"originalDb": 15, "boundDb": 10, "controlRungs": [12, 14, 15]}}))
        self.assertEqual("", mod.att_cells({"energy": None}))

    def test_absent_energy_changes_nothing(self):
        mod = self._mod()
        self.assertIsNone(mod.energy_intent({}, []))
        self.assertEqual("", mod.att_cells({}))
        with self.assertRaises(SystemExit):
            mod.energy_intent({"energy": {"originalDb": 10, "boundDb": 10}}, [{}])


if __name__ == "__main__":
    unittest.main()


class BaselineIsCurrent(unittest.TestCase):
    def test_a_stale_record_is_no_baseline(self):
        from tools.liveconsole.kpi_observer import cell_attenuations_from_lines
        topology = SimpleNamespace(nb_id_to_nci={2816: 87654321})
        lines = [("nb=2816/1445", 16.0, 980.0)]
        self.assertEqual({}, cell_attenuations_from_lines(
            lines, _Adapter(), (87654321,), topology, max_age_s=10.0, wall_s=lambda: 1000.0))
        self.assertEqual({87654321: "16.0"}, cell_attenuations_from_lines(
            lines, _Adapter(), (87654321,), topology, max_age_s=30.0, wall_s=lambda: 1000.0))

    def test_the_rule_needs_every_poll(self):
        self.assertEqual(1.0, DEFAULT_OBSERVATION_RULES[KPI_CELL_TX_ATTENUATION].min_coverage)


class BlockEnvNeverCarriesAnEarlierLadder(unittest.TestCase):
    def test_the_export_line_always_assigns_att_cells(self):
        # Codex review 2026-09-25 #3: the runner sources every block env into one shell.
        src = (Path(__file__).resolve().parents[1]
               / "experiment_results/ota-20260911/ops/make_v47_corpus.py").read_text()
        self.assertIn('f" AIC_BLOCK_ATT_CELLS={(v5_att_cells(reference) if v5 else att_cells(reference)) or \'\'}"', src)
