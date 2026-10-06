"""Scheduler HTTP API, due poller, and SMTP/IMAP delivery workers.

Trust model: the API listens on ClusterIP only and is guarded by a
shared secret. Callers (Roundcube plugin, tracker) authenticate the
user first and pass the owner explicitly; every read/mutation is
owner-scoped. Delivery uses backend trust: pod-network SMTP relay
(permit_mynetworks) and trusted-net IMAP (any-password login).
"""
import base64
import binascii
import email
import hmac
import imaplib
import json
import logging
import smtplib
import threading
import time
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LOG = logging.getLogger('scheduler')

POLICY = policy.default
RETRY_DELAYS = (300, 1800, 7200)
GIVE_UP_AFTER = 24 * 3600
# Dovecot SEARCH can lag minutes behind APPEND/MOVE (live incident:
# fresh mail invisible to search for ~15 min on a shared index).
# Treat "not found" as transient until the row is this old.
NOT_FOUND_GRACE = 30 * 60


def _norm_email(value):
    return (value or '').strip().lower()


def _valid_email(value):
    value = _norm_email(value)
    if '@' not in value or len(value) > 320:
        return ''
    local, _, domain = value.partition('@')
    if not local or '.' not in domain:
        return ''
    return value


def parse_recipients(raw_list, max_recipients):
    """Flatten raw recipient strings (To/Cc/Bcc, comma lists, display
    names) into bare addresses via the stdlib parser."""
    from email.utils import getaddresses
    addrs = [a.strip().lower() for _, a in getaddresses(raw_list or [])]
    addrs = [a for a in addrs if a]
    if not addrs or len(addrs) > max_recipients:
        return None
    if any('@' not in a or len(a) > 320 for a in addrs):
        return None
    return addrs


def _list_name(line):
    """Mailbox name from a LIST response line. Handles both quoted
    ('. "/" "Sent Box"') and unquoted ('." "." Sent', dovecot's
    Maildir++ '.' delimiter) trailing names."""
    parts = line.split(b'"')
    if len(parts) >= 3:
        tail = parts[-1].strip()
        if tail:
            return tail.decode('utf-7', 'replace')
        return parts[-2].decode('utf-7', 'replace')
    return line.rsplit(b' ', 1)[-1].decode('utf-7', 'replace')


class ImapWorker:
    """Trusted-net IMAP helper. One connection per call (poller volume
    is tiny); login password is ignored by the backend on trusted nets.
    """

    def __init__(self, host, port, imap_cls=None):
        self.host = host
        self.port = port
        self.imap_cls = imap_cls or imaplib.IMAP4

    def _connect(self, user):
        conn = self.imap_cls(self.host, self.port)
        conn.login(user, 'scheduler-trusted-net')
        return conn

    def _uids_by_message_id(self, conn, folder, message_id):
        typ, _ = conn.select(folder, readonly=True)
        if typ != 'OK':
            return None
        safe = message_id.replace('\\', '\\\\').replace('"', '\\"')
        # NB: uid() passes args verbatim (no charset slot unlike
        # search()); a stray None breaks the command (live incident).
        typ, data = conn.uid('search', 'HEADER', 'Message-ID', f'"{safe}"')
        if typ != 'OK' or not data or not data[0]:
            return []
        return data[0].split()

    def move_matches(self, user, message_id, from_folder, to_folder):
        """Move every message with message_id from from_folder to
        to_folder. Returns moved count, or None when the source folder
        is missing (caller decides: done vs retry)."""
        conn = self._connect(user)
        try:
            uids = self._uids_by_message_id(conn, message_id=message_id,
                                             folder=from_folder)
            if uids is None:
                return None
            if not uids:
                return 0
            conn.select(from_folder)
            try:
                typ, _ = conn.uid('move', b','.join(uids), to_folder)
                if typ == 'OK':
                    return len(uids)
            except Exception:
                pass
            # fallback for servers without MOVE (RFC 6851)
            typ, _ = conn.uid('copy', b','.join(uids), to_folder)
            if typ != 'OK':
                raise RuntimeError(f'copy to {to_folder} failed')
            conn.uid('store', b','.join(uids), '+FLAGS', '(\\Deleted)')
            conn.expunge()
            return len(uids)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def sent_folder(self, user):
        """Prefer the SPECIAL-USE \\Sent folder, fall back to 'Sent'."""
        conn = self._connect(user)
        try:
            typ, data = conn.list('', '*')
            if typ != 'OK':
                return 'Sent'
            for line in data or []:
                if isinstance(line, bytes) and b'\\Sent' in line:
                    return _list_name(line)
            return 'Sent'
        finally:
            try:
                conn.logout()
            except Exception:
                pass


class Poller:
    def __init__(self, config, store, imap=None, smtp_cls=None):
        self.config = config
        self.store = store
        self.imap = imap or ImapWorker(config.dovecot_host,
                                       config.dovecot_port)
        self.smtp_cls = smtp_cls or smtplib.SMTP

    def _submit(self, sender, recipients, mime):
        with self.smtp_cls(self.config.postfix_host,
                           self.config.postfix_port,
                           timeout=60) as smtp:
            smtp.ehlo()
            smtp.sendmail(sender, recipients, bytes(mime))

    def poll_once(self, now=None):
        now = int(now if now is not None else time.time())
        sent, woken = 0, 0
        for row in self.store.due_scheduled(now):
            if self._send_due(row, now):
                sent += 1
        for row in self.store.due_snoozes(now):
            if self._wake_due(row, now):
                woken += 1
        self._file_copies(now)
        return sent, woken

    def _send_due(self, row, now):
        sid = row['id']
        try:
            stored = json.loads(row['recipients'] or '[]')
        except ValueError:
            stored = []
        recipients = parse_recipients(stored, self.config.max_recipients)
        if recipients is None:
            self.store.mark_schedule_failed(sid, 'bad recipients')
            return False
        try:
            self._submit(row['sender'], recipients, row['mime'])
        except Exception as exc:
            age = now - row['created_at']
            if age > GIVE_UP_AFTER or row['attempts'] >= 10:
                self.store.mark_schedule_failed(sid, str(exc))
                LOG.warning('scheduled %d failed permanently: %s', sid, exc)
            else:
                delay = RETRY_DELAYS[min(row['attempts'],
                                         len(RETRY_DELAYS) - 1)]
                self.store.mark_schedule_retry(sid, now + delay, str(exc))
                LOG.warning('scheduled %d retry in %ds: %s', sid, delay, exc)
            return False
        self.store.mark_sent(sid, now)
        LOG.info('scheduled %d sent to %d rcpts', sid, len(recipients))
        return True

    def _file_copies(self, now):
        """File Scheduled-folder copies to Sent. Retried across polls:
        a search miss on a fresh row is likely index lag, not absence."""
        for row in self.store.sent_unfiled(now):
            sid = row['id']
            try:
                parsed = email.message_from_bytes(bytes(row['mime']),
                                                  policy=POLICY)
                message_id = (parsed.get('Message-ID') or '').strip()
                if not message_id:
                    self.store.mark_filed(sid, now)
                    continue
                sent = self.imap.sent_folder(row['user'])
                moved = self.imap.move_matches(
                    row['user'], message_id, self.config.scheduled_folder,
                    sent)
            except Exception as exc:
                LOG.warning('scheduled %d file-to-sent failed: %s', sid,
                            exc)
                continue
            if moved:
                LOG.info('scheduled %d filed %s copy to %s', sid, moved,
                         sent)
                self.store.mark_filed(sid, now)
            elif now - row['created_at'] > NOT_FOUND_GRACE:
                LOG.info('scheduled %d copy gone (user deleted?), done',
                         sid)
                self.store.mark_filed(sid, now)
            else:
                LOG.info('scheduled %d copy not visible yet, retrying', sid)

    def _wake_due(self, row, now):
        sid = row['id']
        try:
            moved = self.imap.move_matches(row['user'], row['message_id'],
                                           self.config.snooze_folder,
                                           row['folder'] or 'INBOX')
            if moved is None or moved == 0:
                # Missing source folder/message: transient (index lag)
                # while the row is fresh, genuine (user moved or
                # deleted it) once past the grace window.
                if now - row['created_at'] > NOT_FOUND_GRACE:
                    LOG.info('snooze %d nothing to wake (moved/deleted)',
                             sid)
                    self.store.mark_snooze_done(sid, now)
                    return True
                LOG.info('snooze %d not visible yet, retrying', sid)
                return False
            LOG.info('snooze %d woke %d message(s) to %s', sid, moved,
                     row['folder'])
            self.store.mark_snooze_done(sid, now)
            return True
        except Exception as exc:
            LOG.warning('snooze %d wake failed, retrying: %s', sid, exc)
            return False


class SchedulerApp:
    def __init__(self, config, store):
        self.config = config
        self.store = store

    def _check_secret(self, headers):
        given = headers.get('x-scheduler-secret', '')
        return bool(given) and hmac.compare_digest(
            given, self.config.api_secret)

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        return status, {'Content-Type': 'application/json'}, body

    def _window(self, value):
        try:
            at = int(value)
        except (TypeError, ValueError):
            return None
        now = int(time.time())
        if at < now + 30 or at > now + self.config.max_future_days * 86400:
            return None
        return at

    def handle(self, method, path, headers, body):
        parsed = urlparse(path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route == '/healthz' and method == 'GET':
            return self._json(200, {'ok': True})
        if not self._check_secret(headers):
            return self._json(401, {'error': 'bad secret'})
        if route == '/v1/scheduled' and method == 'POST':
            return self._schedule_post(body)
        if route == '/v1/scheduled' and method == 'GET':
            return self._schedule_list(query)
        if route.startswith('/v1/scheduled/') and method == 'DELETE':
            return self._schedule_delete(route, query)
        if route == '/v1/snoozes' and method == 'POST':
            return self._snooze_post(body)
        if route == '/v1/snoozes' and method == 'GET':
            return self._snooze_list(query)
        if route.startswith('/v1/snoozes/') and method == 'DELETE':
            return self._snooze_delete(route, query)
        return self._json(404, {'error': 'not found'})

    def _owner(self, query):
        user = _valid_email((query.get('user') or [''])[0])
        return user

    def _schedule_post(self, body):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        user = _valid_email(data.get('user'))
        sender = _valid_email(data.get('sender'))
        if not user or not sender:
            return self._json(400, {'error': 'bad user/sender'})
        if (self.config.local_domains and
                sender.rsplit('@', 1)[-1] not in self.config.local_domains):
            return self._json(400, {'error': 'sender must be local'})
        recipients = parse_recipients(data.get('recipients'),
                                      self.config.max_recipients)
        if recipients is None:
            return self._json(400, {'error': 'bad recipients'})
        try:
            mime = base64.b64decode(data.get('mime_b64') or '', validate=True)
        except (binascii.Error, ValueError):
            return self._json(400, {'error': 'bad mime'})
        if not mime or len(mime) > self.config.max_mime_bytes:
            return self._json(400, {'error': 'mime missing or too large'})
        try:
            parsed = email.message_from_bytes(mime, policy=POLICY)
            subject = str(parsed.get('Subject') or '')[:500]
        except Exception:
            return self._json(400, {'error': 'mime unparseable'})
        send_at = self._window(data.get('send_at'))
        if send_at is None:
            return self._json(400, {'error': 'bad send_at'})
        sid = self.store.add_scheduled(user, sender, recipients, subject,
                                       mime, send_at)
        return self._json(200, {'id': sid, 'send_at': send_at})

    def _schedule_list(self, query):
        user = self._owner(query)
        if not user:
            return self._json(400, {'error': 'bad user'})
        out = []
        for row in self.store.list_scheduled(user):
            out.append({'id': row['id'], 'sender': row['sender'],
                        'recipients': json.loads(row['recipients'] or '[]'),
                        'subject': row['subject'], 'send_at': row['send_at'],
                        'status': row['status'],
                        'created_at': row['created_at'],
                        'sent_at': row['sent_at'],
                        'error': row['error']})
        return self._json(200, {'scheduled': out})

    def _schedule_delete(self, route, query):
        user = self._owner(query)
        try:
            sid = int(route[len('/v1/scheduled/'):])
        except ValueError:
            return self._json(404, {'error': 'not found'})
        if not user:
            return self._json(400, {'error': 'bad user'})
        if not self.store.cancel_scheduled(sid, user):
            return self._json(404, {'error': 'not found'})
        return self._json(200, {'ok': True})

    def _snooze_post(self, body):
        try:
            data = json.loads(body.decode() or '{}')
        except ValueError:
            return self._json(400, {'error': 'bad json'})
        user = _valid_email(data.get('user'))
        message_id = (data.get('message_id') or '').strip()
        folder = (data.get('folder') or 'INBOX').strip() or 'INBOX'
        if (not user or not message_id or len(message_id) > 500 or
                '\n' in message_id or '\r' in message_id or
                len(folder) > 200 or '\n' in folder or '\r' in folder):
            return self._json(400, {'error': 'bad snooze'})
        wake_at = self._window(data.get('wake_at'))
        if wake_at is None:
            return self._json(400, {'error': 'bad wake_at'})
        sid = self.store.add_snooze(user, message_id, folder, wake_at)
        return self._json(200, {'id': sid, 'wake_at': wake_at})

    def _snooze_list(self, query):
        user = self._owner(query)
        if not user:
            return self._json(400, {'error': 'bad user'})
        return self._json(200, {'snoozes': self.store.list_snoozes(user)})

    def _snooze_delete(self, route, query):
        user = self._owner(query)
        try:
            sid = int(route[len('/v1/snoozes/'):])
        except ValueError:
            return self._json(404, {'error': 'not found'})
        if not user:
            return self._json(400, {'error': 'bad user'})
        if not self.store.cancel_snooze(sid, user):
            return self._json(404, {'error': 'not found'})
        return self._json(200, {'ok': True})


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Scheduler/1.0'

    def _run(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length > 0 else b''
        flat = {k.lower(): v for k, v in self.headers.items()}
        status, headers, out = self.server.app.handle(
            self.command, self.path, flat, body)
        try:
            self.send_response(status)
            self.send_header('Content-Length', str(len(out)))
            for key, val in headers.items():
                if key.lower() != 'content-length':
                    self.send_header(key, val)
            self.end_headers()
            self.wfile.write(out)
        except (ConnectionError, BrokenPipeError):
            pass

    do_GET = _run
    do_POST = _run
    do_DELETE = _run

    def log_message(self, *args):
        pass


def main():
    from .config import Config
    from .db import Store
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s scheduler %(levelname)s'
                        ' %(message)s')
    config = Config()
    store = Store(config.db_path)
    app = SchedulerApp(config, store)
    poller = Poller(config, store)

    def loop():
        while True:
            try:
                sent, woken = poller.poll_once()
                if sent or woken:
                    LOG.info('poll sent=%d woken=%d', sent, woken)
                store.purge(config.retention_days, int(time.time()))
            except Exception:
                LOG.exception('poll cycle failed')
            time.sleep(config.poll_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    server = ThreadingHTTPServer(('0.0.0.0', config.web_port), Handler)
    server.app = app
    server.daemon_threads = True
    LOG.info('scheduler listening on %d', config.web_port)
    server.serve_forever()


if __name__ == '__main__':
    main()
