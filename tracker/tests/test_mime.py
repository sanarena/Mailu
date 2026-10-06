"""Unit tests for MIME tracking injection (no network)."""
import base64
import quopri
import sys
import unittest
from email import message_from_bytes
from email.message import EmailMessage
from email import policy

sys.path.insert(0, ".")

from tracker.mail_tracker.mime import linkify_text, process_body

POLICY = policy.default.clone(linesep='\r\n')
PIXEL = 'https://track.x.test/o/TOKEN.gif'
HOST = 'track.x.test'


def rewrite(url, idx):
    return f'https://track.x.test/c/TOKEN/{idx}?s=sig'


def split(raw):
    head, _, body = raw.partition(b'\r\n\r\n')
    headers = []
    for line in head.decode('utf-8', 'replace').split('\r\n'):
        if line[:1] in (' ', '\t') and headers:
            # folded continuation: milter delivers values unfolded
            name, value = headers.pop()
            headers.append((name, value + ' ' + line.strip()))
        elif ':' in line:
            name, _, value = line.partition(':')
            headers.append((name.strip(), value.strip()))
    return headers, body


def parse_body(body, headers):
    pseudo = b'MIME-Version: 1.0\r\n'
    for name, value in headers:
        if name.lower() in ('content-type', 'content-transfer-encoding'):
            pseudo += f'{name}: {value}\r\n'.encode()
    pseudo += b'\r\n' + body
    return message_from_bytes(pseudo, policy=POLICY)


class MimeTest(unittest.TestCase):
    def test_html_inject_and_links(self):
        msg = EmailMessage(policy=POLICY)
        msg['From'] = 'a@x.test'
        msg.set_content('<html><body><p>Hi</p>'
                        '<a href="http://e.test/a">a</a> '
                        '<a href=\'https://e.test/b\'>b</a> '
                        '<a href="mailto:x@y.test">m</a> '
                        '<a href="/relative">r</a></body></html>',
                        subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, links, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        self.assertEqual(links, ['http://e.test/a', 'https://e.test/b'])
        # decode before asserting: quoted-printable soft-breaks split literals
        text = parse_body(new, headers).get_content()
        self.assertIn('/o/TOKEN.gif', text)
        self.assertLess(text.index('/o/TOKEN.gif'), text.lower().index('</body>'))
        self.assertIn('/c/TOKEN/0', text)
        self.assertIn('/c/TOKEN/1', text)
        self.assertIn('mailto:x@y.test', text)
        self.assertIn('/relative', text)

    def test_html_no_body_tag_appends(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<p>no body tag</p>', subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        self.assertTrue(new.rstrip().endswith(b'/>') or b'/o/TOKEN.gif' in new)

    def test_base64_html_part(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body><p>' + 'x' * 500 + '</p></body></html>',
                        subtype='html', cte='base64')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        parsed = parse_body(new, headers)
        self.assertIn('/o/TOKEN.gif', parsed.get_content())

    def test_quoted_printable_html_part(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body><p>Grüße</p></body></html>',
                        subtype='html', cte='quoted-printable')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        parsed = parse_body(new, headers)
        self.assertIn('/o/TOKEN.gif', parsed.get_content())

    def test_self_links_untouched(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body><a href="https://track.x.test/c/z/0?s=q">'
                        'p</a></body></html>', subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, links, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        self.assertEqual(links, [])

    def test_link_cap(self):
        links_html = ''.join(
            f'<a href="http://e.test/{i}">x</a>' for i in range(10))
        msg = EmailMessage(policy=POLICY)
        msg.set_content(f'<html><body>{links_html}</body></html>',
                        subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, links, _ = process_body(body, headers, PIXEL, rewrite, HOST,
                                     max_links=3)
        self.assertEqual(len(links), 3)
        self.assertIn(b'http://e.test/9', new)  # beyond cap: untouched

    def test_plain_text_converted(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('Hello <world>\nSecond line')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        # body bytes of the new multipart/alternative (headers travel
        # separately through the milter, so no multipart line is present)
        self.assertIn(b'Hello <world>', new)  # text part preserved
        decoded = quopri.decodestring(new).decode('utf-8', 'replace')
        self.assertIn('Hello &lt;world&gt;', decoded)  # html escaped
        self.assertIn('/o/TOKEN.gif', decoded)

    def test_plain_text_links_linkified(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('see https://e.test/landing for details')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, links, reason = process_body(body, headers, PIXEL, rewrite,
                                          HOST)
        self.assertEqual(reason, 'ok')
        self.assertEqual(links, ['https://e.test/landing'])
        decoded = quopri.decodestring(new).decode('utf-8', 'replace')
        self.assertIn('<a href="https://track.x.test/c/TOKEN/0?s=sig">'
                      'https://e.test/landing</a>', decoded)
        # plain part keeps the readable original
        self.assertIn(b'see https://e.test/landing for details', new)

    def test_linkify_trailing_punct_and_parens(self):
        text = ('Visit https://e.test/a. (see https://e.test/b) '
                'wiki https://e.test/X_(y) end')
        html = linkify_text(text, rewrite, HOST, 500, [])
        self.assertIn('<a href="https://track.x.test/c/TOKEN/0?s=sig">'
                      'https://e.test/a</a>.', html)
        self.assertIn('(see <a href="https://track.x.test/c/TOKEN/1?s=sig">'
                      'https://e.test/b</a>)', html)
        self.assertIn('>https://e.test/X_(y)</a> end', html)

    def test_linkify_skips_self_and_caps(self):
        collected = []
        html = linkify_text('a https://track.x.test/o/x.gif b', rewrite,
                            HOST, 500, collected)
        self.assertNotIn('<a href', html)
        self.assertEqual(collected, [])
        collected = []
        html = linkify_text('https://a.test/1 https://b.test/2', rewrite,
                            HOST, 1, collected)
        self.assertEqual(collected, ['https://a.test/1'])
        # second URL left as plain text once the cap is hit
        rest = html.split('</a> ', 1)[1]
        self.assertEqual(rest, 'https://b.test/2')

    def test_alternative_gets_html(self):
        text = EmailMessage(policy=POLICY)
        text.set_content('plain only')
        msg = EmailMessage(policy=POLICY)
        msg.set_type('multipart/alternative')
        msg.attach(text)
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        self.assertIn(b'/o/TOKEN.gif', new)

    def test_mixed_text_plus_attachment(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('see attached')
        msg.add_attachment(b'%PDF-fake', maintype='application',
                           subtype='pdf', filename='f.pdf')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        self.assertIn(b'see attached', new)
        decoded = quopri.decodestring(new).decode('utf-8', 'replace')
        self.assertIn('/o/TOKEN.gif', decoded)
        # attachment survives as base64 (email lib's choice, byte-identical)
        self.assertEqual(base64.b64decode('JVBERi1mYWtl'), b'%PDF-fake')
        self.assertIn(b'JVBERi1mYWtl', new)

    def test_mixed_attachments_only_untracked(self):
        msg = EmailMessage(policy=POLICY)
        msg.add_attachment(b'data', maintype='application',
                           subtype='octet-stream', filename='f.bin')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertIsNone(new)
        self.assertEqual(reason, 'no-text-part')

    def test_signed_skipped(self):
        headers = [('Content-Type',
                    'multipart/signed; boundary="b"; protocol="x"')]
        body = b'--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n--b--\r\n'
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertIsNone(new)
        self.assertEqual(reason, 'signed-or-encrypted')

    def test_encrypted_skipped(self):
        headers = [('Content-Type', 'multipart/encrypted; boundary="b"')]
        new, _, reason = process_body(b'--b\r\n--b--\r\n', headers, PIXEL,
                                      rewrite, HOST)
        self.assertIsNone(new)
        self.assertEqual(reason, 'signed-or-encrypted')

    def test_already_tracked_header(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body>x</body></html>', subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        headers.append(('X-Tracker-ID', 'old'))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertIsNone(new)
        self.assertEqual(reason, 'already-tracked')

    def test_already_tracked_pixel(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body><img src="https://track.x.test/o/z.gif">'
                        '</body></html>', subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertIsNone(new)
        self.assertEqual(reason, 'already-tracked')

    def test_attached_message_untouched(self):
        inner = EmailMessage(policy=POLICY)
        inner['From'] = 'x@y.test'
        inner.set_content('<html><body>inner</body></html>', subtype='html')
        msg = EmailMessage(policy=POLICY)
        msg.set_content('outer')
        msg.add_attachment(inner.as_bytes(policy=POLICY),
                           maintype='message', subtype='rfc822')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        # pixel appears exactly once (outer html), inner untouched
        self.assertEqual(new.count(b'/o/TOKEN.gif'), 1)

    def test_idempotent(self):
        msg = EmailMessage(policy=POLICY)
        msg.set_content('<html><body>hi</body></html>', subtype='html')
        headers, body = split(msg.as_bytes(policy=POLICY))
        new, _, reason = process_body(body, headers, PIXEL, rewrite, HOST)
        self.assertEqual(reason, 'ok')
        again, _, reason2 = process_body(new, headers, PIXEL, rewrite, HOST)
        self.assertIsNone(again)
        self.assertEqual(reason2, 'already-tracked')


if __name__ == '__main__':
    unittest.main()
