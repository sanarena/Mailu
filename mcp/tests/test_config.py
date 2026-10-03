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

    def test_bad_int_names_the_variable(self):
        with patch.dict(os.environ, {"MAILU_IMAP_PORT": "abc"}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                load_config()
        self.assertIn("MAILU_IMAP_PORT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
