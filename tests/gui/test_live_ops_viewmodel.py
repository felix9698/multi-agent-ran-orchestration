"""Track T1: the Live Operations projections.

Everything a workspace shows is built by a pure function, and this is where
those functions are pinned.  Three claims matter more than the rest, because
each of them is a way the screen could quietly lie to an operator:

* the radio inventory is **enumerated from the capability manifest**, so the
  console reflects a deployment rather than this testbed's shape;
* an element the manifest does not describe renders ``Unsupported`` **with its
  reason**, rather than vanishing (which would read as "there is none") or
  turning green (which would be a fabrication);
* the readiness strip **names the blocked segment** of the intent -> policy ->
  evidence chain, which is the difference between a status light and an answer.

No display, no manifest file on disk, no network: the fixtures are literals in
this module so the test says exactly what it assumes.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gui.operator import status as st
from gui.operator.viewmodel.types import (
    ComponentStatusView, ReadinessSegmentView, TimelineEventView,
)
from gui.operator.widgets import timeline as timeline_widget
from gui.operator.widgets import topology as topology_widget
from gui.operator.widgets.statusbadge import badge_text
from gui.operator.workspaces import live_ops
from oran.rapp import status_projection as sp

NOW = datetime(2026, 8, 14, 10, 20, 0, tzinfo=timezone.utc)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The run-012 derived capability manifest, committed with the other fixtures.
#: It is the deployment document that actually carries ``softwareProvenance``,
#: so the version column is asserted against it rather than against a literal
#: written to suit the assertion.
FIXTURE_MANIFEST = (REPO_ROOT / "tests" / "gui" / "fixtures"
                    / "capability-manifest-min.json")

#: Index of the version / release / profile column, read off the column
#: declaration so a reordered grid moves the assertion with it.
VERSION_COLUMN = [key for key, _, _ in topology_widget.COLUMNS].index("version")


def manifest(**overrides):
    """A minimal capability manifest.  Only what the projection reads."""
    document = {
        "manifestId": "m-1",
        "nearRtRicId": "near-rt-ric-fixture-001",
        "effectiveAt": "2026-08-04T00:00:00Z",
        "controlAxes": ["serving_cell"],
        "policyTypes": ["AIC_UECellSteering_1.0.0"],
        "decisionKpis": [{"name": "RRU.PrbDl"}],
        "assuranceKpis": [{"name": "RRU.PrbDl"}, {"name": "DRB.UEThpDl"}],
        "topology": {"cells": [
            {"managedObjectDn":
             "SubNetwork=oran-lab,ManagedElement=oai-gnb,NRCellDU=1",
             "globalE2NodeId": {"nodeType": "GNB"}},
            {"managedObjectDn":
             "SubNetwork=oran-lab,ManagedElement=oai-gnb,NRCellDU=2",
             "globalE2NodeId": {"nodeType": "GNB"}},
        ]},
        "e2Deployment": {"nodes": [
            {"role": "SOURCE_AND_ROLLBACK_TARGET",
             "globalE2NodeId": {"nodeType": "GNB",
                                "nodeId": {"hex": "0x0e00"}},
             "requiredRanFunctions": [{"serviceModel": "E2SM-KPM"}]},
        ]},
    }
    document.update(overrides)
    return sp.project_capability(document)


class _Policy:
    def __init__(self, enforce="ENFORCED", state="ACTIVE",
                 occurred_at="2026-08-14T10:19:50Z"):
        self.enforce_status = enforce
        self.policy_state = state
        self.occurred_at = occurred_at


class _Evidence:
    def __init__(self, quality="OK", observed_at="2026-08-14T10:19:30Z",
                 names=("RRU.PrbDl",)):
        self.quality = quality
        self.observed_at = observed_at
        self.measurement_names = names


class _RappState:
    def __init__(self, policies=(), evidence=(), error=None,
                 captured_at="2026-08-14T10:19:59Z"):
        self.policies = tuple(policies)
        self.evidence = tuple(evidence)
        self.error = error
        self.captured_at = captured_at


def by_id(elements):
    return {view.element_id: view for view in elements}


def by_kind(elements):
    out = {}
    for view in elements:
        out.setdefault(view.kind, []).append(view)
    return out


# --------------------------------------------------------------------------- #
# Inventory enumeration
# --------------------------------------------------------------------------- #


class TopologyEnumeration(unittest.TestCase):

    def test_cells_and_e2_nodes_come_from_the_manifest(self):
        elements = by_kind(live_ops.build_topology(manifest()))
        self.assertEqual(len(elements["O_DU"]), 2)
        labels = sorted(view.label for view in elements["O_DU"])
        self.assertEqual(labels, ["NRCellDU=1", "NRCellDU=2"])
        self.assertEqual(len(elements["GNB"]), 1)
        self.assertEqual(elements["GNB"][0].label, "GNB 0x0e00")

    def test_a_declared_element_states_why_it_has_no_health_value(self):
        cell = by_kind(live_ops.build_topology(manifest()))["O_DU"][0]
        self.assertEqual(cell.status, st.UNKNOWN)
        self.assertEqual(cell.status_reason, live_ops.NO_HEALTH_REASON)
        self.assertEqual(cell.source, "CAPABILITY_MANIFEST")
        self.assertEqual(cell.readiness, "DECLARED")

    def test_an_element_absent_from_the_manifest_renders_unsupported(self):
        elements = by_id(live_ops.build_topology(manifest()))
        for element_id in ("o_cu-undeclared", "o_ru-undeclared",
                           "ue-undeclared"):
            view = elements[element_id]
            self.assertEqual(view.status, st.UNSUPPORTED)
            self.assertEqual(view.status_reason, live_ops.NOT_DECLARED_REASON)

    def test_a_declared_ue_replaces_the_unsupported_placeholder(self):
        capability = manifest(topology={"ues": [{"id": "ue1"}, {"id": "ue2"}]})
        elements = by_kind(live_ops.build_topology(capability))
        self.assertEqual([view.label for view in elements["UE"]],
                         ["ue1", "ue2"])
        self.assertNotIn("ue-undeclared", by_id(
            live_ops.build_topology(capability)))

    def test_an_empty_manifest_invents_no_inventory(self):
        elements = by_id(live_ops.build_topology(sp.project_capability({})))
        for element_id in ("o_cu-undeclared", "o_du-undeclared",
                           "o_ru-undeclared", "gnb-undeclared",
                           "ue-undeclared"):
            self.assertEqual(elements[element_id].status, st.UNSUPPORTED)
        self.assertFalse([view for view in elements.values()
                          if view.kind in ("O_DU", "GNB")
                          and view.status != st.UNSUPPORTED])

    def test_no_element_count_is_hard_coded(self):
        """Three deployments, three different inventories."""
        sizes = []
        for count in (0, 1, 5):
            capability = manifest(topology={"cells": [
                {"managedObjectDn": f"NRCellDU={i}"} for i in range(count)]})
            sizes.append(len(by_kind(live_ops.build_topology(capability))
                             .get("O_DU", [])))
        self.assertEqual(sizes, [1, 1, 5])   # 0 declared -> one Unsupported row


class NonActionableElements(unittest.TestCase):

    def test_the_core_renders_external(self):
        core = by_id(live_ops.build_topology(manifest()))["core-5gc"]
        self.assertEqual(core.status, st.EXTERNAL)
        self.assertEqual(core.kind, "CORE")
        self.assertIn("external dependency", core.status_reason)

    def test_the_usrp_renders_lab_hardware(self):
        usrp = by_id(live_ops.build_topology(manifest()))["usrp"]
        self.assertEqual(usrp.status, st.LAB_HARDWARE)
        self.assertIn("not controllable from this console", usrp.status_reason)

    def test_neither_is_a_health_claim(self):
        """EXTERNAL and LAB_HARDWARE must not roll up as healthy or as failed."""
        elements = by_id(live_ops.build_topology(manifest()))
        for element_id in ("core-5gc", "usrp"):
            self.assertNotEqual(elements[element_id].status, st.OK)
            self.assertNotEqual(elements[element_id].status, st.ERROR)


# --------------------------------------------------------------------------- #
# Applied version / release / profile (phaseB_task.md section 2, GAP-13)
# --------------------------------------------------------------------------- #


def fixture_capability():
    """The committed run-012 manifest, projected.  It carries provenance."""
    return sp.project_capability(
        json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8")))


class VersionReleaseAndProfile(unittest.TestCase):
    """Section 2 asks for the applied version / release / profile per element.

    Two directions are asserted, because either one alone passes while the
    screen lies:

    * a deployment that **declares** provenance must show it - the defect this
      pins was a projection that carried ``softwareProvenance`` and then never
      read it, so a manifest full of build identity rendered as a dash;
    * a deployment that declares **none** must say ``Unsupported`` with the
      reason and the gap id, because a dash there reads as "not measured yet"
      for a field that no source in this deployment will ever measure.
    """

    def test_the_declared_provenance_reaches_the_grid(self):
        elements = by_id(live_ops.build_topology(fixture_capability()))
        node = elements["e2node-0"]
        self.assertEqual(node.version,
                         "OAI 42bf80e9b25dbf521cc692fa6338cbbbebfbcd1d")
        self.assertEqual(node.release, "2026.w30")
        self.assertEqual(node.profile, "E2AP 2.03")
        cell = topology_widget.element_row(node)[VERSION_COLUMN]
        for part in ("42bf80e9b25dbf521cc692fa6338cbbbebfbcd1d", "2026.w30",
                     "E2AP 2.03"):
            self.assertIn(part, cell)
        self.assertNotIn(st.resolve(st.UNSUPPORTED).label, cell)

    def test_each_declared_field_reaches_the_element_it_describes(self):
        elements = by_id(live_ops.build_topology(fixture_capability()))
        self.assertEqual(elements["nearrt-ric"].version,
                         "FlexRIC ef6d722f22191eea74089966983da1f5ec1fedd4")
        self.assertEqual(elements["xapp"].profile,
                         "E2AP 2.03, E2SM-KPM 2.03, E2SM-RC 1.03")
        self.assertEqual(elements["r1"].profile, "oran-aic/1.0.0")
        self.assertEqual(elements["a1-boundary"].profile,
                         "AIC_UECellSteering_1.0.0")
        self.assertEqual(elements["o1-provider"].profile,
                         "oran-aic-o1-pa-file/1.0.0")

    def test_an_element_the_manifest_is_silent_about_says_unsupported(self):
        """Including under a manifest that *does* carry softwareProvenance.

        GAP-13 is that there is no per-element provenance block for O-CU, O-DU,
        O-RU or UE.  Spreading the deployment-wide OAI build across those rows
        would turn a stated gap into a claim the manifest never made, so they
        stay Unsupported even here.
        """
        for label, capability in (("run-012 fixture", fixture_capability()),
                                  ("no provenance at all", manifest())):
            elements = by_id(live_ops.build_topology(capability))
            for element_id in ("cell-0", "ue-undeclared", "usrp"):
                with self.subTest(manifest=label, element=element_id):
                    view = elements[element_id]
                    self.assertIsNone(view.version)
                    self.assertIsNone(view.release)
                    self.assertIsNone(view.profile)
                    self.assertEqual(view.version_status, st.UNSUPPORTED)
                    self.assertEqual(view.version_reason,
                                     live_ops.NO_VERSION_REASON)
                    self.assertEqual(view.version_gap_id, "GAP-13")
                    cell = topology_widget.element_row(view)[VERSION_COLUMN]
                    self.assertNotEqual(cell, st.PRE_MEASUREMENT)
                    self.assertIn(st.resolve(st.UNSUPPORTED).label, cell)
                    self.assertIn(live_ops.NO_VERSION_REASON, cell)
                    self.assertIn("[GAP-13]", cell)

    def test_a_manifest_without_provenance_shows_no_version_anywhere(self):
        """The console never borrows one deployment's build for another."""
        elements = by_id(live_ops.build_topology(manifest()))
        self.assertIsNone(elements["e2node-0"].version)
        self.assertIsNone(elements["nearrt-ric"].version)
        self.assertEqual(elements["e2node-0"].version_gap_id, "GAP-13")

    def test_no_element_renders_a_reasonless_dash_in_the_version_column(self):
        for label, capability in (("run-012 fixture", fixture_capability()),
                                  ("literal", manifest()),
                                  ("empty", sp.project_capability({}))):
            for view in live_ops.build_topology(capability):
                with self.subTest(manifest=label, element=view.element_id):
                    cell = topology_widget.element_row(view)[VERSION_COLUMN]
                    self.assertTrue(cell.strip())
                    self.assertNotEqual(cell.strip(), st.PRE_MEASUREMENT)
                    if not (view.version or view.release or view.profile):
                        self.assertIn(st.resolve(st.UNSUPPORTED).label, cell)


# --------------------------------------------------------------------------- #
# Status provenance
# --------------------------------------------------------------------------- #


class StatusProvenance(unittest.TestCase):

    def test_every_element_names_the_boundary_that_reported_it(self):
        for view in live_ops.build_topology(manifest()):
            self.assertTrue(view.source, f"{view.element_id} has no source")

    def test_a_non_ok_element_always_states_a_reason(self):
        for view in live_ops.build_topology(manifest()):
            if view.status != st.OK:
                self.assertTrue(view.status_reason,
                                f"{view.element_id} is {view.status} silently")

    def test_the_framework_rows_report_an_r1_failure_as_unavailable(self):
        state = _RappState(error="R1Error: connection refused")
        elements = by_id(live_ops.build_topology(manifest(), rapp_state=state))
        for element_id in ("nonrt-ric", "r1"):
            self.assertEqual(elements[element_id].status, st.UNAVAILABLE)
            self.assertIn("connection refused",
                          elements[element_id].status_reason)

    def test_the_a1_row_reflects_the_worst_policy(self):
        state = _RappState(policies=(_Policy(), _Policy("NOT_ENFORCED",
                                                        "ERROR")))
        a1 = by_id(live_ops.build_topology(manifest(),
                                           rapp_state=state))["a1-boundary"]
        self.assertEqual(a1.status, st.DEGRADED)
        self.assertIn("ERROR", a1.status_reason)

    def test_the_near_rt_row_is_unknown_without_a_declared_signal(self):
        for element_id in ("nearrt-ric", "xapp"):
            view = by_id(live_ops.build_topology(manifest()))[element_id]
            self.assertEqual(view.status, st.UNKNOWN)
            self.assertEqual(view.gap_id, "GAP-02")

    def test_an_observed_e2_refusal_blocks_the_near_rt_row(self):
        elements = by_id(live_ops.build_topology(
            manifest(), e2_error_code="AIC_E2_NOT_READY"))
        self.assertEqual(elements["nearrt-ric"].status, st.BLOCKED)
        self.assertIn("AIC_E2_NOT_READY", elements["nearrt-ric"].status_reason)

    def test_declared_readiness_is_the_only_route_to_ok(self):
        elements = by_id(live_ops.build_topology(manifest(), e2_ready=True))
        self.assertEqual(elements["nearrt-ric"].status, st.OK)

    def test_a_synthetic_transition_is_not_read_as_a_measured_one(self):
        real = by_id(live_ops.build_topology(manifest(), transitions=(
            {"from": "S0", "to": "S1", "origin": "REAL",
             "at": "2026-08-14T10:19:59Z"},)))["rapp"]
        synthetic = by_id(live_ops.build_topology(manifest(), transitions=(
            {"from": "S0", "to": "S1", "origin": "SYNTHETIC",
             "at": "2026-08-14T10:19:59Z"},)))["rapp"]
        self.assertEqual(real.status, st.OK)
        self.assertEqual(synthetic.status, st.DEGRADED)
        self.assertIn("synthetic", synthetic.status_reason)

    def test_stale_evidence_is_stale_even_when_its_quality_is_ok(self):
        old = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        elements = by_id(live_ops.build_topology(
            manifest(), rapp_state=_RappState(evidence=(_Evidence(
                observed_at=old),)), now=NOW))
        self.assertEqual(elements["o1-provider"].status, st.STALE)
        self.assertEqual(elements["o1-provider"].freshness, st.FRESHNESS_STALE)

    def test_an_absent_observation_time_is_unknown_freshness_not_fresh(self):
        elements = by_id(live_ops.build_topology(
            manifest(), rapp_state=_RappState(evidence=(_Evidence(
                observed_at=None),)), now=NOW))
        self.assertIsNone(elements["dme"].age_ms)
        self.assertEqual(elements["dme"].freshness, st.FRESHNESS_UNKNOWN)


class AgeComputation(unittest.TestCase):

    def test_age_is_measured_from_the_declared_observation_time(self):
        self.assertAlmostEqual(
            live_ops.age_ms("2026-08-14T10:19:00Z", now=NOW), 60_000.0)

    def test_an_absent_or_unparseable_time_has_no_age(self):
        for value in (None, "", "not a time", "2026-13-45"):
            self.assertIsNone(live_ops.age_ms(value, now=NOW))

    def test_a_future_observation_never_reads_as_negative(self):
        self.assertEqual(live_ops.age_ms("2026-08-14T10:21:00Z", now=NOW), 0.0)


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #


class Readiness(unittest.TestCase):

    def test_the_first_unmet_segment_is_the_blocked_one(self):
        segments = live_ops.build_readiness(_RappState())
        blocked = topology_widget.blocked_segment(segments)
        self.assertEqual(blocked.segment_id, "INTENT_ACCEPTED")
        self.assertEqual(blocked.status, st.BLOCKED)
        self.assertTrue(blocked.unmet_reason)

    def test_downstream_segments_are_not_reached_rather_than_failed(self):
        segments = {s.segment_id: s
                    for s in live_ops.build_readiness(_RappState())}
        for name in ("POLICY_CREATED", "POLICY_ENFORCED",
                     "EVIDENCE_CONFIRMED"):
            self.assertEqual(segments[name].status, st.UNKNOWN)
            self.assertEqual(segments[name].blocked_by, "INTENT_ACCEPTED")

    def test_the_block_moves_along_the_chain_as_evidence_arrives(self):
        with_policy = {s.segment_id: s for s in live_ops.build_readiness(
            _RappState(policies=(_Policy(),)))}
        self.assertEqual(with_policy["POLICY_CREATED"].status, st.OK)
        self.assertEqual(with_policy["POLICY_ENFORCED"].status, st.OK)
        self.assertEqual(with_policy["EVIDENCE_CONFIRMED"].status, st.BLOCKED)

    def test_a_complete_chain_has_no_blocked_segment(self):
        segments = live_ops.build_readiness(
            _RappState(policies=(_Policy(),), evidence=(_Evidence(),)))
        self.assertIsNone(topology_widget.blocked_segment(segments))
        self.assertIn("complete", live_ops.readiness_headline(segments))

    def test_evidence_of_non_ok_quality_does_not_confirm_the_chain(self):
        segments = {s.segment_id: s for s in live_ops.build_readiness(
            _RappState(policies=(_Policy(),),
                       evidence=(_Evidence(quality="SUSPECT"),)))}
        self.assertEqual(segments["EVIDENCE_CONFIRMED"].status, st.BLOCKED)

    def test_an_exposed_ladder_is_prepended_with_its_own_segments(self):
        segments = live_ops.build_readiness(
            _RappState(), ladder={"PRECHECK": True, "TRUST_READY": True,
                                  "SUBSCRIBED": {"confirmed": False,
                                                 "reason": "no subscription"}})
        ids = [segment.segment_id for segment in segments]
        self.assertEqual(ids[:3], ["PRECHECK", "TRUST_READY", "SUBSCRIBED"])
        blocked = topology_widget.blocked_segment(segments)
        self.assertEqual(blocked.segment_id, "SUBSCRIBED")
        self.assertEqual(blocked.unmet_reason, "no subscription")

    def test_an_absent_ladder_is_unsupported_never_a_green_default(self):
        segments = live_ops.build_readiness(_RappState(), ladder={})
        ladder_rows = [s for s in segments if s.segment_id == "LADDER"]
        self.assertEqual(len(ladder_rows), 1)
        self.assertEqual(ladder_rows[0].status, st.UNSUPPORTED)
        self.assertIn("GAP-01", ladder_rows[0].unmet_reason)

    def test_the_headline_names_the_blocked_segment(self):
        headline = live_ops.readiness_headline(
            live_ops.build_readiness(_RappState()))
        self.assertIn("blocked at Intent accepted", headline)


# --------------------------------------------------------------------------- #
# Rendering rules
# --------------------------------------------------------------------------- #


class RenderingRules(unittest.TestCase):

    def test_a_status_is_never_rendered_by_colour_alone(self):
        for view in live_ops.build_topology(manifest()):
            row = topology_widget.element_row(view)
            self.assertTrue(row[2].startswith(st.resolve(view.status).glyph))

    def test_an_unmeasured_column_shows_the_placeholder_not_a_zero(self):
        row = topology_widget.element_row(ComponentStatusView(
            element_id="e", label="e", kind="GNB", status=st.UNKNOWN))
        self.assertEqual(row[3], st.PRE_MEASUREMENT)      # readiness
        self.assertEqual(row[5], st.PRE_MEASUREMENT)      # observed

    def test_an_unprovenanced_element_states_it_rather_than_dashing(self):
        """The version column is not a measurement, so the dash does not fit.

        Readiness and observation time are measurements that have not arrived
        yet, and the em dash says so.  A version is *declared*, not measured: if
        the manifest declares none, no later poll will produce one, and a dash
        there tells the operator to keep waiting for something that is not
        coming.  The vocabulary word plus the reason is the honest cell.
        """
        row = topology_widget.element_row(ComponentStatusView(
            element_id="e", label="e", kind="GNB", status=st.UNKNOWN))
        self.assertNotEqual(row[VERSION_COLUMN], st.PRE_MEASUREMENT)
        self.assertIn(st.resolve(st.UNSUPPORTED).label, row[VERSION_COLUMN])
        self.assertIn(topology_widget.UNREPORTED_VERSION_REASON,
                      row[VERSION_COLUMN])

    def test_the_grid_reports_freshness_next_to_the_age(self):
        row = topology_widget.element_row(ComponentStatusView(
            element_id="e", label="e", kind="DME", status=st.OK,
            observed_at="2026-08-14T10:19:00Z", age_ms=60_000.0,
            freshness=st.FRESH))
        self.assertIn("60s ago", row[5])
        self.assertIn(st.FRESH, row[5])

    def test_only_the_blocking_segment_carries_the_reason(self):
        """The strip must not repeat the same sentence across every segment."""
        blocking = topology_widget.segment_text(ReadinessSegmentView(
            segment_id="POLICY_ENFORCED", label="Policy enforced",
            status=st.BLOCKED, unmet_reason="no policy reports ENFORCED"))
        downstream = topology_widget.segment_text(ReadinessSegmentView(
            segment_id="EVIDENCE_CONFIRMED", label="Evidence confirmed",
            status=st.UNKNOWN, blocked_by="POLICY_ENFORCED",
            unmet_reason="not reached"))
        self.assertIn("no policy reports ENFORCED", blocking)
        self.assertIn("not reached", downstream)
        self.assertNotIn("no policy reports ENFORCED", downstream)

    def test_the_badge_carries_reason_and_gap_id(self):
        text = badge_text(st.UNSUPPORTED, reason="no IQ source", gap_id="GAP-10")
        self.assertIn("no IQ source", text)
        self.assertIn("GAP-10", text)


class TimelineProjection(unittest.TestCase):

    EVENTS = (
        TimelineEventView(seq=1, lane="INTENT", kind="INTENT_SUBMITTED",
                          t_utc="2026-08-14T10:00:00Z", intent_id="i-1"),
        TimelineEventView(seq=2, lane="COORDINATOR", kind="FSM_TRANSITION",
                          origin="DERIVED", derivation="capture ordering",
                          t_rel_s=1.5, intent_id="i-1"),
        TimelineEventView(seq=3, lane="A1", kind="POLICY_ERROR",
                          severity="ERROR", t_utc="2026-08-14T10:00:05Z",
                          policy_id="p-1", intent_id="i-1",
                          component="nonrt-ric", run_id="r-1"),
    )

    def test_filtering_by_lane_severity_and_correlation(self):
        self.assertEqual(len(timeline_widget.filter_events(
            self.EVENTS, lanes=("A1",))), 1)
        self.assertEqual(len(timeline_widget.filter_events(
            self.EVENTS, min_severity="ERROR")), 1)
        self.assertEqual(len(timeline_widget.filter_events(
            self.EVENTS, correlation="i-1")), 3)
        self.assertEqual(len(timeline_widget.filter_events(
            self.EVENTS, correlation="p-1")), 1)

    def test_filtering_preserves_source_order(self):
        filtered = timeline_widget.filter_events(self.EVENTS)
        self.assertEqual([event.seq for event in filtered], [1, 2, 3])

    def test_a_derived_event_is_marked_and_separable(self):
        derived = timeline_widget.event_row(self.EVENTS[1])
        self.assertIn("DERIVED", derived[4])
        self.assertIn("capture ordering", derived[4])
        self.assertEqual(len(timeline_widget.filter_events(
            self.EVENTS, include_derived=False)), 2)

    def test_an_untimestamped_event_shows_elapsed_not_a_wall_clock(self):
        self.assertEqual(timeline_widget.event_time(self.EVENTS[1]), "+1.500s")

    def test_an_error_row_carries_every_required_identifier(self):
        ids = timeline_widget.event_ids(self.EVENTS[2])
        for token in ("intent=i-1", "policy=p-1", "run=r-1",
                      "component=nonrt-ric"):
            self.assertIn(token, ids)

    def test_severity_maps_to_a_distinct_status(self):
        self.assertEqual(timeline_widget.row_status(self.EVENTS[0]), st.OK)
        self.assertEqual(timeline_widget.row_status(self.EVENTS[2]), st.ERROR)


if __name__ == "__main__":
    unittest.main()
