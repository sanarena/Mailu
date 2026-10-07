"""Unit tests for the tracker web layer (no network: fake geo).

Only the Roundcube plugin talks to this API (shared service secret
plus asserted user, owner-scoped). The dashboard is retired: its
routes must 404 (see test_retired_dashboard_gone).
"""
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
    local_domains = {'x.test'}
    hmac_secret = 'hmac-secret-for-tests'
    service_secret = 'svc'

    @property
    def base_url(self):
        return 'https://track.x.test'


class WebTest(unittest.TestCase):
    def setUp(self):
        for var in ('HMAC_SECRET', 'TRACKER_API_SECRET'):
            os.environ[var] = 'x'
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 't.db'))
        self.app = TrackerApp(FakeConfig(), self.store, NullGeoDB())
        self.mid_a = self.store.add_message(
            'tokA', '<a>', 'a@x.test', 'Hello', ['c@y.test'],
            ['http://e.test/'], 10)
        self.mid_b = self.store.add_message(
            'tokB', '<b>', 'b@x.test', 'Other', ['d@y.test'], [], 10)

    def tearDown(self):
        self.store.close()

    def call(self, method, path, body=b'', headers=None, user=None,
             secret='svc'):
        headers = dict(headers or {})
        if secret is not None:
            headers['x-tracker-secret'] = secret
        if isinstance(body, dict):
            body = dict(body)
            if user is not None and 'user' not in body:
                body['user'] = user
            body = json.dumps(body).encode()
        elif user is not None and method == 'GET' and 'user=' not in path:
            sep = '&' if '?' in path else '?'
            path = f'{path}{sep}user={user}'
        return self.app.handle(method, path, headers, body, '9.9.9.9')

    def test_retired_dashboard_gone(self):
        # static SPA
        for path in ('/', '/index.html', '/app.js', '/styles.css',
                     '/favicon.svg'):
            status, _, _ = self.call('GET', path)
            self.assertEqual(status, 404, path)
        # session auth is gone entirely
        for method, path in (('POST', '/api/login'),
                             ('POST', '/api/logout')):
            status, _, _ = self.call(method, path, {'user': 'a@x.test'})
            self.assertEqual(status, 404, path)
        # dashboard-only routes
        status, _, _ = self.call('GET', '/api/me', user='a@x.test')
        self.assertEqual(status, 404)
        for method, path in (('POST', '/api/compose'),
                             ('POST', '/api/optouts/check')):
            status, _, _ = self.call(method, path, {'user': 'a@x.test'},
                                     user='a@x.test')
            self.assertEqual(status, 404, path)

    def test_api_requires_service_auth(self):
        for method, path in (('GET', '/api/messages'),
                             ('GET', '/api/stats'),
                             ('GET', '/api/messages/1'),
                             ('GET', '/api/optouts')):
            status, _, _ = self.app.handle(method, path, {}, b'')
            self.assertEqual(status, 401, path)
        # wrong secret, missing user, bad user
        status, _, _ = self.call('GET', '/api/stats', user='a@x.test',
                                 secret='nope')
        self.assertEqual(status, 401)
        status, _, _ = self.call('GET', '/api/stats', secret='svc')
        self.assertEqual(status, 401)
        status, _, _ = self.call('GET', '/api/stats', user='not-an-email')
        self.assertEqual(status, 401)

    def test_service_disabled_without_secret(self):
        self.app.config.service_secret = ''
        try:
            status, _, _ = self.call('GET', '/api/stats', user='a@x.test')
            self.assertEqual(status, 401)
        finally:
            self.app.config.service_secret = 'svc'

    def test_list_isolation(self):
        _, _, body = self.call('GET', '/api/messages', user='a@x.test')
        data = json.loads(body)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['messages'][0]['sender'], 'a@x.test')
        _, _, body = self.call('GET', '/api/messages', user='b@x.test')
        self.assertEqual(json.loads(body)['total'], 1)
        # all=1 is ignored: there is no admin view anymore
        _, _, body = self.call('GET', '/api/messages?all=1',
                               user='a@x.test')
        self.assertEqual(json.loads(body)['total'], 1)

    def test_detail_isolation_no_leak(self):
        status, _, _ = self.call('GET', f'/api/messages/{self.mid_b}',
                                 user='a@x.test')
        self.assertEqual(status, 404)
        status, _, body = self.call('GET', f'/api/messages/{self.mid_a}',
                                    user='a@x.test')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['recipients'], ['c@y.test'])

    def test_detail_includes_links(self):
        status, _, body = self.call('GET', f'/api/messages/{self.mid_a}',
                                    user='a@x.test')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['links'],
                         [{'idx': 0, 'url': 'http://e.test/'}])
        status, _, body = self.call('GET', f'/api/messages/{self.mid_b}',
                                    user='b@x.test')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['links'], [])

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

    def test_pixel_keeps_ipv6_peer(self):
        self.app.handle('GET', '/o/tokA.gif', {}, b'', '2001:db8::7')
        self.assertEqual(self.store.events_of(self.mid_a)[0]['ip'],
                         '2001:db8::7')

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
        data = json.loads(body)
        self.assertEqual(data['sender_opted_out'], False)
        self.assertNotIn('all_sender_optouts', data)
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
        # cannot opt out other senders (no admins anymore)
        status, _, _ = self.call(
            'POST', '/api/optouts',
            {'scope': 'sender', 'address': 'b@x.test', 'opted_out': True},
            user='a@x.test')
        self.assertEqual(status, 403)
        # own sender scope works, address optional
        status, _, _ = self.call(
            'POST', '/api/optouts',
            {'scope': 'sender', 'opted_out': True}, user='a@x.test')
        self.assertEqual(status, 200)
        self.assertTrue(self.store.sender_opted_out('a@x.test'))

    def test_by_mids(self):
        self.store.add_event(self.mid_a, 'open')
        self.store.add_event(self.mid_a, 'click')
        status, _, body = self.call(
            'POST', '/api/by-mids',
            {'user': 'a@x.test',
             'message_ids': ['<a>', '<b>', '<unknown>']})
        self.assertEqual(status, 200)
        res = json.loads(body)['results']
        # own MID mapped, other's + unknown omitted
        self.assertEqual(set(res), {'<a>'})
        self.assertEqual(
            (res['<a>']['opens'], res['<a>']['clicks'],
             res['<a>']['id'], res['<a>']['tracking']),
            (1, 1, self.mid_a, True))
        self.assertIsNotNone(res['<a>']['last_open'])
        # validation
        status, _, _ = self.call(
            'POST', '/api/by-mids',
            {'user': 'a@x.test', 'message_ids': []})
        self.assertEqual(status, 400)
        status, _, _ = self.call(
            'POST', '/api/by-mids',
            {'user': 'a@x.test', 'message_ids': ['<x>'] * 201})
        self.assertEqual(status, 400)

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


if __name__ == '__main__':
    unittest.main()
