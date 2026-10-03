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
        m_ssl.assert_called_once_with("h", 465)
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


if __name__ == "__main__":
    unittest.main()
