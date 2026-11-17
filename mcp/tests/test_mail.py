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

# Realistic imaplib UID FETCH response: FLAGS + SIZE in the label,
# header bytes in the payload, trailing b')' terminator.
HDR = (b"From: Alice <alice@example.com>\r\nTo: bob@mailu.test\r\n"
       b"Subject: Hello there\r\nDate: Thu, 02 Oct 2026 10:00:00 +0000\r\n"
       b"Message-ID: <abc123@example.com>\r\n")
FETCH_LABEL = (b'3 (FLAGS (\\Seen \\Flagged) RFC822.SIZE 4321 '
               b'BODY[HEADER.FIELDS (From To Cc Date Subject Message-ID '
               b'In-Reply-To)] {%d}' % len(HDR))


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
        # Dovecot '.' delimiter variant (Mailu default is not '/').
        return ("OK", [b'(\\HasNoChildren) "." "INBOX"',
                       b'(\\HasNoChildren) "." "INBOX.Sent"'])

    def uid(self, cmd, *args):
        self.uid_calls.append((cmd, args))
        if cmd == "search":
            return ("OK", [b"1 2 3"])
        if cmd == "fetch":
            uid = args[0]
            if "HEADER.FIELDS" in str(args[1]):
                return ("OK", [(FETCH_LABEL, HDR), b")"])
            raw = _raw_message()
            return ("OK", [(f"{uid} (BODY[] {{{len(raw)}}}".encode(), raw), b")"])
        if cmd in ("store", "copy"):
            return ("OK", [b"ok"])
        raise AssertionError(cmd)

    def append(self, box, flags, date, raw):
        self.appended = (box, flags, raw)
        return ("OK", [b"ok"])

    def expunge(self):
        return ("OK", [b"ok"])


class DovecotIMAP(FakeIMAP):
    """LIST the way Mailu's Dovecot answers: unquoted names, special-use."""
    append_result = ("OK", [b"ok"])
    instances = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        DovecotIMAP.instances.append(self)
        self.created = []

    def list(self):
        return ("OK", [b'(\\HasNoChildren \\Archive) "." Archive',
                       b'(\\HasNoChildren \\Sent) "." Sent',
                       b'(\\HasNoChildren) "." "Old Stuff"',
                       (b'(\\HasNoChildren) "." {9}', b'Odd"Name'), b'',
                       b'(\\HasNoChildren) "." INBOX'])

    def append(self, box, flags, date, raw):
        self.appended = (box, flags, date, raw)
        return DovecotIMAP.append_result

    def create(self, box):
        self.created.append(box)
        return ("OK", [b"created"])


def _smtp_fake():
    fake = MagicMock()
    fake.__enter__ = MagicMock(return_value=fake)
    fake.__exit__ = MagicMock(return_value=False)
    return fake


class SentCopyTests(unittest.TestCase):
    def setUp(self):
        DovecotIMAP.instances = []
        DovecotIMAP.append_result = ("OK", [b"ok"])
        self.cfg = MailConfig(imap_user="me@mailu.test", imap_password="p",
                              smtp_host="h", smtp_port=587, smtp_user="me@mailu.test",
                              smtp_password="p", default_from="me@mailu.test")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DovecotIMAP)
    def test_list_mailboxes_unquoted_dovecot_names(self):
        self.assertEqual(mail_mod.list_mailboxes(self.cfg),
                         ["Archive", "Sent", "Old Stuff", 'Odd"Name', "INBOX"])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DovecotIMAP)
    def test_send_saves_copy_to_sent_with_bcc(self):
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
            out = mail_mod.send_email(self.cfg, to=["a@x.io"], bcc=["hidden@x.io"],
                                      subject="Quarterly", body_text="numbers")
        self.assertEqual(out["saved_to_sent"], "Sent")
        box, flags, date, raw = DovecotIMAP.instances[-1].appended
        self.assertEqual(box, '"Sent"')
        self.assertEqual(flags, "(\\Seen)")
        self.assertTrue(date.startswith('"'))
        self.assertIn(b"Subject: Quarterly", raw)
        self.assertIn(b"Bcc: hidden@x.io", raw)          # sender's copy keeps Bcc
        self.assertIn(b"\r\n", raw)                       # CRLF for APPEND
        sent_msg = fake.send_message.call_args[0][0]
        self.assertIsNone(sent_msg["Bcc"])                # recipients never see it
        self.assertIn("hidden@x.io", fake.send_message.call_args[1]["to_addrs"])

    def test_save_sent_false_skips_imap(self):
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake), \
             patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL") as imap:
            out = mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s", save_sent=False)
        imap.assert_not_called()
        self.assertIsNone(out["saved_to_sent"])

    def test_config_default_off_respected(self):
        self.cfg.save_sent = False
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake), \
             patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL") as imap:
            mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s")
            imap.assert_not_called()
            mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s", save_sent=True)
            imap.assert_called_once()

    def test_no_mailbox_login_skips(self):
        cfg = MailConfig(smtp_host="h", smtp_port=587, default_from="me@x.io")
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake), \
             patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL") as imap:
            out = mail_mod.send_email(cfg, to=["a@x.io"], subject="s")
        imap.assert_not_called()
        self.assertIsNone(out["saved_to_sent"])
        self.assertIn("skipped", out["sent_copy"])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DovecotIMAP)
    def test_copy_failure_does_not_fail_send(self):
        DovecotIMAP.append_result = ("NO", [b"[OVERQUOTA] Quota exceeded"])
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
            out = mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s")
        fake.send_message.assert_called_once()
        self.assertIsNone(out["saved_to_sent"])
        self.assertIn("OVERQUOTA", out["sent_copy_error"])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DovecotIMAP)
    def test_trycreate_creates_folder_and_retries(self):
        results = [("NO", [b"[TRYCREATE] Mailbox doesn't exist"]), ("OK", [b"ok"])]

        def append(self, box, flags, date, raw):
            self.appended = (box, flags, date, raw)
            return results.pop(0)

        with patch.object(DovecotIMAP, "append", append):
            fake = _smtp_fake()
            with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
                out = mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s")
        self.assertEqual(out["saved_to_sent"], "Sent")
        self.assertEqual(DovecotIMAP.instances[-1].created, ['"Sent"'])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_sent_fallback_by_name(self):
        # no \Sent flag: falls back to a well-known name (INBOX.Sent here)
        fake = _smtp_fake()
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
            out = mail_mod.send_email(self.cfg, to=["a@x.io"], subject="s")
        self.assertEqual(out["saved_to_sent"], "INBOX.Sent")

    def test_tool_schema_has_save_sent(self):
        tool = build_server().tools["mail_send"]
        props = tool.input_schema["properties"] if hasattr(tool, "input_schema") else tool.schema["properties"]
        self.assertIn("save_sent", props)


class DraftIMAP(DovecotIMAP):
    capabilities = ("IMAP4REV1", "UIDPLUS")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.expunged = []

    def list(self):
        return ("OK", [b'(\\HasNoChildren \\Drafts) "." Drafts',
                       b'(\\HasNoChildren \\Sent) "." Sent',
                       b'(\\HasNoChildren) "." INBOX'])

    def append(self, box, flags, date, raw):
        self.appended = (box, flags, date, raw)
        return ("OK", [b"[APPENDUID 1701 42] Append completed."])

    def uid(self, cmd, *args):
        self.uid_calls.append((cmd, args))
        if cmd == "expunge":
            self.expunged.append(args[0])
        return ("OK", [b"ok"])


class DraftTests(unittest.TestCase):
    def setUp(self):
        DovecotIMAP.instances = []
        self.cfg = MailConfig(imap_user="sales@hitsteps.test", imap_password="p",
                              smtp_host="h", smtp_port=465, smtp_user="sales@hitsteps.test",
                              smtp_password="p", default_from="sales@hitsteps.test")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DraftIMAP)
    def test_draft_saved_to_drafts_never_sent(self):
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP_SSL") as ssl, \
             patch("mcp.mailu_mcp.mail.smtplib.SMTP") as plain:
            out = mail_mod.save_draft(self.cfg, to=["customer@x.io"], bcc=["armin@x.io"],
                                      subject="Re: Pricing", body_text="Thanks!",
                                      in_reply_to="<orig@x.io>", references="<orig@x.io>")
        ssl.assert_not_called()
        plain.assert_not_called()
        self.assertEqual(out["saved_to"], "Drafts")
        self.assertEqual(out["uid"], 42)
        self.assertFalse(out["sent"])
        self.assertEqual(out["from"], "sales@hitsteps.test")
        box, flags, date, raw = DovecotIMAP.instances[-1].appended
        self.assertEqual(box, '"Drafts"')
        self.assertEqual(flags, "(\\Draft \\Seen)")
        for needle in (b"From: sales@hitsteps.test", b"To: customer@x.io", b"Bcc: armin@x.io",
                       b"Subject: Re: Pricing", b"In-Reply-To: <orig@x.io>", b"References: <orig@x.io>"):
            self.assertIn(needle, raw)

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", DraftIMAP)
    def test_replace_uid_removes_old_draft(self):
        out = mail_mod.save_draft(self.cfg, to=["c@x.io"], subject="v2", replace_uid=41)
        conn = DovecotIMAP.instances[-1]
        self.assertIn(("store", ("41", "+FLAGS", "(\\Deleted)")), conn.uid_calls)
        self.assertEqual(conn.expunged, ["41"])  # UID EXPUNGE: only that draft
        self.assertEqual(out["replaced_uid"], 41)

    def _move_with_caps(self, caps):
        class MoveIMAP(FakeIMAP):
            capabilities = caps

            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.bare_expunges = 0

            def uid(self, cmd, *args):
                if cmd in ("move", "expunge"):
                    self.uid_calls.append((cmd, args))
                    return ("OK", [b"ok"])
                return super().uid(cmd, *args)

            def expunge(self):
                self.bare_expunges += 1
                return ("OK", [b"ok"])
        with patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", MoveIMAP):
            ok = mail_mod.move_message(self.cfg, "INBOX", 7, "Archive")
        return ok, FakeIMAP.last

    def test_move_prefers_uid_move(self):
        ok, conn = self._move_with_caps(("IMAP4REV1", "UIDPLUS", "MOVE"))
        self.assertTrue(ok)
        self.assertEqual(conn.uid_calls, [("move", ("7", '"Archive"'))])
        self.assertEqual(conn.bare_expunges, 0)

    def test_move_without_move_expunges_only_that_uid(self):
        ok, conn = self._move_with_caps(("IMAP4REV1", "UIDPLUS"))
        self.assertTrue(ok)
        self.assertEqual([c for c, _ in conn.uid_calls], ["copy", "store", "expunge"])
        self.assertIn(("expunge", ("7",)), conn.uid_calls)
        self.assertEqual(conn.bare_expunges, 0)  # never purges other \Deleted mail

    def test_capabilities_refreshed_after_login(self):
        # Mailu's nginx greeting omits MOVE/UIDPLUS; Dovecot lists them after login.
        class ProxyIMAP(FakeIMAP):
            capabilities = ("IMAP4REV1", "AUTH=PLAIN")

            def capability(self):
                return ("OK", [b"IMAP4rev1 SASL-IR UIDPLUS MOVE"])

            def uid(self, cmd, *args):
                if cmd == "move":
                    self.uid_calls.append((cmd, args))
                    return ("OK", [b"ok"])
                return super().uid(cmd, *args)
        with patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", ProxyIMAP):
            self.assertTrue(mail_mod.move_message(self.cfg, "INBOX", 9, "Archive"))
        conn = FakeIMAP.last
        self.assertIn("MOVE", conn.capabilities)
        self.assertEqual(conn.uid_calls, [("move", ("9", '"Archive"'))])

    def test_move_last_resort_bare_expunge(self):
        ok, conn = self._move_with_caps(("IMAP4REV1",))
        self.assertTrue(ok)
        self.assertEqual(conn.bare_expunges, 1)

    def test_draft_needs_mailbox_login(self):
        cfg = MailConfig(default_from="x@x.io")
        with self.assertRaises(ValueError):
            mail_mod.save_draft(cfg, to=["c@x.io"], subject="s")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_drafts_fallback_name_created(self):
        # no \Drafts and no Drafts folder at all: falls back to "Drafts"
        out = mail_mod.save_draft(self.cfg, to=["c@x.io"], subject="s")
        self.assertEqual(out["saved_to"], "Drafts")
        self.assertEqual(FakeIMAP.last.appended[0], '"Drafts"')
        self.assertIsNone(out["uid"])

    def test_tool_registered(self):
        self.assertIn("mail_draft", build_server().tools)


class MailTests(unittest.TestCase):
    def test_search_criteria_builder(self):
        c = mail_mod.build_search_criteria(unseen_only=True, from_="a@x.io",
                                          subject="hi", since="01-Jan-2025")
        self.assertIn("UNSEEN", c)
        self.assertIn('FROM "a@x.io"', c)
        self.assertIn('SUBJECT "hi"', c)
        self.assertIn('SINCE "01-Jan-2025"', c)
        self.assertEqual(mail_mod.build_search_criteria(), "ALL")

    def test_rfc2047_subject_decoded(self):
        raw = (b"From: a@x.io\r\n"
               b"Subject: =?UTF-8?B?SMO2c2Ugw7Zmw7ZsZQ==?=\r\n\r\nx\r\n")
        out = mail_mod._message_to_dict(1, raw, False, 0)
        self.assertEqual(out["subject"], "Höse öföle")

    def test_search_limit_clamped(self):
        seen = {}

        def fake_search(cfg, mailbox="INBOX", limit=50, **kw):
            seen["limit"] = limit
            return []

        handler = build_server().tools["mail_search"].handler
        with patch.object(mail_mod, "search_emails", fake_search):
            handler({"limit": None})
            self.assertEqual(seen["limit"], 50)
            handler({"limit": 0})
            self.assertEqual(seen["limit"], 50)
            handler({"limit": 10 ** 9})
            self.assertEqual(seen["limit"], 500)
            handler({"limit": "10"})
            self.assertEqual(seen["limit"], 10)
            with self.assertRaises(ValueError):
                handler({"limit": "abc"})

    def test_send_coerces_string_recipients(self):
        cfg = MailConfig(smtp_host="h", smtp_port=587,
                         default_from="me@x.io")
        fake = MagicMock()
        fake.__enter__ = MagicMock(return_value=fake)
        fake.__exit__ = MagicMock(return_value=False)
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP", return_value=fake):
            out = mail_mod.send_email(cfg, to="a@x.io", cc="c@x.io",
                                      bcc="h@x.io", subject="s")
        sent = fake.send_message.call_args
        self.assertEqual(sent[1]["to_addrs"], ["a@x.io", "c@x.io", "h@x.io"])
        self.assertIn("a@x.io", out["to"])

    def test_search_values_are_escaped(self):
        c = mail_mod.build_search_criteria(subject='say "hi" \\ bye')
        self.assertIn(r'SUBJECT "say \"hi\" \\ bye"', c)

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_search_parses_real_flags_and_size(self):
        cfg = MailConfig(imap_user="u", imap_password="p")
        res = mail_mod.search_emails(cfg, limit=2, unseen_only=True)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["subject"], "Hello there")
        self.assertIn("\\Seen", res[0]["flags"])
        self.assertIn("\\Flagged", res[0]["flags"])
        self.assertEqual(res[0]["size"], 4321)
        self.assertEqual(res[0]["message_id"], "<abc123@example.com>")
        # newest UIDs win the limit
        self.assertEqual([r["uid"] for r in res], [2, 3])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_list_mailboxes_dot_delimiter(self):
        cfg = MailConfig(imap_user="u", imap_password="p")
        self.assertEqual(mail_mod.list_mailboxes(cfg), ["INBOX", "INBOX.Sent"])

    def test_quote_mailbox(self):
        self.assertEqual(mail_mod._quote_mailbox("INBOX"), '"INBOX"')
        self.assertEqual(mail_mod._quote_mailbox('a"b\\c'), r'"a\"b\\c"')

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", FakeIMAP)
    def test_get_email_full(self):
        cfg = MailConfig(imap_user="u", imap_password="p")
        msg = mail_mod.get_email(cfg, "INBOX", 3)
        self.assertEqual(msg["subject"], "Hello there")
        self.assertIn("Hi Bob", msg["body_text"])

    def test_single_part_attachment_not_body(self):
        raw = (b"From: a@x.io\r\nSubject: f\r\n"
               b"Content-Type: text/plain; name=\"n.txt\"\r\n"
               b'Content-Disposition: attachment; filename="n.txt"\r\n'
               b"Content-Transfer-Encoding: base64\r\n\r\nZGF0YQ==\r\n")
        out = mail_mod._message_to_dict(1, raw, True, 0)
        self.assertEqual(len(out["attachments"]), 1)
        self.assertEqual(out["attachments"][0]["filename"], "n.txt")
        self.assertEqual(out["attachments"][0]["size"], 4)
        self.assertEqual(out["body_text"], "")

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

    def test_send_uses_implicit_tls_on_465(self):
        cfg = MailConfig(smtp_host="h", smtp_port=465, smtp_user="u",
                         smtp_password="p", default_from="u@x.io")
        ssl_fake = MagicMock()
        ssl_fake.__enter__ = MagicMock(return_value=ssl_fake)
        ssl_fake.__exit__ = MagicMock(return_value=False)
        with patch("mcp.mailu_mcp.mail.smtplib.SMTP_SSL", return_value=ssl_fake) as m_ssl, \
             patch("mcp.mailu_mcp.mail.smtplib.SMTP") as m_plain:
            mail_mod.send_email(cfg, to=["a@x.io"], subject="s")
        m_ssl.assert_called_once_with("h", 465, timeout=60)
        m_plain.assert_not_called()
        ssl_fake.starttls.assert_not_called()

    def test_wire_format_against_stub_imap_server(self):
        """Real imaplib over TCP against a stub: validates SELECT quoting,
        SEARCH syntax, FETCH label parsing and LIST delimiter handling."""
        import socket
        import threading

        header = (b"From: Wire <wire@example.com>\r\nTo: me@x.io\r\n"
                  b"Subject: sock\r\n\r\n")
        seen = []
        ready = threading.Event()

        def handle(fh):
            fh.write(b"* OK stub ready\r\n")
            fh.flush()
            while True:
                line = fh.readline()
                if not line:
                    return
                text = line.decode("latin-1").strip()
                tag, _, rest = text.partition(" ")
                seen.append(rest)
                cmd = rest.split(" ")[0].upper()
                if cmd == "CAPABILITY":
                    fh.write(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n"
                             + f"{tag} OK capabilities\r\n".encode())
                elif cmd == "LOGIN":
                    fh.write(f"{tag} OK logged in\r\n".encode())
                elif cmd in ("SELECT", "EXAMINE"):
                    fh.write(b"* 2 EXISTS\r\n" + f"{tag} OK selected\r\n".encode())
                elif cmd == "LIST":
                    fh.write(b'* LIST (\\HasNoChildren) "." "INBOX"\r\n'
                             b'* LIST (\\HasNoChildren) "." "INBOX.Archive"\r\n'
                             + f"{tag} OK listed\r\n".encode())
                elif cmd == "UID" and "SEARCH" in rest.upper():
                    fh.write(b"* SEARCH 7 8\r\n" + f"{tag} OK searched\r\n".encode())
                elif cmd == "UID" and "FETCH" in rest.upper():
                    label = (b"* 8 FETCH (UID 8 FLAGS (\\Seen) RFC822.SIZE "
                             + str(len(header)).encode() + b" BODY[HEADER] {"
                             + str(len(header)).encode() + b"}\r\n")
                    fh.write(label + header + b")\r\n"
                             + f"{tag} OK fetched\r\n".encode())
                elif cmd == "LOGOUT":
                    fh.write(b"* BYE\r\n" + f"{tag} OK bye\r\n".encode())
                    fh.flush()
                    return
                else:
                    fh.write(f"{tag} BAD unknown\r\n".encode())
                fh.flush()

        def serve(listener):
            ready.set()
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return  # listener closed: shut down
                with conn:
                    try:
                        handle(conn.makefile("rwb"))
                    except (OSError, ValueError):
                        pass

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        worker = threading.Thread(target=serve, args=(listener,), daemon=True)
        worker.start()
        ready.wait(timeout=5)

        cfg = MailConfig(imap_host="127.0.0.1", imap_port=port,
                         imap_user="u", imap_password="p", imap_ssl=False)
        try:
            boxes = mail_mod.list_mailboxes(cfg)
            res = mail_mod.search_emails(cfg, mailbox="INBOX", limit=10,
                                         subject='a"b')
        finally:
            # shutdown() wakes a thread blocked in accept() on Linux;
            # close() alone only does on macOS.
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()
            worker.join(timeout=10)
        self.assertEqual(boxes, ["INBOX", "INBOX.Archive"])
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["subject"], "sock")
        self.assertIn("\\Seen", res[0]["flags"])
        self.assertEqual(res[0]["size"], len(header))
        selects = [c for c in seen if c.startswith("SELECT") or c.startswith("EXAMINE")]
        self.assertTrue(selects and selects[0].endswith('"INBOX"'), seen)
        searches = [c for c in seen if "SEARCH" in c]
        self.assertTrue(searches and r'SUBJECT "a\"b"' in searches[0], seen)
        self.assertNotIn("None", searches[0])  # charset None is skipped, not sent
        self.assertFalse(worker.is_alive())

    def test_send_wire_format_against_stub_smtp_server(self):
        """Real smtplib over TCP: envelope has to+cc+bcc, headers omit Bcc,
        attachment survives the wire."""
        import socket
        import threading

        transcript = []
        data_lines = []
        ready = threading.Event()

        def handle(fh):
            fh.write(b"220 stub ESMTP\r\n")
            fh.flush()
            while True:
                line = fh.readline()
                if not line:
                    return
                text = line.decode("latin-1").rstrip("\r\n")
                verb = text.split(" ")[0].upper() if text else ""
                transcript.append(text)
                if verb in ("EHLO", "HELO"):
                    fh.write(b"250-stub\r\n250 OK\r\n")
                elif verb == "DATA":
                    fh.write(b"354 end with .\r\n")
                    fh.flush()
                    while True:
                        chunk = fh.readline().decode("latin-1")
                        if chunk in (".\r\n", ".\n"):
                            break
                        data_lines.append(chunk)
                    fh.write(b"250 queued\r\n")
                elif verb == "QUIT":
                    fh.write(b"221 bye\r\n")
                    fh.flush()
                    return
                else:
                    fh.write(b"250 OK\r\n")
                fh.flush()

        def serve(listener):
            ready.set()
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                with conn:
                    try:
                        handle(conn.makefile("rwb"))
                    except (OSError, ValueError):
                        pass

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]
        worker = threading.Thread(target=serve, args=(listener,), daemon=True)
        worker.start()
        ready.wait(timeout=5)

        cfg = MailConfig(smtp_host="127.0.0.1", smtp_port=port,
                         smtp_starttls=False, default_from="me@x.io")
        try:
            out = mail_mod.send_email(
                cfg, to=["a@x.io"], cc=["c@x.io"], bcc=["hidden@x.io"],
                subject="wire", body_text="hello",
                attachments=[{"filename": "n.txt", "content": "ZGF0YQ==",
                              "maintype": "text", "subtype": "plain"}])
        finally:
            # shutdown() wakes a thread blocked in accept() on Linux;
            # close() alone only does on macOS.
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        rcpts = sorted(t.split(":", 1)[1].strip("<> ")
                       for t in transcript if t.upper().startswith("RCPT TO"))
        self.assertEqual(rcpts, ["a@x.io", "c@x.io", "hidden@x.io"])
        blob = "".join(data_lines)
        self.assertIn("Subject: wire", blob)
        self.assertIn("Cc: c@x.io", blob)
        self.assertNotIn("hidden@x.io", blob)
        self.assertIn('filename="n.txt"', blob)
        self.assertIn("ZGF0YQ==", blob)
        self.assertTrue(out["message_id"])
        self.assertIn("@x.io>", out["message_id"])

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

    def test_connection_failure_is_mcp_error_not_crash(self):
        def boom(*a, **kw):
            raise OSError("unreachable")
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "mail_search", "arguments": {}}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        stdout = io.StringIO()
        with patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", boom):
            build_server().run(stdin, stdout)
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)  # error frame, then business as usual
        first = json.loads(lines[0])
        self.assertTrue(first["result"]["isError"])
        self.assertIn("unreachable", first["result"]["content"][0]["text"])

    def test_mcp_rejects_batch_without_crashing(self):
        stdin = io.StringIO(json.dumps([{"jsonrpc": "2.0", "id": 1,
                                         "method": "ping"}]) + "\n"
                              + "not json\n"
                              + json.dumps({"jsonrpc": "2.0", "id": 2,
                                            "method": "ping"}) + "\n")
        stdout = io.StringIO()
        build_server().run(stdin, stdout)
        lines = stdout.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 3)  # batch err, parse err, ping ok
        self.assertEqual(json.loads(lines[0])["error"]["code"], -32600)
        self.assertEqual(json.loads(lines[1])["error"]["code"], -32700)
        self.assertEqual(json.loads(lines[2])["result"], {})


ORIGINAL_ID = "<orig-1@customer.test>"


def _original_raw() -> bytes:
    m = EmailMessage()
    m["From"] = "Robin <robin@customer.test>"
    m["To"] = "sales@hitsteps.test"
    m["Subject"] = "Pricing"
    m["Date"] = "Tue, 17 Nov 2026 10:05:00 +0000"
    m["Message-ID"] = ORIGINAL_ID
    m["References"] = "<older@customer.test>"
    m.set_content("Hi,\n\nhow much is Pro?\nThanks")
    return m.as_bytes()


class QuoteIMAP(DraftIMAP):
    """Drafts + an INBOX that holds the message being answered. Like Mailu's
    Dovecot, SEARCH never finds a Message-ID (FTS excludes the header), so the
    original must be found from the fetched headers."""
    found_in = "INBOX"

    def select(self, box, readonly=False):
        self.selected = box.strip('"')
        return ("OK", [b"3"])

    def fetch(self, msgset, what):
        if self.selected != QuoteIMAP.found_in:
            return ("OK", [])
        rows = [(b"1 (UID 5 BODY[HEADER.FIELDS (MESSAGE-ID)] {30}", b"Message-ID: <other@x.io>\r\n\r\n"), b")",
                (b"3 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {40}",
                 b"Message-ID: " + ORIGINAL_ID.encode() + b"\r\n\r\n"), b")"]
        return ("OK", rows)

    def uid(self, cmd, *args):
        self.uid_calls.append((cmd, args))
        if cmd == "search":
            return ("OK", [b""])
        if cmd == "fetch" and args[0] == "7":
            raw = _original_raw()
            return ("OK", [(f"3 (UID 7 BODY[] {{{len(raw)}}}".encode(), raw), b")"])
        if cmd == "expunge":
            self.expunged.append(args[0])
        return ("OK", [b"ok"])


def _saved_draft():
    from email import policy as _policy
    from email.parser import BytesParser as _BP
    box, flags, date, raw = DovecotIMAP.instances[-1].appended
    return _BP(policy=_policy.default).parsebytes(raw)


class DraftQuoteTests(unittest.TestCase):
    """Drafts must look like a mail client's: a Date, clean threading headers,
    and for a reply the quoted original below the new text."""

    def setUp(self):
        DovecotIMAP.instances = []
        QuoteIMAP.found_in = "INBOX"
        self.cfg = MailConfig(imap_user="sales@hitsteps.test", imap_password="p",
                              smtp_host="h", smtp_port=465, smtp_user="sales@hitsteps.test",
                              smtp_password="p", default_from="sales@hitsteps.test")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_every_draft_has_a_date(self):
        mail_mod.save_draft(self.cfg, to=["c@x.io"], subject="New")
        self.assertTrue(_saved_draft()["Date"])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_reply_quotes_the_original_and_completes_references(self):
        out = mail_mod.save_draft(self.cfg, to=["robin@customer.test"], subject="Re: Pricing",
                                  body_text="Pro is $25 a month.", in_reply_to=ORIGINAL_ID)
        msg = _saved_draft()
        body = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertTrue(body.startswith("Pro is $25 a month."))
        self.assertIn("Robin <robin@customer.test> wrote:", body)
        self.assertIn("> how much is Pro?", body)
        self.assertEqual(str(msg["In-Reply-To"]), ORIGINAL_ID)
        self.assertEqual(str(msg["References"]), "<older@customer.test> " + ORIGINAL_ID)
        self.assertEqual((out["quoted_original"], out["quote_source"]), (True, "INBOX"))

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_original_found_in_archive(self):
        QuoteIMAP.found_in = "Archive"
        out = mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                                  in_reply_to=ORIGINAL_ID)
        self.assertEqual(out["quote_source"], "Archive")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_falls_back_to_references_when_in_reply_to_is_unknown(self):
        # Seen live: an agent pointed In-Reply-To at an earlier draft of its own reply.
        out = mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                                  in_reply_to="<my-old-draft@hitsteps.test>",
                                  references="<older@customer.test> " + ORIGINAL_ID + " <my-old-draft@hitsteps.test>")
        self.assertTrue(out["quoted_original"])
        body = _saved_draft().get_body(preferencelist=("plain",)).get_content()
        self.assertIn("> how much is Pro?", body)

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_quote_can_be_turned_off(self):
        out = mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                                  in_reply_to=ORIGINAL_ID, quote_original=False)
        body = _saved_draft().get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("wrote:", body)
        self.assertFalse(out["quoted_original"])

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_missing_original_still_saves_the_draft(self):
        out = mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                                  in_reply_to="<gone@x.io>")
        self.assertEqual(out["uid"], 42)
        self.assertFalse(out["quoted_original"])
        self.assertIn("not found", out["quote_error"])
        self.assertEqual(str(_saved_draft()["In-Reply-To"]), "<gone@x.io>")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_threading_headers_never_empty_or_duplicated(self):
        mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                            references="<a@x.io> " + ORIGINAL_ID,
                            extra_headers={"In-Reply-To": "", "References": "<other@x.io>",
                                           "X-Hitsteps-Internal-Dispatch": "v1", "Date": "bogus"})
        msg = _saved_draft()
        self.assertEqual(msg.get_all("In-Reply-To"), [ORIGINAL_ID])  # newest id of References
        self.assertEqual(msg.get_all("References"), ["<a@x.io> " + ORIGINAL_ID])
        self.assertEqual(len(msg.get_all("Date")), 1)
        self.assertNotEqual(str(msg["Date"]), "bogus")
        self.assertEqual(str(msg["X-Hitsteps-Internal-Dispatch"]), "v1")

    @patch("mcp.mailu_mcp.mail.imaplib.IMAP4_SSL", QuoteIMAP)
    def test_html_reply_gets_a_cited_blockquote(self):
        mail_mod.save_draft(self.cfg, to=["d@x.io"], subject="Re: P", body_text="ok",
                            body_html="<p>ok</p>", in_reply_to=ORIGINAL_ID)
        html = _saved_draft().get_body(preferencelist=("html",)).get_content()
        self.assertIn('<blockquote type="cite"', html)
        self.assertIn("how much is Pro?", html)


if __name__ == "__main__":
    unittest.main()
