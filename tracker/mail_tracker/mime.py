"""Tracking injection into MIME message bodies.

Operates on the message BODY only (headers arrive separately via the
milter HEADER events and are passed in for content-type discovery).
Never touches signed/encrypted content or attached sub-messages.

Public entry point: process_body() -> (new_body|None, link_urls, reason)
"""
import html as _html
import re
from email import message_from_bytes
from email.message import EmailMessage
from email import policy as _policy

POLICY = _policy.default.clone(linesep='\r\n')

PIXEL_TEMPLATE = ('<img src="{url}" width="1" height="1" border="0" alt=""'
                  ' style="width:1px;height:1px;border:0;" />')

_HREF_RE = re.compile(
    r'''href\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))''', re.IGNORECASE)
_BARE_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_BODY_CLOSE_RE = re.compile(r'</body\s*>', re.IGNORECASE)
_HTML_CLOSE_RE = re.compile(r'</html\s*>', re.IGNORECASE)

_SKIP_TOP_TYPES = {'multipart/signed', 'multipart/encrypted'}


def _headers_dict(headers):
    out = {}
    for name, value in headers:
        out.setdefault(name.strip().lower(), []).append(value.strip())
    return out


def top_content_type(headers):
    vals = _headers_dict(headers).get('content-type', ['text/plain'])
    return vals[0].split(';')[0].strip().lower() or 'text/plain'


def top_header(headers, name):
    vals = _headers_dict(headers).get(name.strip().lower(), [])
    return vals[0] if vals else ''


def rewrite_links(html, rewrite, self_host, max_links, collected):
    """Rewrite http(s) links via rewrite(url, idx)->new_url. Mutates
    collected with original URLs. Returns new html."""

    def sub(match):
        url = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else match.group(4))
        low = url.lower()
        if not low.startswith(('http://', 'https://')):
            return match.group(0)
        if self_host and self_host.lower() in low.split('/')[2:3]:
            return match.group(0)
        if len(collected) >= max_links:
            return match.group(0)
        idx = len(collected)
        collected.append(url)
        new_url = rewrite(url, idx)
        quote = '"' if match.group(2) is not None else (
            "'" if match.group(3) is not None else '')
        return f'href={quote}{new_url}{quote}'

    return _HREF_RE.sub(sub, html)


def inject_pixel(html, pixel_html):
    match = None
    for pattern in (_BODY_CLOSE_RE, _HTML_CLOSE_RE):
        found = list(pattern.finditer(html))
        if found:
            match = found[-1]
            break
    if match is None:
        return html + pixel_html
    return html[:match.start()] + pixel_html + html[match.start():]


def _strip_trailing_punct(url):
    """Split off trailing punctuation that is not part of the URL.
    Returns (url, tail). A closing bracket only splits when unbalanced
    so wikipedia-style .../X_(y) links survive whole."""
    tail = ''
    while url:
        ch = url[-1]
        if ch in '.,;:!?\'"':
            tail = ch + tail
            url = url[:-1]
        elif ch == ')' and url.count('(') < url.count(')'):
            tail = ')' + tail
            url = url[:-1]
        elif ch == ']' and url.count('[') < url.count(']'):
            tail = ']' + tail
            url = url[:-1]
        else:
            break
    return url, tail


def linkify_text(text, rewrite, self_host, max_links, collected):
    """Escape text to HTML and linkify bare http(s) URLs via
    rewrite(url, idx)->tracked_url. Same skip rules as rewrite_links
    (self-host, cap); unlinked URLs stay as plain escaped text."""
    out = []
    pos = 0
    for match in _BARE_URL_RE.finditer(text):
        url, tail = _strip_trailing_punct(match.group(0))
        out.append(_html.escape(text[pos:match.start()]))
        pos = match.end()
        low = url.lower()
        skip = (not url or len(collected) >= max_links or
                (self_host and self_host.lower() in low.split('/')[2:3]))
        if skip:
            out.append(_html.escape(match.group(0)))
            continue
        idx = len(collected)
        collected.append(url)
        out.append(f'<a href="{_html.escape(rewrite(url, idx), quote=True)}">'
                   f'{_html.escape(url)}</a>{_html.escape(tail)}')
    out.append(_html.escape(text[pos:]))
    return ''.join(out).replace('\n', '<br />\n')


def text_to_html(text, pixel_html, rewrite=None, self_host='',
                 max_links=500, collected=None):
    """Plain-text to HTML alternative. With rewrite+collected, bare URLs
    are linkified to tracked links; the plain part keeps originals."""
    if rewrite is not None and collected is not None:
        body = linkify_text(text, rewrite, self_host, max_links, collected)
    else:
        body = _html.escape(text).replace('\n', '<br />\n')
    return (f'<html><body><div style="white-space:pre-wrap;'
            f'font-family:sans-serif;">{body}</div>'
            f'{pixel_html}</body></html>')


def _part_text(part):
    try:
        return part.get_content()
    except Exception:
        return None


def _set_html(part, html):
    cte = (part.get('Content-Transfer-Encoding') or '').lower()
    charset = part.get_content_charset() or 'utf-8'
    kwargs = {'subtype': 'html', 'charset': charset}
    if cte in ('base64', 'quoted-printable', '7bit', '8bit'):
        kwargs['cte'] = cte
    try:
        part.set_content(html, **kwargs)
        return True
    except Exception:
        return False


def _contains_pixel(html, marker):
    return marker in html


def _process_html_parts(msg, pixel_html, rewrite, self_host, max_links,
                        collected, marker, in_protected=False):
    """Walk parts (never into message/rfc822 or signed/encrypted);
    returns count of modified html parts, or -1 if already tracked."""
    ctype = msg.get_content_type().lower()
    if ctype in _SKIP_TOP_TYPES or ctype == 'message/rfc822':
        return 0
    if ctype == 'text/html':
        text = _part_text(msg)
        if text is None:
            return 0
        if _contains_pixel(text, marker):
            return -1
        new = rewrite_links(text, rewrite, self_host, max_links, collected)
        new = inject_pixel(new, pixel_html)
        return 1 if _set_html(msg, new) else 0
    if not msg.is_multipart():
        return 0
    modified = 0
    for part in msg.iter_parts():
        if part.get_content_type().lower() in _SKIP_TOP_TYPES:
            continue
        if part.get_content_type().lower() == 'message/rfc822':
            continue
        result = _process_html_parts(part, pixel_html, rewrite, self_host,
                                     max_links, collected, marker)
        if result == -1:
            return -1
        modified += result
    return modified


def _find_direct_text_plain(msg):
    if not msg.is_multipart():
        return None
    for i, part in enumerate(msg.get_payload()):
        if part.get_content_type().lower() == 'text/plain':
            return i, part
    return None


def process_body(body, headers, pixel_url, rewrite_link, self_host,
                 max_links=500):
    """Inject tracking into a MIME body.

    headers: list of (name, value) from milter HEADER events.
    rewrite_link(url, idx) -> replacement url for tracked links.
    Returns (new_body_bytes|None, [original link urls], reason).
    None body means "send unmodified" (reason explains why).
    """
    top_ctype = top_content_type(headers)
    if top_ctype in _SKIP_TOP_TYPES:
        return None, [], 'signed-or-encrypted'
    if top_header(headers, 'x-tracker-id'):
        return None, [], 'already-tracked'

    pseudo = b'MIME-Version: 1.0\r\n'
    for name in ('content-type', 'content-transfer-encoding',
                 'content-disposition'):
        val = top_header(headers, name)
        if val:
            pseudo += f'{name}: {val}\r\n'.encode('utf-8', 'replace')
    pseudo += b'\r\n' + (body or b'')
    try:
        msg = message_from_bytes(pseudo, policy=POLICY)
    except Exception:
        return None, [], 'unparseable'

    marker = f'{self_host}/o/' if self_host else '/o/'
    pixel_html = PIXEL_TEMPLATE.format(url=pixel_url)
    collected = []
    try:
        modified = _process_html_parts(msg, pixel_html, rewrite_link,
                                       self_host, max_links, collected,
                                       marker)
    except Exception:
        return None, [], 'processing-error'
    if modified == -1:
        return None, [], 'already-tracked'
    if modified > 0:
        return _split_body(msg), collected, 'ok'

    # No html part: try to add one safely.
    if top_ctype == 'multipart/alternative':
        found = _find_direct_text_plain(msg)
        if found:
            _, text_part = found
            text = _part_text(text_part)
            if text is not None:
                html_part = EmailMessage(policy=POLICY)
                html_part.set_content(
                    text_to_html(text, pixel_html, rewrite_link,
                                 self_host, max_links, collected),
                    subtype='html', charset='utf-8')
                msg.attach(html_part)
                return _split_body(msg), collected, 'ok'
        return None, [], 'no-text-part'
    if top_ctype == 'text/plain':
        text = _part_text(msg)
        if text is None:
            return None, [], 'unparseable'
        new_msg = EmailMessage(policy=POLICY)
        charset = msg.get_content_charset() or 'utf-8'
        new_msg.set_content(text, subtype='plain', charset=charset)
        new_msg.add_alternative(
            text_to_html(text, pixel_html, rewrite_link, self_host,
                         max_links, collected),
            subtype='html', charset='utf-8')
        return _split_body(new_msg), collected, 'ok'
    if top_ctype in ('multipart/mixed', 'multipart/related',
                     'multipart/digest', 'multipart/report'):
        found = _find_direct_text_plain(msg)
        if found:
            i, text_part = found
            text = _part_text(text_part)
            if text is not None:
                alt = EmailMessage(policy=POLICY)
                charset = text_part.get_content_charset() or 'utf-8'
                alt.set_content(text, subtype='plain', charset=charset)
                alt.add_alternative(text_to_html(text, pixel_html,
                                                  rewrite_link, self_host,
                                                  max_links, collected),
                                    subtype='html', charset='utf-8')
                payload = msg.get_payload()
                payload[i] = alt
                msg.set_payload(payload)
                return _split_body(msg), collected, 'ok'
        return None, [], 'no-text-part'
    return None, [], 'unsupported-type'


def _split_body(msg):
    raw = msg.as_bytes(policy=POLICY)
    head, _, rest = raw.partition(b'\r\n\r\n')
    if not rest:
        head, _, rest = raw.partition(b'\n\n')
    return rest
