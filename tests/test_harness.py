"""Hermetic tests for the development-only harness contract surface."""

from __future__ import annotations

from dataclasses import is_dataclass
import unittest

from oran.contract.harness import (
    HARNESS_PATHS,
    HarnessFaultRequest,
    HarnessFaultResponse,
    HarnessOperationRequest,
    HarnessOperationResponse,
    HarnessRestartRequest,
    HarnessRestartResponse,
    HarnessStateResponse,
    harness_enabled,
)


class HarnessTest(unittest.TestCase):
    def test_request_and_response_types_are_dataclasses(self):
        types = (
            HarnessOperationRequest,
            HarnessOperationResponse,
            HarnessFaultRequest,
            HarnessFaultResponse,
            HarnessRestartRequest,
            HarnessRestartResponse,
            HarnessStateResponse,
        )
        self.assertTrue(all(is_dataclass(type_) for type_ in types))
        self.assertEqual(HarnessOperationRequest("READBACK", {"target": "cell-1"}).to_json(), {"op": "READBACK", "target": "cell-1"})
        self.assertEqual(HarnessOperationResponse.from_json({"outputs": {"ok": True}}).outputs, {"ok": True})
        self.assertEqual(HarnessFaultRequest("DROP_CALLBACK_DELIVERY", {"interface": "A1"}).to_json(), {"fault": "DROP_CALLBACK_DELIVERY", "boundary": {"interface": "A1"}})
        self.assertEqual(HarnessRestartRequest("Near-RT mock").to_json(), {"component": "Near-RT mock"})

    def test_disabled_development_flag_rejects_harness(self):
        self.assertFalse(harness_enabled(host="127.0.0.1", insecure_dev_flag=False, production=False))
        self.assertFalse(harness_enabled(host="127.0.0.1", insecure_dev_flag=True, production=True))
        self.assertFalse(harness_enabled(host="192.0.2.1", insecure_dev_flag=True, production=False))
        self.assertTrue(harness_enabled(host="localhost", insecure_dev_flag=True, production=False))

    def test_harness_endpoint_paths_are_contract_constants(self):
        self.assertEqual(HARNESS_PATHS, {
            "op": "/harness/op",
            "fault": "/harness/fault",
            "restart": "/harness/restart",
            "state": "/harness/state",
        })
