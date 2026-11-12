"""Append-only mailbox backup to a user-supplied IMAP target.

Source: the Mailu mailbox over trusted-net IMAP (dovecot accepts any
password from the pod network, like the scheduler), opened read-only
with BODY.PEEK so nothing changes on the server. Target: the user's own
IMAP account over verified TLS.

Each folder is walked by UID from the last position (reset when
UIDVALIDITY changes). Every message is identified by the SHA-256 of its
bytes, so a message moved between folders (INBOX -> Archive) is copied
once, and deleting mail on the server never deletes the backup. Progress
is committed per message, so an interrupted run resumes cleanly.
"""
import hashlib
import imaplib
import logging
import socket
import ssl

from .imaputil import (PinnedIMAP4_SSL, TargetError, imap_utf7_encode,
                       login, parse_fetch, parse_list, quote, target_ip,
                       tls_context)

LOG = logging.getLogger('backup')
SOURCE_PASSWORD = 'backup-trusted-net'  # ignored by dovecot on trusted nets
NETWORK_ERRORS = (OSError, ssl.SSLError, socket.timeout, imaplib.IMAP4.abort,
                  EOFError)
# APPEND refusals that mean "not now" (quota, throttling, outage): stop
# the run WITHOUT advancing, so nothing is skipped; retry next cycle.
TRANSIENT_MARKERS = (b'OVERQUOTA', b'LIMIT', b'UNAVAILABLE', b'THROTTLED',
                     b'SERVERBUG', b'INUSE', b'TRY AGAIN', b'TEMPORAR')


def _folder_order(item):
    name = item[0].upper()
    return (0 if name == 'INBOX' else 1 if 'SENT' in name else 2, name)


def _logout(conn):
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


class Syncer:
    def __init__(self, config, store, box, source_cls=None,
                 target_factory=None):
        self.config = config
        self.store = store
        self.box = box
        self.source_cls = source_cls or imaplib.IMAP4
        self.target_factory = target_factory or self._real_target

    # -- connections -------------------------------------------------
    def _real_target(self, host, port):
        ip = target_ip(host, port, self.config.allow_private_targets)
        return PinnedIMAP4_SSL(host, port, ip,
                               tls_context(self.config.insecure_tls),
                               self.config.timeout)

    def source_connect(self, user):
        conn = self.source_cls(self.config.dovecot_host,
                               self.config.dovecot_port)
        conn.login(user, SOURCE_PASSWORD)
        return conn

    def target_connect(self, host, port, username, password):
        try:
            conn = self.target_factory(host, port)
        except TargetError:
            raise
        except ssl.SSLCertVerificationError as exc:
            raise TargetError(f'TLS certificate of {host} is not valid'
                              f' ({exc.verify_message})')
        except NETWORK_ERRORS as exc:
            raise TargetError(f'cannot connect to {host}:{port}'
                              f' over TLS ({exc.__class__.__name__})')
        try:
            login(conn, username, password)
        except imaplib.IMAP4.error as exc:
            _logout(conn)
            detail = str(exc).strip("b'\" ")[:200]
            raise TargetError('login rejected by the target server'
                              + (f': {detail}' if detail else ''))
        return conn

    def test_target(self, host, port, username, password):
        conn = None
        try:
            conn = self.target_connect(host, port, username, password)
            delim = self._target_delim(conn)
            return True, f'Connected and signed in (folder separator "{delim}")'
        except TargetError as exc:
            return False, str(exc)
        except NETWORK_ERRORS as exc:
            return False, f'connection problem ({exc.__class__.__name__})'
        finally:
            _logout(conn)

    # -- helpers -----------------------------------------------------
    @staticmethod
    def _target_delim(conn):
        typ, data = conn.list('""', '""')
        if typ == 'OK':
            for _, delim, _ in parse_list(data):
                if delim:
                    return delim
        return '/'

    @staticmethod
    def target_name(prefix, folder, src_delim, tgt_delim):
        """Prefix levels are split on '/', source levels on the source
        delimiter. A target delimiter INSIDE one level (the dots of an
        email address on a '.'-delimited server) becomes '_' so it does
        not create extra levels."""
        parts = [imap_utf7_encode(p.strip()) for p in prefix.split('/')
                 if p.strip()]
        parts += [p for p in folder.split(src_delim or '.') if p]
        return tgt_delim.join(p.replace(tgt_delim, '_') for p in parts)

    @staticmethod
    def _ensure_folder(conn, name, delim, cache):
        if name in cache:
            return
        parts = name.split(delim)
        for i in range(1, len(parts) + 1):
            path = delim.join(parts[:i])
            if path in cache:
                continue
            typ, data = conn.create(quote(path))
            if typ != 'OK':
                typ2, data2 = conn.list('""', quote(path))
                exists = typ2 == 'OK' and any(data2 or [])
                if not exists and i == len(parts):
                    detail = (data[0] or b'').decode('utf-8', 'replace')
                    raise TargetError(f'cannot create folder {path} on the'
                                      f' target: {detail[:200]}')
            cache.add(path)

    def _append(self, conn, name, delim, created, flag_str, date, raw):
        """True when stored. False = this message was refused for good
        (skip it). Raises TargetError for refusals that mean "not now"."""
        for attempt in (1, 2):
            typ, resp = conn.append(quote(name), flag_str, date, raw)
            if typ == 'OK':
                return True
            text = b' '.join(r for r in (resp or []) if isinstance(r, bytes))
            upper = text.upper()
            if b'TRYCREATE' in upper and attempt == 1:
                created.discard(name)
                self._ensure_folder(conn, name, delim, created)
                continue
            if any(m in upper for m in TRANSIENT_MARKERS):
                raise TargetError('the target refused new messages for now ('
                                  + text.decode('utf-8', 'replace')[:160]
                                  + '); the backup resumes on the next run')
            return False
        return False

    # -- the run -----------------------------------------------------
    def run_user(self, user):
        """Back up one user. Returns a summary dict; progress and status
        are persisted either way."""
        result = {'ok': False, 'error': '', 'copied': 0, 'skipped': 0,
                  'backlog': 0}
        target = self.store.get_target(user)
        if not target or not target['enabled']:
            result['error'] = 'backup is not enabled'
            return result
        password = self.box.open(target['password_enc'])
        if password is None:
            result['error'] = ('stored password cannot be read;'
                               ' enter the password again and save')
            self.store.record_run(user, False, result['error'], 0, 0, 0)
            return result
        src = dst = None
        try:
            src = self.source_connect(user)
            dst = self.target_connect(target['host'], target['port'],
                                      target['username'], password)
            self._copy(user, target, src, dst, result)
            result['ok'] = True
        except TargetError as exc:
            result['error'] = str(exc)
        except NETWORK_ERRORS as exc:
            result['error'] = (f'connection problem during backup'
                               f' ({exc.__class__.__name__});'
                               ' it resumes on the next run')
        except imaplib.IMAP4.error as exc:
            result['error'] = f'IMAP error: {str(exc)[:200]}'
        finally:
            _logout(src)
            _logout(dst)
        if result['ok'] and result['skipped']:
            result['error'] = (f"{result['skipped']} message(s) could not be"
                               ' copied (too large or refused by the target)')
        self.store.record_run(user, result['ok'], result['error'],
                              result['copied'], result['skipped'],
                              result['backlog'])
        LOG.info('backup user=%s ok=%s copied=%d skipped=%d backlog=%d%s',
                 user, result['ok'], result['copied'], result['skipped'],
                 result['backlog'],
                 f" error={result['error']}" if result['error'] else '')
        return result

    def _copy(self, user, target, src, dst, result):
        cfg = self.config
        typ, data = src.list()
        if typ != 'OK':
            raise imaplib.IMAP4.error('source LIST failed')
        folders = sorted(parse_list(data), key=_folder_order)
        tgt_delim = self._target_delim(dst)
        created = set()
        sent_bytes = 0
        for name, src_delim, flags in folders:
            if '\\noselect' in flags or '\\nonexistent' in flags:
                continue
            if not target['include_junk'] and (
                    '\\junk' in flags or name.lower() == 'junk'):
                continue
            src.response('UIDVALIDITY')  # drop any stale value
            typ, _ = src.select(quote(name), readonly=True)
            if typ != 'OK':
                continue
            typ, uv = src.response('UIDVALIDITY')
            uidvalidity = int(uv[-1]) if uv and uv[-1] else 0
            old_uv, last_uid = self.store.folder_state(user, name)
            if old_uv != uidvalidity:
                last_uid = 0
            typ, data = src.uid('SEARCH', None, f'UID {last_uid + 1}:*')
            uids = sorted({int(u) for u in (data[0] or b'').split()
                           if int(u) > last_uid}) if typ == 'OK' else []
            tgt_name = None
            for index, uid in enumerate(uids):
                if (result['copied'] >= cfg.max_messages_per_run or
                        sent_bytes >= cfg.max_bytes_per_run):
                    result['backlog'] += len(uids) - index
                    return
                typ, data = src.uid('FETCH', str(uid),
                                    '(FLAGS INTERNALDATE BODY.PEEK[])')
                flag_str, date, raw = parse_fetch(data if typ == 'OK' else [])
                if raw is not None:
                    if len(raw) > cfg.max_message_bytes:
                        result['skipped'] += 1
                    else:
                        digest = hashlib.sha256(raw).hexdigest()
                        if not self.store.is_copied(user, digest):
                            if tgt_name is None:
                                tgt_name = self.target_name(
                                    target['prefix'], name, src_delim,
                                    tgt_delim)
                                self._ensure_folder(dst, tgt_name, tgt_delim,
                                                    created)
                            if self._append(dst, tgt_name, tgt_delim, created,
                                            flag_str, date, raw):
                                self.store.mark_copied(user, digest)
                                result['copied'] += 1
                                sent_bytes += len(raw)
                            else:
                                result['skipped'] += 1
                self.store.set_folder_state(user, name, uidvalidity, uid)
