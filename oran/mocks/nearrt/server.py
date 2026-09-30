"""Stdlib HTTP server for A1-P v2 and the explicitly non-standard harness."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .producer import POLICY_TYPE, NearRtMock, Problem

def create_server(mock: NearRtMock, host: str = "127.0.0.1", port: int = 0, api_root: str = "", insecure_dev: bool = False) -> ThreadingHTTPServer:
    if not insecure_dev or host not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("development management surface requires explicit loopback insecure_dev")
    prefix = f"{api_root.rstrip('/')}/A1-P/v2"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None: pass
        def _json(self):
            size = int(self.headers.get("Content-Length", "0")); return json.loads(self.rfile.read(size) or b"{}")
        def _out(self, code, body=None, headers=None):
            self.send_response(code); self.send_header("Version", "1.0.0")
            for k, v in (headers or {}).items(): self.send_header(k, v)
            if body is None: self.end_headers(); return
            raw = json.dumps(body).encode(); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def _run(self):
            try:
                parsed = urlparse(self.path); path, parts = parsed.path, parsed.path[len(prefix):].strip("/").split("/")
                if path == "/harness/state" and self.command == "GET": return self._out(200, mock.harness_state())
                if path == "/harness/op" and self.command == "POST": return self._out(200, mock.harness_op(self._json()))
                if path == "/harness/fault" and self.command == "POST": return self._out(200, mock.harness_fault(self._json()))
                if path == "/harness/restart" and self.command == "POST": return self._out(200, mock.harness_op({"op":"PROCESS_RESTART", **self._json()}))
                if not path.startswith(prefix): raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "resource does not exist")
                if parts == ["policytypes"] and self.command == "GET": return self._out(200, mock.list_policy_types())
                if len(parts) >= 2 and parts[0] == "policytypes" and parts[1] != POLICY_TYPE: raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "policy type does not exist")
                if parts == ["policytypes", POLICY_TYPE] and self.command == "GET": return self._out(200, mock.policy_type())
                if parts == ["policytypes", POLICY_TYPE, "policies"] and self.command == "GET": return self._out(200, mock.list_policies())
                if len(parts) >= 4 and parts[:3] == ["policytypes", POLICY_TYPE, "policies"]:
                    pid = parts[3]
                    if len(parts) == 4 and self.command == "PUT":
                        authorization = self.headers.get("Authorization")
                        if mock.security_fixture == "CONFIGURE_INVALID_BEARER_TOKEN" or authorization == "Bearer INVALID":
                            return self._out(401, {"error": "authentication failed"})
                        if (mock.security_fixture == "CONFIGURE_VALID_TOKEN_WITHOUT_POLICY_WRITE_SCOPE"
                                or authorization == "Bearer VALID_READ_ONLY"):
                            return self._out(403, {"error": "policy write scope is required"})
                        request_body = self._json()
                        code, obj = mock.put_policy(pid, request_body, parse_qs(parsed.query).get("notificationDestination", [None])[0])
                        headers = {"Location": f"{prefix}/policytypes/{POLICY_TYPE}/policies/{pid}"}
                        mock.http_interactions.append({
                            "method": self.command, "path": path, "requestBody": request_body,
                            "response": {"status": code, "body": obj, "headers": {"Version": "1.0.0", **headers}},
                        })
                        return self._out(code, obj, headers)
                    if len(parts) == 4 and self.command == "GET": return self._out(200, mock.get_policy(pid))
                    if len(parts) == 4 and self.command == "DELETE": mock.delete_policy(pid); return self._out(204)
                    if len(parts) == 5 and parts[4] == "status" and self.command == "GET": return self._out(200, mock.get_status(pid))
                raise Problem("AIC_RESOURCE_NOT_FOUND", 404, "resource does not exist")
            except Problem as exc:
                mock.observe_problem(exc, self.command, urlparse(self.path).path)
                self._out(exc.status, exc.body)
            except (json.JSONDecodeError, ValueError) as exc: self._out(400, Problem("AIC_SCHEMA_INVALID", 400, str(exc)).body)
        do_GET = _run; do_PUT = _run; do_DELETE = _run; do_POST = _run
    return ThreadingHTTPServer((host, port), Handler)
