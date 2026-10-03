"""HTTP transport tests: real ThreadingHTTPServer on 127.0.0.1, urllib client."""
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

TOKEN = "test-token-123"


def demo_server() -> McpServer:
    srv = McpServer("demo", "9.9")

    @srv.tool("echo", "echo back", {"type": "object"})
    def _echo(args):
        return {"got": args}

    return srv


class HttpTests(unittest.TestCase):
    def setUp(self):
        handler = httpserve.make_handler(demo_server(), TOKEN, 1024)
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

    def _post(self, path, payload, token=TOKEN, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        req = urllib.request.Request(self._url(path), data=body, method="POST")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _get(self, path, token=None):
        req = urllib.request.Request(self._url(path), method="GET")
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_healthz_needs_no_auth(self):
        status, _, body = self._get("/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["tools"], 1)

    def test_unknown_paths_404(self):
        self.assertEqual(self._get("/")[0], 404)
        self.assertEqual(self._get("/mcp")[0], 404)
        status, _ = self._post("/elsewhere", {"jsonrpc": "2.0"})
        self.assertEqual(status, 404)

    def test_missing_token_is_401(self):
        status, body = self._post("/mcp", {"jsonrpc": "2.0", "id": 1,
                                           "method": "ping"}, token=None)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], -32001)

    def test_wrong_token_is_403(self):
        status, body = self._post("/mcp", {"jsonrpc": "2.0", "id": 1,
                                           "method": "ping"}, token="nope")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], -32002)

    def test_non_bearer_scheme_rejected(self):
        req = urllib.request.Request(self._url("/mcp"), data=b"{}", method="POST")
        req.add_header("Authorization", "Basic dXNlcjpwYXNz")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 403")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)

    def test_tools_list_and_call_over_http(self):
        status, body = self._post("/mcp", {"jsonrpc": "2.0", "id": 1,
                                           "method": "tools/list"})
        self.assertEqual(status, 200)
        tools = json.loads(body)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["echo"])
        status, body = self._post(
            "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "echo", "arguments": {"a": 1}}})
        self.assertEqual(status, 200)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"got": {"a": 1}})

    def test_notification_gets_202_without_body(self):
        status, body = self._post("/mcp", {"jsonrpc": "2.0",
                                           "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_malformed_json_is_400(self):
        status, body = self._post("/mcp", None, raw=b"{oops")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

    def test_batch_array_rejected(self):
        status, body = self._post("/mcp", None, raw=b"[]")
        self.assertEqual(status, 400)

    def test_oversized_body_is_413(self):
        status, _ = self._post("/mcp", None, raw=b"x" * 2048)
        self.assertEqual(status, 413)

    def test_refuses_to_start_without_token(self):
        with self.assertRaises(ValueError):
            httpserve.serve_http(demo_server(), "127.0.0.1", 0, "")

    def test_check_auth_cases(self):
        self.assertTrue(httpserve.check_auth("Bearer abc", "abc"))
        self.assertTrue(httpserve.check_auth("bearer abc", "abc"))
        self.assertFalse(httpserve.check_auth(None, "abc"))
        self.assertFalse(httpserve.check_auth("Bearer abc", ""))
        self.assertFalse(httpserve.check_auth("Bearer", "abc"))
        self.assertFalse(httpserve.check_auth("Basic abc", "abc"))


if __name__ == "__main__":
    unittest.main()
