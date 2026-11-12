"""Backup HTTP API (for the Roundcube mail_backup plugin) and poller.

Trust model, same as the scheduler: ClusterIP only, shared secret
(`X-Backup-Secret`), the caller authenticates the user and passes the
owner explicitly; every read/mutation is scoped to that owner. Target
passwords are write-only through the API and stored encrypted.
"""
import hmac
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger('backup')

_HOST_RE = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
TEST_MIN_INTERVAL = 3  # seconds between connection tests per user


def _norm_email(value):
    return (value or '').strip().lower()


def _valid_email(value):
    value = _norm_email(value)
    if '@' not in value or len(value) > 320 or _CTRL_RE.search(value):
        return ''
    local, _, domain = value.partition('@')
    if not local or '.' not in domain:
        return ''
    return value


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def validate_target(data, require_password):
    """(fields, error). Normalizes host/port/username/prefix/password."""
    host = (data.get('host') or '').strip().lower().rstrip('.')
    if not _HOST_RE.match(host) or '..' in host:
        return None, 'enter a valid IMAP server name (e.g. imap.gmail.com)'
    try:
        port = int(data.get('port') or 993)
    except (TypeError, ValueError):
        return None, 'port must be a number'
    if not 1 <= port <= 65535:
        return None, 'port must be between 1 and 65535'
    username = (data.get('username') or '').strip()
    if not username or len(username) > 320 or _CTRL_RE.search(username):
        return None, 'enter the username of the backup account'
    password = data.get('password')
    if password is not None and not isinstance(password, str):
        return None, 'bad password'
    if password and (len(password) > 500 or '\x00' in password):
        return None, 'password is too long or invalid'
    if require_password and not password:
        return None, 'enter the password of the backup account'
    prefix = (data.get('prefix') or '').strip().strip('/')
    if (not prefix or len(prefix) > 120 or _CTRL_RE.search(prefix) or
            '//' in prefix or any(c in prefix for c in '*%')):
        return None, ('enter a folder name for the backup (letters, digits,'
                      ' "/" for subfolders; no * or %)')
    return {'host': host, 'port': port, 'username': username,
            'password': password or None, 'prefix': prefix,
            'enabled': _bool(data.get('enabled', True)),
            'include_junk': _bool(data.get('include_junk', False))}, None


class BackupApp:
    def __init__(self, config, store, box, syncer, poller=None):
        self.config = config
        self.store = store
        self.box = box
        self.syncer = syncer
        self.poller = poller
        self._last_test = {}
        self._test_lock = threading.Lock()

    def _check_secret(self, headers):
        given = headers.get('x-backup-secret', '')
        return bool(given) and hmac.compare_digest(given,
                                                   self.config.api_secret)

    def _json(self, status, obj):
        return status, {'Content-Type': 'application/json'}, json.dumps(
            obj).encode()

    def handle(self, method, path, headers, body):
        parsed = urlparse(path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == '/healthz' and method == 'GET':
            return self._json(200, {'ok': True})
        if not self._check_secret(headers):
            return self._json(401, {'error': 'bad secret'})
        if route == '/v1/target':
            if method == 'GET':
                return self._get(query)
            if method == 'PUT':
                return self._put(body)
            if method == 'DELETE':
                return self._delete(query)
        if route == '/v1/test' and method == 'POST':
            return self._test(body)
        if route == '/v1/run' and method == 'POST':
            return self._run(body)
        return self._json(404, {'error': 'not found'})

    def _owner_from_body(self, body):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return None, None
        if not isinstance(data, dict):
            return None, None
        user = _valid_email(data.get('user'))
        if (user and self.config.local_domains and
                user.rsplit('@', 1)[-1] not in self.config.local_domains):
            user = ''
        return user, data

    def _view(self, user):
        row = self.store.get_target(user)
        running = bool(self.poller and self.poller.running == user)
        queued = bool(self.poller and self.poller.is_queued(user))
        if not row:
            return {'configured': False, 'running': running}
        return {'configured': True, 'enabled': bool(row['enabled']),
                'host': row['host'], 'port': row['port'],
                'username': row['username'], 'prefix': row['prefix'],
                'include_junk': bool(row['include_junk']),
                'has_password': bool(row['password_enc']),
                'last_run_at': row['last_run_at'],
                'last_ok_at': row['last_ok_at'],
                'last_error': row['last_error'],
                'copied_total': row['copied_total'],
                'skipped_total': row['skipped_total'],
                'backlog': row['backlog'],
                'running': running, 'queued': queued}

    def _get(self, query):
        user = _valid_email((query.get('user') or [''])[0])
        if not user:
            return self._json(400, {'error': 'bad user'})
        return self._json(200, self._view(user))

    def _put(self, body):
        user, data = self._owner_from_body(body)
        if not user:
            return self._json(400, {'error': 'bad user'})
        existing = self.store.get_target(user)
        fields, error = validate_target(
            data, require_password=not (existing and existing['password_enc']))
        if error:
            return self._json(400, {'error': error})
        if fields['password']:
            fields['password_enc'] = self.box.seal(fields['password'])
        fields.pop('password')
        reset = self.store.save_target(user, fields)
        if fields['enabled'] and self.poller:
            self.poller.request(user)
        view = self._view(user)
        view['reset'] = reset
        return self._json(200, view)

    def _delete(self, query):
        user = _valid_email((query.get('user') or [''])[0])
        if not user:
            return self._json(400, {'error': 'bad user'})
        removed = self.store.delete_target(user)
        return self._json(200, {'removed': removed})

    def _test(self, body):
        user, data = self._owner_from_body(body)
        if not user:
            return self._json(400, {'error': 'bad user'})
        now = time.monotonic()
        with self._test_lock:
            last = self._last_test.get(user, 0)
            if now - last < TEST_MIN_INTERVAL:
                return self._json(429, {'error': 'wait a moment and retry'})
            self._last_test[user] = now
        existing = self.store.get_target(user)
        data = dict(data)
        data.setdefault('prefix', 'test')
        fields, error = validate_target(data, require_password=False)
        if error:
            return self._json(400, {'ok': False, 'message': error})
        password = fields['password']
        if not password and existing:
            password = self.box.open(existing['password_enc'])
        if not password:
            return self._json(400, {'ok': False,
                                    'message': 'enter the password to test'})
        ok, message = self.syncer.test_target(fields['host'], fields['port'],
                                              fields['username'], password)
        return self._json(200, {'ok': ok, 'message': message})

    def _run(self, body):
        user, _ = self._owner_from_body(body)
        if not user:
            return self._json(400, {'error': 'bad user'})
        row = self.store.get_target(user)
        if not row:
            return self._json(404, {'error': 'no backup configured'})
        if not row['enabled']:
            return self._json(409, {'error': 'backup is turned off'})
        if self.poller:
            self.poller.request(user)
        return self._json(200, {'queued': True})


class Poller:
    """One worker thread: runs every enabled user each POLL_SECONDS,
    sooner when a run was requested or a backlog remains."""

    BACKLOG_PAUSE = 15

    def __init__(self, config, store, syncer):
        self.config = config
        self.store = store
        self.syncer = syncer
        self.running = None
        self._wake = threading.Event()
        self._queue = []
        self._qlock = threading.Lock()

    def request(self, user):
        with self._qlock:
            if user not in self._queue:
                self._queue.append(user)
        self._wake.set()

    def is_queued(self, user):
        with self._qlock:
            return user in self._queue

    def _take_queue(self):
        with self._qlock:
            users, self._queue = self._queue, []
        return users

    def cycle(self, users):
        backlog = False
        for user in users:
            self.running = user
            try:
                result = self.syncer.run_user(user)
                backlog = backlog or bool(result.get('backlog'))
            except Exception:
                LOG.exception('backup run failed user=%s', user)
            finally:
                self.running = None
        return backlog

    def loop(self):
        next_full = 0.0
        while True:
            now = time.monotonic()
            users = self._take_queue()
            if now >= next_full:
                users += [u for u in self.store.list_enabled()
                          if u not in users]
                next_full = now + self.config.poll_seconds
            backlog = self.cycle(users) if users else False
            if backlog:
                next_full = min(next_full,
                                time.monotonic() + self.BACKLOG_PAUSE)
            timeout = max(1.0, next_full - time.monotonic())
            self._wake.wait(timeout)
            self._wake.clear()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'MailBackup/1.0'

    def _run(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(min(length, 65536)) if length > 0 else b''
        flat = {k.lower(): v for k, v in self.headers.items()}
        status, headers, out = self.server.app.handle(
            self.command, self.path, flat, body)
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(len(out)))
            for key, val in headers.items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(out)
        except (ConnectionError, BrokenPipeError):
            pass

    do_GET = _run
    do_PUT = _run
    do_POST = _run
    do_DELETE = _run

    def log_message(self, *args):
        pass


def main():
    from .config import Config
    from .crypto import SecretBox
    from .db import Store
    from .sync import Syncer
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s backup %(levelname)s %(message)s')
    config = Config()
    store = Store(config.db_path)
    box = SecretBox(config.crypt_key)
    syncer = Syncer(config, store, box)
    poller = Poller(config, store, syncer)
    app = BackupApp(config, store, box, syncer, poller)
    threading.Thread(target=poller.loop, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', config.web_port), Handler)
    server.app = app
    server.daemon_threads = True
    LOG.info('mail backup listening on %d (poll every %ds)', config.web_port,
             config.poll_seconds)
    server.serve_forever()


if __name__ == '__main__':
    main()
