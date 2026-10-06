"""Unit tests for the milter processor decisions (no sockets)."""
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from email import policy

sys.path.insert(0, ".")

from tracker.mail_tracker.db import Store
from tracker.mail_tracker.server import Processor

POLICY = policy.default.clone(linesep='\r\n')


class FakeConfig:
    track_host = 'track.x.test'
    local_domains = {'x.test'}
    hmac_secret = 'hmac-secret-for-tests'
    max_recipients = 200
    max_links = 500
    dry_run = False

    @property
    def base_url(self):
        return 'https://track.x.test'


def make_mail(text='<html><body>hi <a href="http://e.test/">e</a></body></html>'):
    msg = EmailMessage(policy=POLICY)
    msg['From'] = 'a@x.test'
    msg['Subject'] = 'Test subject'
    msg.set_content(text, subtype='html')
    raw = msg.as_bytes(policy=POLICY)
    head, _, body = raw.partition(b'\r\n\r\n')
    headers = []
    for line in head.decode().split('\r\n'):
        if ':' in line:
            name, _, value = line.partition(':')
            headers.append((name.strip(), value.strip()))
    return headers, body


class ProcessorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 't.db'))
        self.config = FakeConfig()
        self.proc = Processor(self.config, self.store)

    def tearDown(self):
        self.store.close()

    def test_tracks_local_html(self):
        headers, body = make_mail()
        new, token, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')
        self.assertIsNotNone(new)
        self.assertIn(b'/o/', new)
        self.assertIn(b'/c/', new)
        row = self.store.get_message_by_token(token)
        self.assertEqual(row['sender'], 'a@x.test')
        self.assertEqual(row['subject'], 'Test subject')
        self.assertEqual(self.store.recipients_of(row['id']), ['b@y.test'])
        self.assertEqual(self.store.get_link(row['id'], 0), 'http://e.test/')

    def test_text_conversion_returns_header_ops(self):
        msg = EmailMessage(policy=POLICY)
        msg['From'] = 'a@x.test'
        msg['Subject'] = 'Test subject'
        msg.set_content('hello plain')
        raw = msg.as_bytes(policy=POLICY)
        head, _, body = raw.partition(b'\r\n\r\n')
        headers = []
        for line in head.decode().split('\r\n'):
            if ':' in line:
                name, _, value = line.partition(':')
                headers.append((name.strip(), value.strip()))
        new, token, reason, hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')
        self.assertTrue(hdrs['content-type'].startswith(
            'multipart/alternative; boundary='))
        self.assertIsNone(hdrs['content-transfer-encoding'])

    def test_skips_remote_sender(self):
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'stranger@elsewhere.test', ['a@x.test'], headers, body, {})
        self.assertEqual((new, reason), (None, 'not-local-sender'))
        self.assertEqual(self.store.count_messages(), 0)

    def test_skips_bounce(self):
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process('', ['a@x.test'], headers, body, {})
        self.assertEqual(reason, 'bounce')

    def test_header_opt_out(self):
        headers, body = make_mail()
        headers.append(('X-Tracker-Track', 'no'))
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual((new, reason), (None, 'header-opt-out'))

    def test_header_force_overrides_lists(self):
        self.store.set_sender_optout('a@x.test', True)
        headers, body = make_mail()
        headers.append(('X-Tracker-Track', 'Yes'))
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')

    def test_sender_optout(self):
        self.store.set_sender_optout('a@x.test', True)
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual((new, reason), (None, 'sender-opt-out'))

    def test_recipient_optout_single(self):
        self.store.set_recipient_optout('a@x.test', 'b@y.test', True)
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual((new, reason), (None, 'recipient-opt-out'))

    def test_recipient_optout_multi_still_tracks(self):
        self.store.set_recipient_optout('a@x.test', 'b@y.test', True)
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test', 'c@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')

    def test_recipient_optout_other_owner_ignored(self):
        self.store.set_recipient_optout('zz@x.test', 'b@y.test', True)
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')

    def test_too_many_recipients(self):
        headers, body = make_mail()
        rcpts = [f'r{i}@y.test' for i in range(201)]
        new, _, reason, _hdrs = self.proc.process('a@x.test', rcpts, headers, body, {})
        self.assertEqual((new, reason), (None, 'too-many-recipients'))

    def test_no_recipients(self):
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process('a@x.test', [], headers, body, {})
        self.assertEqual((new, reason), (None, 'no-recipients'))

    def test_dry_run_parses_but_writes_nothing(self):
        self.config.dry_run = True
        headers, body = make_mail()
        new, token, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(new, body)
        self.assertIsNone(token)
        self.assertTrue(reason.startswith('dry-run'))
        self.assertEqual(self.store.count_messages(), 0)

    def test_signed_passthrough(self):
        headers = [('Content-Type', 'multipart/signed; boundary="b"')]
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, b'--b\r\n--b--\r\n', {})
        self.assertEqual((new, reason), (None, 'signed-or-encrypted'))

    def test_encoded_subject_decoded(self):
        headers, body = make_mail()
        headers = [(n, v) for n, v in headers if n.lower() != 'subject']
        headers.append(('Subject', '=?utf-8?q?Gr=C3=BC=C3=9Fe?='))
        new, token, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual(reason, 'ok')
        row = self.store.get_message_by_token(token)
        self.assertIn('Grüße', row['subject'])

    def test_auto_submitted_skipped(self):
        headers, body = make_mail()
        headers.append(('Auto-Submitted', 'auto-replied'))
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers, body, {})
        self.assertEqual((new, reason), (None, 'auto-submitted'))
        headers2 = [(n, v) for n, v in headers
                    if n.lower() != 'auto-submitted']
        headers2.append(('Auto-Submitted', 'no'))
        new, _, reason, _hdrs = self.proc.process(
            'a@x.test', ['b@y.test'], headers2, body, {})
        self.assertEqual(reason, 'ok')

    def test_case_insensitive_sender(self):
        headers, body = make_mail()
        new, _, reason, _hdrs = self.proc.process(
            'A@X.TEST', ['B@Y.TEST'], headers, body, {})
        self.assertEqual(reason, 'ok')


if __name__ == '__main__':
    unittest.main()
