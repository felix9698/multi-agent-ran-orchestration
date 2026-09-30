import json
import tempfile
import unittest
from pathlib import Path

from oran.o1 import (
    AssuranceCorrelator, DurableJson, HttpResult, NetconfPerfMetricJobManager,
    NotificationReceiver, O1Consumer, O1Error, PmNormalizer, SftpRetriever,
    SubscriptionManager, commit_eligible, recover_files, validate_evidence,
)
from oran.mocks.o1provider import O1ProviderMock


BUNDLE = Path(__file__).resolve().parents[1] / "contracts" / "oran-aic" / "1.0.0" / "shared-contract-bundle"
if not BUNDLE.is_dir():
    raise RuntimeError(f"tracked contract bundle is missing: {BUNDLE}")


def schema_mount_reply(*, mount=True, library=True) -> bytes:
    global_mount = '''<sm:schema-mounts>
      <sm:mount-point><sm:module>_3gpp-common-subnetwork</sm:module><sm:label>children-of-SubNetwork</sm:label><sm:config>true</sm:config><sm:schema-ref><sm:inline/></sm:schema-ref></sm:mount-point>
    </sm:schema-mounts>''' if mount else ""
    mounted_library = '''<sn:SubNetwork><sn:id>oran-lab</sn:id><yl:yang-library/></sn:SubNetwork>''' if library else ""
    return f'''<nc:rpc-reply xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"
        xmlns:sm="urn:ietf:params:xml:ns:yang:ietf-yang-schema-mount"
        xmlns:sn="urn:3gpp:sa5:_3gpp-common-subnetwork"
        xmlns:yl="urn:ietf:params:xml:ns:yang:ietf-yang-library"><nc:data>{global_mount}{mounted_library}</nc:data></nc:rpc-reply>'''.encode()


class O1ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads((BUNDLE / "golden/golden-vectors.1.0.0.json").read_text())
        cls.notify = json.loads((BUNDLE / "golden/o1/notify-file-ready.json").read_text())

    def _normalizer(self):
        expected = self.golden["canonicalObjects"]["afterEvidence"]
        return PmNormalizer(self.golden["canonicalObjects"]["capabilityManifest"], expected["correlation"], expected["policyScope"])

    def _context(self, name="B20260804.0001+0000-0002+0000_-oran-aic-nrcelldu-60s_oai-gnb.xml", ready="2026-08-04T00:02:02Z", retrieved="2026-08-04T00:02:04Z"):
        return {"name": name, "readyAt": ready, "retrievedAt": retrieved, "jobId": "oran-aic-nrcelldu-60s"}

    def _variant(self, base, *, observation_id, window, name, sha256, ready, retrieved, meas_info_id, rru_value, rru_quality, suspect=False):
        expected = json.loads(json.dumps(base))
        expected.update({"observationId": observation_id, "observedAt": window["end"], "window": window, "quality": rru_quality})
        expected["source"]["file"] = {"name": name, "sha256": sha256, "readyAt": ready, "retrievedAt": retrieved}
        expected["source"]["pmRecord"]["measInfoId"] = meas_info_id
        expected["samples"][0].update({"value": rru_value, "quality": rru_quality, "suspect": suspect})
        expected["samples"][1].update({"quality": "OK" if rru_quality == "NOT_AVAILABLE" else rru_quality, "suspect": suspect})
        return expected

    def test_o1_001_exact_golden_normalization(self):
        records = self._normalizer().normalize((BUNDLE / "golden/o1/valid-prb.xml").read_bytes(), self._context(), "2026-08-04T00:02:08Z")
        self.assertEqual(records, [self.golden["canonicalObjects"]["afterEvidence"], self.golden["canonicalObjects"]["afterEvidenceCell2"]])

    def test_null_and_suspect_are_not_commit_eligible(self):
        null = self._normalizer().normalize((BUNDLE / "golden/o1/null-prb.xml").read_bytes(), self._context("null.xml", "2026-08-04T00:04:02Z", "2026-08-04T00:04:04Z"), "2026-08-04T00:04:08Z")
        expected_null = self._variant(self.golden["canonicalObjects"]["afterEvidence"], observation_id="aaa32217-60e1-53a8-8ce8-72a212debf1a", window={"start": "2026-08-04T00:03:00Z", "end": "2026-08-04T00:04:00Z"}, name="null.xml", sha256="60ae726c0d70c4268b2a6c5b8311f041484115d3556efc6c17b3813a56e8dff1", ready="2026-08-04T00:04:02Z", retrieved="2026-08-04T00:04:04Z", meas_info_id="oran-aic-pm-null", rru_value=None, rru_quality="NOT_AVAILABLE")
        self.assertEqual(null, [expected_null])
        self.assertFalse(commit_eligible(null[0]))
        suspect = self._normalizer().normalize((BUNDLE / "golden/o1/suspect-prb.xml").read_bytes(), self._context("suspect.xml", "2026-08-04T00:06:02Z", "2026-08-04T00:06:04Z"), "2026-08-04T00:06:08Z")
        expected_suspect = self._variant(self.golden["canonicalObjects"]["afterEvidence"], observation_id="7b8bf1d9-9407-5727-b4cd-c54c54471ee3", window={"start": "2026-08-04T00:05:00Z", "end": "2026-08-04T00:06:00Z"}, name="suspect.xml", sha256="7c798d39ea80189afd42a1834aefedb8c7d59a820d0a3d0950be73e630250c14", ready="2026-08-04T00:06:02Z", retrieved="2026-08-04T00:06:04Z", meas_info_id="oran-aic-pm-suspect", rru_value=41, rru_quality="SUSPECT", suspect=True)
        self.assertEqual(suspect, [expected_suspect])
        self.assertFalse(commit_eligible(suspect[0]))

    def test_overlap_is_ambiguous_but_preserves_intrinsic_samples(self):
        records = self._normalizer().normalize((BUNDLE / "golden/o1/valid-prb.xml").read_bytes(), self._context(), "2026-08-04T00:02:08Z", action_occurred_at="2026-08-04T00:01:30Z")
        expected = [json.loads(json.dumps(self.golden["canonicalObjects"][key])) for key in ("afterEvidence", "afterEvidenceCell2")]
        for record in expected:
            record.update({"phase": "OVERLAPS_ACTION", "quality": "AMBIGUOUS", "ambiguityReason": "ACTION_WINDOW_OVERLAP"})
        self.assertEqual(records, expected)
        self.assertTrue(all(not commit_eligible(record) for record in records))

    def test_precedence_and_required_sample_coherence_rejected(self):
        record = json.loads(json.dumps(self.golden["canonicalObjects"]["afterEvidence"]))
        record["quality"] = "STALE"; record["samples"][0]["quality"] = "STALE"
        record["samples"][1]["quality"] = "NOT_AVAILABLE"; record["samples"][1]["value"] = None
        with self.assertRaises(O1Error): validate_evidence(record)
        record = json.loads(json.dumps(self.golden["canonicalObjects"]["afterEvidence"]))
        record["quality"] = "STALE"; record["samples"][1]["quality"] = "STALE"
        with self.assertRaises(O1Error): validate_evidence(record)

    def test_notification_temporal_rejection_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            receiver = NotificationReceiver(DurableJson(Path(temp) / "notification.json"))
            bad = json.loads(json.dumps(self.notify)); bad["fileInfoList"][0]["fileExpirationTime"] = "2026-08-04T00:02:01Z"
            with self.assertRaises(O1Error) as raised: receiver.accept(bad)
            self.assertEqual(raised.exception.code, "AIC_O1_TEMPORAL_INVALID")

    def test_subscription_unknown_delete_never_posts_duplicate(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            manager = SubscriptionManager(DurableJson(Path(temp) / "sub.json"), "https://loopback.example/notify", lambda body: calls.append(("POST", body)) or HttpResult(201, {"Location": "/subscriptions/a"}, dict(body)), lambda _sid: HttpResult(None))
            manager.ensure()
            with self.assertRaises(O1Error): manager.recover_uncertain(lambda: calls.append(("LOCK", None)))
            self.assertEqual([name for name, _ in calls].count("POST"), 1)

    def test_unlock_requires_durable_subscription_and_sends_fixture_bytes(self):
        directory = BUNDLE / "golden/o1/netconf"
        rpc = {p.stem: p.read_bytes() for p in directory.glob("*.xml")}
        sent = []
        profile = json.loads((BUNDLE / "o1-netconf-yang-profile.1.0.0.json").read_text())
        manager = NetconfPerfMetricJobManager(rpc, lambda value: schema_mount_reply() if value == rpc["get-schema-mount"] else sent.append(value) or {"ok": True}, profile["transport"]["requiredCapabilities"])
        with self.assertRaises(O1Error): manager.start(profile["transport"]["requiredCapabilities"], lambda: False)
        self.assertNotIn(rpc["unlock-perfmetricjob"], sent)

    def test_schema_mount_requires_both_global_and_mounted_response_branches(self):
        profile = json.loads((BUNDLE / "o1-netconf-yang-profile.1.0.0.json").read_text())
        rpc = {p.stem: p.read_bytes() for p in (BUNDLE / "golden/o1/netconf").glob("*.xml")}
        for mount, library in ((False, True), (True, False)):
            manager = NetconfPerfMetricJobManager(rpc, lambda _payload, mount=mount, library=library: schema_mount_reply(mount=mount, library=library), profile["transport"]["requiredCapabilities"])
            with self.subTest(global_mount=mount, mounted_library=library):
                with self.assertRaises(O1Error) as raised:
                    manager.start(profile["transport"]["requiredCapabilities"], lambda: True)
                self.assertEqual(raised.exception.code, "AIC_O1_SCHEMA_MOUNT_INVALID")

        manager = NetconfPerfMetricJobManager(rpc, lambda payload: schema_mount_reply() if payload == rpc["get-schema-mount"] else {"ok": True}, profile["transport"]["requiredCapabilities"])
        manager.start(profile["transport"]["requiredCapabilities"], lambda: True)
        self.assertEqual(manager.trace[:2], ["HELLO", "get-schema-mount"])

    def _matching_status(self, record):
        return {
            "aicStatus": {key: record["correlation"][key] for key in ("policyId", "policyRevision", "episodeId")},
            "policyScope": json.loads(json.dumps(record["policyScope"])),
            "measurementScope": json.loads(json.dumps(record["measurementScope"])),
            "window": json.loads(json.dumps(record["window"])),
        }

    def test_assurance_correlator_requires_full_join_key_and_excludes_failure(self):
        record = self.golden["canonicalObjects"]["afterEvidence"]
        correlator = AssuranceCorrelator()
        self.assertEqual(correlator.correlate(record, self._matching_status(record)), record)

        variants = {}
        status = self._matching_status(record); status["policyScope"]["ueId"]["guAmfUeNgapId"]["amfUeNgapId"] = 999; variants["ue"] = status
        status = self._matching_status(record); status["measurementScope"]["cellId"]["cId"]["ncI"] = 1; variants["cell"] = status
        status = self._matching_status(record); status["measurementScope"]["managedObjectDn"] += ",bad"; variants["managed-object-dn"] = status
        status = self._matching_status(record); status["window"]["end"] = "2026-08-04T00:02:01Z"; variants["window"] = status
        status = self._matching_status(record); status["aicStatus"]["episodeId"] = "different-episode"; variants["episode"] = status
        status = self._matching_status(record); del status["measurementScope"]["managedObjectDn"]; variants["incomplete-context"] = status
        variants["no-status"] = None
        for field, status in variants.items():
            with self.subTest(field=field):
                result = correlator.correlate(record, status)
                self.assertEqual(result["quality"], "MISSING")
                self.assertTrue(all(sample["quality"] == "MISSING" and sample["value"] is None for sample in result["samples"]))
                self.assertFalse(commit_eligible(result))

    def test_recover_files_classifies_unique_empty_and_ambiguous_window_matches(self):
        raw = (BUNDLE / "golden/o1/valid-prb.xml").read_bytes()
        expected = ("2026-08-04T00:01:00Z", "2026-08-04T00:02:00Z")
        unique = {"id": "unique", "raw": raw}
        self.assertEqual(recover_files([unique], lambda item: item["raw"], expected), ("RECOVERED", raw, unique))
        wrong_window = {"id": "wrong", "raw": (BUNDLE / "golden/o1/null-prb.xml").read_bytes()}
        self.assertEqual(recover_files([wrong_window], lambda item: item["raw"], expected), ("EMPTY", None, None))
        duplicate = {"id": "duplicate", "raw": raw}
        self.assertEqual(recover_files([unique, duplicate], lambda item: item["raw"], expected), ("AMBIGUOUS", None, None))

    def test_provider_mock_subscription_notifications_pm_files_sftp_and_faults(self):
        provider = O1ProviderMock(subscription_path="/subscriptions", dev_loopback_insecure=True)
        created = provider.dispatch("POST", "/subscriptions", {"consumerReference": "https://consumer.example/notify", "timeTick": 0})
        self.assertEqual(created.status, 201)
        self.assertTrue(created.headers["Location"].startswith("/subscriptions/"))

        sent = []
        self.assertEqual(provider.send_notification(self.notify, lambda notification: sent.append(notification) or 204), 204)
        preparation_error = {"notificationType": "notifyFilePreparationError", "eventTime": "2026-08-04T00:02:02Z", "systemDN": self.notify["systemDN"]}
        self.assertEqual(provider.send_notification(preparation_error, lambda notification: sent.append(notification) or 204), 204)
        self.assertEqual(sent, [self.notify, preparation_error])

        raw = provider.generate_pm_xml(window_start="2026-08-04T00:01:00Z", window_end="2026-08-04T00:02:00Z", job_id="oran-aic-nrcelldu-60s", measurements={"GNBDUFunction=oai-du,NRCellDU=1": {"RRU.PrbDl": 37, "DRB.UEThpDl": None}})
        self.assertIn(b'<?xml-stylesheet type="text/xsl" href="MeasDataCollection.xsl"?>', raw)
        self.assertIn(b'<measCollecFile xmlns="http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec">', raw)
        info = dict(self.notify["fileInfoList"][0]); info["fileSize"] = len(raw)
        provider.add_file(info, raw)
        later = dict(info); later.update({"fileLocation": "sftp://oai-gnb.example/pm/later.xml", "fileReadyTime": "2026-08-04T00:03:00Z"})
        provider.add_file(later, raw)
        listed = provider.dispatch("GET", "/FileDataReportingMnS/v1/files", query={"fileDataType": "PERFORMANCE", "beginTime": "2026-08-04T00:02:02Z", "endTime": "2026-08-04T00:03:00Z"})
        self.assertEqual(listed.status, 200)
        self.assertEqual(listed.body, [info])

        retriever = SftpRetriever(provider.fetch_sftp, {"oai-gnb.example"}, {"oai-gnb.example": "test-pin"}, sleep=lambda _seconds: None)
        self.assertEqual(retriever.retrieve(info, "2026-08-04T00:02:03Z"), raw)
        harness_retrieval = provider.harness_op({
            "op": "O1_SFTP_RETRIEVE", "source": info["fileLocation"]})["outputs"]
        self.assertNotEqual(info["fileReadyTime"], harness_retrieval["retrievedAt"])
        self.assertLess(harness_retrieval["retrievedAt"], info["fileExpirationTime"])
        provider.harness_fault({"fault": "DROP_O1_NOTIFICATION"})
        self.assertIsNone(provider.send_notification(self.notify, lambda _notification: 204))
        provider.harness_fault({"fault": "FLIP_RETRIEVED_BYTE", "boundary": {"byteOffset": 0}})
        self.assertNotEqual(provider.fetch_sftp(info["fileLocation"], 5000, 30000), raw)
        provider.harness_fault({"fault": "TLS_HANDSHAKE_REJECT"})
        with self.assertRaises(O1Error) as raised:
            provider.fetch_sftp(info["fileLocation"], 5000, 30000)
        self.assertEqual(raised.exception.code, "AIC_O1_TLS_HANDSHAKE_REJECT")
        provider.harness_restart({"component": "O1_PROVIDER"})
        provider.harness_fault({"fault": "DROP_HTTP_RESPONSE"})
        self.assertIsNone(provider.delete_subscription(created.headers["Location"].rsplit("/", 1)[-1]).status)

    def test_consumer_does_not_expose_provider_owned_fault_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            subscription = SubscriptionManager(DurableJson(Path(temp) / "sub.json"), "https://consumer.example/notify", lambda _body: HttpResult(201, {"Location": "/subscriptions/id"}, {}), lambda _sid: HttpResult(204))
            consumer = O1Consumer(subscription, NotificationReceiver(DurableJson(Path(temp) / "notifications.json")), dev_loopback_insecure=True)
            self.assertEqual(consumer.dispatch("POST", "/harness/fault", {"fault": "TLS_HANDSHAKE_REJECT"}).status, 404)
