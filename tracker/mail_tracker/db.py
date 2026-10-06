"""SQLite storage for tracked messages, recipients, links, events, opt-outs.

Single-writer design: one connection guarded by a lock. The tracker runs
as one process (milter + web threads), so this is correct and simple.
WAL mode keeps readers from blocking the writer.
"""
import os
import sqlite3
import threading
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  message_id TEXT,
  sender TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  sent_at INTEGER NOT NULL,
  tracking INTEGER NOT NULL DEFAULT 1,
  size INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender, id);
CREATE INDEX IF NOT EXISTS idx_messages_token ON messages(token);
CREATE TABLE IF NOT EXISTS recipients(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  email TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recipients_msg ON recipients(message_id);
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  url TEXT NOT NULL,
  UNIQUE(message_id, idx)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  at INTEGER NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  geo_country TEXT NOT NULL DEFAULT '',
  geo_city TEXT NOT NULL DEFAULT '',
  client TEXT NOT NULL DEFAULT '',
  device TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  link_idx INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_msg ON events(message_id, id);
CREATE TABLE IF NOT EXISTS sender_optouts(
  sender TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recipient_optouts(
  owner TEXT NOT NULL,
  recipient TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(owner, recipient)
);
'''


def _norm_email(addr):
    return (addr or '').strip().lower()


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False,
                                   timeout=30.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA busy_timeout=30000')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # -- messages ------------------------------------------------------
    def add_message(self, token, message_id, sender, subject, recipients,
                    links, size):
        """Register a tracked message. Returns the message row id."""
        now = int(time.time())
        with self._lock:
            cur = self._db.execute(
                'INSERT INTO messages(token, message_id, sender, subject,'
                ' sent_at, tracking, size) VALUES(?,?,?,?,?,1,?)',
                (token, message_id, _norm_email(sender), subject or '',
                 now, size))
            mid = cur.lastrowid
            self._db.executemany(
                'INSERT INTO recipients(message_id, email) VALUES(?,?)',
                [(mid, _norm_email(r)) for r in recipients])
            self._db.executemany(
                'INSERT INTO links(message_id, idx, url) VALUES(?,?,?)',
                [(mid, i, u) for i, u in enumerate(links)])
            self._db.commit()
            return mid

    def get_message_by_token(self, token):
        with self._lock:
            return self._db.execute(
                'SELECT * FROM messages WHERE token=?', (token,)).fetchone()

    def get_message(self, mid, sender=None):
        """Fetch one message; when sender is given anyone else gets None
        (no existence leak across users)."""
        with self._lock:
            row = self._db.execute(
                'SELECT * FROM messages WHERE id=?', (mid,)).fetchone()
            if row is None:
                return None
            if sender is not None and row['sender'] != _norm_email(sender):
                return None
            return row

    def list_messages(self, sender=None, limit=50, offset=0):
        with self._lock:
            if sender is None:
                rows = self._db.execute(
                    'SELECT * FROM messages ORDER BY id DESC LIMIT ? OFFSET ?',
                    (limit, offset)).fetchall()
            else:
                rows = self._db.execute(
                    'SELECT * FROM messages WHERE sender=?'
                    ' ORDER BY id DESC LIMIT ? OFFSET ?',
                    (_norm_email(sender), limit, offset)).fetchall()
            return [dict(r) for r in rows]

    def count_messages(self, sender=None):
        with self._lock:
            if sender is None:
                row = self._db.execute(
                    'SELECT COUNT(*) c FROM messages').fetchone()
            else:
                row = self._db.execute(
                    'SELECT COUNT(*) c FROM messages WHERE sender=?',
                    (_norm_email(sender),)).fetchone()
            return row['c']

    def recipients_of(self, mid):
        with self._lock:
            return [r['email'] for r in self._db.execute(
                'SELECT email FROM recipients WHERE message_id=? ORDER BY id',
                (mid,)).fetchall()]

    def set_tracking(self, mid, enabled, sender=None):
        """Kill-switch (or re-arm) for one message. Ownership enforced."""
        with self._lock:
            if sender is not None:
                row = self._db.execute(
                    'SELECT sender FROM messages WHERE id=?', (mid,)).fetchone()
                if row is None or row['sender'] != _norm_email(sender):
                    return False
            cur = self._db.execute(
                'UPDATE messages SET tracking=? WHERE id=?',
                (1 if enabled else 0, mid))
            self._db.commit()
            return cur.rowcount > 0

    # -- links ---------------------------------------------------------
    def get_link(self, mid, idx):
        with self._lock:
            row = self._db.execute(
                'SELECT url FROM links WHERE message_id=? AND idx=?',
                (mid, idx)).fetchone()
            return row['url'] if row else None

    def links_of(self, mid):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                'SELECT idx, url FROM links WHERE message_id=? ORDER BY idx',
                (mid,)).fetchall()]

    # -- events --------------------------------------------------------
    def add_event(self, mid, kind, ip='', country='', city='', client='',
                  device='', user_agent='', link_idx=None):
        with self._lock:
            self._db.execute(
                'INSERT INTO events(message_id, kind, at, ip, geo_country,'
                ' geo_city, client, device, user_agent, link_idx)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                (mid, kind, int(time.time()), ip, country, city, client,
                 device, user_agent, link_idx))
            self._db.commit()

    def events_of(self, mid, sender=None):
        with self._lock:
            if sender is not None:
                row = self._db.execute(
                    'SELECT sender FROM messages WHERE id=?', (mid,)).fetchone()
                if row is None or row['sender'] != _norm_email(sender):
                    return None
            return [dict(r) for r in self._db.execute(
                'SELECT kind, at, ip, geo_country, geo_city, client, device,'
                ' link_idx FROM events WHERE message_id=? ORDER BY id',
                (mid,)).fetchall()]

    def event_counts(self, sender=None):
        """Per-message open/click counts (and last-open) for list views."""
        with self._lock:
            if sender is None:
                q = ('SELECT m.id,'
                     ' SUM(e.kind="open") opens, SUM(e.kind="click") clicks,'
                     ' MAX(CASE WHEN e.kind="open" THEN e.at END) last_open'
                     ' FROM messages m LEFT JOIN events e'
                     ' ON e.message_id=m.id GROUP BY m.id')
                rows = self._db.execute(q).fetchall()
            else:
                q = ('SELECT m.id,'
                     ' SUM(e.kind="open") opens, SUM(e.kind="click") clicks,'
                     ' MAX(CASE WHEN e.kind="open" THEN e.at END) last_open'
                     ' FROM messages m LEFT JOIN events e ON e.message_id=m.id'
                     ' WHERE m.sender=? GROUP BY m.id')
                rows = self._db.execute(q, (_norm_email(sender),)).fetchall()
            return {r['id']: {'opens': r['opens'] or 0,
                              'clicks': r['clicks'] or 0,
                              'last_open': r['last_open']} for r in rows}

    def stats(self, sender=None):
        with self._lock:
            if sender is None:
                m = self._db.execute(
                    'SELECT COUNT(*) c FROM messages').fetchone()
                e = self._db.execute(
                    'SELECT SUM(kind="open") o, SUM(kind="click") c,'
                    ' COUNT(DISTINCT CASE WHEN kind="open"'
                    ' THEN message_id END) opened_msgs FROM events').fetchone()
            else:
                s = _norm_email(sender)
                m = self._db.execute(
                    'SELECT COUNT(*) c FROM messages WHERE sender=?',
                    (s,)).fetchone()
                e = self._db.execute(
                    'SELECT SUM(e.kind="open") o, SUM(e.kind="click") c,'
                    ' COUNT(DISTINCT CASE WHEN e.kind="open"'
                    ' THEN e.message_id END) opened_msgs'
                    ' FROM events e JOIN messages m ON m.id=e.message_id'
                    ' WHERE m.sender=?', (s,)).fetchone()
            return {'sent': m['c'], 'opens': e['o'] or 0,
                    'clicks': e['c'] or 0,
                    'opened_messages': e['opened_msgs'] or 0}

    # -- opt-outs ------------------------------------------------------
    def sender_opted_out(self, sender):
        with self._lock:
            return self._db.execute(
                'SELECT 1 FROM sender_optouts WHERE sender=?',
                (_norm_email(sender),)).fetchone() is not None

    def set_sender_optout(self, sender, opted_out):
        with self._lock:
            if opted_out:
                self._db.execute(
                    'INSERT OR IGNORE INTO sender_optouts(sender, created_at)'
                    ' VALUES(?,?)', (_norm_email(sender), int(time.time())))
            else:
                self._db.execute('DELETE FROM sender_optouts WHERE sender=?',
                                 (_norm_email(sender),))
            self._db.commit()

    def recipient_opted_out(self, owner, recipient):
        with self._lock:
            return self._db.execute(
                'SELECT 1 FROM recipient_optouts WHERE owner=? AND recipient=?',
                (_norm_email(owner), _norm_email(recipient))).fetchone() \
                is not None

    def set_recipient_optout(self, owner, recipient, opted_out):
        with self._lock:
            if opted_out:
                self._db.execute(
                    'INSERT OR IGNORE INTO recipient_optouts(owner, recipient,'
                    ' created_at) VALUES(?,?,?)',
                    (_norm_email(owner), _norm_email(recipient),
                     int(time.time())))
            else:
                self._db.execute(
                    'DELETE FROM recipient_optouts WHERE owner=? AND'
                    ' recipient=?',
                    (_norm_email(owner), _norm_email(recipient)))
            self._db.commit()

    def list_recipient_optouts(self, owner):
        with self._lock:
            return [r['recipient'] for r in self._db.execute(
                'SELECT recipient FROM recipient_optouts WHERE owner=?'
                ' ORDER BY recipient', (_norm_email(owner),)).fetchall()]

    def list_sender_optouts(self):
        with self._lock:
            return [r['sender'] for r in self._db.execute(
                'SELECT sender FROM sender_optouts ORDER BY sender').fetchall()]

    # -- retention -----------------------------------------------------
    def purge_old_events(self, retention_days):
        """Delete events (and link URLs) older than the retention window;
        disable tracking tokens of messages with no recent activity so
        stale pixels stop logging."""
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock:
            self._db.execute('DELETE FROM events WHERE at<?', (cutoff,))
            self._db.execute(
                'DELETE FROM links WHERE message_id IN'
                ' (SELECT id FROM messages WHERE sent_at<?)', (cutoff,))
            self._db.execute(
                'UPDATE messages SET tracking=0 WHERE sent_at<?', (cutoff,))
            self._db.commit()
