"""Unit tests for the tracker web layer (no network: fake auth/SMTP/geo)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, ".")

from tracker.mail_tracker.config import Config
from tracker.mail_tracker.db import Store
from tracker.mail_tracker.geo import NullGeoDB
from tracker.mail_tracker.web import TrackerApp, PIXEL_GIF


class FakeConfig:
    track_host = 'track.x.test'
    imap_host = 'imap.x.test'
    imap_port = 993
    smtp_host = 'smtp.x.test'
    smtp_port = 465
    local_domains = {'x.test'}
    admins = {'admin@x.test'}
    session_secret = 'session-secret-for-tests'
    hmac_secret = 'hmac-secret-for-tests'
    session_hours = 12

    @property
    def base_url(self):
        return 'https://track.x.test'


class WebTest(unittest.TestCase):
    def setUp(self):
        for var in ('SESSION_SECRET', 'HMAC_SECRET'):
            os.environ[var] = 'x'
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 't.db'))
        self.passwords = {'admin@x.test': 'adminpw', 'a@x.test': 'apw',
                          'b@x.test': 'bpw'}
        self.sent = []
        self.app = TrackerApp(
            FakeConfig(), self.store, NullGeoDB(),
            auth_fn=lambda u, p: self.passwords.get(u) == p,
            smtp_fn=lambda u, p, m: self.sent.append((u, m)))
        self.mid_a = self.store.add_message(
            'tokA', '<a>', 'a@x.test', 'Hello', ['c@y.test'],
            ['http://e.test/'], 10)
        self.mid_b = self.store.add_message(
            'tokB', '<b>', 'b@x.test', 'Other', ['d@y.test'], [], 10)

    def tearDown(self):
        self.store.close()

    def call(self, method, path, body=b'', cookies=None, headers=None,
             user=None):
        headers = dict(headers or {})
        if user and cookies is None:
            headers['cookie'] = f'tt_session={self.app.make_session(user)}'
        elif cookies:
            headers['cookie'] = cookies
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return self.app.handle(method, path, headers, body, '9.9.9.9')

    def test_login_logout_me(self):
        status, headers, body = self.call(
            'POST', '/api/login', {'user': 'a@x.test', 'password': 'apw'})
        self.assertEqual(status, 200)
        self.assertIn('tt_session=', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';')[0]
        status, _, body = self.call('GET', '/api/me', cookies=cookie)
        self.assertEqual(json.loads(body), {'user': 'a@x.test',
                                            'admin': False})
        status, headers, _ = self.call('POST', '/api/logout', cookies=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])

    def test_login_rejects_bad_password(self):
        status, _, _ = self.call(
            'POST', '/api/login', {'user': 'a@x.test', 'password': 'no'})
        self.assertEqual(status, 401)

    def test_login_throttled_after_ten_failures(self):
        for _ in range(10):
            status, _, _ = self.app.handle(
                'POST', '/api/login', {},
                json.dumps({'user': 'a@x.test',
                            'password': 'wrong'}).encode(), '1.1.1.1')
            self.assertEqual(status, 401)
        status, _, _ = self.app.handle(
            'POST', '/api/login', {},
            json.dumps({'user': 'a@x.test',
                        'password': 'wrong'}).encode(), '1.1.1.1')
        self.assertEqual(status, 429)
        # another IP is unaffected; success clears the counter
        status, _, _ = self.app.handle(
            'POST', '/api/login', {},
            json.dumps({'user': 'a@x.test',
                        'password': 'apw'}).encode(), '2.2.2.2')
        self.assertEqual(status, 200)

    def test_pixel_keeps_ipv6_peer(self):
        self.app.handle('GET', '/o/tokA.gif', {}, b'', '2001:db8::7')
        self.assertEqual(self.store.events_of(self.mid_a)[0]['ip'],
                         '2001:db8::7')

    def test_api_requires_login(self):
        for method, path in (('GET', '/api/me'), ('GET', '/api/messages'),
                             ('GET', '/api/stats'),
                             ('GET', '/api/messages/1')):
            status, _, _ = self.app.handle(method, path, {}, b'')
            self.assertEqual(status, 401, path)

    def test_tampered_session_rejected(self):
        good = self.app.make_session('a@x.test')
        bad = good[:-2] + ('AA' if not good.endswith('AA') else 'BB')
        status, _, _ = self.call('GET', '/api/me',
                                 cookies=f'tt_session={bad}')
        self.assertEqual(status, 401)

    def test_list_isolation(self):
        _, _, body = self.call('GET', '/api/messages', user='a@x.test')
        data = json.loads(body)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['messages'][0]['sender'], 'a@x.test')
        _, _, body = self.call('GET', '/api/messages', user='b@x.test')
        self.assertEqual(json.loads(body)['total'], 1)

    def test_detail_isolation_no_leak(self):
        status, _, _ = self.call(f'GET', f'/api/messages/{self.mid_b}',
                                 user='a@x.test')
        self.assertEqual(status, 404)
        status, _, body = self.call('GET', f'/api/messages/{self.mid_a}',
                                    user='a@x.test')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['recipients'], ['c@y.test'])

    def test_admin_sees_all(self):
        _, _, body = self.call('GET', '/api/messages?all=1',
                               user='admin@x.test')
        self.assertEqual(json.loads(body)['total'], 2)
        status, _, _ = self.call('GET', f'/api/messages/{self.mid_a}',
                                 user='admin@x.test')
        self.assertEqual(status, 200)
        # non-admin ?all=1 is ignored, not an error
        _, _, body = self.call('GET', '/api/messages?all=1', user='a@x.test')
        self.assertEqual(json.loads(body)['total'], 1)

    def test_kill_switch_authz(self):
        status, _, _ = self.call(
            'POST', f'/api/messages/{self.mid_a}/tracking',
            {'enabled': False}, user='b@x.test')
        self.assertEqual(status, 404)
        status, _, body = self.call(
            'POST', f'/api/messages/{self.mid_a}/tracking',
            {'enabled': False}, user='a@x.test')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['tracking'])
        status, _, body = self.call(
            'POST', f'/api/messages/{self.mid_a}/tracking',
            {'enabled': True}, user='admin@x.test')
        self.assertEqual(status, 200)

    def test_pixel_logs_and_serves(self):
        status, headers, body = self.call(
            'GET', '/o/tokA.gif',
            headers={'user-agent': 'GoogleImageProxy'})
        self.assertEqual(status, 200)
        self.assertEqual(body, PIXEL_GIF)
        self.assertIn('no-store', headers['Cache-Control'])
        events = self.store.events_of(self.mid_a)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'open')
        self.assertEqual(events[0]['ip'], '9.9.9.9')
        self.assertEqual(events[0]['client'], 'Gmail image proxy')

    def test_pixel_unknown_and_disabled(self):
        status, _, body = self.call('GET', '/o/nope.gif')
        self.assertEqual((status, body), (200, PIXEL_GIF))
        self.assertEqual(self.store.events_of(self.mid_a), [])
        self.store.set_tracking(self.mid_a, False)
        self.call('GET', '/o/tokA.gif')
        self.assertEqual(self.store.events_of(self.mid_a), [])

    def test_pixel_uses_forwarded_ip(self):
        self.call('GET', '/o/tokA.gif',
                  headers={'x-forwarded-for': '1.2.3.4, 5.6.7.8'})
        self.assertEqual(self.store.events_of(self.mid_a)[0]['ip'], '1.2.3.4')

    def test_click_redirect_and_logging(self):
        sig = self.app.link_signature('tokA', 0, 'http://e.test/')
        status, headers, _ = self.call('GET', f'/c/tokA/0?s={sig}')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], 'http://e.test/')
        events = self.store.events_of(self.mid_a)
        self.assertEqual([(e['kind'], e['link_idx']) for e in events],
                         [('click', 0)])

    def test_click_rejects_forgery(self):
        for path in ('/c/tokA/0', '/c/tokA/0?s=wrong', '/c/tokA/9?s=x',
                     '/c/nope/0?s=x', '/c/tokA/x?s=x', '/c/tokA'):
            status, _, _ = self.call('GET', path)
            self.assertEqual(status, 404, path)
        self.assertEqual(self.store.events_of(self.mid_a), [])

    def test_click_disabled_still_redirects_silently(self):
        self.store.set_tracking(self.mid_a, False)
        sig = self.app.link_signature('tokA', 0, 'http://e.test/')
        status, headers, _ = self.call('GET', f'/c/tokA/0?s={sig}')
        self.assertEqual((status, headers['Location']),
                         (302, 'http://e.test/'))
        self.assertEqual(self.store.events_of(self.mid_a), [])

    def test_optouts(self):
        _, _, body = self.call('GET', '/api/optouts', user='a@x.test')
        self.assertEqual(json.loads(body)['sender_opted_out'], False)
        status, _, _ = self.call(
            'POST', '/api/optouts',
            {'scope': 'recipient', 'address': 'spam@y.test',
             'opted_out': True}, user='a@x.test')
        self.assertEqual(status, 200)
        _, _, body = self.call('GET', '/api/optouts', user='a@x.test')
        self.assertEqual(json.loads(body)['recipients'], ['spam@y.test'])
        # another user's list is untouched and invisible
        _, _, body = self.call('GET', '/api/optouts', user='b@x.test')
        self.assertEqual(json.loads(body)['recipients'], [])
        # non-admin cannot opt out other senders
        status, _, _ = self.call(
            'POST', '/api/optouts',
            {'scope': 'sender', 'address': 'b@x.test', 'opted_out': True},
            user='a@x.test')
        self.assertEqual(status, 403)
        status, _, _ = self.call(
            'POST', '/api/optouts',
            {'scope': 'sender', 'address': 'b@x.test', 'opted_out': True},
            user='admin@x.test')
        self.assertEqual(status, 200)
        self.assertTrue(self.store.sender_opted_out('b@x.test'))

    def test_optouts_check(self):
        _, _, body = self.call(
            'POST', '/api/optouts/check', {'recipients': ['c@y.test']},
            user='a@x.test')
        self.assertTrue(json.loads(body)['track'])
        self.store.set_recipient_optout('a@x.test', 'c@y.test', True)
        _, _, body = self.call(
            'POST', '/api/optouts/check', {'recipients': ['c@y.test']},
            user='a@x.test')
        data = json.loads(body)
        self.assertFalse(data['track'])
        self.assertIn('opt-out', data['reason'])

    def test_compose(self):
        status, _, body = self.call('POST', '/api/compose', {
            'to': ['c@y.test'], 'subject': 'Hi', 'body': 'hello',
            'track': True, 'password': 'apw'}, user='a@x.test')
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sent), 1)
        user, msg = self.sent[0]
        self.assertEqual(user, 'a@x.test')
        self.assertEqual(msg['X-Tracker-Track'], 'yes')
        self.assertEqual(msg['Subject'], 'Hi')

    def test_compose_requires_password_and_validates(self):
        status, _, _ = self.call('POST', '/api/compose', {
            'to': ['c@y.test'], 'subject': 'Hi', 'body': 'x',
            'password': 'wrong'}, user='a@x.test')
        self.assertEqual(status, 401)
        status, _, _ = self.call('POST', '/api/compose', {
            'to': [], 'subject': 'Hi', 'body': 'x', 'password': 'apw'},
            user='a@x.test')
        self.assertEqual(status, 400)
        status, _, _ = self.call('POST', '/api/compose', {
            'to': ['not-an-address'], 'subject': 'H', 'body': 'x',
            'password': 'apw'}, user='a@x.test')
        self.assertEqual(status, 400)
        self.assertEqual(self.sent, [])

    def test_stats_scoped(self):
        self.store.add_event(self.mid_a, 'open')
        _, _, body = self.call('GET', '/api/stats', user='a@x.test')
        data = json.loads(body)
        self.assertEqual((data['sent'], data['opens']), (1, 1))
        self.assertEqual(data['open_rate'], 1.0)
        _, _, body = self.call('GET', '/api/stats', user='b@x.test')
        self.assertEqual(json.loads(body)['opens'], 0)

    def test_healthz_no_auth(self):
        status, _, body = self.app.handle('GET', '/healthz', {}, b'')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True})

    def test_static_dashboard(self):
        status, headers, body = self.call('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'Mail Tracker', body)
        self.assertIn('text/html', headers['Content-Type'])
        status, _, body = self.call('GET', '/app.js')
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'(function'))
        status, _, _ = self.call('GET', '/nope.css')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
