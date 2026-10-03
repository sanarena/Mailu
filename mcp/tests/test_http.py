"""HTTP transport tests: real ThreadingHTTPServer on 127.0.0.1, urllib client."""
import base64
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, ".")

from mcp.mailu_mcp import httpserve
from mcp.mailu_mcp.protocol import McpServer

TOKEN = "test-admin-token-123"


def basic(email, secret):
    raw = base64.b64encode(f"{email}:{secret}".encode()).decode()
    return f"Basic {raw}"


def demo_server(ctx=None) -> McpServer:
    srv = McpServer("demo", "9.9")

    @srv.tool("mail_echo", "echo back", {"type": "object"})
    def _mail(args):
        return {"got": args}

    @srv.tool("admin_echo", "echo back", {"type": "object"})
    def _admin(args):
        return {"got": args}

    @srv.tool("gmail_echo", "echo back", {"type": "object"})
    def _gmail(args):
        return {"got": args}

    return srv


class ServedCase(unittest.TestCase):
    limiter = None

    def setUp(self):
        handler = httpserve.make_handler(demo_server, TOKEN, 1024,
                                         rate_limiter=self.limiter)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive())

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, path, payload, auth=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        req = urllib.request.Request(self._url(path), data=body, method="POST")
        if auth is not None:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _get(self, path):
        req = urllib.request.Request(self._url(path), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def _rpc(self, auth, method, params=None, msg_id=1):
        return self._post("/mcp", {"jsonrpc": "2.0", "id": msg_id,
                                   "method": method, "params": params or {}},
                          auth=auth)


class HttpTests(ServedCase):
    def test_healthz_needs_no_auth(self):
        status, _, body = self._get("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["tools"], 3)

    def test_unknown_paths_404(self):
        self.assertEqual(self._get("/")[0], 404)
        self.assertEqual(self._get("/mcp")[0], 404)
        status, _ = self._post("/elsewhere", {"jsonrpc": "2.0"}, auth="x")
        self.assertEqual(status, 404)

    def test_missing_credentials_is_401(self):
        status, body = self._rpc(None, "ping")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], -32001)

    def test_wrong_bearer_is_403(self):
        status, body = self._rpc("Bearer nope", "ping")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], -32002)

    def test_malformed_basic_is_403(self):
        for bad in ("Basic !!!", "Basic " + base64.b64encode(b"no-sep").decode(),
                    "Basic " + base64.b64encode(b":x").decode(), "Basic "):
            status, _ = self._rpc(bad, "ping")
            self.assertEqual(status, 403, bad)

    def test_unknown_scheme_rejected(self):
        status, _ = self._rpc("Digest abc", "ping")
        self.assertEqual(status, 403)

    def test_user_list_sees_mail_and_gmail_tools(self):
        status, body = self._rpc(basic("a@x.io", "pw"), "tools/list")
        self.assertEqual(status, 200)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["mail_echo", "gmail_echo"])

    def test_admin_list_sees_everything(self):
        status, body = self._rpc(f"Bearer {TOKEN}", "tools/list")
        self.assertEqual(status, 200)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["mail_echo", "admin_echo", "gmail_echo"])

    def test_user_can_call_mail_tools(self):
        status, body = self._rpc(basic("a@x.io", "pw"), "tools/call",
                                 {"name": "mail_echo", "arguments": {"a": 1}})
        self.assertEqual(status, 200)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"got": {"a": 1}})

    def test_user_can_call_gmail_tools(self):
        status, body = self._rpc(basic("a@x.io", "pw"), "tools/call",
                                 {"name": "gmail_echo", "arguments": {"a": 1}})
        self.assertEqual(status, 200)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"got": {"a": 1}})

    def test_user_cannot_call_admin_tools(self):
        status, body = self._rpc(basic("a@x.io", "pw"), "tools/call",
                                 {"name": "admin_echo", "arguments": {}})
        self.assertEqual(status, 403)
        self.assertIn("operator", json.loads(body)["error"]["message"])

    def test_admin_can_call_anything(self):
        for name in ("mail_echo", "admin_echo", "gmail_echo"):
            status, _ = self._rpc(f"Bearer {TOKEN}", "tools/call",
                                  {"name": name, "arguments": {}})
            self.assertEqual(status, 200, name)

    def test_notification_gets_202_without_body(self):
        status, body = self._post("/mcp", {"jsonrpc": "2.0",
                                           "method": "notifications/initialized"},
                                  auth=f"Bearer {TOKEN}")
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_malformed_json_is_400(self):
        status, body = self._post("/mcp", None, auth=f"Bearer {TOKEN}", raw=b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

    def test_batch_array_rejected(self):
        status, _ = self._post("/mcp", None, auth=f"Bearer {TOKEN}", raw=b"[]")
        self.assertEqual(status, 400)

    def test_oversized_body_is_413(self):
        status, _ = self._post("/mcp", None, auth=f"Bearer {TOKEN}", raw=b"x" * 2048)
        self.assertEqual(status, 413)

    def test_refuses_to_start_without_token(self):
        with self.assertRaises(ValueError):
            httpserve.serve_http(demo_server(), "127.0.0.1", 0, "")

    def test_main_passes_factory_not_instance(self):
        import os
        from unittest.mock import patch
        from mcp.mailu_mcp import server as server_mod
        seen = {}

        def fake_serve(factory, host, port, token, max_body):
            seen["factory"] = factory
            seen["host"] = host
            seen["port"] = port

        env = {"MCP_HTTP_ADMIN_TOKEN": "t"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(httpserve, "serve_http", fake_serve):
                server_mod.main(["--http", "--host", "0.0.0.0", "--port", "9000"])
        # Per-request identity requires the factory; a shared instance
        # would serve every caller the same mailbox.
        self.assertIs(seen["factory"], server_mod.build_server)
        self.assertEqual((seen["host"], seen["port"]), ("0.0.0.0", 9000))

    def test_parse_auth_cases(self):
        parse = httpserve.parse_auth
        self.assertEqual(parse(None, TOKEN)[0], "missing")
        self.assertEqual(parse(f"Bearer {TOKEN}", TOKEN)[0], "admin")
        self.assertEqual(parse("Bearer wrong", TOKEN)[0], "invalid")
        self.assertEqual(parse("Bearer x", "")[0], "invalid")
        role, ident = parse(basic("a@x.io", "s3cret"), TOKEN)
        self.assertEqual(role, "user")
        self.assertEqual(ident, {"email": "a@x.io", "secret": "s3cret"})


class RateLimitTests(ServedCase):
    limiter = httpserve.RateLimiter(max_failures=3, window=60)

    def test_failures_trip_429(self):
        for _ in range(3):
            status, _ = self._rpc("Bearer wrong", "ping")
            self.assertEqual(status, 403)
        status, body = self._rpc(f"Bearer {TOKEN}", "ping")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], -32003)


class IdentityTests(unittest.TestCase):
    """Per-request mailbox identity against the REAL server: two Basic
    users must drive two different IMAP logins (fake transport)."""

    def test_each_basic_user_gets_own_imap_login(self):
        from unittest.mock import patch
        from mcp.mailu_mcp.server import build_server

        logins = []

        class FakeIMAP:
            def __init__(self, *a, **k):
                pass

            def login(self, user, password):
                logins.append(user)
                return ("OK", [b"ok"])

            def logout(self):
                return ("OK", [b"bye"])

            def list(self):
                return ("OK", [b'(\\HasNoChildren) "." "INBOX"'])

        handler = httpserve.make_handler(build_server, TOKEN, 65536)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP):
                for email in ("alice@example.com", "bob@example.com"):
                    payload = json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "mail_mailboxes", "arguments": {}}}
                    ).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/mcp", data=payload, method="POST")
                    req.add_header("Authorization", basic(email, "pw"))
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        self.assertEqual(resp.status, 200)
                        json.load(resp)
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=10)
        # Alice's request logged into Alice's mailbox, Bob's into Bob's —
        # the server never substitutes its own identity.
        self.assertEqual(logins, ["alice@example.com", "bob@example.com"])


class GmailIdentityTests(unittest.TestCase):
    """Per-account Gmail import: callers use their own Gmail credentials,
    copies land in their own mailbox, state is namespaced per owner."""

    def setUp(self):
        from mcp.mailu_mcp.server import build_server
        handler = httpserve.make_handler(build_server, TOKEN, 65536)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.worker = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.worker.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(timeout=10)

    def _call(self, auth, name, args):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp",
                                     data=payload, method="POST")
        req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)

    def test_user_search_uses_per_call_credentials(self):
        from unittest.mock import patch
        from mcp.mailu_mcp import gmail_import as gi
        seen = {}

        def fake_search(cfg, query, max_results=50, label_ids=None):
            seen["token"] = cfg.access_token
            seen["client"] = cfg.client_id
            return []

        with patch.object(gi, "gmail_search", fake_search):
            status, body = self._call(basic("a@x.io", "pw"), "gmail_search",
                                      {"query": "x", "gmail_access_token": "user-tok"})
        self.assertEqual(status, 200)
        self.assertEqual(seen, {"token": "user-tok", "client": ""})
        self.assertNotIn("isError", json.dumps(body))

    def test_user_search_without_credentials_fails_closed(self):
        status, body = self._call(basic("a@x.io", "pw"), "gmail_search",
                                  {"query": "x"})
        self.assertEqual(status, 200)
        frame = body["result"]
        self.assertTrue(frame.get("isError"), body)
        self.assertIn("No Gmail credentials", frame["content"][0]["text"])

    def test_admin_search_falls_back_to_server_gmail(self):
        import os
        from unittest.mock import patch
        from mcp.mailu_mcp import gmail_import as gi
        seen = {}

        def fake_search(cfg, query, max_results=50, label_ids=None):
            seen["token"] = cfg.access_token
            return []

        env = {"GMAIL_ACCESS_TOKEN": "server-tok"}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(gi, "gmail_search", fake_search):
                status, _ = self._call(f"Bearer {TOKEN}", "gmail_search",
                                       {"query": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(seen["token"], "server-tok")

    def test_import_lands_in_caller_mailbox_with_owned_state(self):
        import base64 as _b64
        import os
        import tempfile
        from unittest.mock import patch
        from mcp.mailu_mcp import gmail_import as gi

        state = os.path.join(tempfile.mkdtemp(), "state.json")
        raw = _b64.urlsafe_b64encode(b"From: g\r\n\r\nb").decode()

        def fake_api(cfg, path, params=None):
            assert cfg.access_token == "alice-gmail-tok", cfg
            if path == "/users/me/messages":
                return {"messages": [{"id": "g1"}]}
            if (params or {}).get("format") == "raw":
                return {"raw": raw}
            return {"id": "g1", "labelIds": [], "payload": {"headers": []},
                    "snippet": ""}

        appended = []

        def fake_append(cfg, mailbox, msg, flags="(\\Seen)"):
            appended.append((cfg.imap_user, mailbox))
            return True

        env = {"MAILU_IMPORT_STATE": state}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(gi, "_api", fake_api):
                with patch("mcp.mailu_mcp.mail.append_message", fake_append):
                    status, body = self._call(
                        basic("alice@example.com", "pw"), "gmail_import_run",
                        {"query": "x", "job_name": "j",
                         "gmail_access_token": "alice-gmail-tok"})
            # status calls need the same state path from the environment
            _, alice = self._call(basic("alice@example.com", "pw"),
                                  "gmail_import_status", {"job_name": "j"})
            _, bob = self._call(basic("bob@example.com", "pw"),
                                "gmail_import_status", {"job_name": "j"})
        self.assertEqual(status, 200)
        report = json.loads(body["result"]["content"][0]["text"])
        self.assertEqual(report["copied"], 1)
        # destination session belongs to the Basic caller, not the server
        self.assertEqual(appended, [("alice@example.com", "INBOX")])
        # state namespaced: alice's status shows it, bob's shows nothing
        self.assertEqual(json.loads(alice["result"]["content"][0]["text"])["copied_total"], 1)
        self.assertEqual(json.loads(bob["result"]["content"][0]["text"])["copied_total"], 0)
        with open(state) as fh:
            keys = set(json.load(fh))
        self.assertEqual(keys, {"alice@example.com:j"})


if __name__ == "__main__":
    unittest.main()
