"""HTTP layer: tracking pixel, click redirector, dashboard JSON API.

No framework; a thin BaseHTTPRequestHandler adapter (see server.py)
delegates to TrackerApp.handle(). Auth is mailbox credentials verified
over IMAP (same identity as the mailbox itself); sessions are
HMAC-signed cookies. Users only ever see their own mail; admins
(TRACKER_ADMINS) may list everything.
"""
import base64
import binascii
import hashlib
import hmac
import imaplib
import json
import logging
import os
import smtplib
import ssl
import time
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import urlparse, parse_qs

from .sig import link_signature
from .ua import parse_user_agent

LOG = logging.getLogger('tracker.web')

PIXEL_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00'
             b'\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00'
             b'\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')

SESSION_COOKIE = 'tt_session'
STATIC_TYPES = {'.html': 'text/html; charset=utf-8',
                '.js': 'application/javascript; charset=utf-8',
                '.css': 'text/css; charset=utf-8',
                '.png': 'image/png',
                '.svg': 'image/svg+xml'}


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii')


def _b64d(text):
    return base64.urlsafe_b64decode(text.encode('ascii'))


class TrackerApp:
    def __init__(self, config, store, geo, auth_fn=None, smtp_fn=None,
                 schedule_fn=None, file_fn=None):
        self.config = config
        self.store = store
        self.geo = geo
        self.auth_fn = auth_fn or self._imap_auth
        self.smtp_fn = smtp_fn or self._smtp_send
        self.schedule_fn = schedule_fn or self._scheduler_post
        self.file_fn = file_fn or self._imap_append
        self.static_dir = os.path.join(os.path.dirname(
            os.path.abspath(__file__)), 'dashboard')
        self._login_fails = {}

    # -- sessions ------------------------------------------------------
    def _sign(self, payload):
        return hmac.new(self.config.session_secret.encode(), payload,
                        hashlib.sha256).hexdigest()

    def make_session(self, user):
        exp = str(int(time.time()) + self.config.session_hours * 3600)
        body = f'{user.lower()}|{exp}'
        return _b64e(f'{body}|{self._sign(body.encode())}'.encode())

    def check_session(self, cookies):
        raw = cookies.get(SESSION_COOKIE, '')
        try:
            user, exp, sig = _b64d(raw).decode().split('|')
        except (ValueError, binascii.Error, UnicodeError):
            return None
        if not hmac.compare_digest(sig, self._sign(f'{user}|{exp}'.encode())):
            return None
        try:
            if int(exp) < time.time():
                return None
        except ValueError:
            return None
        return {'user': user, 'admin': user in self.config.admins}

    def check_service(self, headers, query, body, method):
        """Service auth for the Roundcube plugin. Returns a non-admin
        session for the asserted user, or None. Never grants admin."""
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
        return {'user': user, 'admin': False, 'service': True}

    @staticmethod
    def _service_allowed(method, route):
        if method == 'GET' and route in (
                '/api/messages', '/api/stats', '/api/optouts'):
            return True
        if route == '/api/optouts' and method == 'POST':
            # self-only: _api_optouts_post 403s non-admins touching
            # anyone else's sender scope; recipient scope is always self
            return True
        if route.startswith('/api/messages/'):
            # GET detail + POST tracking toggle; both owner-scoped inside
            return True
        if method == 'POST' and route == '/api/by-mids':
            return True
        return False

    # -- entry point ---------------------------------------------------
    def handle(self, method, path, headers, body, remote_addr=''):
        parsed = urlparse(path)
        route, query = parsed.path, parse_qs(parsed.query)
        cookies = {}
        for part in headers.get('cookie', '').split(';'):
            if '=' in part:
                key, _, val = part.partition('=')
                cookies[key.strip()] = val.strip()
        try:
            if route == '/' or route == '/index.html':
                return self._static('index.html')
            if route in ('/app.js', '/styles.css', '/favicon.svg'):
                return self._static(route[1:])
            if route.startswith('/o/'):
                return self._pixel(route, headers, remote_addr)
            if route.startswith('/c/'):
                return self._click(route, query, headers, remote_addr)
            if route == '/healthz':
                return self._json(200, {'ok': True})
            if route == '/api/login' and method == 'POST':
                return self._api_login(body, remote_addr)
            if not route.startswith('/api/'):
                return self._json(404, {'error': 'not found'})
            session = self.check_session(cookies)
            if route == '/api/logout' and method == 'POST':
                return self._clear_session()
            if session is None:
                # Service auth (Roundcube plugin): shared secret plus an
                # asserted user, restricted to read/self routes below.
                session = self.check_service(headers, query, body, method)
                if session is not None and not self._service_allowed(
                        method, route):
                    return self._json(403, {'error': 'forbidden'})
            if session is None:
                return self._json(401, {'error': 'login required'})
            return self._api(method, route, query, body, session)
        except Exception:
            LOG.exception('web handler error %s %s', method, route)
            return self._json(500, {'error': 'internal error'})

    # -- static --------------------------------------------------------
    def _static(self, name):
        path = os.path.join(self.static_dir, name)
        if os.path.dirname(os.path.abspath(path)) != self.static_dir:
            return self._json(404, {'error': 'not found'})
        try:
            with open(path, 'rb') as fh:
                content = fh.read()
        except OSError:
            return self._json(404, {'error': 'not found'})
        ext = os.path.splitext(name)[1]
        return 200, {'Content-Type': STATIC_TYPES.get(
            ext, 'application/octet-stream'),
            'Cache-Control': 'no-cache'}, content

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

    # -- auth ----------------------------------------------------------
    def _imap_auth(self, user, password):
        user = (user or '').strip().lower()
        if not user or not password or len(password) > 256:
            return False
        try:
            conn = imaplib.IMAP4_SSL(self.config.imap_host,
                                     self.config.imap_port, timeout=15)
            try:
                conn.login(user, password)
                return True
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception:
            return False

    def _imap_append(self, user, password, folder, mime):
        """Stage a copy in folder (Scheduled). Returns True on success."""
        folder = (folder or 'Scheduled').strip() or 'Scheduled'
        quoted = '"%s"' % folder.replace('\\', '\\\\').replace('"', '\\"')
        conn = imaplib.IMAP4_SSL(self.config.imap_host,
                                 self.config.imap_port, timeout=15)
        try:
            conn.login(user, password)
            typ, _ = conn.select(quoted)
            if typ != 'OK':
                conn.create(quoted)
                try:
                    conn.subscribe(quoted)
                except Exception:
                    pass
            typ, _ = conn.append(quoted, '\\Seen', None, bytes(mime))
            return typ == 'OK'
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _login_throttled(self, ip):
        now = time.time()
        recent = [t for t in self._login_fails.get(ip, []) if now - t < 600]
        if len(self._login_fails) > 10000:
            self._login_fails = {ip: recent}
        else:
            self._login_fails[ip] = recent
        return len(recent) >= 10

    def _api_login(self, body, remote_addr=''):
        ip = (remote_addr or '').strip() or 'unknown'
        if self._login_throttled(ip):
            return self._json(429, {'error': 'too many attempts, wait'})
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        user = (data.get('user') or '').strip().lower()
        if self.auth_fn(user, data.get('password') or ''):
            self._login_fails.pop(ip, None)
            cookie = (f'{SESSION_COOKIE}={self.make_session(user)}; Path=/;'
                      f' HttpOnly; SameSite=Lax; Secure;'
                      f' Max-Age={self.config.session_hours * 3600}')
            return self._json(200, {'user': user,
                                    'admin': user in self.config.admins},
                              {'Set-Cookie': cookie})
        self._login_fails.setdefault(ip, []).append(time.time())
        return self._json(401, {'error': 'invalid credentials'})

    def _clear_session(self):
        return self._json(200, {'ok': True},
                          {'Set-Cookie': f'{SESSION_COOKIE}=; Path=/;'
                           ' HttpOnly; SameSite=Lax; Secure; Max-Age=0'})

    # -- API -----------------------------------------------------------
    def _api(self, method, route, query, body, session):
        user = session['user']
        if route == '/api/me':
            return self._json(200, {'user': user, 'admin': session['admin']})
        if route == '/api/messages' and method == 'GET':
            return self._api_list(query, session)
        if route == '/api/stats' and method == 'GET':
            return self._api_stats(query, session)
        if route == '/api/optouts' and method == 'GET':
            return self._api_optouts_get(session)
        if route == '/api/optouts' and method == 'POST':
            return self._api_optouts_post(body, session)
        if route == '/api/optouts/check' and method == 'POST':
            return self._api_optouts_check(body, session)
        if route == '/api/compose' and method == 'POST':
            return self._api_compose(body, session)
        if route == '/api/by-mids' and method == 'POST':
            return self._api_by_mids(body, session)
        if route.startswith('/api/messages/'):
            return self._api_message(method, route, body, session)
        return self._json(404, {'error': 'not found'})

    def _scope_sender(self, query, session):
        if query.get('all', [''])[0] == '1' and session['admin']:
            return None
        return session['user']

    def _api_list(self, query, session):
        sender = self._scope_sender(query, session)
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

    def _api_stats(self, query, session):
        sender = self._scope_sender(query, session)
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
        sender = None if session['admin'] else session['user']
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
        result = {'sender_opted_out': self.store.sender_opted_out(user),
                  'recipients': self.store.list_recipient_optouts(user)}
        if session['admin']:
            result['all_sender_optouts'] = self.store.list_sender_optouts()
        return self._json(200, result)

    def _api_optouts_post(self, body, session):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        scope = data.get('scope')
        opted_out = bool(data.get('opted_out', True))
        if scope == 'sender':
            target = (data.get('address') or session['user']).strip().lower()
            if target != session['user'] and not session['admin']:
                return self._json(403, {'error': 'admins only'})
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

    def _api_optouts_check(self, body, session):
        """Compose helper: would this mail be tracked by default?"""
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        recipients = [r.strip().lower() for r in data.get('recipients', [])]
        user = session['user']
        if self.store.sender_opted_out(user):
            return self._json(200, {'track': False,
                                    'reason': 'you opted out of tracking'})
        opted = [r for r in recipients
                 if self.store.recipient_opted_out(user, r)]
        if len(recipients) == 1 and opted:
            return self._json(200, {'track': False, 'reason':
                                    f'{opted[0]} is on your opt-out list'})
        if opted:
            return self._json(200, {
                'track': True, 'reason': None,
                'warning': 'opted-out recipients cannot be excluded from'
                           ' multi-recipient mail; tracking stays on'})
        return self._json(200, {'track': True, 'reason': None})

    # -- panel compose -------------------------------------------------
    def _scheduler_post(self, user, sender, recipients, mime, send_at):
        url = (getattr(self.config, 'scheduler_url', '') or '').rstrip('/')
        secret = getattr(self.config, 'scheduler_secret', '') or ''
        if not url or not secret:
            raise RuntimeError('scheduling unavailable')
        payload = json.dumps({
            'user': user, 'sender': sender, 'recipients': recipients,
            'mime_b64': base64.b64encode(bytes(mime)).decode(),
            'send_at': int(send_at)}).encode()
        req = urllib.request.Request(
            url + '/v1/scheduled', data=payload,
            headers={'Content-Type': 'application/json',
                     'X-Scheduler-Secret': secret})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def _smtp_send(self, user, password, message):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port,
                              timeout=30, context=context) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)

    def _api_compose(self, body, session):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        user = session['user']
        recipients = [r.strip() for r in data.get('to', []) if r.strip()]
        subject = (data.get('subject') or '').strip()
        text = data.get('body') or ''
        track = bool(data.get('track', True))
        password = data.get('password') or ''
        send_at = data.get('send_at')
        if not recipients or len(recipients) > 20:
            return self._json(400, {'error': '1-20 recipients required'})
        if any('@' not in r for r in recipients):
            return self._json(400, {'error': 'bad recipient address'})
        if len(subject) > 500 or len(text) > 1024 * 1024:
            return self._json(400, {'error': 'subject/body too long'})
        if send_at is not None:
            try:
                send_at = int(send_at)
            except (TypeError, ValueError):
                return self._json(400, {'error': 'bad send_at'})
            now = int(time.time())
            if send_at < now + 30 or send_at > now + 366 * 86400:
                return self._json(400, {'error': 'bad send_at'})
            if not getattr(self.config, 'scheduler_url', ''):
                return self._json(400, {'error': 'scheduling unavailable'})
        if not self.auth_fn(user, password):
            return self._json(401, {'error': 'mailbox password required'})
        msg = EmailMessage()
        msg['From'] = user
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        msg['X-Tracker-Track'] = 'yes' if track else 'no'
        # Mint the Message-ID at compose time: for scheduled mail the SAME
        # bytes are stored by the daemon and staged in Scheduled, so the
        # Scheduled->Sent filing match depends on a stable, shared MID.
        # (The daemon only injects one when still missing.)
        domain = user.rsplit('@', 1)[-1] if '@' in user else 'localhost'
        msg['Message-ID'] = make_msgid(domain=domain)
        msg.set_content(text)
        if send_at is not None:
            try:
                res = self.schedule_fn(user, user, recipients,
                                       msg.as_bytes(), send_at)
            except Exception as exc:
                LOG.warning('compose schedule failed for %s: %s', user, exc)
                return self._json(502, {'error': 'schedule failed, try again'})
            # Stage the pending copy in Scheduled so the daemon files it to
            # Sent at send time (webmail parity). Best-effort: the mail IS
            # scheduled and will deliver; report honestly via filed.
            folder = getattr(self.config, 'scheduled_folder', 'Scheduled')
            try:
                filed = bool(self.file_fn(user, password, folder,
                                          msg.as_bytes()))
            except Exception as exc:
                LOG.warning('compose file failed for %s: %s', user, exc)
                filed = False
            return self._json(200, {'ok': True,
                                    'scheduled': (res or {}).get('id'),
                                    'send_at': send_at, 'filed': filed})
        try:
            self.smtp_fn(user, password, msg)
        except Exception as exc:
            LOG.warning('compose send failed for %s: %s', user, exc)
            return self._json(502, {'error': 'send failed, try again'})
        return self._json(200, {'ok': True})
