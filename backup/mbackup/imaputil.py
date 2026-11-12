"""IMAP helpers: mailbox name quoting/encoding, LIST/FETCH parsing, and a
TLS connection pinned to a vetted public address."""
import base64
import imaplib
import ipaddress
import re
import socket
import ssl

KEEP_FLAGS = ('\\Seen', '\\Answered', '\\Flagged', '\\Draft')

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) ?(?P<name>.*)$')
_FLAGS_RE = re.compile(rb'FLAGS \(([^)]*)\)')
_DATE_RE = re.compile(rb'INTERNALDATE "([^"]+)"')


class TargetError(Exception):
    """A problem with the user's backup target, safe to show the user."""


def quote(name):
    """IMAP quoted string for a (modified UTF-7 encoded) mailbox name."""
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _unquote(raw):
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"') and len(raw) >= 2:
        return re.sub(rb'\\(.)', rb'\1', raw[1:-1])
    return raw


def imap_utf7_encode(text):
    """RFC 3501 modified UTF-7 for user-typed folder names."""
    out, buf = [], []

    def flush():
        if buf:
            data = ''.join(buf).encode('utf-16-be')
            out.append('&' + base64.b64encode(data).decode('ascii')
                       .rstrip('=').replace('/', ',') + '-')
            buf.clear()

    for ch in text:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append('&-' if ch == '&' else ch)
        else:
            buf.append(ch)
    flush()
    return ''.join(out)


def parse_list(data):
    """[(name, delimiter, flags_lowercase_set)] from an imaplib LIST reply.
    Names stay in their wire (modified UTF-7) form."""
    out = []
    items = list(data or [])
    for item in items:
        literal = None
        if isinstance(item, tuple):
            line, literal = item[0], item[1]
        else:
            line = item
        if not line:
            continue
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        flags = {f.lower() for f in match.group('flags').decode(
            'ascii', 'replace').split()}
        delim_raw = match.group('delim')
        delim = None if delim_raw == b'NIL' else _unquote(delim_raw).decode(
            'ascii', 'replace')
        if literal is not None:
            name = literal
        else:
            name = _unquote(match.group('name'))
        out.append((name.decode('ascii', 'replace'), delim, flags))
    return out


def parse_fetch(data):
    """(flags_str, internaldate_str, raw_bytes) from a single-message
    FETCH (FLAGS INTERNALDATE BODY.PEEK[]); raw is None when the message
    vanished. Only system flags worth preserving are kept."""
    meta, raw = b'', None
    for item in data or []:
        if isinstance(item, tuple):
            meta += item[0] + b' '
            raw = item[1]
        elif isinstance(item, bytes):
            meta += item + b' '
    if raw is None:
        return None, None, None
    flags_m = _FLAGS_RE.search(meta)
    keep = []
    if flags_m:
        have = {f.lower() for f in flags_m.group(1).decode(
            'ascii', 'replace').split()}
        keep = [f for f in KEEP_FLAGS if f.lower() in have]
    date_m = _DATE_RE.search(meta)
    date = '"' + date_m.group(1).decode('ascii') + '"' if date_m else None
    return ('(' + ' '.join(keep) + ')') if keep else None, date, raw


def target_ip(host, port, allow_private=False):
    """Resolve and vet the target: every address must be public, so a
    webmail user cannot point the daemon at cluster-internal services."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise TargetError(f'cannot resolve {host}')
    ips = []
    for info in infos:
        ip = info[4][0].split('%', 1)[0]
        addr = ipaddress.ip_address(ip)
        if not allow_private and not addr.is_global:
            raise TargetError(f'{host} resolves to a private or reserved'
                              ' address; only public servers are allowed')
        ips.append(ip)
    if not ips:
        raise TargetError(f'cannot resolve {host}')
    return ips[0]


class PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    """IMAP over TLS to an already-vetted IP, with SNI and certificate
    checks against the hostname (no DNS rebinding between check and use)."""

    def __init__(self, host, port, ip, ssl_context, timeout):
        self._pinned_ip = ip
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._pinned_ip, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def tls_context(insecure=False):
    ctx = ssl.create_default_context()
    if insecure:  # TEST ONLY (INSECURE_TLS env)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def login(conn, username, password):
    """LOGIN, or AUTHENTICATE PLAIN for non-ASCII credentials."""
    try:
        (username + password).encode('ascii')
    except UnicodeEncodeError:
        payload = ('\0' + username + '\0' + password).encode('utf-8')
        return conn.authenticate('PLAIN', lambda _: payload)
    return conn.login(username, password)
