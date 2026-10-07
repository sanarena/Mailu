"""HTTP layer: tracking pixel, click redirector, service JSON API.

No framework; a thin BaseHTTPRequestHandler adapter (see server.py)
delegates to TrackerApp.handle(). The only UI is the Roundcube
mail_tracking plugin, which authenticates with the shared
X-Tracker-Secret plus an asserted user. Every route is owner-scoped
to that user; there is no admin view and no login. (The standalone
dashboard was retired; track.* keeps serving pixel/click URLs baked
into already-sent mail.)
"""
import hmac
import json
import logging
from urllib.parse import urlparse, parse_qs

from .sig import link_signature
from .ua import parse_user_agent

LOG = logging.getLogger('tracker.web')

PIXEL_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00'
             b'\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00'
             b'\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


class TrackerApp:
    def __init__(self, config, store, geo):
        self.config = config
        self.store = store
        self.geo = geo

    # -- service auth --------------------------------------------------
    def check_service(self, headers, query, body, method):
        """Shared-secret auth for the Roundcube plugin. Returns the
        asserted user, or None. Owner-scoped; never grants admin."""
        secret = getattr(self.config, 'service_secret', '') or ''
        if not secret:
            return None
        presented = headers.get('x-tracker-secret', '')
        if not presented or not hmac.compare_digest(presented, secret):
            return None
        if method == 'GET':
            user = (query.get('user') or [''])[0]
        else:
            try:
                data = json.loads(body.decode() or '{}') or {}
            except ValueError:
                return None
            user = data.get('user') or ''
        user = (user or '').strip().lower()
        if '@' not in user or len(user) > 320:
            return None
        return {'user': user}

    # -- entry point ---------------------------------------------------
    def handle(self, method, path, headers, body, remote_addr=''):
        parsed = urlparse(path)
        route, query = parsed.path, parse_qs(parsed.query)
        try:
            if route.startswith('/o/'):
                return self._pixel(route, headers, remote_addr)
            if route.startswith('/c/'):
                return self._click(route, query, headers, remote_addr)
            if route == '/healthz':
                return self._json(200, {'ok': True})
            if not route.startswith('/api/'):
                return self._json(404, {'error': 'not found'})
            session = self.check_service(headers, query, body, method)
            if session is None:
                return self._json(401, {'error': 'service auth required'})
            return self._api(method, route, query, body, session)
        except Exception:
            LOG.exception('web handler error %s %s', method, route)
            return self._json(500, {'error': 'internal error'})

    def _json(self, status, obj, extra=None):
        headers = {'Content-Type': 'application/json',
                   'Cache-Control': 'no-store'}
        if extra:
            headers.update(extra)
        return status, headers, json.dumps(obj).encode()

    # -- pixel + click -------------------------------------------------
    def _client_ip(self, headers, remote_addr):
        forwarded = headers.get('x-forwarded-for', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
        # remote_addr is already a bare IP (no port); never split it,
        # that would mangle IPv6 peer addresses.
        return (remote_addr or '').strip()

    def _pixel(self, route, headers, remote_addr):
        token = route[3:].split('/')[0]
        if token.endswith('.gif'):
            token = token[:-4]
        if token:
            msg = self.store.get_message_by_token(token)
            if msg is not None and msg['tracking']:
                ua = headers.get('user-agent', '')[:500]
                ip = self._client_ip(headers, remote_addr)[:64]
                country, city = self.geo.lookup(ip)
                client, device = parse_user_agent(ua)
                self.store.add_event(msg['id'], 'open', ip=ip,
                                     country=country, city=city, client=client,
                                     device=device, user_agent=ua)
        return 200, {'Content-Type': 'image/gif',
                     'Content-Length': str(len(PIXEL_GIF)),
                     'Cache-Control': 'no-store, no-cache',
                     'Pragma': 'no-cache'}, PIXEL_GIF

    def link_signature(self, token, idx, url):
        return link_signature(self.config.hmac_secret, token, idx, url)

    def _click(self, route, query, headers, remote_addr):
        parts = route[3:].split('/')
        if len(parts) != 2:
            return self._json(404, {'error': 'not found'})
        token, idx_raw = parts
        try:
            idx = int(idx_raw)
        except ValueError:
            return self._json(404, {'error': 'not found'})
        msg = self.store.get_message_by_token(token) if token else None
        url = self.store.get_link(msg['id'], idx) if msg else None
        sig = (query.get('s', [''])[0] or '')
        if not msg or not url or not hmac.compare_digest(
                sig, self.link_signature(token, idx, url)):
            return self._json(404, {'error': 'not found'})
        if msg['tracking']:
            ua = headers.get('user-agent', '')[:500]
            ip = self._client_ip(headers, remote_addr)[:64]
            country, city = self.geo.lookup(ip)
            client, device = parse_user_agent(ua)
            self.store.add_event(msg['id'], 'click', ip=ip, country=country,
                                 city=city, client=client, device=device,
                                 user_agent=ua, link_idx=idx)
        return 302, {'Location': url, 'Cache-Control': 'no-store'}, b''

    # -- API -----------------------------------------------------------
    def _api(self, method, route, query, body, session):
        if route == '/api/messages' and method == 'GET':
            return self._api_list(query, session)
        if route == '/api/stats' and method == 'GET':
            return self._api_stats(session)
        if route == '/api/optouts' and method == 'GET':
            return self._api_optouts_get(session)
        if route == '/api/optouts' and method == 'POST':
            return self._api_optouts_post(body, session)
        if route == '/api/by-mids' and method == 'POST':
            return self._api_by_mids(body, session)
        if route.startswith('/api/messages/'):
            return self._api_message(method, route, body, session)
        return self._json(404, {'error': 'not found'})

    def _api_list(self, query, session):
        sender = session['user']
        try:
            limit = min(100, max(1, int(query.get('limit', ['50'])[0])))
            offset = max(0, int(query.get('offset', ['0'])[0]))
        except ValueError:
            return self._json(400, {'error': 'bad paging'})
        messages = self.store.list_messages(sender, limit, offset)
        counts = self.store.event_counts(sender)
        out = []
        for msg in messages:
            counts_msg = counts.get(msg['id'], {})
            out.append({
                'id': msg['id'], 'sender': msg['sender'],
                'subject': msg['subject'], 'sent_at': msg['sent_at'],
                'tracking': bool(msg['tracking']),
                'recipients': self.store.recipients_of(msg['id']),
                'opens': counts_msg.get('opens', 0),
                'clicks': counts_msg.get('clicks', 0),
                'last_open': counts_msg.get('last_open')})
        return self._json(200, {'messages': out,
                                'total': self.store.count_messages(sender)})

    def _api_by_mids(self, body, session):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        mids = data.get('message_ids')
        if not isinstance(mids, list) or not mids or len(mids) > 200:
            return self._json(400, {'error': '1-200 message_ids required'})
        clean = []
        for mid in mids:
            mid = mid.strip() if isinstance(mid, str) else ''
            if mid and len(mid) <= 500 and '\n' not in mid and '\r' not in mid:
                clean.append(mid)
        if not clean:
            return self._json(400, {'error': 'bad message_ids'})
        return self._json(200, {'results': self.store.summaries_by_mid(
            session['user'], clean)})

    def _api_stats(self, session):
        sender = session['user']
        stats = self.store.stats(sender)
        stats['open_rate'] = (stats['opened_messages'] / stats['sent']
                              if stats['sent'] else 0)
        return self._json(200, stats)

    def _api_message(self, method, route, body, session):
        rest = route[len('/api/messages/'):]
        mid_raw, _, action = rest.partition('/')
        try:
            mid = int(mid_raw)
        except ValueError:
            return self._json(404, {'error': 'not found'})
        sender = session['user']
        if method == 'GET' and not action:
            msg = self.store.get_message(mid, sender)
            if msg is None:
                return self._json(404, {'error': 'not found'})
            events = self.store.events_of(mid, sender) or []
            return self._json(200, {
                'id': msg['id'], 'sender': msg['sender'],
                'subject': msg['subject'], 'sent_at': msg['sent_at'],
                'tracking': bool(msg['tracking']),
                'recipients': self.store.recipients_of(mid),
                'links': self.store.links_of(mid),
                'events': events})
        if method == 'POST' and action == 'tracking':
            try:
                enabled = bool(json.loads(body.decode() or '{}').get(
                    'enabled', False))
            except ValueError:
                return self._json(400, {'error': 'bad json'})
            if not self.store.set_tracking(mid, enabled, sender):
                return self._json(404, {'error': 'not found'})
            return self._json(200, {'ok': True, 'tracking': enabled})
        return self._json(404, {'error': 'not found'})

    def _api_optouts_get(self, session):
        user = session['user']
        return self._json(200, {
            'sender_opted_out': self.store.sender_opted_out(user),
            'recipients': self.store.list_recipient_optouts(user)})

    def _api_optouts_post(self, body, session):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        scope = data.get('scope')
        opted_out = bool(data.get('opted_out', True))
        if scope == 'sender':
            target = (data.get('address') or session['user']).strip().lower()
            if target != session['user']:
                return self._json(403, {'error': 'own address only'})
            if '@' not in target:
                return self._json(400, {'error': 'bad address'})
            self.store.set_sender_optout(target, opted_out)
            return self._json(200, {'ok': True})
        if scope == 'recipient':
            address = (data.get('address') or '').strip().lower()
            if '@' not in address:
                return self._json(400, {'error': 'bad address'})
            self.store.set_recipient_optout(session['user'], address,
                                            opted_out)
            return self._json(200, {'ok': True})
        return self._json(400, {'error': 'bad scope'})
