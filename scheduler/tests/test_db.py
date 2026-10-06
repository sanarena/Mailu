"""Unit tests for the scheduler store (no network)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, ".")

from scheduler.sched.db import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 's.db'))

    def tearDown(self):
        self.store.close()

    def test_scheduled_lifecycle(self):
        now = int(time.time())
        sid = self.store.add_scheduled(
            'A@x.test', 'a@x.test', ['b@y.test'], 'Hi', b'mime', now + 60)
        rows = self.store.list_scheduled('a@x.test')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'queued')
        # not due yet
        self.assertEqual(self.store.due_scheduled(now), [])
        due = self.store.due_scheduled(now + 61)
        self.assertEqual([r['id'] for r in due], [sid])
        self.assertEqual(due[0]['mime'], b'mime')
        self.store.mark_sent(sid, now + 61)
        self.assertEqual(self.store.due_scheduled(now + 3600), [])
        self.assertEqual(
            self.store.list_scheduled('a@x.test')[0]['status'], 'sent')

    def test_scheduled_cancel_scoped(self):
        now = int(time.time())
        sid = self.store.add_scheduled(
            'a@x.test', 'a@x.test', ['b@y.test'], 'Hi', b'm', now + 60)
        self.assertFalse(self.store.cancel_scheduled(sid, 'evil@x.test'))
        self.assertTrue(self.store.cancel_scheduled(sid, 'a@x.test'))
        self.assertEqual(self.store.due_scheduled(now + 3600), [])

    def test_schedule_retry_and_fail(self):
        now = int(time.time())
        sid = self.store.add_scheduled(
            'a@x.test', 'a@x.test', ['b@y.test'], 'Hi', b'm', now - 10)
        self.store.mark_schedule_retry(sid, now + 300, 'boom')
        row = self.store.list_scheduled('a@x.test')[0]
        self.assertEqual((row['status'], row['attempts'], row['error']),
                         ('queued', 1, 'boom'))
        self.assertEqual(self.store.due_scheduled(now + 10), [])
        self.assertEqual(len(self.store.due_scheduled(now + 301)), 1)
        self.store.mark_schedule_failed(sid, 'dead')
        self.assertEqual(
            self.store.list_scheduled('a@x.test')[0]['status'], 'failed')

    def test_snooze_lifecycle(self):
        now = int(time.time())
        sid = self.store.add_snooze('a@x.test', '<m1>', 'INBOX', now + 60)
        self.assertEqual(self.store.due_snoozes(now), [])
        due = self.store.due_snoozes(now + 61)
        self.assertEqual([r['id'] for r in due], [sid])
        self.assertFalse(self.store.cancel_snooze(sid, 'evil@x.test'))
        self.store.mark_snooze_done(sid, now + 61)
        self.assertEqual(
            self.store.list_snoozes('a@x.test')[0]['status'], 'done')

    def test_purge_keeps_queued(self):
        now = int(time.time())
        old = now - 100 * 86400
        # backdate by hand: terminal old rows purge, queued never purges
        cur = self.store._db.execute(
            "INSERT INTO scheduled(user, sender, recipients, subject, mime,"
            " send_at, status, next_try, created_at, sent_at)"
            " VALUES('a@x.test','a@x.test','[]','old',x'00',?,?,?,?,?)",
            (old, 'sent', old, old, old))
        sent_id = cur.lastrowid
        queued_id = self.store.add_scheduled(
            'a@x.test', 'a@x.test', ['b@y.test'], 'new', b'm', now + 60)
        self.store._db.execute(
            'UPDATE scheduled SET created_at=? WHERE id=?', (old, queued_id))
        self.store._db.commit()
        self.assertEqual(self.store.purge(90, now), 1)
        ids = [r['id'] for r in self.store.list_scheduled('a@x.test')]
        self.assertEqual(ids, [queued_id])
        self.assertNotIn(sent_id, ids)


if __name__ == '__main__':
    unittest.main()
