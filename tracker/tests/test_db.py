"""Unit tests for the tracker store (temp-file SQLite)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, ".")

from tracker.mail_tracker.db import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, 't.db'))

    def tearDown(self):
        self.store.close()

    def _msg(self, token='tok1', sender='a@x.test', subject='Hi',
             rcpts=('b@y.test',), links=('http://e.test/',)):
        return self.store.add_message(token, '<m1>', sender, subject,
                                      list(rcpts), list(links), 100)

    def test_message_roundtrip(self):
        mid = self._msg()
        row = self.store.get_message(mid)
        self.assertEqual(row['sender'], 'a@x.test')
        self.assertEqual(row['subject'], 'Hi')
        self.assertEqual(self.store.recipients_of(mid), ['b@y.test'])
        self.assertEqual(self.store.get_link(mid, 0), 'http://e.test/')
        self.assertIsNone(self.store.get_link(mid, 9))

    def test_get_message_owner_gate(self):
        mid = self._msg()
        self.assertIsNotNone(self.store.get_message(mid, 'a@x.test'))
        self.assertIsNone(self.store.get_message(mid, 'evil@x.test'))
        self.assertIsNone(self.store.get_message(4242, 'a@x.test'))

    def test_list_and_count_scoping(self):
        self._msg(token='t1', sender='a@x.test')
        self._msg(token='t2', sender='a@x.test')
        self._msg(token='t3', sender='b@x.test')
        self.assertEqual(self.store.count_messages('a@x.test'), 2)
        self.assertEqual(self.store.count_messages(), 3)
        rows = self.store.list_messages('a@x.test')
        self.assertEqual([r['token'] for r in rows], ['t2', 't1'])
        page = self.store.list_messages(limit=1, offset=1)
        self.assertEqual(len(page), 1)

    def test_events_and_counts(self):
        mid = self._msg()
        self.store.add_event(mid, 'open', ip='1.2.3.4', country='US',
                             city='Springfield', client='Gmail image proxy')
        self.store.add_event(mid, 'click', ip='1.2.3.4', link_idx=0)
        events = self.store.events_of(mid, 'a@x.test')
        self.assertEqual([e['kind'] for e in events], ['open', 'click'])
        self.assertEqual(events[0]['geo_city'], 'Springfield')
        self.assertIsNone(self.store.events_of(mid, 'evil@x.test'))
        counts = self.store.event_counts('a@x.test')
        self.assertEqual(counts[mid]['opens'], 1)
        self.assertEqual(counts[mid]['clicks'], 1)
        self.assertIsNotNone(counts[mid]['last_open'])
        stats = self.store.stats('a@x.test')
        self.assertEqual(
            (stats['sent'], stats['opens'], stats['clicks'],
             stats['opened_messages']), (1, 1, 1, 1))

    def test_kill_switch(self):
        mid = self._msg()
        self.assertTrue(self.store.set_tracking(mid, False, 'a@x.test'))
        self.assertEqual(self.store.get_message(mid)['tracking'], 0)
        self.assertFalse(self.store.set_tracking(mid, True, 'evil@x.test'))
        self.assertEqual(self.store.get_message(mid)['tracking'], 0)
        self.assertTrue(self.store.set_tracking(mid, True))
        self.assertEqual(self.store.get_message(mid)['tracking'], 1)
        self.assertFalse(self.store.set_tracking(4242, False))

    def test_optouts(self):
        self.assertFalse(self.store.sender_opted_out('a@x.test'))
        self.store.set_sender_optout('A@X.TEST', True)
        self.assertTrue(self.store.sender_opted_out('a@x.test'))
        self.assertEqual(self.store.list_sender_optouts(), ['a@x.test'])
        self.store.set_sender_optout('a@x.test', False)
        self.assertFalse(self.store.sender_opted_out('a@x.test'))
        self.store.set_recipient_optout('a@x.test', 'B@y.test', True)
        self.assertTrue(
            self.store.recipient_opted_out('a@x.test', 'b@y.test'))
        self.assertFalse(
            self.store.recipient_opted_out('zz@x.test', 'b@y.test'))
        self.assertEqual(self.store.list_recipient_optouts('a@x.test'),
                         ['b@y.test'])
        self.store.set_recipient_optout('a@x.test', 'b@y.test', False)
        self.assertEqual(self.store.list_recipient_optouts('a@x.test'), [])

    def test_retention_purge(self):
        mid = self._msg()
        old = int(time.time()) - 400 * 86400
        with self.store._lock:
            self.store._db.execute(
                'UPDATE messages SET sent_at=? WHERE id=?', (old, mid))
            self.store._db.execute(
                'INSERT INTO events(message_id, kind, at) VALUES(?, "open", ?)',
                (mid, old))
            self.store._db.commit()
        self.store.purge_old_events(365)
        self.assertEqual(self.store.events_of(mid), [])
        self.assertIsNone(self.store.get_link(mid, 0))
        self.assertEqual(self.store.get_message(mid)['tracking'], 0)
        # Message metadata itself survives (the mail log stays).
        self.assertIsNotNone(self.store.get_message(mid))


if __name__ == '__main__':
    unittest.main()
