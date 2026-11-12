"""API: secret, validation, owner scoping, write-only encrypted password."""
import json
import os
import sqlite3
import tempfile
import unittest

from tests.fakes import FakeConfig, FakeServer
from mbackup.crypto import SecretBox
from mbackup.db import Store
from mbackup.server import BackupApp, validate_target
from mbackup.sync import Syncer
from cryptography.fernet import Fernet

H = {'x-backup-secret': 'test-secret'}


class FakePoller:
    def __init__(self):
        self.requested = []
        self.running = None

    def request(self, user):
        self.requested.append(user)

    def is_queued(self, user):
        return user in self.requested


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'b.db')
        self.store = Store(self.path)
        self.box = SecretBox(Fernet.generate_key())
        self.dst = FakeServer(delim='/')
        self.syncer = Syncer(FakeConfig(), self.store, self.box,
                             source_cls=FakeServer().connect,
                             target_factory=self.dst.connect)
        self.poller = FakePoller()
        self.app = BackupApp(FakeConfig(), self.store, self.box, self.syncer,
                             self.poller)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=H):
        status, _, out = self.app.handle(
            method, path, headers, json.dumps(body).encode() if body else b'')
        return status, json.loads(out)

    def put(self, **over):
        body = {'user': 'a@x.test', 'host': 'imap.gmail.com', 'port': 993,
                'username': 'me@gmail.com', 'password': 'app pass word',
                'prefix': 'Backup/a@x.test', 'enabled': True}
        body.update(over)
        return self.call('PUT', '/v1/target', body)

    def test_secret_required_health_open(self):
        self.assertEqual(self.call('GET', '/healthz', headers={})[0], 200)
        self.assertEqual(self.call('GET', '/v1/target?user=a@x.test',
                                   headers={})[0], 401)
        self.assertEqual(self.call('GET', '/v1/target?user=a@x.test',
                                   headers={'x-backup-secret': 'nope'})[0], 401)

    def test_put_get_never_returns_password_and_encrypts(self):
        status, view = self.put()
        self.assertEqual(status, 200, view)
        self.assertTrue(view['has_password'])
        self.assertNotIn('password', view)
        self.assertEqual(self.poller.requested, ['a@x.test'])
        raw = sqlite3.connect(self.path).execute(
            'SELECT password_enc FROM targets').fetchone()[0]
        self.assertNotIn('app pass word', raw)
        self.assertEqual(self.box.open(raw), 'app pass word')
        status, view = self.call('GET', '/v1/target?user=a@x.test')
        self.assertEqual(view['host'], 'imap.gmail.com')
        self.assertNotIn('app pass word', json.dumps(view))
        self.assertNotIn('password_enc', view)

    def test_password_required_first_then_kept(self):
        status, view = self.put(password='')
        self.assertEqual(status, 400)
        self.assertIn('password', view['error'])
        self.put()
        status, view = self.put(password='', include_junk=True)
        self.assertEqual(status, 200)
        self.assertTrue(view['include_junk'])
        enc = self.store.get_target('a@x.test')['password_enc']
        self.assertEqual(self.box.open(enc), 'app pass word')

    def test_identity_change_resets(self):
        self.put()
        self.store.mark_copied('a@x.test', 'd1')
        self.assertFalse(self.put(include_junk=True)[1]['reset'])
        self.assertTrue(self.store.is_copied('a@x.test', 'd1'))
        self.assertTrue(self.put(prefix='Elsewhere')[1]['reset'])
        self.assertFalse(self.store.is_copied('a@x.test', 'd1'))

    def test_validation(self):
        for over, word in ((dict(host='bad host'), 'server'),
                           (dict(host='-x.com'), 'server'),
                           (dict(port=70000), 'port'),
                           (dict(port='x'), 'port'),
                           (dict(username=''), 'username'),
                           (dict(prefix='a*b'), 'folder'),
                           (dict(prefix='a//b'), 'folder'),
                           (dict(prefix=''), 'folder'),
                           (dict(password='x' * 600), 'password')):
            status, view = self.put(**over)
            self.assertEqual(status, 400, over)
            self.assertIn(word, view['error'], over)

    def test_owner_must_be_local(self):
        self.assertEqual(self.put(user='evil@other.test')[0], 400)
        self.assertEqual(self.put(user='not-an-email')[0], 400)

    def test_owner_scoping(self):
        self.put()
        status, view = self.call('GET', '/v1/target?user=b@x.test')
        self.assertFalse(view['configured'])

    def test_delete(self):
        self.put()
        self.assertTrue(self.call('DELETE', '/v1/target?user=a@x.test')[1]['removed'])
        self.assertFalse(self.call('GET', '/v1/target?user=a@x.test')[1]['configured'])

    def test_test_endpoint_uses_stored_password_and_throttles(self):
        self.put()
        status, out = self.call('POST', '/v1/test', {
            'user': 'a@x.test', 'host': 'imap.gmail.com', 'port': 993,
            'username': 'me@gmail.com'})
        self.assertEqual(status, 200, out)
        self.assertTrue(out['ok'])
        self.assertEqual(self.dst.logins[-1], ('me@gmail.com', 'app pass word'))
        status, out = self.call('POST', '/v1/test', {
            'user': 'a@x.test', 'host': 'imap.gmail.com', 'username': 'u'})
        self.assertEqual(status, 429)

    def test_test_endpoint_needs_some_password(self):
        status, out = self.call('POST', '/v1/test', {
            'user': 'a@x.test', 'host': 'imap.gmail.com', 'username': 'u'})
        self.assertEqual(status, 400)
        self.assertFalse(out['ok'])

    def test_run(self):
        self.assertEqual(self.call('POST', '/v1/run', {'user': 'a@x.test'})[0], 404)
        self.put()
        self.poller.requested.clear()
        self.assertEqual(self.call('POST', '/v1/run', {'user': 'a@x.test'})[0], 200)
        self.assertEqual(self.poller.requested, ['a@x.test'])
        self.put(enabled=False)
        self.assertEqual(self.call('POST', '/v1/run', {'user': 'a@x.test'})[0], 409)

    def test_validate_normalizes(self):
        fields, err = validate_target({'host': 'IMAP.Gmail.com.', 'port': '993',
                                       'username': ' me ', 'prefix': '/B/x/',
                                       'password': 'p'}, True)
        self.assertIsNone(err)
        self.assertEqual((fields['host'], fields['port'], fields['username'],
                          fields['prefix']), ('imap.gmail.com', 993, 'me', 'B/x'))


class CryptoTests(unittest.TestCase):
    def test_roundtrip_and_wrong_key(self):
        box = SecretBox(Fernet.generate_key())
        token = box.seal('s3cret')
        self.assertNotIn('s3cret', token)
        self.assertEqual(box.open(token), 's3cret')
        self.assertIsNone(SecretBox(Fernet.generate_key()).open(token))
        self.assertIsNone(box.open(''))


if __name__ == '__main__':
    unittest.main()
