"""Sendmail milter (v6) protocol server, stdlib-only.

Speaks the libmilter wire protocol over TCP so postfix can chain this
filter in smtpd_milters. Protocol handling is fail-open: ANY unexpected
condition answers CONTINUE mid-stream and ACCEPT at end-of-message, so a
tracker bug can never lose or defer mail by itself (a dead tracker still
lets postfix tempfail+retry per milter_default_action).

Message decisions come from a caller-supplied `processor` object:

    processor.process(sender, recipients, headers, body, macros)
        -> (new_body_or_None, token_or_None, reason)

Wire reference (sendmail libmilter):
- packet: u32be length (command + data, excluding the length field),
  1 command byte, then data.
- commands: O optneg, C connect, H helo, M mail, R rcpt, D macro,
  T data, L header, N end-of-headers, B body chunk, E end-of-message,
  A abort, Q quit, U unknown command.
- macro packet: 1 command byte the macros annotate, then NUL-terminated
  name/value pairs.
- D (macro) and A (abort) are FIRE-AND-FORGET: the MTA reads no
  reply for them. Answering them injects stale bytes into the MTA's
  reply stream and shifts every later reply out of phase (live
  incident: EOM actions silently ignored). Q (quit) closes.
- replies used: c continue, a accept, h addheader (name NUL value
  NUL - postfix requires BOTH strings terminated), b replbody (full
  new body), m chgheader (index u32be, name NUL, value NUL; empty
  value deletes the header - per postfix milter8.c).
- EOM action order: all b chunks contiguous first (postfix finalizes
  body replacement at the first non-b request), then m, h, a.
"""
import itertools
import logging
import socket
import socketserver
import struct

LOG = logging.getLogger('tracker.milter')

PROTO_VERSION = 6
ACT_ADDHDRS = 0x01
ACT_CHGBODY = 0x02
ACT_CHGHDRS = 0x10  # SMFIF_CHGHDRS: change/delete headers at EOM
SKIP_CONNECT_HELO = 0x01 | 0x02  # SMFIP_NOCONNECT | SMFIP_NOHELO

MAX_PACKET = 64 * 1024 * 1024


def _split_cstrings(data):
    parts, cur = [], bytearray()
    for byte in data:
        if byte == 0:
            parts.append(bytes(cur).decode('utf-8', 'replace'))
            cur = bytearray()
        else:
            cur.append(byte)
    if cur:
        parts.append(bytes(cur).decode('utf-8', 'replace'))
    return parts


class MilterSession:
    """One SMTP connection's worth of milter state."""

    def __init__(self, processor, max_body):
        self.processor = processor
        self.max_body = max_body
        self.macros = {}
        self.reset_message()

    def reset_message(self):
        self.sender = None
        self.mail_macros = {}
        self.recipients = []
        self.headers = []
        self.body_chunks = []
        self.body_len = 0
        self.oversize = False
        self.aborted = False

    # -- packet helpers ------------------------------------------------
    @staticmethod
    def encode(cmd, data=b''):
        if isinstance(data, str):
            data = data.encode('utf-8')
        return struct.pack('>I', 1 + len(data)) + cmd + data

    # -- command handlers -------------------------------------------
    # Returns the reply packet, b'' for fire-and-forget commands (D, A:
    # the MTA reads no reply - answering desyncs the whole session),
    # or None to close (Q).
    def handle(self, cmd, data):
        try:
            if cmd == b'O':
                return self._optneg(data)
            if cmd == b'D':
                self._macro(data)
                return b''
            if cmd == b'M':
                return self._mail(data)
            if cmd == b'R':
                return self._rcpt(data)
            if cmd == b'L':
                return self._header(data)
            if cmd == b'N':
                return self.encode(b'c')
            if cmd == b'B':
                return self._body(data)
            if cmd == b'E':
                return self._eom()
            if cmd == b'A':
                self.reset_message()
                self.aborted = True
                return b''
            if cmd == b'Q':
                return None
            # C, H, T, U and anything unknown: not interested.
            return self.encode(b'c')
        except Exception:
            LOG.exception('milter handler error on %r; failing open', cmd)
            self.oversize = True  # force plain ACCEPT at EOM
            return self.encode(b'c')

    def _optneg(self, data):
        if len(data) >= 12:
            version, offer_actions, offer_proto = struct.unpack(
                '>III', data[0:12])
        else:
            version, offer_actions, offer_proto = 2, 0, 0
        LOG.info('optneg offer version=%d actions=0x%x protocol=0x%x',
                 version, offer_actions, offer_proto)
        agreed = min(PROTO_VERSION, version)
        payload = struct.pack(
            '>III', agreed,
            ACT_ADDHDRS | ACT_CHGBODY | ACT_CHGHDRS, SKIP_CONNECT_HELO)
        return self.encode(b'O', payload)

    def _macro(self, data):
        # fire-and-forget: caller returns b'' and sends nothing
        if not data:
            return
        which = bytes(data[0:1])
        parts = _split_cstrings(data[1:])
        pairs = {}
        for i in range(0, len(parts) - 1, 2):
            pairs[parts[i]] = parts[i + 1]
        self.macros[which] = pairs

    def _mail(self, data):
        self.reset_message()
        parts = _split_cstrings(data)
        sender = parts[0] if parts else ''
        if sender.startswith('<') and sender.endswith('>'):
            sender = sender[1:-1]
        self.sender = sender
        self.mail_macros = dict(self.macros.get(b'M', {}))
        # macros annotate one command; never leak them into later messages
        self.macros = {}
        return self.encode(b'c')

    def _rcpt(self, data):
        parts = _split_cstrings(data)
        rcpt = parts[0] if parts else ''
        if rcpt.startswith('<') and rcpt.endswith('>'):
            rcpt = rcpt[1:-1]
        # hard cap: the processor skips past max_recipients anyway
        if rcpt and len(self.recipients) < 5000:
            self.recipients.append(rcpt)
        return self.encode(b'c')

    def _header(self, data):
        if len(self.headers) >= 500:
            return self.encode(b'c')
        parts = _split_cstrings(data)
        if len(parts) >= 2:
            self.headers.append((parts[0][:256], parts[1][:16384]))
        elif len(parts) == 1:
            self.headers.append((parts[0][:256], ''))
        return self.encode(b'c')

    def _body(self, data):
        if not self.oversize:
            if self.body_len + len(data) > self.max_body:
                self.oversize = True
                self.body_chunks = []
            else:
                self.body_chunks.append(data)
                self.body_len += len(data)
        return self.encode(b'c')

    def _eom(self):
        try:
            if self.oversize or self.sender is None:
                return self.encode(b'a')
            body = b''.join(self.body_chunks)
            new_body, token, reason, top_headers = self.processor.process(
                self.sender, self.recipients, self.headers, body,
                self.mail_macros)
            if new_body is None or new_body == body:
                LOG.info('eom sender=%s rcpt=%d tracked=no reason=%s',
                         self.sender, len(self.recipients), reason)
                return self.encode(b'a')
            LOG.info('eom sender=%s rcpt=%d tracked=yes token=%s..',
                     self.sender, len(self.recipients),
                     (token or '')[:8])
            out = b''
            packets = []
            if not getattr(self.processor, 'header_only', False):
                out += self.encode(b'b', new_body)
                packets.append('b')
            for name, value in (top_headers or {}).items():
                # chgheader: index u32be + name NUL + value NUL. Empty
                # value deletes (postfix del_header); index 1 = first
                # instance. Header match is case-insensitive.
                payload = (struct.pack('>I', 1) + name.encode('utf-8') +
                           b'\x00' + (value or '').encode('utf-8') +
                           b'\x00')
                out += self.encode(b'm', payload)
                packets.append('m')
            if token:
                # Postfix parses TWO NUL-terminated strings and rejects
                # left-over bytes; the trailing NUL is mandatory, and a
                # malformed action aborts the whole pending edit set.
                out += self.encode(
                    b'h', 'X-Tracker-ID\x00' + token + '\x00')
                packets.append('h')
            out += self.encode(b'a')
            packets.append('a')
            LOG.info('eom reply bytes=%d packets=%s', len(out),
                     '+'.join(packets))
            return out
        except Exception:
            LOG.exception('eom processing failed; accepting unmodified')
            return self.encode(b'a')
        finally:
            self.reset_message()


class MilterHandler(socketserver.BaseRequestHandler):
    _ids = itertools.count(1)

    def handle(self):
        self.request.settimeout(120)
        session = MilterSession(self.server.processor,
                                self.server.max_body)
        debug = bool(getattr(self.server, 'debug', False))
        conn_id = next(MilterHandler._ids) if debug else 0
        if debug:
            LOG.info('conn %d open from %s', conn_id,
                     self.client_address[0]
                     if self.client_address else '?')

        def recv_exact(n):
            buf = bytearray()
            while len(buf) < n:
                chunk = self.request.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError('eof')
                buf += chunk
            return bytes(buf)

        try:
            while True:
                raw_len = recv_exact(4)
                (length,) = struct.unpack('>I', raw_len)
                if length < 1 or length > MAX_PACKET:
                    return
                payload = recv_exact(length)
                if debug:
                    LOG.info('conn %d C->S %r len=%d', conn_id,
                             payload[0:1], length)
                reply = session.handle(payload[0:1], payload[1:])
                if reply is None:
                    if debug:
                        LOG.info('conn %d close (quit)', conn_id)
                    return
                if reply == b'':
                    continue  # fire-and-forget command: no reply
                if debug:
                    cmds = []
                    rest = reply
                    while len(rest) >= 5:
                        (rlen,) = struct.unpack('>I', rest[:4])
                        cmds.append('%r:%d' % (rest[4:5], rlen))
                        rest = rest[4 + rlen:]
                    LOG.info('conn %d S->C %s', conn_id, ' '.join(cmds))
                self.request.sendall(reply)
        except (ConnectionError, socket.timeout):
            if debug:
                LOG.info('conn %d close (eof/timeout)', conn_id)
            return
        except Exception:
            LOG.exception('milter connection error; closing')
            return


class MilterServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, processor, max_body, debug=False):
        self.processor = processor
        self.max_body = max_body
        self.debug = debug
        super().__init__(address, MilterHandler)
