"""Import-from-Gmail copy jobs (stdlib only: urllib + imaplib).

Give a copy job a Gmail search query; every message matching the query is
copied verbatim (raw RFC822) into a Mailu mailbox via IMAP APPEND.

Two Gmail source modes:
  * ``api``  — Gmail REST API with OAuth2 (refresh-token flow). Faithful
    copy via ``messages.get(format=raw)``.
  * ``imap`` — Gmail IMAP (``imap.gmail.com``, app password) searched with
    the same friendly filters as :mod:`mailu_mcp.mail`.

Jobs are idempotent: copied Gmail IDs are recorded in a JSON state file so
re-running a job only copies what is new. ``dry_run`` previews the match
set without copying anything.
"""
import base64
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import mail as mail_mod
from .config import AppConfig, GmailConfig, MailConfig

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Serializes copy jobs in this process: without it, two concurrent runs of
# the same job would both read empty state, both APPEND, and the second
# save would wipe the first run's copied IDs (duplicates on retry).
# Single-replica deployment + this lock = one writer at a time.
_STATE_LOCK = threading.Lock()


class GmailError(RuntimeError):
    pass


def _http(method: str, url: str, body: Optional[dict] = None,
          headers: Optional[Dict[str, str]] = None) -> Any:
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8", "replace")
        except Exception:
            payload = str(exc)
        raise GmailError(f"Gmail HTTP {exc.code}: {payload}")


def ensure_access_token(cfg: GmailConfig) -> str:
    """Return a usable OAuth access token, refreshing it when needed."""
    if cfg.access_token and not (cfg.refresh_token and cfg.client_id):
        return cfg.access_token
    if not (cfg.client_id and cfg.client_secret and cfg.refresh_token):
        raise GmailError("Gmail OAuth not configured: need GMAIL_CLIENT_ID + "
                         "GMAIL_CLIENT_SECRET + GMAIL_REFRESH_TOKEN (or GMAIL_ACCESS_TOKEN)")
    token = _http("POST", OAUTH_TOKEN_URL, {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": cfg.refresh_token,
        "grant_type": "refresh_token",
    })
    access = token.get("access_token", "")
    if not access:
        raise GmailError(f"Token refresh failed: {token}")
    cfg.access_token = access
    return access


def _api(cfg: GmailConfig, path: str, params: Optional[dict] = None) -> Any:
    token = ensure_access_token(cfg)
    url = f"{GMAIL_API}{path}"
    if params:
        # doseq: list values (labelIds) become repeated query params,
        # which is what the Gmail API expects.
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return _http("GET", url, headers={"Authorization": f"Bearer {token}"})


def _payload_headers(payload: dict) -> Dict[str, str]:
    out = {}
    for h in payload.get("headers", []):
        out[h.get("name", "")] = h.get("value", "")
    return out


MAX_SEARCH_RESULTS = 2000


def gmail_search(cfg: GmailConfig, query: str, max_results: int = 50,
                 label_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Run a Gmail search query; return message metadata (id/thread/subject/from/date)."""
    max_results = min(max(int(max_results), 0), MAX_SEARCH_RESULTS)
    params: Dict[str, Any] = {"q": query, "maxResults": min(max(max_results, 1), 500)}
    if label_ids:
        params["labelIds"] = list(label_ids)
    found: List[Dict[str, Any]] = []
    page = None
    while len(found) < max_results:
        if page:
            params["pageToken"] = page
        listing = _api(cfg, "/users/me/messages", params)
        for m in listing.get("messages", []):
            if len(found) >= max_results:
                break
            meta = _api(cfg, f"/users/me/messages/{m['id']}", {"format": "metadata",
                       "metadataHeaders": "From,To,Subject,Date"})
            headers = _payload_headers(meta.get("payload", {}))
            found.append({"id": m["id"], "threadId": m.get("threadId", ""),
                          "labelIds": meta.get("labelIds", []),
                          "from": headers.get("From", ""), "to": headers.get("To", ""),
                          "subject": headers.get("Subject", ""),
                          "date": headers.get("Date", ""),
                          "snippet": meta.get("snippet", "")})
        page = listing.get("nextPageToken")
        if not page:
            break
    return found


def gmail_fetch_raw(cfg: GmailConfig, gmail_id: str) -> bytes:
    """Fetch one Gmail message in raw RFC822 form (base64url) for verbatim copy."""
    full = _api(cfg, f"/users/me/messages/{gmail_id}", {"format": "raw"})
    raw = full.get("raw", "")
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def gmail_mark_read(cfg: GmailConfig, gmail_id: str) -> None:
    req = urllib.request.Request(
        f"{GMAIL_API}/users/me/messages/{gmail_id}/modify",
        data=json.dumps({"removeLabelIds": ["UNREAD"]}).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {ensure_access_token(cfg)}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60):
            pass
    except urllib.error.HTTPError as exc:
        raise GmailError(f"Gmail HTTP {exc.code} on modify")


def default_state_path() -> str:
    return os.getenv("MAILU_IMPORT_STATE",
                     os.path.expanduser("~/.cache/mailu-mcp/import-state.json"))


def load_state(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or default_state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, Any], path: Optional[str] = None) -> str:
    path = path or default_state_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    return path


@dataclass
class CopyJob:
    """A Gmail → Mailu copy job: search `query`, copy matches to `dest_mailbox`.

    `owner` namespaces the state entry (HTTP Basic-auth email): two owners
    running the same `job_name` must never share copied-ID state, since
    Gmail IDs are only unique per Gmail account.
    """
    query: str
    dest_mailbox: str = "INBOX"
    max_messages: int = 100
    dry_run: bool = False
    job_name: str = "default"
    label_ids: Optional[List[str]] = None
    mark_source_read: bool = False
    skip_existing: bool = True
    state_path: Optional[str] = None
    owner: str = ""


def state_key(job_name: str, owner: str = "") -> str:
    # Owner lower-cased: "Alice@x" and "alice@x" are the same mailbox and
    # must share state, otherwise a case variant re-copies everything.
    return f"{owner.lower()}:{job_name}" if owner else job_name


@dataclass
class CopyReport:
    job_name: str
    query: str
    matched: int
    copied: int
    skipped: int
    failed: int
    copied_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"job_name": self.job_name, "query": self.query,
                "matched": self.matched, "copied": self.copied,
                "skipped": self.skipped, "failed": self.failed,
                "copied_ids": self.copied_ids, "errors": self.errors,
                "dry_run": self.dry_run}


def run_copy_job(app: AppConfig, job: CopyJob) -> CopyReport:
    """Execute a copy job: Gmail search → IMAP APPEND each raw message to Mailu."""
    matches = gmail_search(app.gmail, job.query, job.max_messages, job.label_ids)
    report = CopyReport(job_name=job.job_name, query=job.query,
                        matched=len(matches), copied=0, skipped=0, failed=0,
                        dry_run=job.dry_run)
    # Everything from the state read to the state write runs under one lock:
    # a concurrent twin must see this run's copied IDs, never an empty set.
    with _STATE_LOCK:
        state = load_state(job.state_path)
        key = state_key(job.job_name, job.owner)
        done = set(state.get(key, {}).get("copied_ids", []))
        if job.dry_run:
            report.copied_ids = [m["id"] for m in matches if m["id"] not in done]
            report.skipped = len(matches) - len(report.copied_ids)
            return report
        for m in matches:
            gid = m["id"]
            if job.skip_existing and gid in done:
                report.skipped += 1
                continue
            try:
                raw = gmail_fetch_raw(app.gmail, gid)
                if not mail_mod.append_message(app.mail, job.dest_mailbox, raw):
                    raise RuntimeError(f"IMAP APPEND to {job.dest_mailbox} failed")
                # Record the copy BEFORE the optional mark-read: the bytes are
                # safe in Mailu even if flagging the Gmail original fails, and
                # the next run must not copy it a second time.
                done.add(gid)
                report.copied += 1
                report.copied_ids.append(gid)
                if job.mark_source_read:
                    try:
                        gmail_mark_read(app.gmail, gid)
                    except Exception as exc:
                        report.errors.append(f"{gid}: copied, but mark-read failed: {exc}")
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{gid}: {exc}")
        state.setdefault(key, {})["copied_ids"] = sorted(done)
        state[key]["last_query"] = job.query
        save_state(state, job.state_path)
    return report


def job_status(job_name: str = "default", state_path: Optional[str] = None,
               owner: str = "") -> Dict[str, Any]:
    state = load_state(state_path)
    entry = state.get(state_key(job_name, owner), {})
    return {"job_name": job_name, "copied_total": len(entry.get("copied_ids", [])),
            "last_query": entry.get("last_query", ""),
            "copied_ids": entry.get("copied_ids", [])}
