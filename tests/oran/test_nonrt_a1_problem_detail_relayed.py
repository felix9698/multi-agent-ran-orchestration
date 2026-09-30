"""2026-09-29: A1-P refuses with problem+json ({"detail": ...}); the owner id in
'UE scope is already owned by policy <id>.' must survive into the relayed message."""
import unittest

from oran.nonrt.a1_client import A1ProtocolError, TransportResponse


class TheProblemDetailIsRelayed(unittest.TestCase):
    def test_detail_is_used_when_there_is_no_error_key(self):
        body = {"status": 409, "title": "AIC_POLICY_CONFLICT",
                "detail": "UE scope is already owned by policy 11111111-2222-3333-4444-555555555555."}
        msg = str(A1ProtocolError(TransportResponse(409, body, {})))
        self.assertIn("owned by policy 11111111-2222-3333-4444-555555555555", msg)

    def test_error_key_still_wins(self):
        msg = str(A1ProtocolError(TransportResponse(409, {"error": "x", "detail": "y"}, {})))
        self.assertTrue(msg.endswith(": x"))


if __name__ == "__main__":
    unittest.main()
