"""The lo1-capture replay source: fixture integrity, schema binding, resolution.

Hermetic.  Everything here reads ``tests/gui/fixtures/`` and a temp directory;
nothing touches the read-only staging area the capture was taken from, hardware,
an LLM API or the network.  (The staging path is recorded in
``tests/gui/fixtures/PROVENANCE.md``; a host checkout path in a ``.py`` file
under ``tests/`` is what ``tests/test_portability.py`` fails closed on, and
rightly - a test that needs one is not portable.)

Three properties are pinned, and each of them is a rule the campaign would
otherwise be able to lose quietly:

1. **The golden fixture is still a real capture.**  It is re-validated against
   the vendored schema on every run and its artifact digests are re-checked, so
   a fixture edit that made it convenient but no longer faithful fails here
   rather than in a workspace three tracks later.
2. **Binding is by declaration.**  The two vendored capture schemas have
   *identical* required key sets - only the ``schemaVersion`` const differs - so
   an adapter that matched on shape would read a future capture as a 2.0.0 one.
   An unknown or absent version is refused, and the refusal names what was
   observed and what is supported.
3. **Resolution goes through the manifest.**  Packaging renames every artifact.
   A glob-based resolver appears to work on the staged layout and silently loses
   files on the packaged one, so the bundle case is tested with names that
   deliberately do not match the staged ones.
"""

import copy
import hashlib
import json
import shutil
import unittest
from pathlib import Path

from gui.operator.sources.adapters import lo1_capture as lo1

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "gui" / "fixtures"
GOLDEN = FIXTURES / "lo1-capture-min"
NEGATIVE = FIXTURES / "lo1-capture-negative"

#: Documented in fixtures/PROVENANCE.md: two binary wire dumps and the release
#: manifest are referenced by the capture and deliberately not committed.
DOCUMENTED_OMISSIONS = {
    "raw/netconf/session-client-to-server.bin",
    "raw/netconf/session-server-to-client.bin",
    "raw/release/RELEASE-MANIFEST.json",
}


def _golden_document():
    path = GOLDEN / "capture-sc084-min.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _pointer_apply(document, mutation):
    """Apply one RFC 6901-style mutation descriptor in place."""
    parts = [p for p in mutation["pointer"].split("/") if p != ""]
    node = document
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    leaf = parts[-1]
    key = int(leaf) if isinstance(node, list) else leaf
    if mutation["op"] == "set":
        node[key] = mutation["value"]
    elif mutation["op"] == "remove":
        del node[key]
    else:                                    # pragma: no cover - descriptor typo
        raise AssertionError(f"unknown mutation op {mutation['op']!r}")


def _materialize(document, destination: Path) -> Path:
    """Write a mutated capture into a staged-layout directory."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in GOLDEN.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, destination / entry.name, dirs_exist_ok=True)
    (destination / "capture.json").write_text(
        json.dumps(document, indent=1, sort_keys=True), encoding="utf-8")
    return destination


def _build_bundle(destination: Path) -> Path:
    """Package the golden capture the way the release tooling would.

    Every artifact is renamed, so a resolver that globs on the staged filename
    finds nothing.  Only the ``files{}`` map, matched by content digest, resolves.
    """
    destination.mkdir(parents=True, exist_ok=True)
    evidence = destination / "evidence"
    (evidence / "netconf").mkdir(parents=True, exist_ok=True)
    (evidence / "o1").mkdir(parents=True, exist_ok=True)

    capture_bytes = (GOLDEN / "capture-sc084-min.json").read_bytes()
    (evidence / "capture.json").write_bytes(capture_bytes)
    files = {"evidence/capture.json": {
        "sha256": hashlib.sha256(capture_bytes).hexdigest(),
        "byteCount": len(capture_bytes)}}

    ordinal = 0
    for src in sorted(GOLDEN.rglob("*")):
        if not src.is_file() or src.name == "capture-sc084-min.json":
            continue
        if src.name == ".gitkeep":
            continue
        rel = src.relative_to(GOLDEN).as_posix()
        payload = src.read_bytes()
        if rel.startswith("raw/o1/pm-"):
            member = "evidence/o1/pm-00.xml"
        elif rel.startswith("raw/o1/notification-"):
            member = "evidence/o1/notification-00.json"
        elif rel.startswith("raw/netconf/"):
            member = f"evidence/netconf/wire-{ordinal:02d}.xml"
            ordinal += 1
        else:
            member = f"evidence/release/asset-{ordinal:02d}.json"
            ordinal += 1
        target = destination / member
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        files[member] = {"sha256": hashlib.sha256(payload).hexdigest(),
                         "byteCount": len(payload)}

    manifest = {
        "schemaVersion": "oran-aic-upper-live-o1-harness-evidence-manifest/1.0.0",
        "evidenceId": "upper-live-o1-harness-evidence",
        "capturePath": "evidence/capture.json",
        "captureSha256": files["evidence/capture.json"]["sha256"],
        "evidenceFileCount": len(files),
        "files": files,
    }
    (destination / "EVIDENCE-MANIFEST.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return destination


def _artifact_references(node, found=None):
    if found is None:
        found = {}
    if isinstance(node, dict):
        for key in ("artifactPath", "rawArtifactPath"):
            rel = node.get(key)
            if isinstance(rel, str) and rel:
                digest = node.get("sha256") or node.get("byteSha256")
                found.setdefault(rel, digest if isinstance(digest, str) else None)
        for value in node.values():
            _artifact_references(value, found)
    elif isinstance(node, list):
        for value in node:
            _artifact_references(value, found)
    return found


class GoldenFixtureIntegrity(unittest.TestCase):
    """The committed excerpt must remain a faithful capture."""

    def setUp(self):
        self.document = _golden_document()

    def test_fixture_validates_against_its_declared_schema(self):
        self.assertEqual(lo1.validate_capture(self.document),
                         "oran-aic-upper-live-o1-harness-capture/2.0.0")

    def test_every_referenced_artifact_resolves_or_is_documented(self):
        references = _artifact_references(self.document)
        self.assertGreater(len(references), 20)
        missing = {rel for rel in references if not (GOLDEN / rel).is_file()}
        self.assertEqual(missing, DOCUMENTED_OMISSIONS,
                         "fixture omissions must match fixtures/PROVENANCE.md")

    def test_committed_artifact_digests_match_the_capture(self):
        """Redaction changed three artifacts; the capture was re-sealed for them.

        Content-addressed bundle resolution depends on this, so a fixture whose
        recorded digest and bytes disagree is not usable at all.
        """
        for rel, digest in _artifact_references(self.document).items():
            path = GOLDEN / rel
            if not path.is_file() or digest is None:
                continue
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             digest, f"digest drift for {rel}")

    def test_no_committed_file_carries_the_lab_address_or_staging_path(self):
        for path in GOLDEN.rglob("*"):
            if not path.is_file():
                continue
            payload = path.read_bytes()
            self.assertNotIn(b"192.168.0.50", payload, f"lab address in {path.name}")
            self.assertNotIn(b"a1p-stage", payload, f"staging path in {path.name}")

    def test_the_capture_asserts_no_secret_value_was_captured(self):
        redaction = self.document["redaction"]
        self.assertFalse(redaction["secretValuesCaptured"])
        self.assertFalse(redaction["credentialMaterialCaptured"])
        # opaque references are retained on purpose - names, never values
        self.assertIn("secret://sftp", redaction["secretReferencesObserved"])

    def test_the_load_bearing_content_survived_the_excerpt(self):
        """What the workspaces actually read must still be there."""
        self.assertEqual(len(self.document["normalization"]["records"]), 2)
        self.assertEqual(
            {r["value"] for r in self.document["normalization"]["records"]},
            {30, 40})
        self.assertEqual(len(self.document["coordinator"]["fsmHistory"]), 7)
        self.assertEqual(len(self.document["netconf"]["readiness"]["states"]), 5)
        self.assertEqual(self.document["run"]["orderingRule"],
                         "MONOTONIC_OBSERVED_THEN_ARRAY_ORDER")


class SchemaBinding(unittest.TestCase):
    """Bind by the version the document declares, never by its shape."""

    def test_both_vendored_versions_are_bindable(self):
        self.assertEqual(
            set(lo1.supported_schema_versions()),
            {"oran-aic-upper-live-o1-harness-capture/1.0.0",
             "oran-aic-upper-live-o1-harness-capture/2.0.0"})
        for version in lo1.supported_schema_versions():
            schema = lo1.load_schema(version)
            self.assertEqual(schema["properties"]["schemaVersion"]["const"], version)

    def test_the_two_schemas_are_indistinguishable_by_shape(self):
        """This is *why* binding is by declaration; if it stops being true the
        reasoning in the adapter docstring should be revisited, not the rule."""
        one = lo1.load_schema("oran-aic-upper-live-o1-harness-capture/1.0.0")
        two = lo1.load_schema("oran-aic-upper-live-o1-harness-capture/2.0.0")
        self.assertEqual(set(one["required"]), set(two["required"]))

    def test_unknown_version_is_refused_and_names_both_sides(self):
        document = _golden_document()
        document["schemaVersion"] = "oran-aic-upper-live-o1-harness-capture/3.0.0"
        with self.assertRaises(lo1.CaptureError) as raised:
            lo1.validate_capture(document)
        self.assertEqual(raised.exception.kind, "SCHEMA_MISMATCH")
        self.assertIn("3.0.0", raised.exception.detail)
        self.assertIn("2.0.0", raised.exception.detail)

    def test_absent_version_fails_closed_rather_than_guessing(self):
        document = _golden_document()
        del document["schemaVersion"]
        with self.assertRaises(lo1.CaptureError) as raised:
            lo1.bind_schema(document)
        self.assertIn("schemaVersion", raised.exception.detail)

    def test_a_non_object_document_is_refused(self):
        with self.assertRaises(lo1.CaptureError):
            lo1.bind_schema([1, 2, 3])


class SchemaNegativeCases(unittest.TestCase):
    """Every committed negative descriptor, applied to the golden capture."""

    def setUp(self):
        self.cases = {}
        for path in sorted(NEGATIVE.glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            self.cases[case["case"]] = case
        self.assertTrue(self.cases, "no negative descriptors committed")

    def _mutated(self, name):
        document = _golden_document()
        for mutation in self.cases[name]["mutations"]:
            _pointer_apply(document, mutation)
        return document

    def test_every_descriptor_states_a_reason_and_an_expectation(self):
        for name, case in self.cases.items():
            self.assertTrue(case.get("why"), f"{name}: no rationale")
            self.assertTrue(case.get("expect"), f"{name}: no expectation")
            self.assertTrue(case.get("mutations"), f"{name}: no mutation")

    def test_refusing_cases_refuse_with_the_declared_kind_and_detail(self):
        for name, case in self.cases.items():
            expect = case["expect"]
            if expect.get("loads"):
                continue
            with self.subTest(case=name):
                with self.assertRaises(lo1.CaptureError) as raised:
                    lo1.validate_capture(self._mutated(name))
                self.assertEqual(raised.exception.kind, expect["kind"])
                for fragment in expect.get("detailContains", ()):
                    self.assertIn(fragment, raised.exception.detail)

    def test_accepting_cases_still_validate(self):
        """A capture may be *valid* and still not be a success.

        ``aborted-disposition`` is the case that matters: it satisfies the schema
        completely, and it must still never be summarized as COMPLETED.  That
        distinction is made downstream, not by refusing the document.
        """
        for name, case in self.cases.items():
            if not case["expect"].get("loads"):
                continue
            with self.subTest(case=name):
                lo1.validate_capture(self._mutated(name))

    def test_a_truncated_document_is_a_data_issue_not_a_crash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "truncated"
            root.mkdir()
            payload = (GOLDEN / "capture-sc084-min.json").read_bytes()[:4096]
            (root / "capture.json").write_bytes(payload)
            with self.assertRaises(lo1.CaptureError) as raised:
                lo1.CaptureSource.open(root)
            self.assertEqual(raised.exception.kind, "DATA_TRUNCATED")


class ArtifactResolution(unittest.TestCase):
    """Staged directory and packaged bundle must resolve to the same bytes."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_staged_layout_opens_and_resolves(self):
        source = lo1.CaptureSource.open(GOLDEN)
        self.assertEqual(source.layout, lo1.LAYOUT_STAGED)
        self.assertEqual(source.run_id, "sc084-pc1-upper-109-20260813T141759Z")
        record = source.document["normalization"]["records"][0]
        payload = source.read_bytes(record["rawArtifactPath"])
        self.assertIsNotNone(payload)
        self.assertIn(b"RRU.PrbDl", payload)

    def test_bundle_layout_resolves_through_the_manifest_not_a_glob(self):
        bundle = _build_bundle(self.tmp / "bundle")
        source = lo1.CaptureSource.open(bundle)
        self.assertEqual(source.layout, lo1.LAYOUT_BUNDLE)

        record = source.document["normalization"]["records"][0]
        staged_rel = record["rawArtifactPath"]
        # the staged name does not exist in the bundle at all
        self.assertFalse((bundle / staged_rel).exists())
        payload = source.read_bytes(staged_rel)
        self.assertIsNotNone(payload, "manifest resolution lost the PM artifact")
        self.assertEqual(payload, (GOLDEN / staged_rel).read_bytes())

    def test_both_layouts_agree_on_every_resolvable_artifact(self):
        bundle = _build_bundle(self.tmp / "bundle")
        staged = lo1.CaptureSource.open(GOLDEN)
        packed = lo1.CaptureSource.open(bundle)
        resolved = 0
        for rel in staged.artifact_references():
            left, right = staged.read_bytes(rel), packed.read_bytes(rel)
            self.assertEqual(left, right, f"layout disagreement for {rel}")
            resolved += left is not None
        self.assertGreater(resolved, 20)

    def test_a_missing_artifact_resolves_to_none_rather_than_raising(self):
        source = lo1.CaptureSource.open(GOLDEN)
        for rel in DOCUMENTED_OMISSIONS:
            self.assertIsNone(source.resolve(rel))

    def test_the_adapter_never_walks_out_of_the_capture_directory(self):
        """The parent run directory holds pki/ and secrets/ material."""
        source = lo1.CaptureSource.open(GOLDEN)
        for escape in ("../secrets/id_ed25519", "../../etc/passwd",
                       "raw/../../pki/ca.pem"):
            with self.subTest(rel=escape):
                with self.assertRaises(lo1.CaptureError):
                    source.resolve(escape)

    def test_a_directory_without_a_capture_is_refused(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(lo1.CaptureError) as raised:
            lo1.CaptureSource.open(empty)
        self.assertEqual(raised.exception.kind, "CONNECTION_LOST")

    def test_two_capture_documents_are_ambiguous_and_refused(self):
        root = _materialize(_golden_document(), self.tmp / "double")
        shutil.copy(root / "capture.json", root / "capture-second.json")
        with self.assertRaises(lo1.CaptureError):
            lo1.CaptureSource.open(root)


class CaptureNormalization(unittest.TestCase):
    """What the golden capture becomes once it is a SessionStore run."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        from gui.operator.sources import replay

        cls._tmp = tempfile.TemporaryDirectory()
        manifest = json.loads(
            (FIXTURES / "capability-manifest-min.json").read_text(encoding="utf-8"))
        cls.store = replay.load(GOLDEN, Path(cls._tmp.name),
                                capability_manifest=manifest)
        cls.manifest = cls.store.read_manifest()
        cls.events = list(cls.store.read_events())
        cls.samples = list(cls.store.read_telemetry())
        cls.index = cls.store.read_metric_index()
        cls.summary = cls.store.read_summary()

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls._tmp.cleanup()

    # -- mode ------------------------------------------------------------- #

    def test_the_run_is_replay_and_cannot_be_anything_else(self):
        self.assertEqual(self.manifest["mode"], "REPLAY")
        self.assertEqual(self.manifest["modeEvidence"]["basis"],
                         "REPLAY_OF_RECORDED_SOURCE")
        self.assertEqual(self.manifest["modeEvidence"]["sourceRunIds"],
                         ["sc084-pc1-upper-109-20260813T141759Z"])
        self.assertTrue(self.manifest["runId"].startswith("replay-"))

    def test_the_capture_disposition_maps_to_completed_only_for_completed(self):
        from gui.operator.sources.adapters import lo1_capture as adapter

        self.assertEqual(self.manifest["disposition"], "COMPLETED")
        self.assertEqual(set(adapter._DISPOSITION_MAP), {"COMPLETED"})

    # -- timeline --------------------------------------------------------- #

    def test_all_event_arrays_merge_into_one_ordered_timeline(self):
        self.assertEqual(len(self.events), 67)
        self.assertEqual([e["seq"] for e in self.events],
                         list(range(len(self.events))))
        observed = [e["tUtc"] for e in self.events if e["tUtc"]]
        self.assertEqual(observed, sorted(observed),
                         "observed events must be in observation order")

    def test_every_lane_the_capture_can_populate_is_present(self):
        lanes = {e["lane"] for e in self.events}
        self.assertEqual(lanes, {"SESSION", "R1", "A1", "DME", "O1",
                                 "COORDINATOR", "TEARDOWN"})

    def test_a_dme_endpoint_is_not_filed_under_r1_merely_for_containing_r1(self):
        """r1DmePushDestination contains both tokens; it is a DME exchange."""
        dme = [e for e in self.events if e["lane"] == "DME"]
        self.assertTrue(dme)
        for event in dme:
            self.assertIn("dme", str(event["detail"]["endpointRef"]).lower())

    def test_untimestamped_fsm_transitions_are_derived_and_carry_no_time(self):
        derived = [e for e in self.events if e["kind"] == "FSM_TRANSITION"]
        self.assertEqual(len(derived), 7)
        for event in derived:
            self.assertEqual(event["origin"], "DERIVED")
            self.assertIsNone(event["tUtc"])
            self.assertIn("GAP-12", event["derivation"])
        self.assertEqual([e["detail"]["to"] for e in derived],
                         ["S0", "S1", "S2", "S3", "S4", "S6", "S0"])

    def test_derived_transitions_follow_the_operation_that_produced_them(self):
        intent = next(e for e in self.events
                      if e["kind"] == "COORDINATOR_PROCESS_INTENT")
        first_derived = next(e for e in self.events
                             if e["kind"] == "FSM_TRANSITION")
        self.assertGreater(first_derived["seq"], intent["seq"])

    def test_exchange_bodies_are_marked_unsupported_rather_than_faked(self):
        exchanges = [e for e in self.events if e["kind"].startswith("HTTP_")]
        self.assertTrue(exchanges)
        for event in exchanges:
            self.assertFalse(event["detail"]["bodyAvailable"])
            self.assertEqual(event["detail"]["gapId"], "GAP-04")

    def test_the_created_policy_id_is_correlated_onto_its_event(self):
        created = [e for e in self.events if e["ids"]["policyId"]]
        self.assertTrue(created)
        self.assertEqual(created[0]["ids"]["policyId"],
                         "4811fa7f-923f-4c54-95dd-e6d2d542177f")

    # -- telemetry -------------------------------------------------------- #

    def test_the_two_prb_scalars_carry_window_aggregation_and_quality(self):
        prb = [s for s in self.samples if s["metric"] == "RRU.PrbDl"]
        self.assertEqual(len(prb), 2)
        self.assertEqual({s["value"] for s in prb}, {30, 40})
        self.assertEqual({s["scope"]["id"] for s in prb},
                         {"NRCellDU=1", "NRCellDU=2"})
        for sample in prb:
            self.assertEqual(sample["aggregation"], "PERIOD_MEAN")
            self.assertEqual(sample["window"]["start"], "2026-08-13T14:19:00Z")
            self.assertEqual(sample["quality"], "OK")
            self.assertEqual(sample["ageMs"], 585)
            self.assertEqual(sample["source"]["boundary"], "O1_ASSURANCE")
            self.assertTrue(sample["source"]["artifactRef"].endswith(".xml"))

    def test_the_drill_down_artifact_was_copied_into_the_run(self):
        prb = next(s for s in self.samples if s["metric"] == "RRU.PrbDl")
        rel = prb["source"]["artifactRef"]
        self.assertTrue((self.store.run_dir / rel).is_file())
        self.assertIn(b"RRU.PrbDl", (self.store.run_dir / rel).read_bytes())

    def test_two_points_are_not_a_time_series(self):
        """The registry says so explicitly, and the metric index carries n."""
        self.assertEqual(self.index["RRU.PrbDl"]["sampleCount"], 2)
        self.assertLess(self.index["RRU.PrbDl"]["sampleCount"], 3,
                        "fewer than three samples renders as discrete points")

    def test_policy_and_evidence_counts_are_recorded_before_and_after(self):
        counts = [s for s in self.samples if s["metric"] == "policy_count"]
        self.assertEqual([s["value"] for s in counts], [0.0, 1.0])
        evidence = [s for s in self.samples
                    if s["metric"] == "evidence_record_count"]
        self.assertEqual([s["value"] for s in evidence], [0.0, 2.0])

    # -- metric availability ---------------------------------------------- #

    def test_the_subscribed_but_undelivered_metric_is_unavailable_with_a_reason(self):
        entry = self.index["DRB.UEThpDl"]
        self.assertEqual(entry["status"], "UNAVAILABLE")
        self.assertEqual(entry["gapId"], "GAP-05")
        self.assertIn("PerfMetricJob", entry["reason"])
        self.assertEqual(entry["sampleCount"], 0)

    def test_radio_and_iq_metrics_are_unsupported_here(self):
        for metric, gap in (("ue_dl_ss_rsrp_dbm", "GAP-06"),
                            ("gnb_ul_avg_rsrp_dbm", "GAP-06"),
                            ("ue_dl_sinr_db", "GAP-06"),
                            ("mcs", "GAP-07"),
                            ("serving_cell", "GAP-09"),
                            ("constellation_iq", "GAP-10")):
            with self.subTest(metric=metric):
                self.assertEqual(self.index[metric]["status"], "UNSUPPORTED")
                self.assertEqual(self.index[metric]["gapId"], gap)

    def test_no_metric_is_silently_absent(self):
        for metric_id, entry in self.index.items():
            with self.subTest(metric=metric_id):
                if entry["status"] != "OK":
                    self.assertTrue(entry["reason"], f"{metric_id} has no reason")

    # -- decision --------------------------------------------------------- #

    def test_the_single_episode_maps_to_its_eq12_state(self):
        episodes = list(self.store.read_episodes())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["terminalOutcome"], "commit_original")
        self.assertEqual(episodes[0]["eq12State"], "Admitted")
        self.assertEqual(episodes[0]["fsmPath"],
                         ["S0", "S0", "S1", "S2", "S3", "S4", "S6", "S0"])

    def test_absent_decision_inputs_are_null_not_zero(self):
        cycles = list(self.store.read_cycles())
        self.assertEqual(len(cycles), 1)
        for field in ("rawConfidence", "calibratedProbability", "threshold",
                      "feasible", "routedTo"):
            self.assertIsNone(cycles[0][field], field)
        self.assertEqual(cycles[0]["thresholdAppliedTo"],
                         "calibrated_probability")
        for value in cycles[0]["latencies"].values():
            self.assertIsNone(value)

    def test_no_llm_call_is_invented_for_a_capture(self):
        self.assertEqual(list(self.store.read_llm_calls()), [])

    def test_the_conflict_verdict_stays_unknown(self):
        """GAP-03: the coordinator does not persist the S1 screening result."""
        episodes = list(self.store.read_episodes())
        self.assertIsNone(episodes[0]["hasConflict"])

    # -- issues, readiness, provenance ------------------------------------ #

    def test_the_documented_omissions_become_dangling_reference_issues(self):
        dangling = [i for i in self.manifest["dataIssues"]
                    if i["kind"] == "DANGLING_REFERENCE"]
        details = " ".join(i["detail"] for i in dangling)
        for rel in DOCUMENTED_OMISSIONS:
            self.assertIn(rel, details)

    def test_the_teardown_removed_state_is_recorded_with_its_reason(self):
        durable = [i for i in self.manifest["dataIssues"]
                   if "durableStatePath" in i["detail"]]
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0]["gapId"], "GAP-14")
        self.assertIn("teardown", durable[0]["detail"])

    def test_the_five_readiness_segments_reach_the_summary(self):
        readiness = self.summary["readiness"]
        self.assertEqual([s["segmentId"] for s in readiness["segments"]],
                         ["PRECHECK", "TRUST_READY", "SUBSCRIBED",
                          "JOB_ACTIVE", "ASSURANCE_READY"])
        self.assertTrue(readiness["complete"])
        self.assertEqual(readiness["unmet"], [])
        for segment in readiness["segments"]:
            self.assertTrue(segment["confirmed"])
            self.assertTrue(segment["at"])

    def test_the_provenance_panel_carries_the_gate_and_external_call_record(self):
        provenance = self.summary["provenance"]
        self.assertEqual(provenance["gateResult"], "ADMITTED")
        self.assertEqual(len(provenance["checks"]), 10)
        self.assertTrue(all(c["outcome"] == "PASS" for c in provenance["checks"]))
        # the machine-checked form of the boundary this console also obeys
        self.assertEqual(provenance["externalCalls"]["hardwareCalls"], 0)
        self.assertEqual(provenance["externalCalls"]["externalLiveTargetCalls"], 0)
        self.assertEqual(provenance["externalCalls"]["forbiddenEgressAttempts"], 0)
        self.assertTrue(provenance["externalCalls"]["guardInstalled"])

    def test_secret_references_are_names_and_nothing_else(self):
        provenance = self.summary["provenance"]
        self.assertIn("secret://sftp", provenance["secretReferencesObserved"])
        self.assertFalse(provenance["secretValuesCaptured"])
        for path in self.store.run_dir.rglob("*"):
            if path.is_file():
                payload = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", payload)

    def test_the_capture_directory_was_not_written_to(self):
        for path in GOLDEN.rglob("*"):
            self.assertNotIn("session-client-to-server.bin", str(path.name),
                             "omitted artifacts must stay omitted")


class BundleNormalization(unittest.TestCase):
    """A packaged bundle normalizes to the same run a staged directory does.

    The adapter resolves artifacts differently in the two layouts, so "both
    layouts return the same bytes" is not by itself enough: the *run* they
    produce has to agree too, or a replay of a packaged evidence bundle would
    quietly be a different run from a replay of the staged capture it came from.
    """

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_packaged_bundle_produces_an_equivalent_run(self):
        from gui.operator.sources import replay

        bundle = _build_bundle(self.tmp / "bundle")
        staged_store = replay.load(GOLDEN, self.tmp / "runs")
        packed_store = replay.load(bundle, self.tmp / "runs")
        self.addCleanup(staged_store.close)
        self.addCleanup(packed_store.close)

        self.assertEqual(packed_store.mode, "REPLAY")
        self.assertEqual(list(packed_store.read_telemetry()),
                         list(staged_store.read_telemetry()))
        self.assertEqual(list(packed_store.read_events()),
                         list(staged_store.read_events()))
        self.assertEqual(list(packed_store.read_episodes()),
                         list(staged_store.read_episodes()))
        self.assertEqual(packed_store.read_metric_index(),
                         staged_store.read_metric_index())

        # only the provenance differs, and it differs honestly
        packed_source = packed_store.read_manifest()["sources"][0]
        staged_source = staged_store.read_manifest()["sources"][0]
        self.assertEqual(packed_source["kind"], "LO1_EVIDENCE_BUNDLE")
        self.assertEqual(staged_source["kind"], "LO1_CAPTURE")
        self.assertEqual(packed_source["digest"], staged_source["digest"])


if __name__ == "__main__":
    unittest.main()
