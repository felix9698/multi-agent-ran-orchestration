"""Per-family policy builders: gateway command -> validated A1 body."""

from __future__ import annotations

import json
import unittest

from oran.campaign5.builders import (
    Campaign5BuilderError,
    fixed_validity,
    make_policy_builder,
)
from oran.campaign5.families import CAMPAIGN5_FAMILIES, validate_campaign5

VALIDITY = fixed_validity("2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z")


def command(fam, *, scope, value, fence=3, tx="tx-1"):
    return {
        "operation": "APPLY", "transactionId": tx, "trialId": "trial-1",
        "fencingToken": fence, "commandSequence": 2, "commandIndex": 0,
        "idempotencyKey": f"{tx}:APPLY:0", "scope": dict(scope),
        "axis": fam.axis, "value": dict(value),
    }


CASES = {
    "cap": ({"cellId": "cell-1", "ueId": "ue-1"}, {"maxDlPrbs": 12},
            {"cellId": "cell-1", "ueId": "ue-1", "maxDlPrbs": 12}),
    "priority": ({"cellId": "cell-1", "ueId": "ue-1"}, {"pfWeight": 4.0},
                 {"cellId": "cell-1", "ueId": "ue-1", "pfWeight": 4.0}),
    "mcs": ({"cellId": "cell-1"}, {"minDlMcs": 4, "maxDlMcs": 16},
            {"cellId": "cell-1", "minDlMcs": 4, "maxDlMcs": 16}),
    "power": ({"cellId": "cell-1", "gnbId": "gnb-1"}, {"txAttenuationDb": 6},
              {"cellId": "cell-1", "gnbId": "gnb-1", "txAttenuationDb": 6}),
}


class PolicyBuilders(unittest.TestCase):
    def test_builder_assembles_and_validates_the_config_from_scope_and_value(self):
        for key, (scope, value, expected_config) in CASES.items():
            fam = CAMPAIGN5_FAMILIES[key]
            build = make_policy_builder(fam, validity_provider=VALIDITY)
            body = build(command(fam, scope=scope, value=value, fence=7))
            with self.subTest(family=key):
                self.assertEqual(body["config"], expected_config)
                self.assertEqual(body["validity"],
                                 {"notBefore": "2026-09-02T00:00:00Z",
                                  "notAfter": "2026-09-03T00:00:00Z"})
                # A1 revision and the Kernel's resource fence are separate axes.
                # 2026-09-20: the traceId names the scope too, one A1 id per scope.
                scope_text = "/".join(f"{k}={scope[k]}" for k in fam.scope_fields)
                self.assertEqual(body["trace"], {"traceId": f"tx-1#{scope_text}",
                                                 "revision": 1, "fencingToken": 7})
                validate_campaign5(body, f"{fam.policy_type_id}.policy")

    def test_single_leaf_family_accepts_a_bare_scalar_value(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1", "ueId": "ue-1"}, value={"maxDlPrbs": 8})
        cmd["value"] = 8  # bare scalar, like steering's servingCell string
        self.assertEqual(build(cmd)["config"]["maxDlPrbs"], 8)

    def test_identity_only_comes_from_scope_never_from_value(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1", "ueId": "ue-1"},
                      value={"maxDlPrbs": 12, "cellId": "spoofed"})
        with self.assertRaises(Campaign5BuilderError):
            build(cmd)

    def test_mcs_requires_min_not_greater_than_max(self):
        fam = CAMPAIGN5_FAMILIES["mcs"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1"}, value={"minDlMcs": 20, "maxDlMcs": 4})
        with self.assertRaisesRegex(Campaign5BuilderError, "minDlMcs"):
            build(cmd)

    def test_out_of_range_value_is_refused_by_the_schema(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1", "ueId": "ue-1"}, value={"maxDlPrbs": 999})
        with self.assertRaises(Campaign5BuilderError):
            build(cmd)

    def test_missing_scope_identity_is_refused(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1"}, value={"maxDlPrbs": 12})
        with self.assertRaisesRegex(Campaign5BuilderError, "ueId"):
            build(cmd)

    def test_wrong_axis_is_refused(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1", "ueId": "ue-1"}, value={"maxDlPrbs": 12})
        cmd["axis"] = "servingCell"
        with self.assertRaisesRegex(Campaign5BuilderError, "axis"):
            build(cmd)

    def test_zero_is_a_valid_first_fence_and_revision_starts_at_one(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        first = command(
            fam, scope={"cellId": "cell-1", "ueId": "ue-1"},
            value={"maxDlPrbs": 12}, fence=0,
        )
        body = build(first, last_revision=0)
        self.assertEqual(
            body["trace"],
            {"traceId": "tx-1#cellId=cell-1/ueId=ue-1", "revision": 1, "fencingToken": 0},
        )
        # An equal-fence retry is the exact validated body, even though the
        # journal now reports the revision allocated by PREPARE.
        replay = build(first, last_revision=1)
        self.assertEqual(replay, body)
        body_bytes = json.dumps(
            body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        replay_bytes = json.dumps(
            replay, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(replay_bytes, body_bytes)

    def test_an_older_fence_is_refused_after_a_newer_one(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        base = {"cellId": "cell-1", "ueId": "ue-1"}
        self.assertEqual(
            build(command(fam, scope=base, value={"maxDlPrbs": 12}, fence=0),
                  last_revision=0)
            ["trace"]["revision"],
            1,
        )
        self.assertEqual(
            build(command(fam, scope=base, value={"maxDlPrbs": 8}, fence=1),
                  last_revision=1)
            ["trace"]["revision"],
            2,
        )
        with self.assertRaisesRegex(Campaign5BuilderError, "stale fencingToken"):
            build(command(fam, scope=base, value={"maxDlPrbs": 6}, fence=0),
                  last_revision=2)

    def test_reconstructed_builder_continues_from_the_durable_revision(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        base = {"cellId": "cell-1", "ueId": "ue-1"}
        rebuilt = make_policy_builder(fam, validity_provider=VALIDITY)

        third = rebuilt(
            command(fam, scope=base, value={"maxDlPrbs": 6}, fence=2),
            last_revision=2,
        )
        self.assertEqual(
            third["trace"],
            {"traceId": "tx-1#cellId=cell-1/ueId=ue-1", "revision": 3, "fencingToken": 2},
        )
        with self.assertRaisesRegex(Campaign5BuilderError, "stale fencingToken"):
            rebuilt(
                command(fam, scope=base, value={"maxDlPrbs": 8}, fence=1),
                last_revision=3,
            )

    def test_same_fence_cannot_change_the_policy_body(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        base = {"cellId": "cell-1", "ueId": "ue-1"}
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        build(command(fam, scope=base, value={"maxDlPrbs": 12}, fence=0),
              last_revision=0)
        with self.assertRaisesRegex(Campaign5BuilderError, "same fencingToken"):
            build(command(fam, scope=base, value={"maxDlPrbs": 8}, fence=0),
                  last_revision=1)

    def test_a_negative_fence_is_refused(self):
        fam = CAMPAIGN5_FAMILIES["cap"]
        build = make_policy_builder(fam, validity_provider=VALIDITY)
        cmd = command(fam, scope={"cellId": "cell-1", "ueId": "ue-1"}, value={"maxDlPrbs": 12}, fence=-1)
        with self.assertRaisesRegex(Campaign5BuilderError, "fencingToken"):
            build(cmd)


if __name__ == "__main__":
    unittest.main()


class OneTransactionTwoScopesTwoIds(unittest.TestCase):
    """2026-09-20 03:30: pfWeight on ue1 (cell 87654321) and ue3 (cell 12345678)
    in one trial got one A1 id; the second cell's create was refused."""

    def test_same_type_different_scopes_get_different_producer_ids(self):
        from oran.campaign5.producer import Campaign5PolicyProducer
        for key, other in (("priority", {"cellId": "cell-2", "ueId": "ue-3"}),
                           ("cap", {"cellId": "cell-2", "ueId": "ue-3"}),
                           ("cap", {"cellId": "cell-1", "ueId": "ue-3"}),
                           ("power", {"cellId": "cell-2", "gnbId": "gnb-2"})):
            fam = CAMPAIGN5_FAMILIES[key]
            first_scope, value, _ = CASES[key]
            with self.subTest(family=key, other=other):
                ids = {Campaign5PolicyProducer._r1_policy_id(
                           "ric", fam.policy_type_id,
                           make_policy_builder(fam, validity_provider=VALIDITY)(
                               command(fam, scope=scope, value=value)))
                       for scope in (first_scope, other)}
                self.assertEqual(2, len(ids))

    def test_a_retry_of_the_same_write_keeps_its_id(self):
        from oran.campaign5.producer import Campaign5PolicyProducer
        fam = CAMPAIGN5_FAMILIES["priority"]
        scope, value, _ = CASES["priority"]
        ids = {Campaign5PolicyProducer._r1_policy_id(
                   "ric", fam.policy_type_id,
                   make_policy_builder(fam, validity_provider=VALIDITY)(
                       command(fam, scope=scope, value=value)))
               for _ in range(2)}
        self.assertEqual(1, len(ids))
