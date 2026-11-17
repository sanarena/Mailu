"""IMAP search/read + SMTP send for Mailu mailboxes (stdlib only)."""
import copy
import imaplib
import re
import smtplib
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any, Dict, List, Optional

from .config import MailConfig

HEADER_FIELDS = ["From", "To", "Cc", "Date", "Subject", "Message-ID", "In-Reply-To"]


def _decode(value: Any) -> str:
    if value is None:
        return ""
    parts = []
    for chunk, enc in decode_header(str(value)):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", "replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _escape(value: str) -> str:
    """Escape a value for use inside an IMAP quoted-string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build_search_criteria(
    unseen_only: bool = False,
    seen: Optional[bool] = None,
    flagged: Optional[bool] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    text: Optional[str] = None,
    header: Optional[str] = None,
    header_value: Optional[str] = None,
    uids: Optional[List[int]] = None,
    larger: Optional[int] = None,
    smaller: Optional[int] = None,
) -> str:
    """Build an IMAP SEARCH criteria string from friendly filter parameters."""
    crit: List[str] = []
    if unseen_only:
        crit.append("UNSEEN")
    if seen is True:
        crit.append("SEEN")
    elif seen is False:
        crit.append("UNSEEN")
    if flagged is True:
        crit.append("FLAGGED")
    elif flagged is False:
        crit.append("UNFLAGGED")
    if since:
        crit.append(f'SINCE "{_escape(since)}"')
    if before:
        crit.append(f'BEFORE "{_escape(before)}"')
    if from_:
        crit.append(f'FROM "{_escape(from_)}"')
    if to:
        crit.append(f'TO "{_escape(to)}"')
    if cc:
        crit.append(f'CC "{_escape(cc)}"')
    if subject:
        crit.append(f'SUBJECT "{_escape(subject)}"')
    if body:
        crit.append(f'BODY "{_escape(body)}"')
    if text:
        crit.append(f'TEXT "{_escape(text)}"')
    if header and header_value:
        crit.append(f'HEADER "{_escape(header)}" "{_escape(header_value)}"')
    if larger is not None:
        crit.append(f"LARGER {int(larger)}")
    if smaller is not None:
        crit.append(f"SMALLER {int(smaller)}")
    if uids:
        crit.append("UID " + ",".join(str(u) for u in uids))
    return " ".join(crit) if crit else "ALL"


def _refresh_capabilities(conn) -> None:
    """Re-read CAPABILITY after login.

    Behind a proxy (Mailu's nginx front) the greeting lists only the proxy's
    capabilities; the backend's (Dovecot: UIDPLUS, MOVE, ...) appear only
    after login, and imaplib never refreshes ``conn.capabilities`` itself.
    """
    try:
        typ, dat = conn.capability()
    except Exception:
        return
    if typ == "OK" and dat and dat[0]:
        raw = dat[0].decode() if isinstance(dat[0], bytes) else str(dat[0])
        conn.capabilities = tuple(raw.upper().split())


@contextmanager
def connect_imap(cfg: MailConfig):
    if cfg.imap_ssl:
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=cfg.timeout)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=cfg.timeout)
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
        _refresh_capabilities(conn)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _quote_mailbox(mailbox: str) -> str:
    """Quote an IMAP mailbox name (escapes backslash and double-quote)."""
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_overview(uid: int, fetch: List[Any]) -> Dict[str, Any]:
    """Parse one UID FETCH response.

    Real imaplib responses look like
    ``[(b'1 (FLAGS (\\\\Seen) RFC822.SIZE 2048 BODY[HEADER.FIELDS (...) {90}',
    b'From: ...'), b')']`` — FLAGS and RFC822.SIZE live in the *label*,
    the header bytes in the payload.
    """
    flags = ""
    size = 0
    raw_header = b""
    for part in fetch:
        if isinstance(part, tuple) and len(part) == 2:
            label, payload = part[0], part[1]
            if isinstance(label, bytes):
                m = re.search(rb"FLAGS \(([^)]*)\)", label)
                if m:
                    flags = m.group(1).decode("utf-8", "replace")
                m = re.search(rb"RFC822\.SIZE (\d+)", label)
                if m:
                    size = int(m.group(1))
            if isinstance(payload, bytes):
                raw_header += payload
    msg = BytesParser(policy=policy.default).parsebytes(raw_header) if raw_header else None
    return {
        "uid": uid,
        "flags": flags,
        "size": size,
        "from": _decode(msg["From"]) if msg else "",
        "to": _decode(msg["To"]) if msg else "",
        "cc": _decode(msg["Cc"]) if msg else "",
        "date": str(msg["Date"]) if msg and msg["Date"] else "",
        "subject": _decode(msg["Subject"]) if msg else "",
        "message_id": str(msg["Message-ID"]) if msg and msg["Message-ID"] else "",
    }


def search_emails(cfg: MailConfig, mailbox: str = "INBOX", limit: int = 50, **filters) -> List[Dict[str, Any]]:
    criteria = build_search_criteria(**filters)
    with connect_imap(cfg) as conn:
        conn.select(_quote_mailbox(mailbox), readonly=True)
        typ, data = conn.uid("search", None, criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ} {data}")
        uid_list = [int(u) for u in (data[0] or b"").split()] if data and data[0] else []
        uid_list = uid_list[-limit:] if limit else uid_list
        results = []
        for uid in uid_list:
            typ, fetched = conn.uid(
                "fetch", str(uid),
                "(FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (%s)])" % " ".join(HEADER_FIELDS),
            )
            if typ != "OK":
                continue
            results.append(_parse_overview(uid, fetched))
        return results


@dataclass
class MailboxMessage:
    uid: int
    headers: Dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    body_html: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)


def _message_to_dict(uid: int, raw: bytes, include_body: bool, max_bytes: int) -> Dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    headers = {k: _decode(v) for k, v in msg.items()}
    out: Dict[str, Any] = {"uid": uid, "headers": headers, "from": _decode(msg["From"]),
                            "to": _decode(msg["To"]), "cc": _decode(msg["Cc"]),
                            "date": str(msg["Date"]) if msg["Date"] else "",
                            "subject": _decode(msg["Subject"]),
                            "message_id": str(msg["Message-ID"]) if msg["Message-ID"] else ""}
    body_text, body_html, attachments = "", "", []
    if include_body:
        if msg.is_multipart():
            for part in msg.walk():
                if part.is_multipart():
                    continue
                ctype = part.get_content_type()
                disp = part.get_content_disposition()
                if disp == "attachment":
                    payload = part.get_payload(decode=True) or b""
                    attachments.append({"filename": part.get_filename() or "",
                                        "content_type": ctype, "size": len(payload)})
                elif ctype == "text/plain" and not body_text:
                    body_text = part.get_content() or ""
                elif ctype == "text/html" and not body_html:
                    body_html = part.get_content() or ""
        else:
            ctype = msg.get_content_type()
            if msg.get_content_disposition() == "attachment":
                payload = msg.get_payload(decode=True) or b""
                attachments.append({"filename": msg.get_filename() or "",
                                    "content_type": ctype, "size": len(payload)})
            else:
                content = msg.get_content() if hasattr(msg, "get_content") else ""
                if ctype == "text/html":
                    body_html = content or ""
                else:
                    body_text = content or ""
    if max_bytes and max_bytes > 0:
        body_text = body_text[:max_bytes]
        body_html = body_html[:max_bytes]
    out.update({"body_text": body_text, "body_html": body_html, "attachments": attachments})
    return out


def get_email(cfg: MailConfig, mailbox: str, uid: int, mark_seen: bool = False,
              include_body: bool = True, max_bytes: int = 200000) -> Dict[str, Any]:
    with connect_imap(cfg) as conn:
        conn.select(_quote_mailbox(mailbox), readonly=not mark_seen)
        mode = "BODY[]" if mark_seen else "BODY.PEEK[]"
        typ, fetched = conn.uid("fetch", str(uid), f"({mode})")
        if typ != "OK" or not fetched or fetched[0] is None:
            raise LookupError(f"Message UID {uid} not found in {mailbox}")
        raw = fetched[0][1] if isinstance(fetched[0], tuple) else b""
        return _message_to_dict(uid, raw, include_body, max_bytes)


_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s*(?P<name>.*)$')

SENT_FALLBACK_NAMES = ("sent", "sent items", "sent messages", "inbox.sent", "inbox/sent")


def _parse_list(data: List[Any]) -> List[Any]:
    """[(name, lowercase_flags)] from an imaplib LIST reply. Handles quoted
    names ("Sent Items"), unquoted ones (Dovecot answers `"." Sent`) and
    names sent as literals (imaplib gives a (line, name) tuple)."""
    out = []
    for entry in data or []:
        literal = None
        if isinstance(entry, tuple):
            entry, literal = entry[0], entry[1]
        if not entry:
            continue
        text = entry.decode("utf-8", "replace") if isinstance(entry, bytes) else str(entry)
        m = _LIST_RE.match(text.strip())
        if not m:
            continue
        if literal is not None:
            name = literal.decode("utf-8", "replace") if isinstance(literal, bytes) else str(literal)
        else:
            name = m.group("name").strip()
            if len(name) >= 2 and name[0] == name[-1] == '"':
                name = re.sub(r'\\(.)', r'\1', name[1:-1])
        if name:
            out.append((name, {f.lower() for f in m.group("flags").split()}))
    return out


def list_mailboxes(cfg: MailConfig) -> List[str]:
    with connect_imap(cfg) as conn:
        typ, data = conn.list()
        if typ != "OK":
            raise RuntimeError(f"IMAP LIST failed: {typ}")
        return [name for name, _ in _parse_list(data)]


DRAFTS_FALLBACK_NAMES = ("drafts", "draft", "inbox.drafts", "inbox/drafts")


def _find_special(conn, flag: str, fallbacks) -> Optional[str]:
    """A special-use mailbox (e.g. \\Sent, \\Drafts), else a common name."""
    typ, data = conn.list()
    if typ != "OK":
        return None
    boxes = _parse_list(data)
    for name, flags in boxes:
        if flag in flags:
            return name
    by_lower = {name.lower(): name for name, _ in boxes}
    for candidate in fallbacks:
        if candidate in by_lower:
            return by_lower[candidate]
    return None


def find_sent_mailbox(conn) -> Optional[str]:
    """The Sent folder: the \\Sent special-use mailbox, else a common name."""
    return _find_special(conn, "\\sent", SENT_FALLBACK_NAMES)


def find_drafts_mailbox(conn) -> Optional[str]:
    """The Drafts folder: the \\Drafts special-use mailbox, else a common name."""
    return _find_special(conn, "\\drafts", DRAFTS_FALLBACK_NAMES)


def _resp_text(resp) -> bytes:
    return b" ".join(r for r in (resp or []) if isinstance(r, bytes))


def _append(conn, box: str, flags: str, raw: bytes):
    """APPEND, creating the folder once on TRYCREATE. Returns the response."""
    date = imaplib.Time2Internaldate(time.time())
    typ, resp = conn.append(_quote_mailbox(box), flags, date, raw)
    if typ != "OK" and b"TRYCREATE" in _resp_text(resp).upper():
        conn.create(_quote_mailbox(box))
        typ, resp = conn.append(_quote_mailbox(box), flags, date, raw)
    if typ != "OK":
        raise RuntimeError(f"APPEND to {box} refused: "
                           f"{_resp_text(resp).decode('utf-8', 'replace')[:160]}")
    return resp


def save_sent_copy(cfg: MailConfig, raw: bytes) -> str:
    """APPEND a sent message (\\Seen) to the Sent folder; returns its name.
    Creates "Sent" when the server has none (TRYCREATE)."""
    with connect_imap(cfg) as conn:
        box = find_sent_mailbox(conn) or "Sent"
        _append(conn, box, "(\\Seen)", raw)
        return box


def set_flags(cfg: MailConfig, mailbox: str, uid: int, flags: List[str], mode: str = "add") -> bool:
    op = {"add": "+FLAGS", "remove": "-FLAGS", "set": "FLAGS"}.get(mode, "+FLAGS")
    with connect_imap(cfg) as conn:
        conn.select(_quote_mailbox(mailbox))
        typ, _ = conn.uid("store", str(uid), op, f"({' '.join(flags)})")
        return typ == "OK"


def delete_message(cfg: MailConfig, mailbox: str, uid: int, expunge: bool = True) -> bool:
    with connect_imap(cfg) as conn:
        conn.select(_quote_mailbox(mailbox))
        typ, _ = conn.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        if expunge and typ == "OK":
            conn.expunge()
        return typ == "OK"


def move_message(cfg: MailConfig, mailbox: str, uid: int, dest_mailbox: str) -> bool:
    """Move one message, touching nothing else in the source mailbox.

    Prefers UID MOVE (RFC 6851, Dovecot has it). Without MOVE it copies,
    flags \\Deleted and removes only that UID (UID EXPUNGE, UIDPLUS); a bare
    EXPUNGE, which would also purge every other \\Deleted message another
    client left behind, is the last resort for servers with neither.
    Flags such as unread are kept either way.
    """
    with connect_imap(cfg) as conn:
        conn.select(_quote_mailbox(mailbox))
        caps = getattr(conn, "capabilities", ()) or ()
        if "MOVE" in caps:
            typ, _ = conn.uid("move", str(uid), _quote_mailbox(dest_mailbox))
            return typ == "OK"
        typ, _ = conn.uid("copy", str(uid), _quote_mailbox(dest_mailbox))
        if typ != "OK":
            return False
        conn.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        if "UIDPLUS" in caps:
            conn.uid("expunge", str(uid))
        else:
            conn.expunge()
        return True


def append_message(cfg: MailConfig, mailbox: str, raw: bytes, flags: str = "(\\Seen)") -> bool:
    """Append a raw RFC822 message to a mailbox (used by the Gmail copy job)."""
    with connect_imap(cfg) as conn:
        typ, _ = conn.append(_quote_mailbox(mailbox), flags, None, raw)
        return typ == "OK"


def _build_message(
    cfg: MailConfig,
    to: List[str],
    subject: str,
    body_text: str = "",
    body_html: Optional[str] = None,
    from_addr: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    priority: Optional[str] = None,
):
    """The outgoing message, shared by mail_send and mail_draft.
    Returns (msg, sender, to, cc, bcc); Bcc is never a header here."""
    sender = from_addr or cfg.default_from or cfg.smtp_user
    if not sender:
        raise ValueError("No sender: pass from_addr or set MAILU_DEFAULT_FROM/MAILU_SMTP_USER")
    # Lenient coercion: LLM callers often send a bare string address.
    if isinstance(to, str):
        to = [to]
    if isinstance(cc, str):
        cc = [cc]
    if isinstance(bcc, str):
        bcc = [bcc]
    if not to:
        raise ValueError("Recipient list 'to' must not be empty")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if priority == "high":
        msg["X-Priority"] = "1"
        msg["Importance"] = "high"
    elif priority == "low":
        msg["X-Priority"] = "5"
        msg["Importance"] = "low"
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg["Subject"] = subject
    if "Message-ID" not in msg:
        from email.utils import make_msgid
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
        msg["Message-ID"] = make_msgid(domain=domain)
    if body_html:
        msg.set_content(body_text or "")
        msg.add_alternative(body_html, subtype="html")
    else:
        msg.set_content(body_text or "")
    for att in attachments or []:
        data = att.get("content")
        if isinstance(data, str):
            import base64
            data = base64.b64decode(data)
        msg.add_attachment(data, maintype=att.get("maintype", "application"),
                           subtype=att.get("subtype", "octet-stream"),
                           filename=att.get("filename", "attachment"))
    return msg, sender, list(to), list(cc or []), list(bcc or [])


def send_email(
    cfg: MailConfig,
    to: List[str],
    subject: str,
    body_text: str = "",
    body_html: Optional[str] = None,
    from_addr: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    priority: Optional[str] = None,
    save_sent: Optional[bool] = None,
) -> Dict[str, Any]:
    msg, sender, to, cc, bcc = _build_message(
        cfg, to, subject, body_text=body_text, body_html=body_html, from_addr=from_addr,
        cc=cc, bcc=bcc, reply_to=reply_to, in_reply_to=in_reply_to, references=references,
        extra_headers=extra_headers, attachments=attachments, priority=priority)
    recipients = list(to) + list(cc or []) + list(bcc or [])
    implicit_tls = cfg.smtp_port == 465  # smtps: TLS from the first byte
    if implicit_tls:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                                timeout=cfg.timeout)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
    with server as s:
        if cfg.smtp_starttls and not implicit_tls:
            s.starttls()
        if cfg.smtp_user:
            s.login(cfg.smtp_user, cfg.smtp_password)
        s.send_message(msg, from_addr=sender, to_addrs=recipients)
    result: Dict[str, Any] = {"message_id": str(msg["Message-ID"] or ""), "from": sender,
                              "to": ", ".join(recipients)}

    # Keep a copy in Sent, like a mail client does. Never fails the send:
    # the message is already out, so problems are only reported.
    want = cfg.save_sent if save_sent is None else bool(save_sent)
    if not want:
        result["saved_to_sent"] = None
    elif not cfg.imap_user:
        result["saved_to_sent"] = None
        result["sent_copy"] = "skipped: this session has no mailbox login"
    else:
        stored = copy.deepcopy(msg)
        if bcc:  # recipients see no Bcc; the sender's own copy keeps it
            stored["Bcc"] = ", ".join(bcc)
        try:
            result["saved_to_sent"] = save_sent_copy(cfg, stored.as_bytes(policy=policy.SMTP))
        except Exception as exc:
            result["saved_to_sent"] = None
            result["sent_copy_error"] = f"{type(exc).__name__}: {exc}"[:240]
    return result


def save_draft(
    cfg: MailConfig,
    to: List[str],
    subject: str,
    body_text: str = "",
    body_html: Optional[str] = None,
    from_addr: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    priority: Optional[str] = None,
    replace_uid: Optional[int] = None,
) -> Dict[str, Any]:
    """Save an unsent message to the Drafts folder (nothing is sent).

    The draft keeps Bcc (a mail client sends it from there), In-Reply-To and
    References (threading). replace_uid removes that older draft from Drafts
    after the new one is stored, so a draft can be revised in place."""
    if not cfg.imap_user:
        raise ValueError("Saving a draft needs a mailbox login (HTTP Basic auth)")
    msg, sender, to, cc, bcc = _build_message(
        cfg, to, subject, body_text=body_text, body_html=body_html, from_addr=from_addr,
        cc=cc, bcc=bcc, reply_to=reply_to, in_reply_to=in_reply_to, references=references,
        extra_headers=extra_headers, attachments=attachments, priority=priority)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    raw = msg.as_bytes(policy=policy.SMTP)
    with connect_imap(cfg) as conn:
        box = find_drafts_mailbox(conn) or "Drafts"
        resp = _append(conn, box, "(\\Draft \\Seen)", raw)
        m = re.search(rb"APPENDUID \d+ (\d+)", _resp_text(resp))
        result: Dict[str, Any] = {"saved_to": box, "uid": int(m.group(1)) if m else None,
                                  "message_id": str(msg["Message-ID"] or ""), "from": sender,
                                  "to": ", ".join(to + cc), "sent": False}
        if replace_uid:
            conn.select(_quote_mailbox(box))
            typ, _ = conn.uid("store", str(int(replace_uid)), "+FLAGS", "(\\Deleted)")
            if typ == "OK":
                caps = getattr(conn, "capabilities", ()) or ()
                if "UIDPLUS" in caps:
                    conn.uid("expunge", str(int(replace_uid)))
                else:
                    conn.expunge()
            result["replaced_uid"] = int(replace_uid) if typ == "OK" else None
    return result
