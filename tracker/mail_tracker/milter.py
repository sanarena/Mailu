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
- replies used: c continue, a accept, h addheader (name NUL value),
  b replbody (full new body).
"""
import logging
import socket
import socketserver
import struct

LOG = logging.getLogger('tracker.milter')

PROTO_VERSION = 6
ACT_ADDHDRS = 0x01
ACT_CHGBODY = 0x02
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

    # -- command handlers (return reply packet or None to close) -------
    def handle(self, cmd, data):
        try:
            if cmd == b'O':
                return self._optneg(data)
            if cmd == b'D':
                return self._macro(data)
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
                return self.encode(b'c')
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
            version = struct.unpack('>I', data[0:4])[0]
        else:
            version = 2
        agreed = min(PROTO_VERSION, version)
        payload = struct.pack('>III', agreed, ACT_ADDHDRS | ACT_CHGBODY,
                              SKIP_CONNECT_HELO)
        return self.encode(b'O', payload)

    def _macro(self, data):
        if not data:
            return self.encode(b'c')
        which = bytes(data[0:1])
        parts = _split_cstrings(data[1:])
        pairs = {}
        for i in range(0, len(parts) - 1, 2):
            pairs[parts[i]] = parts[i + 1]
        self.macros[which] = pairs
        return self.encode(b'c')

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
            new_body, token, reason = self.processor.process(
                self.sender, self.recipients, self.headers, body,
                self.mail_macros)
            if new_body is None or new_body == body:
                LOG.info('eom sender=%s rcpt=%d tracked=no reason=%s',
                         self.sender, len(self.recipients), reason)
                return self.encode(b'a')
            LOG.info('eom sender=%s rcpt=%d tracked=yes token=%s..',
                     self.sender, len(self.recipients),
                     (token or '')[:8])
            out = self.encode(b'b', new_body)
            if token:
                out += self.encode(
                    b'h', 'X-Tracker-ID\x00' + token)
            out += self.encode(b'a')
            return out
        except Exception:
            LOG.exception('eom processing failed; accepting unmodified')
            return self.encode(b'a')
        finally:
            self.reset_message()


class MilterHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(120)
        session = MilterSession(self.server.processor,
                                self.server.max_body)

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
                reply = session.handle(payload[0:1], payload[1:])
                if reply is None:
                    return
                self.request.sendall(reply)
        except (ConnectionError, socket.timeout):
            return
        except Exception:
            LOG.exception('milter connection error; closing')
            return


class MilterServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, processor, max_body):
        self.processor = processor
        self.max_body = max_body
        super().__init__(address, MilterHandler)
