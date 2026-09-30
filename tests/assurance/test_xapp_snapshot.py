"""Common KPI snapshot: measured-only direction, lookup, subsets, seams."""
import unittest

from assurance.core.provenance import Provenance, TypedQuantity
from assurance.xapps import (
    CommonKpiSnapshot, SnapshotError, snapshot_from_samples,
)
from assurance.xapps.snapshot import KpiSnapshotEntry

from tests.assurance.xapp_support import (
    NOW, SOURCE_CELL, TAKEN, TARGET_UE, attribution_snapshot, raw_sample,
)


class MeasurementDirectionTests(unittest.TestCase):
    """gNB -> E2 -> Collector -> Snapshot.  Nothing else gets in."""

    def test_snapshots_are_assembled_from_collector_samples_only(self):
        with self.assertRaisesRegex(SnapshotError, "RawSample"):
            snapshot_from_samples(snapshot_id="snap/x", taken_at=TAKEN,
                                  samples=[{"counter": "fake"}])

    def test_an_entry_refuses_a_non_measured_value(self):
        derived = TypedQuantity(1.0, "Mbps", Provenance.DERIVED, "summary/x",
                                derivation_rule="mean", input_refs=("a",))
        with self.assertRaisesRegex(SnapshotError, "MEASURED"):
            KpiSnapshotEntry(
                counter_id="DRB.UEThpDl", value=derived, scope={},
                observed_at=TAKEN,
                clock_health=__import__(
                    "assurance.collector.samples",
                    fromlist=["ClockHealth"]).ClockHealth.SYNCHRONISED,
                sample_id="s", sample_hash="c" * 64)

    def test_entries_walk_back_to_their_raw_samples(self):
        sample = raw_sample("s-1", "RRU.PrbDl", 25.0, "percent",
                            {"cellId": SOURCE_CELL})
        snapshot = snapshot_from_samples(snapshot_id="snap/1",
                                         taken_at=TAKEN, samples=[sample])
        entry = snapshot.entries[0]
        self.assertEqual(entry.sample_id, "s-1")
        self.assertEqual(entry.sample_hash, sample.content_hash())


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = attribution_snapshot()

    def test_serving_cell_and_active_ue_lookup(self):
        entry = self.snapshot.serving_cell_of(TARGET_UE)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.scope["cellId"], SOURCE_CELL)
        self.assertEqual(self.snapshot.active_ue_ids(SOURCE_CELL),
                         ("ue-heavy", "ue-target"))
        self.assertEqual(self.snapshot.active_ue_ids("99999999"), ())

    def test_absent_kpis_stay_absent(self):
        self.assertIsNone(self.snapshot.latest("DRB.UEThpDl"))

    def test_freshness_is_bounded_in_both_directions(self):
        self.assertTrue(self.snapshot.is_fresh(NOW, freshness_bound_ms=5000))
        self.assertFalse(self.snapshot.is_fresh(
            "2026-08-31T10:01:00.000000Z", freshness_bound_ms=5000))
        self.assertFalse(self.snapshot.is_fresh(
            "2026-08-31T09:59:59.000000Z", freshness_bound_ms=5000))


class SubsetTests(unittest.TestCase):
    """xApps receive precondition/readback subsets, not the whole snapshot."""

    def test_subset_keeps_lineage_and_filters_counters(self):
        extra = raw_sample("s-prb", "RRU.PrbDl", 25.0, "percent",
                           {"cellId": SOURCE_CELL})
        snapshot = attribution_snapshot(extra_samples=(extra,))
        subset = snapshot.subset_for_counters(
            ["UE.ServingCell"], subset_id="snap/attribution-1/ue-sched")
        self.assertEqual(subset.parent_snapshot_id, snapshot.snapshot_id)
        self.assertEqual({e.counter_id for e in subset.entries},
                         {"UE.ServingCell"})
        self.assertEqual(subset.taken_at, snapshot.taken_at)

    def test_round_trip_hash_is_stable(self):
        snapshot = attribution_snapshot()
        self.assertEqual(snapshot.content_hash(), snapshot.content_hash())
        rebuilt = CommonKpiSnapshot(
            snapshot_id=snapshot.snapshot_id, taken_at=snapshot.taken_at,
            entries=snapshot.entries)
        self.assertEqual(rebuilt.content_hash(), snapshot.content_hash())


class LegacyPathSeamTests(unittest.TestCase):
    """The legacy Telnet research path is never the coordination layer's
    execution path, and nothing in ``assurance.xapps`` imports it."""

    def test_no_xapps_module_imports_the_legacy_telnet_stack(self):
        """Import statements only: a docstring may *cite* the legacy knob as
        provenance, but no module may execute through it."""
        import pathlib
        import assurance.xapps as package
        package_dir = pathlib.Path(package.__file__).parent
        for path in sorted(package_dir.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("import executor", "from executor import",
                              "from executor.", "import coordinator",
                              "from coordinator import", "from coordinator.",
                              "import decision", "from decision import",
                              "from decision.", "import telnetlib",
                              "from telnetlib", "import oai_executor",
                              "from oai_executor"):
                self.assertNotIn(forbidden, source,
                                 f"{path.name} imports the legacy path "
                                 f"({forbidden!r})")

    def test_the_telnet_knob_is_recorded_as_backend_note_not_e2_path(self):
        from assurance.xapps import default_capability_manifests
        from tests.assurance.xapp_support import deployment
        manifests = {m.xapp_id: m
                     for m in default_capability_manifests(deployment())}
        scheduler = manifests["xapp/ue-scheduler"]
        for binding in scheduler.action_bindings:
            self.assertIn("telnet", binding.live_backend)
        self.assertNotEqual(scheduler.execution_path_state.value,
                            "OTA_LIVE_VERIFIED")


if __name__ == "__main__":
    unittest.main()
