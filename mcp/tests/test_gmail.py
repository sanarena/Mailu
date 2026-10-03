"""Unit tests for the Gmail import lane (no network: fakes)."""
import base64
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, ".")

from mcp.mailu_mcp import gmail_import as gi
from mcp.mailu_mcp.config import AppConfig, GmailConfig, MailConfig
from mcp.mailu_mcp.server import build_server

RAW = b"From: alice@gmail.com\r\nTo: me@mailu.test\r\nSubject: hi\r\n\r\nbody"


def _raw_b64():
    return base64.urlsafe_b64encode(RAW).decode().rstrip("=")


class GmailTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.state = os.path.join(tmp, "state.json")
        self.gmail = GmailConfig(access_token="tok")
        self.app = AppConfig(mail=MailConfig(imap_user="u", imap_password="p"),
                             gmail=self.gmail)

    def _api(self, calls):
        def fake(cfg, path, params=None):
            calls.append((path, params))
            if path == "/users/me/messages":
                return {"messages": [{"id": "g1", "threadId": "t1"},
                                     {"id": "g2", "threadId": "t2"}]}
            if path == "/users/me/messages/g1":
                if (params or {}).get("format") == "raw":
                    return {"raw": _raw_b64()}
                return {"id": "g1", "labelIds": ["INBOX"],
                        "payload": {"headers": [
                            {"name": "From", "value": "alice@gmail.com"},
                            {"name": "Subject", "value": "hi"}]},
                        "snippet": "snip"}
            if path == "/users/me/messages/g2":
                if (params or {}).get("format") == "raw":
                    return {"raw": _raw_b64()}
                return {"id": "g2", "labelIds": ["INBOX"],
                        "payload": {"headers": [
                            {"name": "From", "value": "bob@gmail.com"},
                            {"name": "Subject", "value": "yo"}]},
                        "snippet": "snip2"}
            raise AssertionError(path)
        return fake

    def test_search_returns_metadata(self):
        calls = []
        with patch.object(gi, "_api", self._api(calls)):
            out = gi.gmail_search(self.gmail, "from:alice", max_results=10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["subject"], "hi")
        list_call = [c for c in calls if c[0] == "/users/me/messages"][0]
        self.assertEqual(list_call[1]["q"], "from:alice")

    def test_fetch_raw_decodes(self):
        with patch.object(gi, "_api", self._api([])):
            self.assertEqual(gi.gmail_fetch_raw(self.gmail, "g1"), RAW)

    def test_copy_job_appends_and_is_idempotent(self):
        calls = []
        appended = []
        with patch.object(gi, "_api", self._api(calls)):
            with patch("mcp.mailu_mcp.mail.append_message",
                       lambda cfg, box, raw, flags="(\\Seen)": appended.append((box, raw)) or True):
                job = gi.CopyJob(query="in:inbox", dest_mailbox="INBOX",
                                 job_name="j1", state_path=self.state)
                rep = gi.run_copy_job(self.app, job).as_dict()
        self.assertEqual(rep["copied"], 2)
        self.assertEqual(appended[0], ("INBOX", RAW))
        # second run skips both
        with patch.object(gi, "_api", self._api([])):
            with patch("mcp.mailu_mcp.mail.append_message",
                       lambda *a, **k: appended.append(a) or True):
                rep2 = gi.run_copy_job(self.app, job).as_dict()
        self.assertEqual(rep2["copied"], 0)
        self.assertEqual(rep2["skipped"], 2)
        st = gi.job_status("j1", self.state)
        self.assertEqual(st["copied_total"], 2)

    def test_dry_run_copies_nothing(self):
        appended = []
        with patch.object(gi, "_api", self._api([])):
            with patch("mcp.mailu_mcp.mail.append_message",
                       lambda *a, **k: appended.append(a) or True):
                job = gi.CopyJob(query="in:inbox", dry_run=True,
                                 job_name="dry", state_path=self.state)
                rep = gi.run_copy_job(self.app, job).as_dict()
        self.assertTrue(rep["dry_run"])
        self.assertEqual(appended, [])
        self.assertEqual(rep["matched"], 2)

    def test_oauth_required(self):
        with self.assertRaises(gi.GmailError):
            gi.ensure_access_token(GmailConfig())

    def test_gmail_tools_registered(self):
        names = set(build_server().tools)
        for expected in ("gmail_search", "gmail_import_run", "gmail_import_status"):
            self.assertIn(expected, names, expected)
        self.assertGreaterEqual(len(names), 7 + 38 + 3)


if __name__ == "__main__":
    unittest.main()
