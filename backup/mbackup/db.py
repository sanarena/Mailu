"""SQLite store: per-user backup targets and sync progress. WAL, one lock."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets(
  user TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 993,
  username TEXT NOT NULL,
  password_enc TEXT NOT NULL DEFAULT '',
  prefix TEXT NOT NULL,
  include_junk INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_run_at INTEGER,
  last_ok_at INTEGER,
  last_error TEXT NOT NULL DEFAULT '',
  copied_total INTEGER NOT NULL DEFAULT 0,
  skipped_total INTEGER NOT NULL DEFAULT 0,
  backlog INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS folder_state(
  user TEXT NOT NULL,
  folder TEXT NOT NULL,
  uidvalidity INTEGER NOT NULL,
  last_uid INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user, folder)
);
CREATE TABLE IF NOT EXISTS copied(
  user TEXT NOT NULL,
  digest TEXT NOT NULL,
  copied_at INTEGER NOT NULL,
  PRIMARY KEY(user, digest)
);
"""

# Changing any of these points the backup somewhere new: start over.
IDENTITY_FIELDS = ('host', 'port', 'username', 'prefix')


class Store:
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # -- targets -----------------------------------------------------
    def get_target(self, user):
        with self._lock:
            row = self._db.execute(
                'SELECT * FROM targets WHERE user=?', (user,)).fetchone()
        return dict(row) if row else None

    def list_enabled(self):
        with self._lock:
            rows = self._db.execute(
                'SELECT user FROM targets WHERE enabled=1 ORDER BY user'
            ).fetchall()
        return [r['user'] for r in rows]

    def save_target(self, user, fields):
        """Insert or update. `fields` holds validated columns; a missing
        password_enc keeps the stored one. Returns True when the target
        identity changed and the sync state was reset."""
        now = int(time.time())
        with self._lock:
            old = self._db.execute(
                'SELECT * FROM targets WHERE user=?', (user,)).fetchone()
            if old is None:
                self._db.execute(
                    'INSERT INTO targets(user, enabled, host, port, username,'
                    ' password_enc, prefix, include_junk, created_at,'
                    ' updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (user, int(fields['enabled']), fields['host'],
                     fields['port'], fields['username'],
                     fields.get('password_enc', ''), fields['prefix'],
                     int(fields['include_junk']), now, now))
                reset = True
            else:
                reset = any(str(old[k]) != str(fields[k])
                            for k in IDENTITY_FIELDS)
                self._db.execute(
                    'UPDATE targets SET enabled=?, host=?, port=?,'
                    ' username=?, prefix=?, include_junk=?, updated_at=?,'
                    ' password_enc=? WHERE user=?',
                    (int(fields['enabled']), fields['host'], fields['port'],
                     fields['username'], fields['prefix'],
                     int(fields['include_junk']), now,
                     fields.get('password_enc', old['password_enc']), user))
                if reset:
                    self._db.execute(
                        "UPDATE targets SET copied_total=0, skipped_total=0,"
                        " backlog=0, last_error='', last_ok_at=NULL"
                        " WHERE user=?", (user,))
            if reset:
                self._reset_state(user)
            self._db.commit()
        return reset

    def delete_target(self, user):
        with self._lock:
            cur = self._db.execute('DELETE FROM targets WHERE user=?', (user,))
            self._reset_state(user)
            self._db.commit()
        return cur.rowcount > 0

    def _reset_state(self, user):
        self._db.execute('DELETE FROM folder_state WHERE user=?', (user,))
        self._db.execute('DELETE FROM copied WHERE user=?', (user,))

    def record_run(self, user, ok, error, copied, skipped, backlog):
        now = int(time.time())
        with self._lock:
            self._db.execute(
                'UPDATE targets SET last_run_at=?, last_ok_at=CASE WHEN ?'
                ' THEN ? ELSE last_ok_at END, last_error=?,'
                ' copied_total=copied_total+?, skipped_total=skipped_total+?,'
                ' backlog=? WHERE user=?',
                (now, int(ok), now, error[:500], copied, skipped, backlog,
                 user))
            self._db.commit()

    # -- sync progress -----------------------------------------------
    def folder_state(self, user, folder):
        with self._lock:
            row = self._db.execute(
                'SELECT uidvalidity, last_uid FROM folder_state'
                ' WHERE user=? AND folder=?', (user, folder)).fetchone()
        return (row['uidvalidity'], row['last_uid']) if row else (None, 0)

    def set_folder_state(self, user, folder, uidvalidity, last_uid):
        with self._lock:
            self._db.execute(
                'INSERT INTO folder_state(user, folder, uidvalidity, last_uid)'
                ' VALUES(?,?,?,?) ON CONFLICT(user, folder) DO UPDATE SET'
                ' uidvalidity=excluded.uidvalidity,'
                ' last_uid=excluded.last_uid', (user, folder, uidvalidity,
                                                last_uid))
            self._db.commit()

    def is_copied(self, user, digest):
        with self._lock:
            row = self._db.execute(
                'SELECT 1 FROM copied WHERE user=? AND digest=?',
                (user, digest)).fetchone()
        return row is not None

    def mark_copied(self, user, digest):
        with self._lock:
            self._db.execute(
                'INSERT OR IGNORE INTO copied(user, digest, copied_at)'
                ' VALUES(?,?,?)', (user, digest, int(time.time())))
            self._db.commit()
