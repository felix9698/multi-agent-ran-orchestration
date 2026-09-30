"""Development callback and harness HTTP surfaces for the headless rApp.

The harness routes are not O-RAN interfaces and cannot be enabled in an
operational profile.  Plain HTTP is restricted to an explicit loopback-only
insecure development flag.
"""

from __future__ import annotations

import json
import ssl
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Mapping
from urllib.parse import urlparse

from oran.contract.harness import harness_enabled

from .coordinator_adapter import RAppCoordinatorAdapter
from .r1_client import R1Client, R1Error


class ServerConfigurationError(ValueError):
    pass


class RAppHttpServer:
    def __init__(self, *, host: str, port: int, client: R1Client,
                 coordinator: RAppCoordinatorAdapter,
                 status_callback_path: str,
                 dme_push_base_path: str,
                 insecure_dev: bool = False,
                 enable_harness: bool = False,
                 integration_control_approved: bool = False,
                 ssl_context: ssl.SSLContext | None = None,
                 authorize: Callable[[Mapping[str, str]], bool] | None = None,
                 a1_status_callback_path: str | None = None,
                 accept_a1_status: Callable[[Mapping[str, Any]], int] | None = None,
                 on_evidence_accepted: Callable[[str, Mapping[str, Any]], None] | None = None):
        if insecure_dev:
            if host not in {"127.0.0.1", "::1", "localhost"}:
                raise ServerConfigurationError(
                    "insecure development server must bind to loopback")
        elif ssl_context is None or authorize is None:
            raise ServerConfigurationError(
                "operational callback server requires TLS/mTLS context and OAuth hook")
        if enable_harness and not (insecure_dev or integration_control_approved):
            raise ServerConfigurationError(
                "harness surface requires insecure_dev or approved integration control")
        if enable_harness and integration_control_approved and not harness_enabled(
                host=host, insecure_dev_flag=True, production=False):
            raise ServerConfigurationError(
                "integration control surface is loopback-only")
        self.client = client
        self.coordinator = coordinator
        self.status_path = status_callback_path
        self.dme_base = dme_push_base_path.rstrip("/")
        self.authorize = authorize
        self.insecure_dev = insecure_dev
        self.enable_harness = enable_harness
        self.integration_control_approved = integration_control_approved
        self.on_evidence_accepted = on_evidence_accepted
        self.a1_status_callback_path = a1_status_callback_path
        self.accept_a1_status = accept_a1_status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "oran-aic-rapp-callback/1.0.0"

            def log_message(self, _format, *args):
                return

            def _json(self, status: int, payload: Any = None,
                      *, version: str | None = None):
                raw = b"" if payload is None else json.dumps(
                    payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                if version:
                    self.send_header("Version", version)
                if raw:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _read(self) -> Dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise R1Error("request body must be one JSON object") from exc
                if not isinstance(body, dict):
                    raise R1Error("request body must be one JSON object")
                return body

            def _authorized(self) -> bool:
                return outer.insecure_dev or bool(outer.authorize(dict(self.headers)))

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/harness/state" and outer.enable_harness:
                    transitions = outer.coordinator.transition_snapshot()
                    terminal = [item for item in transitions if item["to"] == "S6"]
                    observations = outer.coordinator.harness_observations()
                    self._json(200, {
                        "transitions": transitions,
                        "coordinatorState": terminal[-1]["to"] if terminal else (
                            transitions[-1]["to"] if transitions else "S0"),
                        "terminalOutcomeCount": len(terminal),
                        "upperOutcome": terminal[-1]["outcome"] if terminal else None,
                        **observations,
                    })
                    return
                self._json(404, {"error": "resource not found"})

            def do_POST(self):
                path = urlparse(self.path).path
                if not self._authorized():
                    self._json(401, {"error": "authorization failed"})
                    return
                try:
                    body = self._read()
                    if (outer.a1_status_callback_path is not None
                            and path == outer.a1_status_callback_path):
                        if outer.accept_a1_status is None:
                            raise R1Error("A1 status callback sink is not configured")
                        if self.headers.get("Version") not in {None, "1.0.0"}:
                            raise R1Error("A1 status Version must be 1.0.0")
                        status = outer.accept_a1_status(body)
                        self._json(status, version="1.0.0")
                        return
                    if path == outer.status_path:
                        if self.headers.get("Version") != "1.0.0":
                            raise R1Error("status callback Version must be 1.0.0")
                        outer.client.handle_status_notification(body)
                        self._json(204, version="1.0.0")
                        return
                    if path.startswith(outer.dme_base + "/"):
                        # (P0) scenario-runner-contract.1.0.1.json
                        # #/operations/R1_DME_PUBLISH declares the push as
                        # "exactly one schema-valid policy-evidence record as
                        # application/json" and the catalog's push steps declare
                        # only `contentType`; no Version header is contract-
                        # declared for this boundary.  A wrong version is still
                        # rejected, an absent one is not -- same rule the A1
                        # status callback path above already applies.
                        if self.headers.get("Version") not in {None, "1.0.0"}:
                            raise R1Error("DME push Version must be 1.0.0")
                        if self.headers.get("Content-Type", "").split(";", 1)[0] \
                                != "application/json":
                            raise R1Error("DME push requires application/json")
                        binding = path[len(outer.dme_base) + 1:]
                        if not binding or "/" in binding:
                            self._json(404, {"error": "unknown delivery binding"})
                            return
                        try:
                            outer.client.accept_evidence(binding, body)
                        except KeyError:
                            self._json(404, {"error": "unknown delivery binding"})
                            return
                        if outer.on_evidence_accepted is not None:
                            outer.on_evidence_accepted(binding, body)
                        self._json(204, version="1.0.0")
                        return
                    if path == "/harness/op" and outer.enable_harness:
                        if body.get("op") == "RESET_SCENARIO_STATE":
                            outer.coordinator.harness_reset()
                            outer.client.harness_reset()
                            self._json(200, {"outputs": {"resetCompleted": True}})
                            return
                        if body.get("op") == "R1_DME_BINDING_PRECREATE":
                            outer.client.harness_precreate_binding(
                                str(body["deliveryBindingId"]), body["job"])
                            self._json(200, {"outputs": {}})
                            return
                        if body.get("op") == "R1_DME_BINDING_COMMIT":
                            outer.client.harness_commit_binding(
                                str(body["deliveryBindingId"]), str(body["dataJobId"]))
                            self._json(200, {"outputs": {}})
                            return
                        if body.get("op") == "COORDINATOR_PROCESS_INTENT":
                            from .headless import _context, _typed_intent
                            now = datetime.fromisoformat(
                                str(body["now"]).replace("Z", "+00:00"))
                            evidence_records = json.loads(json.dumps(
                                body["evidenceRecords"]))
                            for record in evidence_records:
                                record["correlation"]["policyId"] = body[
                                    "policyId"]
                            result = outer.coordinator.process_intent(
                                _typed_intent(body["intent"]),
                                context=_context(body["policyContext"]),
                                evidence_records=evidence_records,
                                now=now,
                                identifiers=body["identifiers"],
                            )
                            observed = outer.coordinator.harness_observations()
                            cycles = result.get("cycles") or []
                            last_cycle = cycles[-1] if cycles else {}
                            self._json(200, {"outputs": {
                                "processIntentCalls": observed["processIntentCalls"],
                                "fsmHistory": observed["coordinatorFsmHistory"],
                                "terminalOutcome": result.get("terminal_outcome"),
                                "terminalEvidenceRef": result.get("evidence_record_id"),
                                "ledgerReferences": observed[
                                    "coordinatorLedgerReferences"],
                                "profileError": last_cycle.get("profile_error"),
                                "routedTo": last_cycle.get("routed_to"),
                            }})
                            return
                        if body.get("op") != "COORDINATOR_TRANSITION":
                            self._json(400, {"error": "operation not owned by rApp"})
                            return
                        source, target = body.get("from"), body.get("to")
                        found = next((event for event in reversed(
                            outer.coordinator.transition_snapshot())
                                      if event["from"] == source and
                                      event["to"] == target), None)
                        if found is not None:
                            outputs = {"state": found["to"], "outcome": found["outcome"]}
                        else:
                            outcome = body.get("terminalOutcome") or body.get("judgement")
                            outputs = outer.coordinator.harness_transition(
                                str(source), str(target), str(outcome) if outcome else None)
                        self._json(200, {"outputs": outputs})
                        return
                    if path in {"/harness/fault", "/harness/restart"} \
                            and outer.enable_harness:
                        self._json(400, {"error": "operation not owned by rApp"})
                        return
                    self._json(404, {"error": "resource not found"})
                except R1Error as exc:
                    code = str(exc) if str(exc).startswith("AIC_") else "AIC_SCHEMA_INVALID"
                    self._json(400, {"code": code, "detail": str(exc)})
                except Exception:
                    self._json(400, {"code": "AIC_SCHEMA_INVALID",
                                     "detail": "contract validation failed"})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        if ssl_context is not None:
            self.httpd.socket = ssl_context.wrap_socket(
                self.httpd.socket, server_side=True)
        self._thread: threading.Thread | None = None

    @property
    def address(self):
        return self.httpd.server_address

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="rapp-callback", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
