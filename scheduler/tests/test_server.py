"""Unit tests for the scheduler API + poller (fake SMTP/IMAP)."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, ".")

from scheduler.sched.db import Store
from scheduler.sched.server import ImapWorker, Poller, SchedulerApp


class FakeConfig:
    api_secret = 'test-secret'
    dovecot_host = 'dovecot'
    dovecot_port = 143
    postfix_host = 'postfix'
    postfix_port = 25
    local_domains = {'x.test'}
    max_mime_bytes = 1024 * 1024
    max_recipients = 200
    max_future_days = 366
    retention_days = 90
    snooze_folder = 'Snoozed'
    scheduled_folder = 'Scheduled'


MIME = (b'From: a@x.test\r\nTo: b@y.test\r\nSubject: Hi\r\n'
        b'Message-ID: <m1@x>\r\n\r\nhello\r\n')


def sched_payload(**over):
    data = {'user': 'a@x.test', 'sender': 'a@x.test',
            'recipients': ['B@y.test', 'C <c@y.test>'],
            'mime_b64': base64.b64encode(MIME).decode(),
            'send_at': int(time.time()) + 120}
    data.update(over)
    return data


class FakeSMTP:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def ehlo(self):
        pass

    def sendmail(self, sender, recipients, mime):
        FakeSMTP.sent.append((sender, recipients, mime))


class FakeIMAP:
    """Folder -> {uid: message-id}; records moves."""
    folders = {}
    moves = []
    fail_move = False

    def __init__(self, host, port):
        pass

    def login(self, user, password):
        self.user = user
        return 'OK', []

    def select(self, folder, readonly=False):
        if folder not in FakeIMAP.folders:
            return 'NO', []
        self.selected = folder
        return 'OK', []

    def list(self, *args):
        return 'OK', [b'(\\HasNoChildren) "/" "Sent"',
                      b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def uid(self, cmd, *args):
        assert None not in args, 'uid() takes no charset slot'
        if cmd == 'search':
            assert args == ('ALL',), args
            box = FakeIMAP.folders.get(self.selected, {})
            return 'OK', [' '.join(str(u) for u in sorted(box)).encode()]
        if cmd == 'fetch':
            box = FakeIMAP.folders.get(self.selected, {})
            out = []
            for raw in args[0].split(b','):
                uid = int(raw)
                if uid in box:
                    out.append((
                        f'{uid} (UID {uid} BODY[HEADER.FIELDS'
                        ' (MESSAGE-ID)] {10 + len(box[uid])})'.encode(),
                        f'Message-ID: {box[uid]}\r\n\r\n'.encode()))
            return 'OK', out
        if cmd == 'move':
            if FakeIMAP.fail_move:
                return 'NO', []
            return self._move(args[0], args[1])
        if cmd == 'copy':
            return self._move(args[0], args[1], delete=False)
        if cmd == 'store':
            return 'OK', []
        raise AssertionError(cmd)

    def _move(self, uids, dest, delete=True):
        box = FakeIMAP.folders.setdefault(self.selected, {})
        dst = FakeIMAP.folders.setdefault(dest, {})
        n = len(uids.split(b','))
        FakeIMAP.moves.append((self.user, self.selected, dest, n))
        return 'OK', []

    def expunge(self):
        return 'OK', []

    def logout(self):
        return 'OK', []


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 's.db'))
        self.app = SchedulerApp(FakeConfig(), self.store)
        self.hdr = {'x-scheduler-secret': 'test-secret'}

    def tearDown(self):
        self.store.close()

    def call(self, method, path, data=None, headers=None):
        body = b'' if data is None else json.dumps(data).encode()
        if headers is None:
            headers = self.hdr
        return self.app.handle(method, path, headers, body)

    def test_secret_required(self):
        # healthz stays open for k8s probes; everything else needs it
        status, _, body = self.call('GET', '/healthz', headers={})
        self.assertEqual((status, json.loads(body)), (200, {'ok': True}))
        status, _, _ = self.call('GET', '/v1/snoozes?user=a@x.test',
                                 headers={})
        self.assertEqual(status, 401)
        status, _, _ = self.call('GET', '/v1/snoozes?user=a@x.test',
                                 headers={'x-scheduler-secret': 'nope'})
        self.assertEqual(status, 401)
        status, _, _ = self.call('GET', '/v1/snoozes?user=a@x.test')
        self.assertEqual(status, 200)

    def test_schedule_roundtrip(self):
        status, _, body = self.call('POST', '/v1/scheduled',
                                    sched_payload())
        self.assertEqual(status, 200)
        sid = json.loads(body)['id']
        status, _, body = self.call(
            'GET', '/v1/scheduled?user=a@x.test')
        rows = json.loads(body)['scheduled']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['recipients'], ['b@y.test', 'c@y.test'])
        self.assertEqual(rows[0]['subject'], 'Hi')
        # other users see nothing; cancel is owner-scoped
        _, _, body = self.call('GET', '/v1/scheduled?user=e@vil.test')
        self.assertEqual(json.loads(body)['scheduled'], [])
        status, _, _ = self.call(
            'DELETE', f'/v1/scheduled/{sid}?user=e@vil.test')
        self.assertEqual(status, 404)
        status, _, _ = self.call(
            'DELETE', f'/v1/scheduled/{sid}?user=a@x.test')
        self.assertEqual(status, 200)

    def test_schedule_validation(self):
        bad = [
            {'sender': 'stranger@elsewhere.test'},  # non-local sender
            {'recipients': []},  # no recipients
            {'recipients': ['not-an-address']},
            {'mime_b64': '!!!not-base64!!!'},
            {'mime_b64': base64.b64encode(b'x' * 20).decode(),
             'send_at': int(time.time()) - 10},  # past
            {'send_at': int(time.time()) + 400 * 86400},  # too far
        ]
        for over in bad:
            status, _, body = self.call('POST', '/v1/scheduled',
                                        sched_payload(**over))
            self.assertEqual(status, 400, over)

    def test_snooze_roundtrip(self):
        now = int(time.time())
        status, _, body = self.call(
            'POST', '/v1/snoozes',
            {'user': 'a@x.test', 'message_id': '<m1>',
             'folder': 'INBOX', 'wake_at': now + 120})
        self.assertEqual(status, 200)
        sid = json.loads(body)['id']
        _, _, body = self.call('GET', '/v1/snoozes?user=a@x.test')
        self.assertEqual(len(json.loads(body)['snoozes']), 1)
        _, _, body = self.call('GET', '/v1/snoozes?user=e@vil.test')
        self.assertEqual(json.loads(body)['snoozes'], [])
        status, _, _ = self.call(
            'DELETE', f'/v1/snoozes/{sid}?user=e@vil.test')
        self.assertEqual(status, 404)
        status, _, _ = self.call(
            'DELETE', f'/v1/snoozes/{sid}?user=a@x.test')
        self.assertEqual(status, 200)
        # validation
        status, _, _ = self.call('POST', '/v1/snoozes',
                                 {'user': 'a@x.test', 'message_id': '',
                                  'folder': 'INBOX', 'wake_at': now + 60})
        self.assertEqual(status, 400)


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 's.db'))
        FakeSMTP.sent = []
        FakeIMAP.folders = {}
        FakeIMAP.moves = []
        FakeIMAP.fail_move = False
        imap = ImapWorker('dovecot', 143, imap_cls=FakeIMAP)
        self.poller = Poller(FakeConfig(), self.store, imap=imap,
                             smtp_cls=FakeSMTP)

    def tearDown(self):
        self.store.close()

    def test_due_send_submits_and_files(self):
        now = int(time.time())
        FakeIMAP.folders = {'Scheduled': {1: '<m1@x>'}}
        self.store.add_scheduled('a@x.test', 'a@x.test', ['b@y.test'],
                                 'Hi', MIME, now - 5)
        sent, woken = self.poller.poll_once(now)
        self.assertEqual((sent, woken), (1, 0))
        sender, rcpts, mime = FakeSMTP.sent[0]
        self.assertEqual((sender, rcpts), ('a@x.test', ['b@y.test']))
        self.assertIn(b'Message-ID: <m1@x>', mime)
        self.assertEqual(FakeIMAP.moves,
                         [('a@x.test', 'Scheduled', 'Sent', 1)])
        # second poll: nothing due
        self.assertEqual(self.poller.poll_once(now), (0, 0))

    def test_submit_failure_retries_then_fails(self):
        class Boom(FakeSMTP):
            def sendmail(self, *args):
                raise ConnectionError('smtp down')

        self.poller.smtp_cls = Boom
        now = int(time.time())
        sid = self.store.add_scheduled('a@x.test', 'a@x.test', ['b@y.test'],
                                       'Hi', MIME, now - 5)
        self.assertEqual(self.poller.poll_once(now), (0, 0))
        row = self.store.list_scheduled('a@x.test')[0]
        self.assertEqual((row['status'], row['attempts']), ('queued', 1))
        # force old age -> permanent failure, no infinite retry
        self.store._db.execute(
            'UPDATE scheduled SET created_at=?, next_try=? WHERE id=?',
            (now - 25 * 3600, now - 1, sid))
        self.store._db.commit()
        self.assertEqual(self.poller.poll_once(now), (0, 0))
        row = self.store.list_scheduled('a@x.test')[0]
        self.assertEqual(row['status'], 'failed')

    def test_wake_moves_message_back(self):
        now = int(time.time())
        FakeIMAP.folders = {'Snoozed': {1: '<m9>', 2: '<m9>'}}
        self.store.add_snooze('a@x.test', '<m9>', 'INBOX', now - 5)
        sent, woken = self.poller.poll_once(now)
        self.assertEqual((sent, woken), (0, 1))
        self.assertEqual(FakeIMAP.moves, [('a@x.test', 'Snoozed', 'INBOX', 2)])
        row = self.store.list_snoozes('a@x.test')[0]
        self.assertEqual(row['status'], 'done')

    def test_wake_matches_exact_mid_only(self):
        now = int(time.time())
        FakeIMAP.folders = {'Snoozed': {1: '<other>', 2: '  <M9>  ',
                                        3: '<m9x>'}}
        self.store.add_snooze('a@x.test', '<m9>', 'INBOX', now - 5)
        self.assertEqual(self.poller.poll_once(now), (0, 1))
        # uid 2 matches case-insensitively; decoys untouched
        self.assertEqual(FakeIMAP.moves, [('a@x.test', 'Snoozed', 'INBOX', 1)])

    def test_wake_missing_retries_then_done(self):
        now = int(time.time())
        FakeIMAP.folders = {'Snoozed': {}}
        sid = self.store.add_snooze('a@x.test', '<gone>', 'INBOX', now - 5)
        # fresh miss = likely index lag: stays pending, retried
        self.assertEqual(self.poller.poll_once(now), (0, 0))
        row = self.store.list_snoozes('a@x.test')[0]
        self.assertEqual(row['status'], 'pending')
        # past the grace window a miss is genuine: complete it
        self.store._db.execute(
            'UPDATE snoozes SET created_at=? WHERE id=?',
            (now - 31 * 60, sid))
        self.store._db.commit()
        self.assertEqual(self.poller.poll_once(now), (0, 1))
        row = self.store.list_snoozes('a@x.test')[0]
        self.assertEqual(row['status'], 'done')

    def test_filing_retries_then_gives_up(self):
        now = int(time.time())
        FakeIMAP.folders = {'Scheduled': {}, 'Sent': {}}
        sid = self.store.add_scheduled('a@x.test', 'a@x.test', ['b@y.test'],
                                       'Hi', MIME, now - 5)
        self.poller.poll_once(now)  # sends; copy not visible yet
        row = self.store._db.execute(
            'SELECT status, filed_at FROM scheduled WHERE id=?',
            (sid,)).fetchone()
        self.assertEqual((row[0], row[1]), ('sent', None))
        # copy appears later -> filed on a later poll
        FakeIMAP.folders['Scheduled'] = {1: '<m1@x>'}
        self.poller.poll_once(now + 60)
        row = self.store._db.execute(
            'SELECT filed_at FROM scheduled WHERE id=?', (sid,)).fetchone()
        self.assertEqual(row[0], now + 60)
        self.assertEqual(FakeIMAP.moves,
                         [('a@x.test', 'Scheduled', 'Sent', 1)])
        # ...but an old row whose copy never shows is closed out
        sid2 = self.store.add_scheduled('a@x.test', 'a@x.test', ['b@y.test'],
                                        'Hi', MIME, now - 5)
        FakeIMAP.folders['Scheduled'] = {}
        self.store._db.execute(
            "UPDATE scheduled SET status='sent', sent_at=?, created_at=?"
            ' WHERE id=?', (now - 3600, now - 3600, sid2))
        self.store._db.commit()
        self.poller.poll_once(now)
        row = self.store._db.execute(
            'SELECT filed_at FROM scheduled WHERE id=?', (sid2,)).fetchone()
        self.assertIsNotNone(row[0])

    def test_sent_folder_list_forms(self):
        from scheduler.sched.server import _list_name
        # dovecot Maildir++: trailing name unquoted, '.' delimiter
        self.assertEqual(
            _list_name(b'* LIST (\\HasNoChildren \\Sent) "." Sent'), 'Sent')
        # quoted name with a space
        self.assertEqual(
            _list_name(b'* LIST (\\HasNoChildren \\Sent) "/" "Sent Mail"'),
            'Sent Mail')

    def test_move_fallback_copy_delete(self):
        FakeIMAP.fail_move = True
        now = int(time.time())
        FakeIMAP.folders = {'Snoozed': {1: '<m9>'}}
        self.store.add_snooze('a@x.test', '<m9>', 'INBOX', now - 5)
        self.assertEqual(self.poller.poll_once(now), (0, 1))
        self.assertEqual(FakeIMAP.moves, [('a@x.test', 'Snoozed', 'INBOX', 1)])


if __name__ == '__main__':
    unittest.main()
