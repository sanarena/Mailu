"""Unit tests for the mail lane (no network: fakes for imaplib/smtplib)."""
import base64
import io
import json
import sys
import unittest
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

from mcp.mailu_mcp import mail as mail_mod
from mcp.mailu_mcp.config import MailConfig
from mcp.mailu_mcp.protocol import McpServer
from mcp.mailu_mcp.server import build_server

RAW_HEADER = (b"2 (FLAGS (\\Seen) RFC822.SIZE 1234 "
              b"BODY[HEADER.FIELDS (FROM TO SUBJECT)] {90}\r\n"
              b"From: Alice <alice@example.com>\r\nTo: bob@mailu.test\r\n"
              b"Subject: Hello there\r\n\r\n")


def _raw_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@mailu.test"
    msg["Subject"] = "Hello there"
    msg.set_content("Hi Bob")
    return msg.as_bytes()


class FakeIMAP:
    last = None

    def __init__(self, *a, **kw):
        FakeIMAP.last = self
        self.selected = None
        self.uid_calls = []

    def login(self, u, p):
        return ("OK", [b"ok"])

    def logout(self):
        return ("OK", [b"bye"])

    def select(self, box, readonly=False):
        self.selected = box
        return ("OK", [b"1"])

    def list(self):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"',
                       b'(\\HasNoChildren) "/" "Sent"'])

    def uid(self, cmd, *args):
        self.uid_calls.append((cmd, args))
        if cmd == "search":
            return ("OK", [b"1 2 3"])
        if cmd == "fetch":
            uid = args[0]
            if "HEADER.FIELDS" in str(args[1]):
                return ("OK", [(b"2 (FLAGS (\\Seen)", b"\\Seen)"),
                               (b"RFC822.SIZE 1234", b""),
                               (b"BODY[HEADER]", RAW_HEADER.split(b"\r\n\r\n", 1)[0]
                                if False else b"From: Alice <alice@example.com>\r\n"
                                b"To: bob@mailu.test\r\nSubject: Hello there\r\n")])
            return ("OK", [(f"{uid} (BODY[] {{{len(_raw_message())}}}".encode(),
                             _raw_message())])
        if cmd in ("store", "copy"):
            return ("OK", [b"ok"])
        raise AssertionError(cmd)

    def append(self, box, flags, date, raw):
        self.appended = (box, flags, raw)
        return ("OK", [b"ok"])

    def copy(self, *a):
        return ("OK", [b"ok"])

    def expunge(self):
        return ("OK", [b"ok"])


class MailTests(unittest.TestCase):
    def test_search_criteria_builder(self):
        c = mail_mod.build_search_criteria(unseen_only=True, from_="a@x.io",
                                          subject="hi", since="01-Jan-2025")
        self.assertIn("UNSEEN", c)
        self.assertIn('FROM "a@x.io"', c)
        self.assertIn('SUBJECT "hi"', c)
        self.assertIn('SINCE "01-Jan-2025"', c)
        self.assertEqual(mail_mod.build_search_criteria(), "ALL")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_search_and_list(self):
        cfg = MailConfig(imap_user="u", imap_password="p")
        res = mail_mod.search_emails(cfg, limit=2, unseen_only=True)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["subject"], "Hello there")
        boxes = mail_mod.list_mailboxes(cfg)
        self.assertIn("INBOX", boxes)

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_get_email_full(self):
        cfg = MailConfig(imap_user="u", imap_password="p")
        msg = mail_mod.get_email(cfg, "INBOX", 3)
        self.assertEqual(msg["subject"], "Hello there")
        self.assertIn("Hi Bob", msg["body_text"])

    def test_send_email_builds_message(self):
        cfg = MailConfig(smtp_host="h", smtp_port=587, smtp_user="me@mailu.test",
                         smtp_password="s", default_from="me@mailu.test")
        fake = MagicMock()
        fake.__enter__ = MagicMock(return_value=fake)
        fake.__exit__ = MagicMock(return_value=False)
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
            out = mail_mod.send_email(cfg, to=["a@x.io"], cc=["c@x.io"],
                                      bcc=["h@x.io"], subject="s", body_text="b",
                                      reply_to="r@x.io", priority="high",
                                      attachments=[{"filename": "n.txt",
                                                    "content": base64.b64encode(b"data").decode(),
                                                    "maintype": "text",
                                                    "subtype": "plain"}])
        self.assertIn("a@x.io", out["to"])
        sent = fake.send_message.call_args
        self.assertEqual(sent[1]["from_addr"], "me@mailu.test")
        self.assertIn("h@x.io", sent[1]["to_addrs"])

    def test_mcp_tools_list_and_call(self):
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list"}) + "\n")
        stdout = io.StringIO()
        build_server().run(stdin, stdout)
        resp = json.loads(stdout.getvalue().strip().splitlines()[-1])
        names = {t["name"] for t in resp["result"]["tools"]}
        for expected in ("mail_search", "mail_read", "mail_send", "mail_mailboxes",
                         "mail_flag", "mail_move", "mail_delete"):
            self.assertIn(expected, names)

    def test_mcp_unknown_tool(self):
        srv = McpServer("t")
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", resp)


if __name__ == "__main__":
    unittest.main()
