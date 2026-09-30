"""Hardware-free conformance tests for E2SM-RC Style 2 / Action 6."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from oran.slice_actuator.codec import CodecError, decode_control_request, encode_control_request
from oran.slice_actuator.model import PlmnIdentity, PrbRatios, SliceIdentity, SliceQuota


ROOT = Path(__file__).resolve().parents[2]
VECTOR = ROOT / "oran" / "slice_actuator" / "vectors" / "style2-action6-dual-slice.json"
UE_ANCHOR = "imsi-208950000000032"


def quota(*, minimum: int = 20, maximum: int = 80, dedicated: int = 10,
          sst: int = 222, sd: str | None = "00007B") -> SliceQuota:
    return SliceQuota(
        identity=SliceIdentity(
            plmn=PlmnIdentity(mcc="208", mnc="95"),
            sst=sst,
            sd=sd,
        ),
        ratios=PrbRatios(minimum=minimum, maximum=maximum, dedicated=dedicated),
    )


class Style2Action6Codec(unittest.TestCase):
    def test_deployed_dual_slice_vector_is_exact_and_round_trips(self) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        self.assertEqual(vector["deploymentSource"]["plmn"], "208-95")
        self.assertEqual(vector["deploymentSource"]["snssai"], {"sst": 222, "sd": "00007B"})
        self.assertEqual(vector["deploymentSource"]["coreServingPlmn"], "20895")
        self.assertEqual(
            vector["deploymentSource"]["gnbConfigSha256"],
            "ee7ebb64ad3c7c080f674150bfd4ad65a5853f5823106dac6d7e93a5ef0551bc",
        )
        self.assertEqual(
            vector["deploymentSource"]["coreSubscriberDatabaseSha256"],
            "94b3ff8722c71352a7e3b03052f9b2d5e2abbadccfab15ac3a25d248abe70fcb",
        )
        self.assertEqual(vector["deploymentSource"]["ueAnchorRef"], UE_ANCHOR)
        encoded = encode_control_request((quota(),), ue_anchor_ref=UE_ANCHOR)
        self.assertEqual(encoded, vector["request"])
        self.assertEqual(decode_control_request(encoded), (quota(),))

    def test_header_and_full_nested_parameter_tree_match_rc_1_03(self) -> None:
        encoded = encode_control_request((quota(),), ue_anchor_ref=UE_ANCHOR)
        self.assertEqual(encoded["header"], {
            "format": 1, "ricStyleType": 2, "ricControlActionId": 6,
            "ueId": {"anchorRef": UE_ANCHOR},
        })
        root = encoded["message"]["ranParameters"]
        self.assertEqual([node["id"] for node in root], [1])
        group = root[0]["value"]["items"][0]
        self.assertEqual(group["id"], 2)
        children = group["value"]["children"]
        self.assertEqual([node["id"] for node in children], [3, 11, 12, 13])
        member_list = children[0]["value"]["children"][0]
        self.assertEqual(member_list["id"], 5)
        member = member_list["value"]["items"][0]
        self.assertEqual(member["id"], 6)
        self.assertEqual([node["id"] for node in member["value"]["children"]], [7, 8])
        self.assertEqual(member["value"]["children"][0]["value"]["octets"], "02f859")
        self.assertEqual(
            [node["id"] for node in member["value"]["children"][1]["value"]["children"]],
            [9, 10],
        )
        self.assertEqual(
            member["value"]["children"][1]["value"]["children"][0]["value"]["octets"],
            "de",
        )

    def test_optional_sd_is_omitted_without_changing_the_tree(self) -> None:
        encoded = encode_control_request((quota(sd=None),), ue_anchor_ref=UE_ANCHOR)
        snssai = encoded["message"]["ranParameters"][0]["value"]["items"][0]["value"]["children"][0]["value"]["children"][0]["value"]["items"][0]["value"]["children"][1]
        self.assertEqual([child["id"] for child in snssai["value"]["children"]], [9])
        self.assertEqual(decode_control_request(encoded), (quota(sd=None),))

    def test_missing_snssai_is_rejected_during_decode(self) -> None:
        encoded = encode_control_request((quota(),), ue_anchor_ref=UE_ANCHOR)
        member_children = encoded["message"]["ranParameters"][0]["value"]["items"][0]["value"]["children"][0]["value"]["children"][0]["value"]["items"][0]["value"]["children"]
        member_children[:] = [node for node in member_children if node["id"] != 8]
        with self.assertRaisesRegex(CodecError, "S-NSSAI"):
            decode_control_request(encoded)

    def test_malformed_tree_is_rejected_not_partially_read(self) -> None:
        encoded = encode_control_request((quota(),), ue_anchor_ref=UE_ANCHOR)
        malformed = copy.deepcopy(encoded)
        malformed["message"]["ranParameters"][0]["id"] = 11
        with self.assertRaisesRegex(CodecError, "RRM Policy Ratio List"):
            decode_control_request(malformed)

    def test_header_format_one_refuses_a_missing_ue_anchor(self) -> None:
        with self.assertRaisesRegex(CodecError, "UE anchor"):
            encode_control_request((quota(),), ue_anchor_ref="")


class RatioValidation(unittest.TestCase):
    def test_sst_zero_is_not_an_admitted_slice_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "SST"):
            SliceIdentity(PlmnIdentity("208", "95"), 0, None)

    def test_zero_and_one_hundred_are_valid_boundaries(self) -> None:
        self.assertEqual(PrbRatios(0, 0, 0), PrbRatios(0, 0, 0))
        self.assertEqual(PrbRatios(100, 100, 100), PrbRatios(100, 100, 100))

    def test_out_of_range_and_reversed_ratios_are_rejected(self) -> None:
        for values in ((-1, 50, 0), (0, 101, 0), (20, 10, 5), (20, 80, 21)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    PrbRatios(*values)

    def test_aggregate_minimum_and_dedicated_ratios_are_bounded(self) -> None:
        second = SliceQuota(
            SliceIdentity(PlmnIdentity("208", "95"), 1, None),
            PrbRatios(50, 80, 40),
        )
        with self.assertRaisesRegex(CodecError, "aggregate minimum"):
            encode_control_request(
                (quota(minimum=51, dedicated=40), second), ue_anchor_ref=UE_ANCHOR,
            )
        dedicated_heavy = SliceQuota(
            SliceIdentity(PlmnIdentity("208", "95"), 1, None),
            PrbRatios(50, 80, 50),
        )
        with self.assertRaisesRegex(CodecError, "aggregate dedicated"):
            encode_control_request(
                (quota(minimum=60, dedicated=60), dedicated_heavy), ue_anchor_ref=UE_ANCHOR,
            )

    def test_duplicate_slice_member_is_rejected(self) -> None:
        with self.assertRaisesRegex(CodecError, "duplicate"):
            encode_control_request((quota(), quota()), ue_anchor_ref=UE_ANCHOR)


if __name__ == "__main__":
    unittest.main()
