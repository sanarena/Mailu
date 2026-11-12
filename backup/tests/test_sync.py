"""Sync engine against in-memory source and target accounts."""
import os
import tempfile
import unittest

from tests.fakes import FakeConfig, FakeServer
from mbackup.crypto import SecretBox
from mbackup.db import Store
from mbackup.sync import Syncer
from cryptography.fernet import Fernet

USER = 'a@x.test'


def msg(n):
    return (f'From: s{n}@y.test\r\nTo: {USER}\r\nSubject: m{n}\r\n'
            f'Message-ID: <m{n}@y.test>\r\n\r\nbody {n}\r\n').encode()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'b.db'))
        self.box = SecretBox(Fernet.generate_key())
        self.config = FakeConfig()
        self.src = FakeServer(delim='.', special={'Junk': ['\\Junk'],
                                                  'Sent': ['\\Sent']})
        self.dst = FakeServer(delim='/')
        for f in ('INBOX', 'Sent', 'Junk', 'Archive', 'Drafts'):
            self.src.add_folder(f)
        self.syncer = Syncer(self.config, self.store, self.box,
                             source_cls=self.src.connect,
                             target_factory=self.dst.connect)
        self.save()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, **over):
        fields = {'enabled': True, 'host': 'imap.example.com', 'port': 993,
                  'username': 'me@example.com', 'prefix': 'Backup/a',
                  'include_junk': False,
                  'password_enc': self.box.seal('app-password')}
        fields.update(over)
        return self.store.save_target(USER, fields)

    def names(self):
        return {k: [m['raw'] for m in v['msgs']]
                for k, v in self.dst.folders.items() if v['msgs']}

    def test_initial_backup_all_folders_but_junk(self):
        self.src.add('INBOX', msg(1), flags=['\\Seen', '\\Recent'])
        self.src.add('Sent', msg(2), flags=['\\Seen', '\\Answered'])
        self.src.add('Junk', msg(3))
        self.src.add('Archive', msg(4), flags=['\\Flagged'])
        r = self.syncer.run_user(USER)
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['copied'], 3)
        got = self.names()
        self.assertEqual(set(got), {'Backup/a/INBOX', 'Backup/a/Sent',
                                    'Backup/a/Archive'})
        sent = self.dst.folders['Backup/a/Sent']['msgs'][0]
        self.assertEqual(sorted(sent['flags']), ['\\Answered', '\\Seen'])
        self.assertEqual(sent['date'], '07-Oct-2026 21:26:16 +0000')
        inbox = self.dst.folders['Backup/a/INBOX']['msgs'][0]
        self.assertEqual(inbox['flags'], ['\\Seen'])  # \Recent dropped
        # parents created for nested labels
        self.assertIn('Backup', self.dst.folders)
        # trusted-net source login as the mailbox owner
        self.assertEqual(self.src.logins[0][0], USER)
        self.assertEqual(self.dst.logins[0], ('me@example.com', 'app-password'))
        row = self.store.get_target(USER)
        self.assertEqual(row['copied_total'], 3)
        self.assertTrue(row['last_ok_at'])
        self.assertEqual(row['last_error'], '')

    def test_include_junk(self):
        self.save(include_junk=True)
        self.src.add('Junk', msg(3))
        self.assertEqual(self.syncer.run_user(USER)['copied'], 1)
        self.assertIn('Backup/a/Junk', self.names())

    def test_incremental_and_server_delete_keeps_backup(self):
        u1 = self.src.add('INBOX', msg(1))
        self.syncer.run_user(USER)
        self.src.remove('INBOX', u1)
        self.src.add('INBOX', msg(2))
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 1)
        self.assertEqual(self.names()['Backup/a/INBOX'], [msg(1), msg(2)])

    def test_move_between_folders_is_not_duplicated(self):
        u1 = self.src.add('INBOX', msg(1))
        self.syncer.run_user(USER)
        self.src.remove('INBOX', u1)
        self.src.add('Archive', msg(1))
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 0)
        self.assertNotIn('Backup/a/Archive', self.names())

    def test_uidvalidity_change_rescans_without_duplicates(self):
        self.src.add('INBOX', msg(1))
        self.syncer.run_user(USER)
        self.src.folders['INBOX']['uidvalidity'] = 99
        self.src.add('INBOX', msg(2))
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 1)
        self.assertEqual(len(self.names()['Backup/a/INBOX']), 2)

    def test_budget_leaves_backlog_then_continues(self):
        self.config.max_messages_per_run = 2
        for n in range(5):
            self.src.add('INBOX', msg(n))
        r = self.syncer.run_user(USER)
        self.assertEqual((r['copied'], r['backlog']), (2, 3))
        r = self.syncer.run_user(USER)
        r = self.syncer.run_user(USER)
        self.assertEqual(r['backlog'], 0)
        self.assertEqual(len(self.names()['Backup/a/INBOX']), 5)

    def test_oversize_skipped_and_reported(self):
        self.config.max_message_bytes = 10
        self.src.add('INBOX', msg(1))
        r = self.syncer.run_user(USER)
        self.assertTrue(r['ok'])
        self.assertEqual((r['copied'], r['skipped']), (0, 1))
        self.assertIn('could not be copied', self.store.get_target(USER)['last_error'])

    def test_transient_refusal_does_not_advance(self):
        self.src.add('INBOX', msg(1))
        self.dst.refuse = [('NO', b'[OVERQUOTA] Account exceeded quota')]
        r = self.syncer.run_user(USER)
        self.assertFalse(r['ok'])
        self.assertIn('resumes', r['error'])
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 1)

    def test_permanent_refusal_skips(self):
        self.src.add('INBOX', msg(1))
        self.src.add('INBOX', msg(2))
        self.dst.refuse = [('NO', b'[CANNOT] Invalid message')]
        r = self.syncer.run_user(USER)
        self.assertEqual((r['copied'], r['skipped']), (1, 1))

    def test_trycreate_retries(self):
        self.src.add('INBOX', msg(1))
        self.syncer.run_user(USER)
        del self.dst.folders['Backup/a/INBOX']  # removed on the target
        self.src.add('INBOX', msg(2))
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 1)
        self.assertEqual(self.names()['Backup/a/INBOX'], [msg(2)])

    def test_target_login_failure_is_reported(self):
        self.dst.login_ok = False
        self.src.add('INBOX', msg(1))
        r = self.syncer.run_user(USER)
        self.assertFalse(r['ok'])
        self.assertIn('login rejected', self.store.get_target(USER)['last_error'])

    def test_unreadable_password(self):
        self.save(password_enc='not-a-token')
        r = self.syncer.run_user(USER)
        self.assertFalse(r['ok'])
        self.assertIn('enter the password again', r['error'])

    def test_disabled_does_nothing(self):
        self.save(enabled=False)
        self.src.add('INBOX', msg(1))
        r = self.syncer.run_user(USER)
        self.assertFalse(r['ok'])
        self.assertEqual(self.names(), {})

    def test_new_target_starts_over(self):
        self.src.add('INBOX', msg(1))
        self.syncer.run_user(USER)
        self.assertTrue(self.save(prefix='Other'))
        r = self.syncer.run_user(USER)
        self.assertEqual(r['copied'], 1)
        self.assertIn('Other/INBOX', self.names())

    def test_target_name_mapping(self):
        tn = Syncer.target_name
        self.assertEqual(tn('Backup/a@x.test', 'INBOX', '.', '/'),
                         'Backup/a@x.test/INBOX')
        self.assertEqual(tn('Backup/a@x.test', 'Work.Q1', '.', '/'),
                         'Backup/a@x.test/Work/Q1')
        # dots inside one level on a '.'-delimited target stay one level
        self.assertEqual(tn('Backup/a@x.test', 'Work.Q1', '.', '.'),
                         'Backup.a@x_test.Work.Q1')
        # a '/' inside a dovecot folder name stays one level on Gmail
        self.assertEqual(tn('Backup', 'A/B', '.', '/'), 'Backup/A_B')
        self.assertEqual(tn('Büro', 'INBOX', '.', '/'), 'B&APw-ro/INBOX')

    def test_test_target(self):
        ok, message = self.syncer.test_target('imap.example.com', 993, 'u', 'p')
        self.assertTrue(ok)
        self.assertIn('"/"', message)
        self.dst.login_ok = False
        ok, message = self.syncer.test_target('imap.example.com', 993, 'u', 'p')
        self.assertFalse(ok)
        self.assertIn('login rejected', message)


if __name__ == '__main__':
    unittest.main()
