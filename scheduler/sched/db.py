"""SQLite store for scheduled sends and snoozes. Single-writer, WAL."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled(
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL,
  sender TEXT NOT NULL,
  recipients TEXT NOT NULL DEFAULT '[]',
  subject TEXT NOT NULL DEFAULT '',
  mime BLOB NOT NULL,
  send_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  sent_at INTEGER,
  filed_at INTEGER,
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due
  ON scheduled(status, next_try, send_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_user ON scheduled(user);
CREATE TABLE IF NOT EXISTS snoozes(
  id INTEGER PRIMARY KEY,
  user TEXT NOT NULL,
  message_id TEXT NOT NULL,
  folder TEXT NOT NULL,
  wake_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at INTEGER NOT NULL,
  done_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snoozes_due ON snoozes(status, wake_at);
CREATE INDEX IF NOT EXISTS idx_snoozes_user ON snoozes(user);
"""


def _norm_email(value):
    return (value or '').strip().lower()


class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False,
                                   timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA foreign_keys=ON')
            self._db.executescript(SCHEMA)
            try:
                self._db.execute('ALTER TABLE scheduled'
                                 ' ADD COLUMN filed_at INTEGER')
            except sqlite3.OperationalError:
                pass  # already migrated
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # -- scheduled ---------------------------------------------------
    def add_scheduled(self, user, sender, recipients, subject, mime,
                      send_at):
        now = int(time.time())
        with self._lock:
            cur = self._db.execute(
                'INSERT INTO scheduled(user, sender, recipients, subject,'
                ' mime, send_at, status, next_try, created_at)'
                ' VALUES(?,?,?,?,?,?,?, ?,?)',
                (_norm_email(user), _norm_email(sender),
                 json.dumps(recipients), subject[:500], bytes(mime),
                 int(send_at), 'queued', int(send_at), now))
            self._db.commit()
            return cur.lastrowid

    def list_scheduled(self, user):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                'SELECT id, sender, recipients, subject, send_at, status,'
                ' attempts, created_at, sent_at, error FROM scheduled'
                ' WHERE user=? ORDER BY send_at', (_norm_email(user),))]

    def cancel_scheduled(self, sid, user):
        with self._lock:
            cur = self._db.execute(
                "UPDATE scheduled SET status='cancelled'"
                " WHERE id=? AND user=? AND status='queued'",
                (sid, _norm_email(user)))
            self._db.commit()
            return cur.rowcount > 0

    def due_scheduled(self, now, limit=50):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM scheduled WHERE status='queued'"
                " AND send_at<=? AND next_try<=? ORDER BY send_at LIMIT ?",
                (now, now, limit))]

    def mark_sent(self, sid, now):
        with self._lock:
            self._db.execute(
                "UPDATE scheduled SET status='sent', sent_at=?"
                " WHERE id=? AND status='queued'", (now, sid))
            self._db.commit()

    def update_mime(self, sid, mime):
        """Replace stored MIME (e.g. after Message-ID injection)."""
        with self._lock:
            self._db.execute(
                "UPDATE scheduled SET mime=? WHERE id=?",
                (bytes(mime), sid))
            self._db.commit()

    def mark_schedule_retry(self, sid, next_try, error):
        with self._lock:
            self._db.execute(
                'UPDATE scheduled SET attempts=attempts+1, next_try=?,'
                " error=? WHERE id=? AND status='queued'",
                (next_try, (error or '')[:500], sid))
            self._db.commit()

    def mark_schedule_failed(self, sid, error):
        with self._lock:
            self._db.execute(
                "UPDATE scheduled SET status='failed', error=?"
                " WHERE id=? AND status='queued'",
                ((error or '')[:500], sid))
            self._db.commit()

    def sent_unfiled(self, now, limit=20):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM scheduled WHERE status='sent'"
                " AND filed_at IS NULL AND sent_at>? ORDER BY sent_at"
                " LIMIT ?", (now - 24 * 3600, limit))]

    def mark_filed(self, sid, now):
        with self._lock:
            self._db.execute(
                'UPDATE scheduled SET filed_at=? WHERE id=?', (now, sid))
            self._db.commit()

    # -- snoozes -----------------------------------------------------
    def add_snooze(self, user, message_id, folder, wake_at):
        now = int(time.time())
        with self._lock:
            cur = self._db.execute(
                'INSERT INTO snoozes(user, message_id, folder, wake_at,'
                " status, created_at) VALUES(?,?,?,?,'pending',?)",
                (_norm_email(user), message_id[:500], folder[:200],
                 int(wake_at), now))
            self._db.commit()
            return cur.lastrowid

    def list_snoozes(self, user):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                'SELECT id, message_id, folder, wake_at, status,'
                ' created_at, done_at FROM snoozes WHERE user=?'
                ' ORDER BY wake_at', (_norm_email(user),))]

    def cancel_snooze(self, sid, user):
        with self._lock:
            cur = self._db.execute(
                "UPDATE snoozes SET status='cancelled'"
                " WHERE id=? AND user=? AND status='pending'",
                (sid, _norm_email(user)))
            self._db.commit()
            return cur.rowcount > 0

    def due_snoozes(self, now, limit=100):
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM snoozes WHERE status='pending'"
                " AND wake_at<=? ORDER BY wake_at LIMIT ?", (now, limit))]

    def mark_snooze_done(self, sid, now):
        with self._lock:
            self._db.execute(
                "UPDATE snoozes SET status='done', done_at=?"
                " WHERE id=? AND status='pending'", (now, sid))
            self._db.commit()

    # -- retention ---------------------------------------------------
    def purge(self, retention_days, now):
        cutoff = now - retention_days * 86400
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM scheduled WHERE status IN"
                " ('sent','failed','cancelled') AND created_at<?", (cutoff,))
            sched = cur.rowcount
            cur = self._db.execute(
                "DELETE FROM snoozes WHERE status IN"
                " ('done','cancelled') AND created_at<?", (cutoff,))
            self._db.commit()
            return sched + cur.rowcount
