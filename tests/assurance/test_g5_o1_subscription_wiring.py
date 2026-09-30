"""Gate 5 stage 2: lifecycle ordinal 5, the FileDataReportingMnS subscription.

``o1-netconf-yang-profile.1.0.0.json#/lifecycle`` ordinal 5 carries an
``externalPrecondition`` rather than a ``requestFixture`` because it is HTTPS,
not NETCONF.  The first version of ``tools/g5ota/perfmetricjob.py`` filtered
fixture-less ordinals out of the lifecycle, which is exactly how the step came
to be skipped -- and the managed element then refused the unlock, because its
sysrepo agent runs with ``--subscription-ready`` and will not turn a measurement
job on without one.

These tests are hermetic: no HTTPS, no NETCONF, no deployment secrets.  They
check the two things that can be wrong without a network -- that the request
comes from the frozen profile rather than from this tool's idea of a
subscription, and that "durable" is answered from the filesystem.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from oran.release.lo1.o1_netconf import SubscriptionError
from tools.g5ota import filedatasub

#: A values map with the shape ``load_integration_values`` produces, carrying
#: only what this seam reads.  The paths are never opened: the TLS and OAuth
#: constructors are replaced below, because building a real context would make
#: the test depend on one machine's secret store.
VALUES: Mapping[str, Any] = {
    "o1.fileDataReporting.mnsRoot": "https://mns.invalid:8443/o1",
    "o1.fileDataReporting.mnsVersion": "v1",
    "o1.fileDataReporting.consumerReference": "https://smo.invalid/notify",
    "o1.https.truststoreRef": "file:///secrets/ca.crt",
    "o1.https.consumerClientCertificateRef": "file:///secrets/consumer.crt",
    "o1.https.consumerClientPrivateKeyRef": "file:///secrets/consumer.key",
    "o1.https.consumerOauthTokenEndpoint": "https://oauth.invalid/oauth2/token",
    "o1.https.consumerOauthClientId": "smo-o1-consumer",
    "o1.https.consumerOauthCredentialRef": "file:///secrets/o1/inbound-jwt-hmac.secret",
    "o1.https.consumerOauthAudience": "oran-aic-o1-provider",
    "o1.https.consumerOauthScope": "o1:file-data-reporting",
}


class _Recorder:
    """Stands in for the TLS and OAuth constructors, remembering the arguments."""

    def __init__(self) -> None:
        self.tls_profiles: list[Mapping[str, Any]] = []
        self.oauth_profiles: list[Mapping[str, Any]] = []

    def client_context(self, profile, _resolver):
        self.tls_profiles.append(dict(profile))
        return object()

    def oauth(self, profile, _resolver, timeout=30.0):
        self.oauth_profiles.append(dict(profile))
        return type("_Credentials", (), {
            "authorization_header": staticmethod(lambda: "Bearer test")})()


class SubscriptionWiringFixture(unittest.TestCase):

    def setUp(self) -> None:
        self.recorder = _Recorder()
        self._saved = (filedatasub.client_context,
                       filedatasub.OAuthClientCredentials)
        filedatasub.client_context = self.recorder.client_context
        filedatasub.OAuthClientCredentials = self.recorder.oauth
        self.addCleanup(self._restore)
        self.directory = Path(tempfile.mkdtemp())
        self.state_path = self.directory / "o1-subscription.json"

    def _restore(self) -> None:
        (filedatasub.client_context,
         filedatasub.OAuthClientCredentials) = self._saved

    def build(self):
        return filedatasub.build_subscription(VALUES, state_path=self.state_path)


class TheRequestComesFromTheFrozenProfile(SubscriptionWiringFixture):

    def test_the_collection_and_item_resources_are_the_profile_s(self) -> None:
        subscription = self.build()
        self.assertEqual(
            subscription.collection_uri,
            "https://mns.invalid:8443/o1/FileDataReportingMnS/v1/subscriptions")
        self.assertEqual(
            subscription.item_uri("abc"),
            "https://mns.invalid:8443/o1/FileDataReportingMnS/v1/subscriptions/abc")

    def test_the_profile_is_read_rather_than_restated(self) -> None:
        frozen = json.loads(
            filedatasub.PA_FILE_PROFILE.read_text(encoding="utf-8"))
        expected = frozen["delivery"]["subscription"]
        subscription = self.build()
        self.assertEqual(subscription.subscription_profile, expected)
        # The ordering the whole exercise turned on.
        self.assertTrue(expected["consumerCreatesBeforeJobUnlock"])
        self.assertTrue(expected["consumerPersistsSubscriptionId"])
        self.assertEqual(expected["createSuccessStatus"], 201)

    def test_the_consumer_reference_is_the_deployment_s(self) -> None:
        self.assertEqual(self.build().consumer_reference,
                         VALUES["o1.fileDataReporting.consumerReference"])


class TheClientSecretIsTheClientsOwn(SubscriptionWiringFixture):

    def test_the_recorded_reference_is_replaced_for_this_client(self) -> None:
        """The integration fact, asserted rather than left in a comment.

        ``o1.https.consumerOauthCredentialRef`` names the Provider's inbound
        JWT anchor -- the file the *verifier* reads, owned by the provider's uid
        at mode 0400 so that no consumer can read it.  The client-credentials
        POST needs the client's own Basic-auth secret instead.
        """
        self.build()
        profile = self.recorder.oauth_profiles[-1]
        self.assertEqual(profile["clientId"], "smo-o1-consumer")
        self.assertEqual(profile["credentialRef"],
                         filedatasub.CLIENT_SECRET_OVERRIDE["smo-o1-consumer"])
        self.assertNotEqual(profile["credentialRef"],
                            VALUES["o1.https.consumerOauthCredentialRef"])
        self.assertNotIn("inbound-jwt-hmac", profile["credentialRef"])

    def test_everything_else_stays_the_deployment_s(self) -> None:
        self.build()
        profile = self.recorder.oauth_profiles[-1]
        self.assertEqual(profile["audience"], "oran-aic-o1-provider")
        self.assertEqual(profile["scope"], "o1:file-data-reporting")
        self.assertEqual(profile["truststoreRef"], VALUES["o1.https.truststoreRef"])
        self.assertEqual(profile["clientCertificateRef"],
                         VALUES["o1.https.consumerClientCertificateRef"])

    def test_an_unknown_client_gets_no_substitution(self) -> None:
        values = dict(VALUES)
        values["o1.https.consumerOauthClientId"] = "someone-else"
        filedatasub.build_subscription(values, state_path=self.state_path)
        self.assertEqual(self.recorder.oauth_profiles[-1]["credentialRef"],
                         VALUES["o1.https.consumerOauthCredentialRef"])

    def test_the_mutual_tls_pair_is_the_consumer_s(self) -> None:
        self.build()
        tls = self.recorder.tls_profiles[-1]
        self.assertEqual(
            tls,
            {
                "truststoreRef": VALUES["o1.https.truststoreRef"],
                "clientCertificateRef":
                    VALUES["o1.https.consumerClientCertificateRef"],
                "clientPrivateKeyRef":
                    VALUES["o1.https.consumerClientPrivateKeyRef"],
            },
        )


class DurableMeansReadBackOffTheFilesystem(SubscriptionWiringFixture):

    def test_an_absent_identifier_refuses_the_unlock(self) -> None:
        with self.assertRaises(SubscriptionError) as raised:
            filedatasub.confirm_subscription(VALUES, state_path=self.state_path)
        self.assertIn("must not be unlocked", str(raised.exception))

    def test_an_unreadable_identifier_refuses_too(self) -> None:
        self.state_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(SubscriptionError):
            filedatasub.confirm_subscription(VALUES, state_path=self.state_path)

    def test_a_persisted_identifier_is_confirmed(self) -> None:
        self.state_path.write_text(
            json.dumps({"subscriptionId": "sub-1",
                        "consumerReference": "https://smo.invalid/notify"}),
            encoding="utf-8")
        confirmed = filedatasub.confirm_subscription(
            VALUES, state_path=self.state_path)
        self.assertTrue(confirmed["durable"])
        self.assertEqual(confirmed["subscriptionId"], "sub-1")

    def test_the_durable_store_is_outside_the_repository(self) -> None:
        """A durable identifier that lives in a worktree is not durable."""
        default = filedatasub.DEFAULT_STATE_PATH
        self.assertTrue(default.is_absolute())
        self.assertNotIn("orca/workspaces", str(default))


class TheLifecycleDispatchesFixturelessOrdinals(unittest.TestCase):

    def test_ordinal_five_is_no_longer_filtered_out(self) -> None:
        from tools.g5ota import perfmetricjob

        ordinals = [entry.get("ordinal")
                    for entry in perfmetricjob._steps(perfmetricjob.LIFECYCLE)]
        self.assertEqual(ordinals, [1, 2, 3, 4, 5, 6, 7, 8])
        external = [entry for entry in perfmetricjob._steps(perfmetricjob.LIFECYCLE)
                    if entry.get("requestFixture") is None]
        self.assertEqual([entry["state"] for entry in external],
                         ["SUBSCRIPTION_DURABLE"])

    def test_the_external_step_refuses_a_precondition_it_does_not_know(self) -> None:
        from tools.g5ota import perfmetricjob

        record = perfmetricjob._external_step(
            {"ordinal": 99, "state": "SOMETHING_ELSE"}, VALUES)
        self.assertFalse(record["ok"])
        self.assertIn("unknown external precondition", record["error"])


if __name__ == "__main__":
    unittest.main()
