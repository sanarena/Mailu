"""imaputil: quoting, modified UTF-7, LIST/FETCH parsing, target vetting."""
import socket
import unittest
from unittest import mock

from tests import fakes  # noqa: F401  (sets sys.path)
from mbackup.imaputil import (TargetError, imap_utf7_encode, parse_fetch,
                              parse_list, quote, target_ip)


class Quoting(unittest.TestCase):
    def test_quote_escapes(self):
        self.assertEqual(quote('Sent Items'), '"Sent Items"')
        self.assertEqual(quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_utf7(self):
        self.assertEqual(imap_utf7_encode('Backup'), 'Backup')
        self.assertEqual(imap_utf7_encode('Büro'), 'B&APw-ro')
        self.assertEqual(imap_utf7_encode('A&B'), 'A&-B')
        self.assertEqual(imap_utf7_encode('日本'), '&ZeVnLA-')


class ListParsing(unittest.TestCase):
    def test_dovecot_lines(self):
        data = [b'(\\HasNoChildren \\Archive) "." Archive',
                b'(\\HasNoChildren \\Junk) "." Junk',
                b'(\\HasNoChildren) "." "Sent Items"',
                b'(\\Noselect) NIL root',
                (b'(\\HasNoChildren) "." {9}', b'Odd"Name'), b'']
        out = parse_list(data)
        self.assertEqual(out[0], ('Archive', '.', {'\\hasnochildren', '\\archive'}))
        self.assertIn('\\junk', out[1][2])
        self.assertEqual(out[2][0], 'Sent Items')
        self.assertEqual(out[3][1], None)
        self.assertEqual(out[4][0], 'Odd"Name')

    def test_root_delimiter(self):
        self.assertEqual(parse_list([b'(\\Noselect) "/" ""'])[0][1], '/')


class FetchParsing(unittest.TestCase):
    def test_flags_date_raw(self):
        data = [(b'1 (UID 7 FLAGS (\\Seen \\Recent \\Flagged $Label1)'
                 b' INTERNALDATE "07-Oct-2026 21:26:16 +0000" BODY[] {5}',
                 b'hello'), b')']
        flags, date, raw = parse_fetch(data)
        self.assertEqual(flags, '(\\Seen \\Flagged)')
        self.assertEqual(date, '"07-Oct-2026 21:26:16 +0000"')
        self.assertEqual(raw, b'hello')

    def test_items_after_literal(self):
        data = [(b'1 (UID 7 BODY[] {2}', b'hi'),
                b' FLAGS (\\Answered) INTERNALDATE "01-Jan-2026 00:00:00 +0000")']
        flags, date, raw = parse_fetch(data)
        self.assertEqual(flags, '(\\Answered)')
        self.assertTrue(date.startswith('"01-Jan-2026'))

    def test_no_flags_and_vanished(self):
        flags, _, raw = parse_fetch([(b'1 (UID 7 FLAGS () BODY[] {1}', b'x'), b')'])
        self.assertIsNone(flags)
        self.assertEqual(raw, b'x')
        self.assertEqual(parse_fetch([None]), (None, None, None))


def _ai(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 993)) for ip in ips]


class TargetVetting(unittest.TestCase):
    def test_public_ok(self):
        with mock.patch('socket.getaddrinfo', return_value=_ai('142.250.1.108')):
            self.assertEqual(target_ip('imap.gmail.com', 993), '142.250.1.108')

    def test_private_rejected(self):
        for ip in ('10.43.0.10', '127.0.0.1', '169.254.1.1', '100.64.0.1',
                   '192.168.1.5'):
            with mock.patch('socket.getaddrinfo', return_value=_ai(ip)):
                with self.assertRaises(TargetError):
                    target_ip('evil.example', 993)

    def test_any_private_address_rejects(self):
        with mock.patch('socket.getaddrinfo',
                        return_value=_ai('142.250.1.108', '10.0.0.1')):
            with self.assertRaises(TargetError):
                target_ip('mixed.example', 993)

    def test_allow_private_for_tests(self):
        with mock.patch('socket.getaddrinfo', return_value=_ai('10.0.0.1')):
            self.assertEqual(target_ip('t', 993, allow_private=True), '10.0.0.1')

    def test_unresolvable(self):
        with mock.patch('socket.getaddrinfo', side_effect=socket.gaierror()):
            with self.assertRaises(TargetError):
                target_ip('nope.invalid', 993)


if __name__ == '__main__':
    unittest.main()
