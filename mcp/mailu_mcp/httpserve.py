"""HTTP transport for the Mailu MCP server (stdlib only).

JSON-RPC 2.0 over ``POST /mcp`` (one message per request, like the stdio
framing but over HTTP), plus an unauthenticated ``GET /healthz`` for load
balancer probes.

Isolation model (this is what keeps user A from seeing user B's mail):
  * ``mail_*`` and ``gmail_*`` tools require HTTP Basic auth with the
    caller's own Mailu credentials (``email:password`` or
    ``email:app-token``). Every request builds its IMAP/SMTP session from
    those credentials, so Dovecot and Postfix — not our code — enforce the
    boundary. There is no shared mailbox identity in HTTP mode.
  * Gmail import is per account too: callers pass their own Gmail OAuth
    credentials with each call (never the operator's), copies land in
    their own mailbox, and job state is namespaced per owner.
  * ``admin_*`` tools require the operator Bearer token
    (``MCP_HTTP_ADMIN_TOKEN``) — they touch the global admin API, so
    ordinary users get 403.
  * The operator token may also call ``mail_*``/``gmail_*`` tools, which
    then use the single server-configured mailbox (``MAILU_IMAP_*``) and
    Gmail account — one explicit identity, never another user's.
  * ``tools/list`` is filtered by role: users see ``mail_*`` + ``gmail_*``.

Brute-force braking: failed authentications are counted per client IP
(``X-Forwarded-For``-aware) plus a global bucket; over-threshold clients
get 429. Always terminate TLS at the load balancer — this server speaks
plain HTTP.
"""
import base64
import binascii
import hmac
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from .config import MailConfig, load_config
from .protocol import McpServer, RequestContext

USER_TOOL_PREFIXES = ("mail_", "gmail_")


def parse_auth(authorization: Optional[str], admin_token: str
               ) -> Tuple[str, Optional[Dict[str, str]]]:
    """Classify credentials. Returns ("user", {"email","secret"}),
    ("admin", None), ("missing", None) or ("invalid", None)."""
    if not authorization:
        return "missing", None
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() == "bearer":
        if admin_token and hmac.compare_digest(presented.strip(), admin_token):
            return "admin", None
        return "invalid", None
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(presented.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return "invalid", None
        email, sep, secret = decoded.partition(":")
        if not sep or not email or not secret:
            return "invalid", None
        return "user", {"email": email, "secret": secret}
    return "invalid", None


def check_auth(authorization: Optional[str], token: str) -> bool:
    """Legacy single-token check (admin Bearer only)."""
    role, _ = parse_auth(authorization, token)
    return role == "admin"


class RateLimiter:
    """Failed-auth buckets: per-IP plus a global cap (thread-safe)."""

    def __init__(self, max_failures: int = 20, window: float = 600.0,
                 global_max: int = 500):
        self.max_failures = max_failures
        self.window = window
        self.global_max = global_max
        self._lock = threading.Lock()
        self._per_ip: Dict[str, deque] = {}
        self._global: deque = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._global and self._global[0] < cutoff:
            self._global.popleft()
        for ip in list(self._per_ip):
            bucket = self._per_ip[ip]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                del self._per_ip[ip]

    def blocked(self, ip: str) -> bool:
        with self._lock:
            self._prune(time.monotonic())
            bucket = self._per_ip.get(ip)
            if bucket is not None and len(bucket) >= self.max_failures:
                return True
            return len(self._global) >= self.global_max

    def failed(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._per_ip.setdefault(ip, deque()).append(now)
            self._global.append(now)


def user_mail_config(identity: Dict[str, str]) -> MailConfig:
    """Per-request mailbox config: server hosts, caller identity.

   Host/port/TLS come from the server environment (one Mailu for all
    callers); user, secret and default sender come from HTTP Basic auth,
    so replies and sends can never default to someone else's address.
    """
    base = load_config().mail
    return MailConfig(
        imap_host=base.imap_host, imap_port=base.imap_port,
        imap_user=identity["email"], imap_password=identity["secret"],
        imap_ssl=base.imap_ssl, smtp_host=base.smtp_host,
        smtp_port=base.smtp_port, smtp_user=identity["email"],
        smtp_password=identity["secret"], smtp_starttls=base.smtp_starttls,
        default_from=identity["email"],
    )


def make_handler(server_factory, admin_token: str, max_body: int,
                 rate_limiter: Optional[RateLimiter] = None):
    """Build a handler class.

    ``server_factory`` is ``build_server`` (takes an optional
    ``RequestContext``); a fresh server is built per request so no mailbox
    identity is ever shared between callers.
    """
    limiter = rate_limiter or RateLimiter()

    class McpHttpHandler(BaseHTTPRequestHandler):
        server_version = "MailuMCP/1.0"

        def log_message(self, fmt, *args):  # one line per request on stderr
            import sys
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _client_ip(self) -> str:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
            return self.client_address[0]

        def _send_json(self, status: int, payload, extra: Optional[dict] = None):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _rpc_error(self, status: int, code: int, message: str,
                       extra: Optional[dict] = None, msg_id: Any = None):
            self._send_json(status, {"jsonrpc": "2.0", "id": msg_id,
                                     "error": {"code": code, "message": message}},
                             extra)

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/healthz":
                probe = server_factory()
                self._send_json(200, {"status": "ok",
                                      "server": probe.name,
                                      "version": probe.version,
                                      "tools": len(probe.tools)})
            else:
                self._rpc_error(404, -32000, "Not found")

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/mcp":
                self._rpc_error(404, -32000, "Not found")
                return
            ip = self._client_ip()
            if limiter.blocked(ip):
                self._rpc_error(429, -32003, "Too many failed attempts; slow down",
                                {"Retry-After": "60"})
                return
            role, identity = parse_auth(self.headers.get("Authorization"), admin_token)
            if role == "missing":
                limiter.failed(ip)
                self._rpc_error(401, -32001, "Missing Authorization header",
                                {"WWW-Authenticate": 'Basic realm="mailu-mcp", Bearer'})
                return
            if role == "invalid":
                limiter.failed(ip)
                self._rpc_error(403, -32002, "Invalid credentials")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0:
                self._rpc_error(411, -32600, "Content-Length required")
                return
            if length > max_body:
                self._rpc_error(413, -32600, f"Body exceeds {max_body} bytes")
                return
            try:
                msg = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._rpc_error(400, -32700, f"Parse error: {exc}")
                return
            if not isinstance(msg, dict):
                self._rpc_error(400, -32600, "Invalid Request: batch not supported")
                return
            method = msg.get("method", "")
            params = msg.get("params") or {}
            if role == "user":
                ctx = RequestContext(role="user", email=identity["email"],
                                     mail_config=user_mail_config(identity),
                                     allow_server_gmail=False)
                server = server_factory(ctx)
                if method == "tools/call":
                    name = params.get("name", "")
                    if not name.startswith(USER_TOOL_PREFIXES):
                        limiter.failed(ip)
                        self._rpc_error(403, -32002,
                                              f"Tool {name!r} requires operator access",
                                              msg_id=msg.get("id"))
                        return
            else:
                server = server_factory(RequestContext(role="admin"))
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
            if method == "tools/list" and role == "user":
                tools = resp.get("result", {}).get("tools", [])
                resp["result"]["tools"] = [t for t in tools
                                           if t.get("name", "").startswith(USER_TOOL_PREFIXES)]
            self._send_json(200, resp)

    return McpHttpHandler


def serve_http(server_or_factory, host: str, port: int, token: str,
               max_body: int = 1024 * 1024) -> None:
    """Serve forever on HTTP. Refuses to start without an admin token.

    Accepts a server factory (``build_server``) or, for tests, a ready
    ``McpServer`` instance (used as-is; per-request identity then only
    applies to role gating, not mailbox config).
    """
    if not token:
        raise ValueError("Refusing to serve public HTTP without MCP_HTTP_ADMIN_TOKEN: "
                         "set a long random Bearer token first")
    if isinstance(server_or_factory, McpServer):
        ready = server_or_factory
        factory = lambda ctx=None: ready  # noqa: E731
    else:
        factory = server_or_factory
    httpd = ThreadingHTTPServer((host, port), make_handler(factory, token, max_body))
    httpd.daemon_threads = True
    probe = factory()
    print(f"mailu-mcp: HTTP listening on http://{host}:{httpd.server_port}/mcp "
          f"({len(probe.tools)} tools; health at /healthz)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
