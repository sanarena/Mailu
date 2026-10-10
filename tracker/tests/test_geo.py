"""Unit tests for GeoIP conversion + lookup (tiny fixture database)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, ".")
sys.path.insert(0, "tracker/tools")

from tracker.mail_tracker.geo import GeoDB, NullGeoDB, load_geo_db
import build_geo

FIXTURE_CSV = """\
1.0.0.0,1.0.0.255,OC,AU,Queensland,South Brisbane,-27.4,153.0
1.0.1.0,1.0.1.255,OC,AU,Queensland,South Brisbane,-27.4,153.0
2.0.0.0,2.0.0.255,EU,DE,,Berlin,52.5,13.4
3.0.0.0,3.255.255.255,ZZ,ZZ,,,0,0
2001:db8::,2001:db8::ffff,NA,US,California,Mountain View,37.4,-122.0
"""


class GeoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.csv_path = os.path.join(cls.tmp, 'mini.csv')
        cls.dat_path = os.path.join(cls.tmp, 'mini.dat')
        with open(cls.csv_path, 'w') as fh:
            fh.write(FIXTURE_CSV)
        build_geo.build(cls.csv_path, cls.dat_path)
        cls.db = GeoDB(cls.dat_path)

    def test_v4_hit(self):
        self.assertEqual(self.db.lookup('1.0.0.128'),
                         ('AU', 'South Brisbane'))

    def test_v4_merged_adjacent(self):
        # 1.0.0.x and 1.0.1.x share a location: one merged range.
        self.assertEqual(self.db.lookup('1.0.1.7'),
                         ('AU', 'South Brisbane'))

    def test_v4_other(self):
        self.assertEqual(self.db.lookup('2.0.0.1'), ('DE', 'Berlin'))

    def test_v4_miss_and_skipped_filler(self):
        self.assertEqual(self.db.lookup('4.4.4.4'), ('', ''))
        self.assertEqual(self.db.lookup('3.3.3.3'), ('', ''))

    def test_v6_hit_and_miss(self):
        self.assertEqual(self.db.lookup('2001:db8::1'),
                         ('US', 'Mountain View'))
        self.assertEqual(self.db.lookup('2001:db9::1'), ('', ''))

    def test_garbage(self):
        for bad in ('', 'not-an-ip', '1.2.3', '1.2.3.4.5', '999.1.1.1',
                    ':::', '1.2.3.4:5678'):
            self.assertEqual(self.db.lookup(bad), ('', ''), bad)

    def test_null_db_and_missing_file(self):
        self.assertEqual(NullGeoDB().lookup('1.0.0.1'), ('', ''))
        self.assertIsInstance(
            load_geo_db(os.path.join(self.tmp, 'nope.dat')), NullGeoDB)


if __name__ == '__main__':
    unittest.main()
