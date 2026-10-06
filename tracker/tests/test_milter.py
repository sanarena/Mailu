"""Milter protocol tests: scripted sessions plus a live socket round-trip."""
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, ".")

from tracker.mail_tracker.milter import MilterServer, MilterSession


def pkt(cmd, data=b''):
    if isinstance(data, str):
        data = data.encode()
    return struct.pack('>I', 1 + len(data)) + cmd + data


def cstr(*parts):
    return b'\x00'.join(p.encode() for p in parts) + b'\x00'


class FakeProcessor:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def process(self, sender, recipients, headers, body, macros):
        self.calls.append((sender, recipients, headers, body, macros))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def full_conversation(session, sender='<a@x.test>', rcpts=('<b@y.test>',),
                      headers=(('Subject', 'Hi'),),
                      body=b'hello\r\n', macros=None):
    replies = []
    replies.append(session.handle(b'O', struct.pack('>III', 6, 0xFFFF, 0)))
    if macros:
        replies.append(session.handle(b'D', b'M' + macros))
    replies.append(session.handle(b'M', cstr(sender)))
    for rcpt in rcpts:
        replies.append(session.handle(b'R', cstr(rcpt)))
    for name, value in headers:
        replies.append(session.handle(b'L', cstr(name, value)))
    replies.append(session.handle(b'N', b''))
    replies.append(session.handle(b'B', body))
    replies.append(session.handle(b'E', b''))
    return replies


class SessionTest(unittest.TestCase):
    def test_optneg(self):
        session = MilterSession(FakeProcessor(), 1000)
        reply = session.handle(b'O', struct.pack('>III', 6, 0, 0))
        length, = struct.unpack('>I', reply[:4])
        self.assertEqual(reply[4:5], b'O')
        version, actions, proto = struct.unpack('>III', reply[5:17])
        self.assertEqual((version, actions, proto), (6, 0x03, 0x03))
        self.assertEqual(length, 1 + 12)

    def test_optneg_old_version(self):
        session = MilterSession(FakeProcessor(), 1000)
        reply = session.handle(b'O', struct.pack('>III', 2, 0, 0))
        version = struct.unpack('>I', reply[5:9])[0]
        self.assertEqual(version, 2)

    def test_accept_unmodified(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        replies = full_conversation(MilterSession(proc, 1000))
        # replies[0] is the OPTNEG answer; the rest pre-EOM must continue
        self.assertEqual(replies[0][4:5], b'O')
        self.assertTrue(all(r[4:5] == b'c' for r in replies[1:-1]))
        self.assertEqual(replies[-1][4:5], b'a')
        sender, rcpts, headers, body, macros = proc.calls[0]
        self.assertEqual(sender, 'a@x.test')
        self.assertEqual(rcpts, ['b@y.test'])
        self.assertEqual(headers, [('Subject', 'Hi')])
        self.assertEqual(body, b'hello\r\n')

    def test_modify_replbody_and_header(self):
        proc = FakeProcessor(result=(b'new-body\r\n', 'TOK', 'ok'))
        replies = full_conversation(MilterSession(proc, 1000))
        eom = replies[-1]
        # reply = replbody packet + addheader packet + accept packet
        first_len, = struct.unpack('>I', eom[:4])
        self.assertEqual(eom[4:5], b'b')
        self.assertEqual(eom[5:5 + first_len - 1], b'new-body\r\n')
        rest = eom[4 + first_len:]
        second_len, = struct.unpack('>I', rest[:4])
        self.assertEqual(rest[4:5], b'h')
        self.assertEqual(rest[5:5 + second_len - 1], b'X-Tracker-ID\x00TOK')
        rest = rest[4 + second_len:]
        self.assertEqual(rest, pkt(b'a'))

    def test_macros_reach_processor(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        macros = cstr('{auth_authen}', 'a@x.test', 'i', 'queue1')
        full_conversation(MilterSession(proc, 1000), macros=macros)
        self.assertEqual(proc.calls[0][4],
                         {'{auth_authen}': 'a@x.test', 'i': 'queue1'})

    def test_oversize_accepts_without_processing(self):
        proc = FakeProcessor(result=(b'x', 'T', 'ok'))
        session = MilterSession(proc, 10)
        full_conversation(session, body=b'z' * 100)
        self.assertEqual(proc.calls, [])

    def test_abort_resets(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        session = MilterSession(proc, 1000)
        session.handle(b'M', cstr('<a@x.test>'))
        session.handle(b'R', cstr('<b@y.test>'))
        self.assertEqual(session.handle(b'A', b''), pkt(b'c'))
        self.assertEqual(proc.calls, [])
        # next message on the same connection works
        full_conversation(session)
        self.assertEqual(len(proc.calls), 1)

    def test_quit_closes(self):
        session = MilterSession(FakeProcessor(), 1000)
        self.assertIsNone(session.handle(b'Q', b''))

    def test_unknown_commands_continue(self):
        session = MilterSession(FakeProcessor(), 1000)
        for cmd in (b'C', b'H', b'T', b'U', b'Z'):
            self.assertEqual(session.handle(cmd, b'junk'), pkt(b'c'))

    def test_processor_exception_fails_open(self):
        proc = FakeProcessor(result=RuntimeError('boom'))
        replies = full_conversation(MilterSession(proc, 1000))
        self.assertEqual(replies[-1][4:5], b'a')

    def test_malformed_packets_fail_open(self):
        session = MilterSession(FakeProcessor(), 1000)
        self.assertEqual(session.handle(b'O', b'short')[4:5], b'O')
        self.assertEqual(session.handle(b'D', b''), pkt(b'c'))
        self.assertEqual(session.handle(b'M', b''), pkt(b'c'))
        self.assertEqual(session.handle(b'E', b'')[4:5], b'a')

    def test_macros_do_not_leak_across_messages(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        session = MilterSession(proc, 1000)
        full_conversation(session, macros=cstr('{auth_authen}', 'a@x.test'))
        full_conversation(session)
        self.assertEqual(proc.calls[0][4], {'{auth_authen}': 'a@x.test'})
        self.assertEqual(proc.calls[1][4], {})

    def test_header_and_rcpt_caps(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        session = MilterSession(proc, 10 ** 9)
        session.handle(b'O', struct.pack('>III', 6, 0, 0))
        session.handle(b'M', cstr('<a@x.test>'))
        for i in range(510):
            session.handle(b'L', cstr(f'X-N{i}', 'v' * 20000))
        for i in range(5100):
            session.handle(b'R', cstr(f'<r{i}@y.test>'))
        session.handle(b'N', b'')
        session.handle(b'B', b'x')
        self.assertEqual(session.handle(b'E', b'')[4:5], b'a')
        _, rcpts, headers, _, _ = proc.calls[0]
        self.assertEqual(len(headers), 500)
        self.assertLessEqual(len(headers[0][1]), 16384)
        self.assertEqual(len(rcpts), 5000)

    def test_two_messages_one_connection(self):
        proc = FakeProcessor(result=(None, None, 'skip'))
        session = MilterSession(proc, 1000)
        full_conversation(session, rcpts=('<one@y.test>',))
        full_conversation(session, rcpts=('<two@y.test>',))
        self.assertEqual([c[1] for c in proc.calls],
                         [['one@y.test'], ['two@y.test']])


class LiveSocketTest(unittest.TestCase):
    def test_round_trip(self):
        proc = FakeProcessor(result=(b'new\r\n', 'TOK', 'ok'))
        server = MilterServer(('127.0.0.1', 0), proc, 100000)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            sock = socket.create_connection(server.server_address, timeout=10)
            sock.sendall(pkt(b'O', struct.pack('>III', 6, 0, 0)))

            def read_reply():
                raw = sock.recv(4)
                (length,) = struct.unpack('>I', raw)
                data = b''
                while len(data) < length:
                    chunk = sock.recv(length - len(data))
                    if not chunk:
                        raise AssertionError('eof')
                    data += chunk
                return data

            self.assertEqual(read_reply()[0:1], b'O')
            sock.sendall(pkt(b'M', cstr('<a@x.test>')))
            self.assertEqual(read_reply(), b'c')
            sock.sendall(pkt(b'R', cstr('<b@y.test>')))
            self.assertEqual(read_reply(), b'c')
            sock.sendall(pkt(b'L', cstr('Subject', 'Hi')))
            self.assertEqual(read_reply(), b'c')
            sock.sendall(pkt(b'N', b''))
            self.assertEqual(read_reply(), b'c')
            sock.sendall(pkt(b'B', b'hello\r\n'))
            self.assertEqual(read_reply(), b'c')
            sock.sendall(pkt(b'E', b''))
            eom = read_reply()
            self.assertEqual(eom[0:1], b'b')  # replbody first
            rest = b''
            while len(rest) < 4:
                rest += sock.recv(4 - len(rest))
            (length,) = struct.unpack('>I', rest[:4])
            while len(rest) < 4 + length:
                rest += sock.recv(4 + length - len(rest))
            self.assertEqual(rest[4:5], b'h')
            sock.sendall(pkt(b'Q', b''))
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        self.assertEqual(len(proc.calls), 1)


if __name__ == '__main__':
    unittest.main()
