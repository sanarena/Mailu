"""HTTP transport for the Mailu MCP server (stdlib only).

JSON-RPC 2.0 over ``POST /mcp`` (one message per request, like the stdio
framing but over HTTP), plus an unauthenticated ``GET /healthz`` for load
balancer probes.

Public-exposure hardening:
  * Bearer-token auth on every ``/mcp`` call, compared in constant time.
    The server refuses to start without ``MCP_HTTP_TOKEN``.
  * Request bodies capped (``MCP_HTTP_MAX_BODY``, default 1 MiB) → 413.
  * Binds 127.0.0.1 by default; override with ``MCP_HTTP_HOST`` for
    containers / load balancers. Always terminate TLS at the load
    balancer — this server speaks plain HTTP.
"""
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .protocol import McpServer


def check_auth(authorization: Optional[str], token: str) -> bool:
    """True when ``Authorization: Bearer <token>`` matches (constant time)."""
    if not authorization or not token:
        return False
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip(), token)


def make_handler(server: McpServer, token: str, max_body: int):
    """Build a request-handler class bound to one MCP server + token."""

    class McpHttpHandler(BaseHTTPRequestHandler):
        server_version = "MailuMCP/1.0"

        def log_message(self, fmt, *args):  # one line per request on stderr
            import sys
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, status: int, payload, extra: Optional[dict] = None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if check_auth(self.headers.get("Authorization"), token):
                return True
            if not self.headers.get("Authorization"):
                self._send_json(401, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32001,
                                                "message": "Missing Authorization header"}},
                                {"WWW-Authenticate": "Bearer"})
            else:
                self._send_json(403, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32002,
                                                "message": "Invalid Bearer token"}})
            return False

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":
                self._send_json(200, {"status": "ok",
                                      "server": server.name,
                                      "version": server.version,
                                      "tools": len(server.tools)})
            else:
                self._send_json(404, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32000,
                                                "message": "Not found"}})

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/mcp":
                self._send_json(404, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32000,
                                                "message": "Not found"}})
                return
            if not self._require_auth():
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0:
                self._send_json(411, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600,
                                                "message": "Content-Length required"}})
                return
            if length > max_body:
                self._send_json(413, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600,
                                                "message": f"Body exceeds {max_body} bytes"}})
                return
            try:
                msg = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700,
                                                "message": f"Parse error: {exc}"}})
                return
            if not isinstance(msg, dict):
                self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32600,
                                                "message": "Invalid Request: batch not supported"}})
                return
            try:
                resp = server.handle(msg)
            except Exception as exc:
                resp = {"jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(exc)}}
            if resp is None:  # notification: accepted, no reply
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_json(200, resp)

    return McpHttpHandler


def serve_http(server: McpServer, host: str, port: int, token: str,
               max_body: int = 1024 * 1024) -> None:
    """Serve forever on HTTP. Refuses to start without an auth token."""
    if not token:
        raise ValueError("Refusing to serve public HTTP without MCP_HTTP_TOKEN: "
                         "set a long random Bearer token first")
    httpd = ThreadingHTTPServer((host, port), make_handler(server, token, max_body))
    httpd.daemon_threads = True
    print(f"mailu-mcp: HTTP listening on http://{host}:{httpd.server_port}/mcp "
          f"({len(server.tools)} tools; health at /healthz)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
