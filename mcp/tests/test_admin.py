"""Unit tests for the admin lane (no network: fake urlopen)."""
import io
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

from mcp.mailu_mcp.admin import AdminClient, AdminError
from mcp.mailu_mcp.config import AdminConfig
from mcp.mailu_mcp.server import build_server


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


captured = {}


def fake_urlopen(req, timeout=None):
    captured["url"] = req.full_url
    captured["method"] = req.get_method()
    captured["auth"] = req.get_header("Authorization")
    if req.data:
        captured["body"] = json.loads(req.data.decode())
    if "domain/example.com" in req.full_url and req.get_method() == "GET":
        return FakeResp({"name": "example.com"})
    return FakeResp([{"name": "example.com"}])


class AdminTests(unittest.TestCase):
    def setUp(self):
        captured.clear()
        self.client = AdminClient(AdminConfig(base_url="https://m.test/api",
                                             api_token="tok"))

    def test_requires_token(self):
        with self.assertRaises(ValueError):
            AdminClient(AdminConfig(base_url="https://m.test/api", api_token=""))

    @patch("mcp.mailu_mcp.admin.urllib.request.urlopen", fake_urlopen)
    def test_list_and_get(self):
        self.assertEqual(self.client.list_domains()[0]["name"], "example.com")
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["auth"], "Bearer tok")
        self.assertTrue(captured["url"].endswith("/v1/domain"))
        self.assertEqual(self.client.get_domain("example.com")["name"], "example.com")

    @patch("mcp.mailu_mcp.admin.urllib.request.urlopen", fake_urlopen)
    def test_create_user_body(self):
        self.client.create_user("a@example.com", "s3cret", quota_bytes=10,
                                forward_enabled=True,
                                forward_destination=["b@example.com"])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["email"], "a@example.com")
        self.assertEqual(captured["body"]["quota_bytes"], 10)

    @patch("mcp.mailu_mcp.admin.urllib.request.urlopen", fake_urlopen)
    def test_patch_body_has_no_path_identifiers(self):
        self.client.update_user("a@example.com", comment="hi", quota_bytes=5)
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(captured["body"], {"comment": "hi", "quota_bytes": 5})
        self.client.update_domain("example.com", comment="c")
        self.assertEqual(captured["body"], {"comment": "c"})
        self.client.update_token("7", comment="t")
        self.assertEqual(captured["body"], {"comment": "t"})

    @patch("mcp.mailu_mcp.admin.urllib.request.urlopen", fake_urlopen)
    def test_delete_alias(self):
        self.client.delete_alias("x@example.com")
        self.assertEqual(captured["method"], "DELETE")
        self.assertTrue(captured["url"].endswith("/v1/alias/x@example.com"))

    @patch("mcp.mailu_mcp.admin.urllib.request.urlopen", fake_urlopen)
    def test_plus_address_path_is_quoted(self):
        self.client.get_alias("a+b@example.com")
        self.assertTrue(captured["url"].endswith("/v1/alias/a%2Bb@example.com"))

    def test_http_error_surfaces(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {},
                                         io.BytesIO(b'{"message": "nope"}'))

        with patch("mcp.mailu_mcp.admin.urllib.request.urlopen", boom):
            with self.assertRaises(AdminError) as ctx:
                self.client.get_user("z@example.com")
        self.assertEqual(ctx.exception.status, 404)

    def test_401_surfaces_as_mcp_error(self):
        import json as _json
        import io as _io
        import os
        import urllib.error

        def denied(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "unauth", {},
                                         _io.BytesIO(b'{"message": "bad token"}'))

        env = {"MAILU_API_BASE": "https://m.test/api", "MAILU_API_TOKEN": "wrong"}
        stdin = _io.StringIO(_json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "admin_users_list", "arguments": {}}}) + "\n")
        stdout = _io.StringIO()
        with patch.dict(os.environ, env):
            with patch("mcp.mailu_mcp.admin.urllib.request.urlopen", denied):
                build_server().run(stdin, stdout)
        resp = _json.loads(stdout.getvalue().strip())
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("401", resp["result"]["content"][0]["text"])

    def test_all_admin_tools_registered(self):
        names = set(build_server().tools)
        for expected in ("admin_domains_list", "admin_domain_get",
                         "admin_domain_create", "admin_domain_update",
                         "admin_domain_delete", "admin_domain_dkim",
                         "admin_managers_list", "admin_manager_add",
                         "admin_manager_check", "admin_manager_remove",
                         "admin_domain_users", "admin_alternatives_list",
                         "admin_alternative_get", "admin_alternative_create",
                         "admin_alternative_delete", "admin_users_list",
                         "admin_user_get", "admin_user_create",
                         "admin_user_update", "admin_user_delete",
                         "admin_aliases_list", "admin_alias_get",
                         "admin_alias_create", "admin_alias_update",
                         "admin_alias_delete", "admin_aliases_by_domain",
                         "admin_relays_list", "admin_relay_get",
                         "admin_relay_create", "admin_relay_update",
                         "admin_relay_delete", "admin_tokens_list",
                         "admin_token_create", "admin_tokens_of_user",
                         "admin_user_token_create", "admin_token_get",
                         "admin_token_update", "admin_token_delete"):
            self.assertIn(expected, names, expected)
        mail_names = {n for n in names if n.startswith("mail_")}
        self.assertGreaterEqual(len(mail_names), 7)


if __name__ == "__main__":
    unittest.main()
