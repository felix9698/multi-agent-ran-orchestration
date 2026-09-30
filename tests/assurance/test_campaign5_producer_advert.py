"""The in-repo producer advertises the four new types beside quota (Option B).

Design section 4.6: a second, in-repo producer stands up beside the external
steering producer and advertises only the new types.  Quota's own producer
(``A1PolicyProducer``) keeps ``AIC_SliceSLATarget_1.0.0`` and can advertise the
four beside it on one discovery surface, without gaining any slice-foreign
policy handling.  Building quota's wire path does not make its objective
submittable -- that invariant is checked elsewhere and is untouched here.
"""

from __future__ import annotations

import unittest

from oran.slice_actuator.a1 import A1PolicyProducer, POLICY_TYPE_ID
from oran.campaign5.families import CAMPAIGN5_POLICY_TYPES, campaign5_capability_manifest
from oran.campaign5.producer import Campaign5PolicyProducer


class BesideQuota(unittest.TestCase):
    def test_default_quota_producer_still_advertises_only_quota(self):
        self.assertEqual(A1PolicyProducer().get_policytypes(), [POLICY_TYPE_ID])

    def test_quota_producer_can_advertise_the_four_new_types_beside_quota(self):
        producer = A1PolicyProducer(additional_types=CAMPAIGN5_POLICY_TYPES)
        advertised = producer.get_policytypes()
        self.assertEqual(advertised[0], POLICY_TYPE_ID)
        self.assertEqual(set(advertised[1:]), set(CAMPAIGN5_POLICY_TYPES))
        self.assertEqual(len(advertised), 5)

    def test_a_duplicate_advertised_type_is_refused(self):
        with self.assertRaises(Exception):
            A1PolicyProducer(additional_types=(POLICY_TYPE_ID,))

    def test_campaign5_producer_advertises_exactly_the_four_new_types(self):
        producer = Campaign5PolicyProducer()
        self.assertEqual(set(producer.get_policytypes()), set(CAMPAIGN5_POLICY_TYPES))
        self.assertNotIn(POLICY_TYPE_ID, producer.get_policytypes())

    def test_producer_digests_agree_with_the_pinned_capability_manifest(self):
        producer = Campaign5PolicyProducer()
        manifest = campaign5_capability_manifest()
        for type_id in CAMPAIGN5_POLICY_TYPES:
            digests = producer.schema_digests(type_id)
            with self.subTest(type_id=type_id):
                self.assertEqual(digests["policySchemaJcsSha256"],
                                 manifest["schemaDigests"][type_id]["policy"])
                self.assertEqual(digests["statusSchemaJcsSha256"],
                                 manifest["schemaDigests"][type_id]["status"])


if __name__ == "__main__":
    unittest.main()
