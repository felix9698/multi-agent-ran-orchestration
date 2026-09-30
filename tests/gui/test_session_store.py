"""SessionStore, record builders and the metric availability index.

Hermetic: temp directories only.  No display, no hardware, no network.

The properties pinned here are the ones that make a stored run *evidence*
rather than a report:

* an unfinalized run re-opens as ``INTERRUPTED`` and is never a success;
* a truncated final line costs one record, not the run;
* a credential value cannot reach disk through the store;
* an unsupported metric cannot carry samples, and a null value cannot become a
  zero.
"""

import json
import tempfile
import unittest
from pathlib import Path

from gui.operator.store import metric_index as mi
from gui.operator.store import records as rec
from gui.operator.store import session_store as ss
from gui.operator.store.session_store import SessionStore, SessionStoreError

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"

REPLAY_EVIDENCE = {"basis": "REPLAY_OF_RECORDED_SOURCE"}


def _sample(seq=0, metric="RRU.PrbDl", value=30.0, scope_id="NRCellDU=1",
            **extra):
    fields = dict(seq=seq, metric=metric, value=value, unit="percent",
                  scope_level="CELL", scope_id=scope_id,
                  boundary="O1_ASSURANCE", t_utc="2026-08-13T14:20:00Z",
                  quality="OK")
    fields.update(extra)
    return rec.telemetry_sample(**fields)


class StoreLifecycle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _create(self, mode="REPLAY", evidence=None, **kwargs):
        return SessionStore.create(self.root, mode=mode,
                                   mode_evidence=evidence or REPLAY_EVIDENCE,
                                   **kwargs)

    def test_run_id_grammar_and_directory_layout(self):
        store = self._create()
        self.assertRegex(store.run_id,
                         r"^(live|replay|synthetic|emulated)-"
                         r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
        for rel in ss.REQUIRED_ENTRIES:
            self.assertTrue((store.run_dir / rel).exists(), f"missing {rel}")
        self.assertTrue((store.run_dir / "raw").is_dir())

    def test_manifest_is_written_last_and_atomically(self):
        store = self._create()
        store.append_event(rec.timeline_event(seq=0, lane="SESSION",
                                              kind="SESSION_STARTED"))
        self.assertFalse((store.run_dir / "manifest.json").exists(),
                         "a running run must not look finalized")
        manifest = store.finalize("COMPLETED")
        self.assertTrue((store.run_dir / "manifest.json").is_file())
        self.assertFalse(list(store.run_dir.glob("*.tmp")))
        self.assertEqual(manifest["disposition"], "COMPLETED")
        self.assertEqual(manifest["schemaVersion"], ss.SCHEMA_VERSION)

    def test_only_completed_is_a_success(self):
        self.assertEqual(ss.SUCCESS_DISPOSITIONS, ("COMPLETED",))
        for disposition in ("ABORTED", "FAILED", "INTERRUPTED"):
            store = self._create()
            store.finalize(disposition)
            self.assertFalse(store.is_success, disposition)

    def test_finalize_refuses_running(self):
        store = self._create()
        with self.assertRaises(SessionStoreError):
            store.finalize("RUNNING")

    def test_unfinalized_run_reopens_as_interrupted_with_artifacts_intact(self):
        store = self._create()
        store.append_telemetry(_sample())
        store.append_episode(rec.episode_trace(
            episode_id="e1", intent_text="raise ue2 throughput",
            terminal_outcome="commit_original"))
        # simulate a crash: no finalize, handles never closed
        run_dir = store.run_dir

        reopened = SessionStore.open(run_dir)
        self.assertEqual(reopened.disposition, "INTERRUPTED")
        self.assertFalse(reopened.is_success)
        self.assertEqual(len(list(reopened.read_telemetry())), 1)
        self.assertEqual(len(list(reopened.read_episodes())), 1)
        self.assertTrue(any(i["kind"] == "PARTIAL_DATA"
                            for i in reopened.data_issues()))

    def test_reopening_an_interrupted_run_does_not_write_to_it(self):
        store = self._create()
        store.append_telemetry(_sample())
        before = sorted(p.name for p in store.run_dir.rglob("*"))
        SessionStore.open(store.run_dir)
        self.assertEqual(sorted(p.name for p in store.run_dir.rglob("*")), before)

    def test_context_manager_finalizes_as_interrupted(self):
        with self._create() as store:
            store.append_event(rec.timeline_event(seq=0, lane="SESSION",
                                                  kind="SESSION_STARTED"))
            run_dir = store.run_dir
        manifest = json.loads((run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["disposition"], "INTERRUPTED")

    def test_a_reopened_run_is_read_only(self):
        store = self._create()
        store.finalize("COMPLETED")
        reopened = SessionStore.open(store.run_dir)
        with self.assertRaises(SessionStoreError):
            reopened.append_telemetry(_sample())
        with self.assertRaises(SessionStoreError):
            reopened.record_issue("STALE_DATA", "x")

    def test_list_runs_is_newest_first_and_survives_an_interrupted_run(self):
        first = self._create()
        first.finalize("COMPLETED")
        second = self._create()          # left RUNNING
        headers = SessionStore.list_runs(self.root)
        self.assertEqual(len(headers), 2)
        self.assertEqual({h["runId"] for h in headers},
                         {first.run_id, second.run_id})
        dispositions = {h["runId"]: h["disposition"] for h in headers}
        self.assertEqual(dispositions[first.run_id], "COMPLETED")
        self.assertEqual(dispositions[second.run_id], "INTERRUPTED")

    def test_list_runs_on_a_missing_root_is_empty_not_an_error(self):
        self.assertEqual(SessionStore.list_runs(self.root / "nope"), [])


class ModeIsEvidence(unittest.TestCase):
    """A Replay run presented as LIVE is the failure this guards against."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_live_requires_a_live_transport_basis(self):
        with self.assertRaises(SessionStoreError):
            SessionStore.create(self.root, mode="LIVE",
                                mode_evidence=REPLAY_EVIDENCE)

    def test_replay_cannot_claim_live_transport_evidence(self):
        with self.assertRaises(SessionStoreError):
            SessionStore.create(self.root, mode="REPLAY",
                                mode_evidence={"basis": "LIVE_R1_TRANSPORT"})

    def test_a_recording_can_never_produce_a_live_manifest(self):
        """The correction for the Replay-reads-as-LIVE defect.

        LIVE is reserved for data arriving over a live transport *now*.  A run
        read back from a recording is a Replay session however the recording was
        made, so there is no argument list that yields mode LIVE from a
        recorded source.
        """
        for evidence in ({"basis": "REPLAY_OF_RECORDED_SOURCE"},
                         {"basis": "REPLAY_OF_RECORDED_SOURCE",
                          "sourceRunIds": ["20260102_000000"]},
                         {"basis": "OFFLINE_MODEL"}):
            with self.subTest(evidence=evidence["basis"]):
                with self.assertRaises(SessionStoreError):
                    SessionStore.create(self.root, mode="LIVE",
                                        mode_evidence=evidence)

    def test_unknown_mode_and_basis_are_refused(self):
        with self.assertRaises(SessionStoreError):
            SessionStore.create(self.root, mode="DEMO",
                                mode_evidence=REPLAY_EVIDENCE)
        with self.assertRaises(SessionStoreError):
            SessionStore.create(self.root, mode="REPLAY",
                                mode_evidence={"basis": "TRUST_ME"})

    def test_the_mode_has_no_setter(self):
        store = SessionStore.create(self.root, mode="REPLAY",
                                    mode_evidence=REPLAY_EVIDENCE)
        self.assertEqual(store.mode, "REPLAY")
        with self.assertRaises(AttributeError):
            store.mode = "LIVE"          # type: ignore[misc]


class Durability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = SessionStore.create(self.root, mode="REPLAY",
                                         mode_evidence=REPLAY_EVIDENCE)

    def test_records_are_flushed_per_record(self):
        self.store.append_telemetry(_sample(seq=0))
        path = self.store.run_dir / "normalized" / "telemetry.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 1,
                         "a record that is not on disk is a record a crash loses")

    def test_a_truncated_trailing_line_costs_one_record_not_the_run(self):
        for i in range(3):
            self.store.append_telemetry(_sample(seq=i))
        path = self.store.run_dir / "normalized" / "telemetry.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 3, "metric": "RRU.Pr')     # crash mid-write

        reopened = SessionStore.open(self.store.run_dir)
        rows = list(reopened.read_telemetry())
        self.assertEqual(len(rows), 3)
        self.assertTrue(any(i["kind"] == "DATA_TRUNCATED"
                            for i in reopened.data_issues()))

    def test_artifact_index_carries_size_digest_and_line_count(self):
        self.store.append_telemetry(_sample())
        self.store.append_telemetry(_sample(seq=1, value=None, quality="MISSING"))
        manifest = self.store.finalize("COMPLETED")
        entry = manifest["artifacts"]["normalized/telemetry.jsonl"]
        self.assertEqual(entry["lineCount"], 2)
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertGreater(entry["byteCount"], 0)
        self.assertNotIn("manifest.json", manifest["artifacts"])

    def test_counts_are_recorded_per_stream(self):
        self.store.append_telemetry(_sample())
        self.store.append_episode(rec.episode_trace(
            episode_id="e1", intent_text="t", terminal_outcome="commit_revised"))
        self.store.append_cycle(rec.cycle_trace(episode_id="e1", cycle_index=0))
        self.store.append_llm_call(rec.llm_call_trace(
            seq=0, stage="FEASIBILITY", backend_name="mock:deterministic"))
        self.store.append_event(rec.timeline_event(
            seq=0, lane="WARNING", kind="EVIDENCE_STALE", severity="WARNING"))
        counts = self.store.finalize("COMPLETED")["counts"]
        self.assertEqual(counts["telemetrySamples"], 1)
        self.assertEqual(counts["episodes"], 1)
        self.assertEqual(counts["cycles"], 1)
        self.assertEqual(counts["llmCalls"], 1)
        self.assertEqual(counts["timelineEvents"], 1)
        self.assertEqual(counts["warnings"], 1)

    def test_raw_artifacts_are_copied_byte_for_byte(self):
        source = FIXTURES / "lo1-capture-min" / "raw" / "o1"
        pm = next(source.glob("pm-*.xml"))
        rel = self.store.copy_raw("lo1-capture", pm, "raw/o1/" + pm.name)
        self.assertEqual((self.store.run_dir / rel).read_bytes(), pm.read_bytes())

    def test_a_raw_path_cannot_escape_the_run_directory(self):
        pm = next((FIXTURES / "lo1-capture-min" / "raw" / "o1").glob("pm-*.xml"))
        with self.assertRaises(SessionStoreError):
            self.store.copy_raw("lo1-capture", pm, "../../escaped.xml")

    def test_data_issues_reach_the_manifest(self):
        self.store.record_issue("DANGLING_REFERENCE",
                                "durableStatePath points into a removed state/",
                                gap_id="GAP-14")
        manifest = self.store.finalize("COMPLETED")
        self.assertEqual(manifest["dataIssues"][0]["gapId"], "GAP-14")

    def test_an_unknown_issue_kind_is_refused(self):
        with self.assertRaises(SessionStoreError):
            self.store.record_issue("SOMETHING_ODD", "x")


class NoSecretEverReachesDisk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_config_snapshot_credentials_are_scrubbed_before_the_write(self):
        store = SessionStore.create(
            self.root, mode="REPLAY", mode_evidence=REPLAY_EVIDENCE,
            profile_id="p1",
            config_snapshot={
                "llm": {"api_key": "sk-live-0123456789abcdef",
                        "backend": "claude-sonnet"},
                "sftp": {"password": "hunter2"},
                "refs": {"apiKeyRef": "env:ANTHROPIC_API_KEY"},
                "nested": [{"token": "ghp_realtoken"}],
            })
        text = (store.run_dir / "config-snapshot.json").read_text()
        for leaked in ("sk-live-0123456789abcdef", "hunter2", "ghp_realtoken"):
            self.assertNotIn(leaked, text)
        self.assertIn(ss.REDACTED, text)
        # references are kept: a reference NAME is what Settings displays
        self.assertIn("env:ANTHROPIC_API_KEY", text)
        # the shape of the configuration survives
        self.assertIn("claude-sonnet", text)

    def test_credential_refs_must_be_reference_names(self):
        store = SessionStore.create(self.root, mode="REPLAY",
                                    mode_evidence=REPLAY_EVIDENCE)
        store.set_llm(backend_name="claude-sonnet",
                      credential_refs=["env:ANTHROPIC_API_KEY"])
        with self.assertRaises(SessionStoreError):
            store.set_llm(backend_name="claude-sonnet",
                          credential_refs=["sk-live-0123456789abcdef"])

    def test_no_written_file_matches_a_secret_shape(self):
        store = SessionStore.create(
            self.root, mode="REPLAY", mode_evidence=REPLAY_EVIDENCE,
            config_snapshot={"private_key": "-----BEGIN OPENSSH PRIVATE KEY-----"})
        store.finalize("COMPLETED")
        for path in store.run_dir.rglob("*"):
            if path.is_file():
                payload = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", payload)
                self.assertNotIn("sk-", payload)


class LlmRecordsAreContentFree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = SessionStore.create(Path(self._tmp.name), mode="REPLAY",
                                         mode_evidence=REPLAY_EVIDENCE)

    def test_the_builder_refuses_model_text(self):
        for field in ("prompt", "response", "reasoning_content", "thinking"):
            with self.subTest(field=field):
                with self.assertRaises(rec.RecordError):
                    rec.llm_call_trace(seq=0, stage="PARSE",
                                       backend_name="claude-sonnet",
                                       **{field: "chain of thought"})

    def test_the_store_refuses_model_text_too(self):
        """Two gates on purpose: the store is what actually writes to disk."""
        with self.assertRaises(SessionStoreError):
            self.store.append_llm_call(
                {"seq": 0, "stage": "PARSE", "backendName": "x",
                 "reasoning_content": "<think>...</think>"})

    def test_an_error_string_is_capped(self):
        trace = rec.llm_call_trace(seq=0, stage="PARSE", backend_name="x",
                                   error="e" * 900)
        self.assertEqual(len(trace["error"]), 500)


class RecordHonesty(unittest.TestCase):
    def test_a_null_value_is_never_quietly_ok(self):
        sample = rec.telemetry_sample(
            seq=0, metric="throughput_mbps", value=None, unit="Mbps",
            scope_level="UE", scope_id="ue2", boundary="EXPERIMENT_RECORD",
            t_rel_s=15.0, quality="OK")
        self.assertIsNone(sample["value"])
        self.assertEqual(sample["quality"], "MISSING")

    def test_zero_is_a_real_measurement_and_stays_ok(self):
        sample = _sample(value=0.0)
        self.assertEqual(sample["value"], 0.0)
        self.assertEqual(sample["quality"], "OK")

    def test_non_finite_values_are_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(rec.RecordError):
                _sample(value=bad)

    def test_a_sample_needs_an_observation_or_relative_time(self):
        with self.assertRaises(rec.RecordError):
            rec.telemetry_sample(seq=0, metric="m", value=1.0, unit="u",
                                 scope_level="RUN", scope_id="run",
                                 boundary="RAPP_INTERNAL", quality="OK")

    def test_a_derived_sample_must_state_its_derivation(self):
        with self.assertRaises(rec.RecordError):
            _sample(derived=True)
        sample = _sample(derived=True, derivation="sigma = 10^(-SINR/20)")
        self.assertTrue(sample["derived"])

    def test_a_derived_event_must_state_its_derivation(self):
        with self.assertRaises(rec.RecordError):
            rec.timeline_event(seq=0, lane="COORDINATOR", kind="FSM_TRANSITION",
                               origin="DERIVED")
        event = rec.timeline_event(
            seq=0, lane="COORDINATOR", kind="FSM_TRANSITION", origin="DERIVED",
            derivation="FSM transition order only; the capture carries no "
                       "per-transition timestamp")
        self.assertEqual(event["origin"], "DERIVED")
        self.assertIsNone(event["tUtc"])

    def test_an_error_event_must_carry_a_correlation_id(self):
        with self.assertRaises(rec.RecordError):
            rec.timeline_event(seq=0, lane="R1", kind="POLICY_FAILED",
                               severity="ERROR")
        event = rec.timeline_event(seq=0, lane="R1", kind="POLICY_FAILED",
                                   severity="ERROR", ids={"policyId": "p-1"})
        self.assertEqual(event["ids"]["policyId"], "p-1")

    def test_annotatable_kinds_are_marked_automatically(self):
        self.assertTrue(rec.timeline_event(seq=0, lane="INTENT",
                                           kind="INTENT_SUBMITTED")["annotatable"])
        self.assertFalse(rec.timeline_event(seq=0, lane="O1",
                                            kind="RPC_SENT")["annotatable"])

    def test_the_eq12_mapping_is_the_one_the_research_console_uses(self):
        from gui.operator import status as st

        self.assertEqual(rec.TERMINAL_OUTCOME_TO_EQ12,
                         st.TERMINAL_OUTCOME_TO_EQ12)
        trace = rec.episode_trace(episode_id="e", intent_text="t",
                                  terminal_outcome="commit_revised")
        self.assertEqual(trace["eq12State"], "Admitted")
        self.assertEqual(trace["terminalOutcome"], "commit_revised")

    def test_conflict_is_unknown_and_never_inferred(self):
        """GAP-03: the coordinator does not persist the S1 verdict."""
        trace = rec.episode_trace(episode_id="e", intent_text="t",
                                  terminal_outcome="commit_original")
        self.assertIsNone(trace["hasConflict"])

    def test_an_unknown_terminal_outcome_is_refused_not_approximated(self):
        with self.assertRaises(rec.RecordError):
            rec.episode_trace(episode_id="e", intent_text="t",
                              terminal_outcome="probably_fine")

    def test_routing_is_recorded_as_being_on_the_calibrated_probability(self):
        cycle = rec.cycle_trace(episode_id="e", cycle_index=0,
                                rawConfidence=0.88, calibratedProbability=0.62,
                                threshold=0.4)
        self.assertEqual(cycle["thresholdAppliedTo"], "calibrated_probability")


class MetricIndexRules(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (FIXTURES / "capability-manifest-min.json").read_text(encoding="utf-8"))

    def test_a_non_ok_status_must_state_a_reason(self):
        with self.assertRaises(rec.RecordError):
            rec.metric_index_entry(status="UNAVAILABLE", source="O1_DME",
                                   scope_level="CELL", unit="percent")

    def test_an_unsupported_metric_cannot_carry_samples(self):
        """Samples under an unsupported metric mean something was substituted."""
        with self.assertRaises(rec.RecordError):
            rec.metric_index_entry(status="UNSUPPORTED", source="NONE",
                                   scope_level="UE", unit="index",
                                   reason="no source", sample_count=3)

    def test_capture_source_marks_the_documented_gaps(self):
        builder = mi.MetricIndexBuilder(source_id="lo1-capture",
                                        capability_manifest=self.manifest)
        builder.observe(_sample(seq=0, value=30.0))
        builder.observe(_sample(seq=1, value=40.0, scope_id="NRCellDU=2"))
        index = builder.build()

        self.assertEqual(index["RRU.PrbDl"]["status"], "OK")
        self.assertEqual(index["RRU.PrbDl"]["sampleCount"], 2)
        self.assertEqual(sorted(index["RRU.PrbDl"]["scopeIds"]),
                         ["NRCellDU=1", "NRCellDU=2"])

        # subscribed in the PerfMetricJob, never delivered
        self.assertEqual(index["DRB.UEThpDl"]["status"], "UNAVAILABLE")
        self.assertEqual(index["DRB.UEThpDl"]["gapId"], "GAP-05")
        self.assertTrue(index["DRB.UEThpDl"]["reason"])

        for metric, gap in (("mcs", "GAP-07"), ("latency_ran_ms", "GAP-08"),
                            ("serving_cell", "GAP-09"),
                            ("constellation_iq", "GAP-10")):
            with self.subTest(metric=metric):
                self.assertEqual(index[metric]["status"], "UNSUPPORTED")
                self.assertEqual(index[metric]["gapId"], gap)
                self.assertEqual(index[metric]["sampleCount"], 0)

    def test_no_manifest_means_unknown_not_a_hard_coded_list(self):
        builder = mi.MetricIndexBuilder(source_id="live", capability_manifest=None)
        index = builder.build()
        self.assertEqual(index["RRU.PrbDl"]["status"], "UNKNOWN")
        self.assertEqual(index["RRU.PrbDl"]["reason"], mi.REASON_NO_MANIFEST)

    def test_a_metric_the_manifest_does_not_declare_is_unsupported(self):
        manifest = {"assuranceKpis": [{"name": "RRU.PrbDl", "unit": "percent",
                                       "managedObjectClass": "NRCellDU"}]}
        builder = mi.MetricIndexBuilder(source_id="live",
                                        capability_manifest=manifest)
        index = builder.build()
        self.assertEqual(index["DRB.UEThpDl"]["status"], "UNSUPPORTED")
        self.assertEqual(index["DRB.UEThpDl"]["reason"], mi.REASON_NOT_DECLARED)

    def test_a_declared_metric_the_registry_does_not_know_is_still_listed(self):
        manifest = {"assuranceKpis": [
            {"name": "RRU.PrbUl", "unit": "percent",
             "managedObjectClass": "NRCellDU"}]}
        builder = mi.MetricIndexBuilder(source_id="live",
                                        capability_manifest=manifest)
        index = builder.build()
        self.assertIn("RRU.PrbUl", index)
        self.assertEqual(index["RRU.PrbUl"]["unit"], "percent")
        self.assertEqual(builder.display_names()["RRU.PrbUl"], "RRU.PrbUl")

    def test_all_null_samples_are_unavailable_not_ok(self):
        builder = mi.MetricIndexBuilder(source_id="experiment-run")
        for i in range(3):
            builder.observe(rec.telemetry_sample(
                seq=i, metric="throughput_mbps", value=None, unit="Mbps",
                scope_level="UE", scope_id="ue2", boundary="EXPERIMENT_RECORD",
                t_rel_s=float(i), quality="MISSING"))
        entry = builder.build()["throughput_mbps"]
        self.assertEqual(entry["status"], "UNAVAILABLE")
        self.assertIn("null", entry["reason"])

    def test_some_null_samples_degrade_rather_than_hide(self):
        builder = mi.MetricIndexBuilder(source_id="experiment-run")
        builder.observe(rec.telemetry_sample(
            seq=0, metric="throughput_mbps", value=3.6, unit="Mbps",
            scope_level="UE", scope_id="ue1", boundary="EXPERIMENT_RECORD",
            t_rel_s=0.0, quality="OK"))
        builder.observe(rec.telemetry_sample(
            seq=1, metric="throughput_mbps", value=None, unit="Mbps",
            scope_level="UE", scope_id="ue1", boundary="EXPERIMENT_RECORD",
            t_rel_s=15.0, quality="MISSING"))
        entry = builder.build()["throughput_mbps"]
        self.assertEqual(entry["status"], "DEGRADED")
        self.assertEqual(entry["sampleCount"], 2)
        self.assertEqual(entry["qualityCounts"]["MISSING"], 1)

    def test_the_derived_constellation_needs_a_measured_sinr(self):
        builder = mi.MetricIndexBuilder(source_id="experiment-run")
        self.assertEqual(builder.build()["constellation_derived"]["status"],
                         "UNSUPPORTED")
        builder.observe(rec.telemetry_sample(
            seq=0, metric="ue_dl_sinr_db", value=12.5, unit="dB",
            scope_level="UE", scope_id="ue1", boundary="EXPERIMENT_RECORD",
            t_rel_s=0.0, quality="OK"))
        # still UNSUPPORTED until a derived sample actually exists, but the
        # requirement is now satisfied rather than structurally impossible
        entry = builder.build()["constellation_derived"]
        self.assertIn(entry["status"], ("UNAVAILABLE", "OK"))
        self.assertNotEqual(entry["status"], "UNSUPPORTED")

    def test_every_registry_metric_appears_in_the_index(self):
        """A metric that vanishes from the UI teaches the operator nothing."""
        builder = mi.MetricIndexBuilder(source_id="lo1-capture",
                                        capability_manifest=self.manifest)
        index = builder.build()
        self.assertTrue(set(mi.registry_metrics()).issubset(index))
        for metric_id, entry in index.items():
            with self.subTest(metric=metric_id):
                self.assertIn(entry["status"], rec.METRIC_STATUSES)
                if entry["status"] != "OK":
                    self.assertTrue(entry["reason"], f"{metric_id} has no reason")


class TheFrozenSurfaceIsFrozen(unittest.TestCase):
    """``session_store.py`` signatures must equal the seam the design froze.

    Why this exists: the surface was checked by ``hasattr`` alone
    (``tests/test_gui_operator_seams.py``) plus a hand-read of
    ``inspect.signature`` output.  Neither catches a *parameter annotation*
    added to a frozen method, so five of them drifted - ``create``,
    ``record_issue``, ``write_summary``, ``read_telemetry`` and ``read_events``
    each acquired an ``Optional[...]`` the seam did not have.  Call sites and
    defaults were unaffected, which is precisely why it went unnoticed and
    precisely why an eyeball check is not a gate.

    Two independent assertions, because they fail on different things:

    * exact ``str(inspect.signature(...))`` equality against the table below,
      transcribed from the seam at the campaign base commit - this catches
      annotation drift, which is what was missed;
    * parameter names, kinds and defaults against
      ``file-ownership.1.0.0.json`` ``moduleInterfaces`` - the machine-readable
      authority, which survives after the base commit is out of reach.
    """

    #: Transcribed from ``gui/operator/store/session_store.py`` at the campaign
    #: base commit ``a4692ff7``, verified equal by AST at transcription time.
    FROZEN = {
        "create": "(runs_root, *, mode: 'str', mode_evidence: 'Mapping[str, Any]',"
                  " profile_id=None, config_snapshot=None) -> \"'SessionStore'\"",
        "open": "(run_dir) -> \"'SessionStore'\"",
        "list_runs": "(runs_root) -> 'list'",
        "finalize": "(self, disposition: 'str') -> 'Mapping[str, Any]'",
        "append_telemetry": "(self, sample: 'Mapping[str, Any]') -> 'None'",
        "append_event": "(self, event: 'Mapping[str, Any]') -> 'None'",
        "append_episode": "(self, episode: 'Mapping[str, Any]') -> 'None'",
        "append_cycle": "(self, cycle: 'Mapping[str, Any]') -> 'None'",
        "append_llm_call": "(self, call: 'Mapping[str, Any]') -> 'None'",
        "record_issue": "(self, kind: 'str', detail: 'str', *, at=None,"
                        " gap_id=None) -> 'None'",
        "copy_raw": "(self, source_id: 'str', src, rel: 'str') -> 'str'",
        "write_metric_index": "(self, index: 'Mapping[str, Any]') -> 'None'",
        "write_summary": "(self, summary: 'Mapping[str, Any]',"
                         " statistics=None) -> 'None'",
        "write_figure": "(self, figure_id: 'str', *, paths: 'Sequence[str]',"
                        " source_csv: 'str', metadata: 'Mapping[str, Any]')"
                        " -> 'None'",
        "save_gui_state": "(self, state: 'Mapping[str, Any]') -> 'None'",
        "read_telemetry": "(self, *, metric=None, scope_id=None)"
                          " -> 'Iterator[Mapping[str, Any]]'",
        "read_events": "(self, *, lane=None) -> 'Iterator[Mapping[str, Any]]'",
        "read_episodes": "(self) -> 'Iterator[Mapping[str, Any]]'",
        "__enter__": "(self) -> \"'SessionStore'\"",
        "__exit__": "(self, *exc: 'Any') -> 'None'",
        # properties: the spec declares them as defs, and they are frozen too
        "run_id": "(self) -> 'str'",
        "run_dir": "(self) -> 'Path'",
        "mode": "(self) -> 'str'",
    }

    @staticmethod
    def _callable(name):
        """The underlying function, whether the attribute is a method or a
        property - a property's signature is its getter's."""
        import inspect

        attribute = inspect.getattr_static(SessionStore, name)
        if isinstance(attribute, property):
            return attribute.fget
        return getattr(SessionStore, name)

    def test_every_frozen_signature_is_byte_identical_including_annotations(self):
        import inspect

        drift = []
        for name, expected in sorted(self.FROZEN.items()):
            actual = str(inspect.signature(self._callable(name)))
            if actual != expected:
                drift.append(f"{name}\n     frozen: {expected}\n     actual: {actual}")
        self.assertEqual(drift, [], "frozen signature drift:\n" + "\n".join(drift))

    def test_the_frozen_set_is_complete(self):
        """A frozen method that vanished from the table would never be checked."""
        from gui.operator.store import session_store as module

        spec = json.loads(
            (REPO_ROOT / "docs" / "phase-b-gui" / "file-ownership.1.0.0.json")
            .read_text(encoding="utf-8"))
        declared = set()
        for line in spec["moduleInterfaces"]["gui/operator/store/session_store.py"]:
            stripped = line.strip()
            if stripped.startswith("def "):
                declared.add(stripped[4:].split("(", 1)[0])
        self.assertTrue(declared.issubset(set(self.FROZEN)),
                        f"unchecked frozen methods: {declared - set(self.FROZEN)}")
        self.assertEqual(module.SCHEMA_VERSION, ss.SCHEMA_VERSION)

    def test_parameters_match_the_ownership_specification(self):
        """Names, kinds and defaults against the machine-readable authority."""
        import inspect
        import re

        spec = json.loads(
            (REPO_ROOT / "docs" / "phase-b-gui" / "file-ownership.1.0.0.json")
            .read_text(encoding="utf-8"))
        mismatches = []
        for line in spec["moduleInterfaces"]["gui/operator/store/session_store.py"]:
            stripped = line.strip()
            if not stripped.startswith("def "):
                continue
            name = stripped[4:].split("(", 1)[0]
            inner = stripped[stripped.index("(") + 1: stripped.rindex(")")]
            expected = []
            for part in re.split(r",(?![^\[]*\])", inner):
                part = part.strip()
                if not part or part in ("self", "cls"):
                    continue
                if part == "*":
                    continue
                token = part.split(":", 1)[0].split("=", 1)[0].strip()
                # *args keeps its name, not its star: inspect reports the name
                token = token.lstrip("*")
                expected.append((token, "=" in part))
            actual = [(p.name, p.default is not inspect.Parameter.empty)
                      for p in inspect.signature(
                          self._callable(name)).parameters.values()
                      if p.name not in ("self", "cls")]
            if expected != actual:
                mismatches.append(f"{name}: spec {expected} != code {actual}")
        self.assertEqual(mismatches, [])

    def test_additive_methods_are_not_mistaken_for_frozen_ones(self):
        """T3 added methods; they are additive and must stay outside the table."""
        for added in ("add_source", "set_llm", "set_contract", "close",
                      "reopen_for_analysis", "finalize_analysis"):
            self.assertTrue(hasattr(SessionStore, added), added)
            self.assertNotIn(added, self.FROZEN)


if __name__ == "__main__":
    unittest.main()
