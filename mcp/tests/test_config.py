"""Unit tests for configuration loading."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

from mcp.mailu_mcp.config import AppConfig, load_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.mail.imap_port, 993)
        self.assertEqual(cfg.mail.smtp_port, 587)
        self.assertTrue(cfg.mail.imap_ssl)
        self.assertEqual(cfg.admin.timeout, 30)

    def test_env_override(self):
        env = {"MAILU_IMAP_HOST": "mail.example.com", "MAILU_IMAP_PORT": "143",
               "MAILU_IMAP_SSL": "false", "MAILU_API_TOKEN": "tok"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.mail.imap_host, "mail.example.com")
        self.assertEqual(cfg.mail.imap_port, 143)
        self.assertFalse(cfg.mail.imap_ssl)
        self.assertEqual(cfg.admin.api_token, "tok")
        self.assertEqual(cfg.mail.timeout, 60)
        with patch.dict(os.environ, {"MAILU_TIMEOUT": "5"}, clear=True):
            self.assertEqual(load_config().mail.timeout, 5)

    def test_bad_int_names_the_variable(self):
        with patch.dict(os.environ, {"MAILU_IMAP_PORT": "abc"}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                load_config()
        self.assertIn("MAILU_IMAP_PORT", str(ctx.exception))

    def test_http_defaults_and_override(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.http.host, "127.0.0.1")
        self.assertEqual(cfg.http.port, 8000)
        self.assertEqual(cfg.http.token, "")
        self.assertEqual(cfg.http.max_body, 1024 * 1024)
        env = {"MCP_HTTP_HOST": "0.0.0.0", "MCP_HTTP_PORT": "9000",
               "MCP_HTTP_ADMIN_TOKEN": "s3cret"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.http.host, "0.0.0.0")
        self.assertEqual(cfg.http.port, 9000)
        self.assertEqual(cfg.http.token, "s3cret")


if __name__ == "__main__":
    unittest.main()
