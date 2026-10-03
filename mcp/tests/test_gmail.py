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

    def test_search_total_is_capped(self):
        calls = []

        def fake(cfg, path, params=None):
            if path == "/users/me/messages":
                calls.append(params.get("maxResults"))
                return {"messages": [{"id": f"g{n}"} for n in range(500)],
                        "nextPageToken": "more"}
            return {"id": "x", "labelIds": [], "payload": {"headers": []},
                    "snippet": ""}

        with patch.object(gi, "_api", fake):
            out = gi.gmail_search(self.gmail, "x", max_results=10 ** 9)
        self.assertEqual(len(out), 2000)
        self.assertTrue(calls and all(c <= 500 for c in calls))
        with patch.object(gi, "_api", fake):
            self.assertEqual(gi.gmail_search(self.gmail, "x", max_results=0), [])

    def test_state_key_owner_case_insensitive(self):
        self.assertEqual(gi.state_key("j", "Alice@Example.COM"), "alice@example.com:j")
        self.assertEqual(gi.state_key("j"), "j")

    def test_label_ids_sent_as_repeated_params(self):
        seen = {}

        def fake(cfg, path, params=None):
            seen.update(params or {})
            return {"messages": []}

        with patch.object(gi, "_api", fake):
            gi.gmail_search(self.gmail, "x", label_ids=["INBOX", "STARRED"])
        self.assertEqual(seen.get("labelIds"), ["INBOX", "STARRED"])

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

    def test_search_follows_next_page(self):
        calls = []

        def fake(cfg, path, params=None):
            calls.append(params.get("pageToken") if params else None)
            if path == "/users/me/messages" and not (params or {}).get("pageToken"):
                return {"messages": [{"id": "g1", "threadId": "t1"}],
                        "nextPageToken": "p2"}
            if path == "/users/me/messages" :
                return {"messages": [{"id": "g2", "threadId": "t2"}]}
            return {"id": "x", "labelIds": [], "payload": {"headers": []}, "snippet": ""}

        with patch.object(gi, "_api", fake):
            out = gi.gmail_search(self.gmail, "x", max_results=10)
        self.assertEqual([m["id"] for m in out], ["g1", "g2"])
        self.assertIn("p2", calls)

    def test_partial_append_failure_continues_and_saves(self):
        def appender(cfg, box, raw, flags="(\\Seen)"):
            if raw == b"FAIL":
                raise OSError("disk full")
            return True

        raws = {"g1": RAW, "g2": b"FAIL", "g3": RAW}

        def fake(cfg, path, params=None):
            if path == "/users/me/messages":
                return {"messages": [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}]}
            if (params or {}).get("format") == "raw":
                import base64 as _b
                gid = path.rsplit("/", 1)[-1]
                return {"raw": _b.urlsafe_b64encode(raws[gid]).decode()}
            return {"id": "x", "labelIds": [], "payload": {"headers": []}, "snippet": ""}

        with patch.object(gi, "_api", fake):
            with patch("mcp.mailu_mcp.mail.append_message", appender):
                job = gi.CopyJob(query="x", job_name="part", state_path=self.state)
                rep = gi.run_copy_job(self.app, job).as_dict()
        self.assertEqual(rep["copied"], 2)
        self.assertEqual(rep["failed"], 1)
        self.assertTrue(rep["errors"][0].startswith("g2:"))
        st = gi.job_status("part", self.state)
        self.assertEqual(sorted(st["copied_ids"]), ["g1", "g3"])

    def test_corrupt_state_file_starts_empty(self):
        with open(self.state, "w") as fh:
            fh.write("{not json")
        self.assertEqual(gi.load_state(self.state), {})
        self.assertEqual(gi.job_status("nope", self.state)["copied_total"], 0)

    def test_mark_read_failure_keeps_copy_and_records(self):
        def fake(cfg, path, params=None):
            if path == "/users/me/messages":
                return {"messages": [{"id": "g1"}]}
            if (params or {}).get("format") == "raw":
                return {"raw": _raw_b64()}
            return {"id": "g1", "labelIds": [], "payload": {"headers": []}, "snippet": ""}

        with patch.object(gi, "_api", fake):
            with patch("mcp.mailu_mcp.mail.append_message", lambda *a, **k: True):
                with patch.object(gi, "gmail_mark_read",
                                  side_effect=OSError("gmail down")):
                    job = gi.CopyJob(query="x", job_name="mr",
                                     mark_source_read=True, state_path=self.state)
                    rep = gi.run_copy_job(self.app, job).as_dict()
        self.assertEqual(rep["copied"], 1)
        self.assertEqual(rep["failed"], 0)
        self.assertEqual(len(rep["errors"]), 1)
        self.assertIn("mark-read failed", rep["errors"][0])
        # recorded as done: no duplicate on re-run
        self.assertEqual(gi.job_status("mr", self.state)["copied_total"], 1)

    def test_mark_read_http_error_maps_to_gmail_error(self):
        import io as _io
        import urllib.error as _ue

        def boom(req, timeout=None):
            raise _ue.HTTPError(req.full_url, 500, "srv", {}, _io.BytesIO(b"x"))

        with patch("mcp.mailu_mcp.gmail_import.urllib.request.urlopen", boom):
            with self.assertRaises(gi.GmailError):
                gi.gmail_mark_read(self.gmail, "g1")

    def test_oauth_required(self):
        with self.assertRaises(gi.GmailError):
            gi.ensure_access_token(GmailConfig())

    def test_gmail_tools_registered(self):
        names = set(build_server().tools)
        for expected in ("gmail_search", "gmail_import_run", "gmail_import_status"):
            self.assertIn(expected, names, expected)
        self.assertGreaterEqual(len(names), 7 + 43 + 3)


if __name__ == "__main__":
    unittest.main()
