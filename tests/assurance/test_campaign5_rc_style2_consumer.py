"""``rc_style2_actions`` now has a Kernel/Gateway consumer (the official route).

SEAMS-GATE2.md recorded the Style-2 declaration layer as having no crossing yet;
this is the crossing.  It maps the three Style-2 UE/cell declarations onto their
Campaign 5 families and produces the official ``r1-<family>`` plan, while the
catalog stays the sole authority on parameter validity, and while nothing here
makes the declared actions live-admissible (that needs a deployed definition,
encoder and readback that do not exist hardware-free).
"""

from __future__ import annotations

import unittest

from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.contracts.rc_style2_actions import (
    rc_style2_actions,
    validate_rc_style2_parameters,
)
from assurance.actions.catalog import ActionParameterError

from oran.campaign5.families import Campaign5Error
from tools.campaign5.route import (
    RC_STYLE2_FAMILY_KEYS,
    family_for_rc_style2,
    rc_style2_official_plan,
)


def deployment():
    return DeploymentBinding(
        contract_id="deployment/rc-style2-actions", version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION, document_status="NORMATIVE",
        standard_mapping={"e2sm-rc": "1.03"}, endpoint_id="near-rt-ric",
        base_url="https://near-rt.invalid", transport_security=TransportSecurity.MTLS,
        secret_refs={"clientCertificate": "file:/run/secrets/rc.crt"},
    )


class RcStyle2Consumer(unittest.TestCase):
    def test_the_three_declarations_map_one_to_one_onto_campaign5_families(self):
        declared = {item.key for item in rc_style2_actions(deployment())}
        self.assertEqual(declared, set(RC_STYLE2_FAMILY_KEYS))
        self.assertEqual(RC_STYLE2_FAMILY_KEYS, {
            "ue-dl-prb-cap": "cap",
            "scheduler-priority": "priority",
            "dl-mcs-bounds": "mcs",
        })

    def test_consumer_builds_the_official_family_plan(self):
        cases = {
            "ue-dl-prb-cap": ({"cellId": "cell-1", "ueId": "ue-1"},
                              {"maxDlPrbs": 24}, {"maxDlPrbs": 12}, "r1-cap"),
            "scheduler-priority": ({"cellId": "cell-1", "ueId": "ue-1"},
                                   {"pfWeight": 1.0}, {"pfWeight": 4.0}, "r1-priority"),
            "dl-mcs-bounds": ({"cellId": "cell-1"},
                              {"minDlMcs": 0, "maxDlMcs": 28},
                              {"minDlMcs": 4, "maxDlMcs": 16}, "r1-mcs"),
        }
        for key, (scope, baseline, target, adapter) in cases.items():
            fam, plan = rc_style2_official_plan(key, scope=scope, baseline=baseline, target=target)
            with self.subTest(action=key):
                self.assertEqual(plan["adapter"], adapter)
                self.assertEqual(plan["steps"][0]["value"], target)
                self.assertEqual(plan["scope"], {f: scope[f] for f in fam.scope_fields})

    def test_power_is_not_a_style2_declaration_and_has_no_declaration_route(self):
        # dl-rf-attenuation is fully custom (design section 4.4 level 4); it is
        # routed directly, not through the Style-2 declaration layer.
        with self.assertRaises(Campaign5Error):
            family_for_rc_style2("dl-rf-attenuation")

    def test_the_catalog_remains_the_authority_on_parameter_validity(self):
        actions = {item.key: item for item in rc_style2_actions(deployment())}
        # A value the catalog forbids is refused before any plan is built.
        with self.assertRaises(ActionParameterError):
            validate_rc_style2_parameters(actions["ue-dl-prb-cap"],
                                          {"rnti": 0x4601, "maxDlPrbs": 276})


if __name__ == "__main__":
    unittest.main()
