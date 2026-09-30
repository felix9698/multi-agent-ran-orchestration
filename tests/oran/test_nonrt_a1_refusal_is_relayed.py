"""A1-P 409 is a decision, not a lost message (v46r8 board 462, 2026-09-23).

The hand-back for a UE that had died was refused by the producer with 409; the
Non-RT service relayed it as 503 without the reason and left the refused update
pending, where a later reconciliation would have re-sent the stale pin.
"""
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from oran.nonrt.a1_client import A1PClient, A1ProtocolError, A1Transport, TransportResponse
from oran.nonrt.service import NonRtRicService, ServiceFailure


class _Store:
    def __init__(self): self.executed = []
    def execute(self, sql, params=()): self.executed.append((" ".join(sql.split()), params))
    def rows(self, sql, params=()): return []
    @staticmethod
    def decode(value): return value


def _service():
    service = NonRtRicService.__new__(NonRtRicService)
    service.store = _Store()
    return service


class TheProducersRefusalReachesTheCaller(unittest.TestCase):

    def test_the_reason_is_kept(self):
        exc = A1ProtocolError(TransportResponse(409, {"error": "UE scope has no unique fresh KPM/header identity"}))
        self.assertEqual(str(exc), "A1-P returned HTTP 409: UE scope has no unique fresh KPM/header identity")
        self.assertEqual(str(A1ProtocolError(TransportResponse(503))), "A1-P returned HTTP 503")

    def test_a_409_clears_the_pending_operation_and_stays_a_409(self):
        service = _service()
        exc = A1ProtocolError(TransportResponse(409, {"error": "no identity"}))
        with self.assertRaises(ServiceFailure) as caught:
            service._drop_refused_pending("pol-1", exc)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("no identity", caught.exception.detail)
        self.assertEqual(len(service.store.executed), 1)
        self.assertIn("pending_operation=NULL", service.store.executed[0][0])

    def test_any_other_status_keeps_the_pending_operation(self):
        service = _service()
        service._drop_refused_pending("pol-1", A1ProtocolError(TransportResponse(500)))
        self.assertEqual(service.store.executed, [])


    def test_400_and_404_are_refusals_too(self):
        for status, code in ((400, "AIC_SCHEMA_INVALID"), (404, "AIC_RESOURCE_NOT_FOUND")):
            with self.subTest(status=status):
                service = _service()
                with self.assertRaises(ServiceFailure) as caught:
                    service._drop_refused_pending("pol-1", A1ProtocolError(TransportResponse(status)))
                self.assertEqual((status, code), (caught.exception.status, caught.exception.code))
                self.assertIn("pending_operation=NULL", service.store.executed[0][0])


class _A1:
    def __init__(self, status): self.status, self.puts = status, []
    def put_policy(self, *args):
        self.puts.append(args)
        raise A1ProtocolError(TransportResponse(self.status, {"error": "refused"}))
    def delete_policy(self, *args):
        raise A1ProtocolError(TransportResponse(self.status, {"error": "refused"}))


def _creating(status):
    service = _service()
    service.a1 = _A1(status)
    service.a1_pre_put_reconciliation_probe = False
    service.a1_notification_destination = "https://nonrt/a1-status"
    service._policy_row = lambda pid: {"policy_id": pid, "policy_type_id": "T",
                                       "idempotency_key": "k", "policy_json": {}}
    return service


class ARefusedCreateNeverBecomesAPolicy(unittest.TestCase):
    """2026-09-23 audit: the create path still turned A1-P 400/404/409 into 503
    and left the row UNCERTAIN, where reconciliation would re-PUT it."""

    def test_a_refused_create_is_relayed_and_its_row_removed(self):
        for status, code in ((400, "AIC_SCHEMA_INVALID"), (404, "AIC_RESOURCE_NOT_FOUND"),
                             (409, "AIC_POLICY_CONFLICT")):
            with self.subTest(status=status):
                service = _creating(status)
                with self.assertRaises(ServiceFailure) as caught:
                    service._reconcile_policy("pol-1")
                self.assertEqual((status, code), (caught.exception.status, caught.exception.code))
                sql = [q for q, _ in service.store.executed]
                self.assertIn("DELETE FROM policies WHERE policy_id=?", sql)
                self.assertFalse(any("UNCERTAIN" in q for q in sql))

    def test_a_server_fault_stays_uncertain(self):
        service = _creating(502)
        with self.assertRaises(A1ProtocolError):
            service._reconcile_policy("pol-1")
        sql = [q for q, _ in service.store.executed]
        self.assertTrue(any("UNCERTAIN" in q for q in sql))
        self.assertNotIn("DELETE FROM policies WHERE policy_id=?", sql)

    def test_a_delete_answered_404_is_already_done(self):
        service = _creating(404)
        service._versioned = lambda status, body=None, **h: status
        self.assertEqual(204, service._delete_policy("pol-1"))
        sql = [q for q, _ in service.store.executed]
        self.assertIn("DELETE FROM policies WHERE policy_id=?", sql)


class ANonJsonErrorBodyIsNotARefusal(unittest.TestCase):
    """An HTML 502 from a proxy raised JSONDecodeError, which the service maps to
    400 AIC_SCHEMA_INVALID -- "unknown" became "definitively refused"."""

    def _transport(self):
        return A1Transport("http://127.0.0.1:9", insecure_dev_mode=True)

    def test_an_html_502_keeps_its_status(self):
        error = HTTPError("http://127.0.0.1:9/x", 502, "Bad Gateway", {},
                          io.BytesIO(b"<html><body>502 Bad Gateway</body></html>"))
        with patch("oran.nonrt.a1_client.urlopen", side_effect=error):
            response = self._transport().request("GET", "/A1-P/v2/policytypes")
        self.assertEqual(502, response.status)
        with patch("oran.nonrt.a1_client.urlopen", side_effect=error):
            with self.assertRaises(A1ProtocolError):
                A1PClient(self._transport()).discover_policy_types()

    def test_a_non_json_success_body_is_a_transport_fault(self):
        class Reply:
            status, headers = 200, {}
            @staticmethod
            def read(): return b"<html>ok</html>"
        with patch("oran.nonrt.a1_client.urlopen", return_value=Reply()):
            with self.assertRaises(OSError):
                self._transport().request("GET", "/A1-P/v2/policytypes")


if __name__ == "__main__":
    unittest.main()
