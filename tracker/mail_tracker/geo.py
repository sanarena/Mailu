"""Offline GeoIP city/country lookup over a compact binary database.

The database is built at image-build time from DB-IP Lite CSV
(see tools/build_geo.py; data (c) DB-IP, CC-BY-4.0, see README).

Binary layout (all big-endian):
  magic      4s  b'TRKG'
  version    H   1
  v4count    I
  v4 entries v4count * (start I, end I, loc I)   sorted by start
  v6count    I
  v6 entries v6count * (start 16s, end 16s, loc I)  sorted by start
  stab_len   I
  stab       stab_len bytes: entries of CC(2s) + NUL + city + NUL;
             loc is the byte offset of an entry.
Lookup is a bisect on the start arrays. Unknown/private IPs miss.
"""
import bisect
import ipaddress
import struct

MAGIC = b'TRKG'
VERSION = 1


class GeoDB:
    def __init__(self, path):
        with open(path, 'rb') as fh:
            blob = fh.read()
        if blob[:4] != MAGIC or struct.unpack('>H', blob[4:6])[0] != VERSION:
            raise ValueError('bad geo database (magic/version)')
        off = 6
        v4count = struct.unpack('>I', blob[off:off + 4])[0]
        off += 4
        self._v4starts = []
        self._v4ends = []
        self._v4locs = []
        for _ in range(v4count):
            s, e, loc = struct.unpack('>III', blob[off:off + 12])
            self._v4starts.append(s)
            self._v4ends.append(e)
            self._v4locs.append(loc)
            off += 12
        v6count = struct.unpack('>I', blob[off:off + 4])[0]
        off += 4
        self._v6starts = []
        self._v6ends = []
        self._v6locs = []
        for _ in range(v6count):
            s = int.from_bytes(blob[off:off + 16], 'big')
            e = int.from_bytes(blob[off + 16:off + 32], 'big')
            loc = struct.unpack('>I', blob[off + 32:off + 36])[0]
            self._v6starts.append(s)
            self._v6ends.append(e)
            self._v6locs.append(loc)
            off += 36
        stab_len = struct.unpack('>I', blob[off:off + 4])[0]
        off += 4
        self._stab = blob[off:off + stab_len]

    def _loc(self, offset):
        cc = self._stab[offset:offset + 2].decode('ascii', 'replace')
        end = self._stab.index(b'\x00', offset + 3)
        city = self._stab[offset + 3:end].decode('utf-8', 'replace')
        return cc, city

    def lookup(self, ip):
        """Return (country_code, city); ('', '') when unknown."""
        try:
            if ':' in ip:
                num = int.from_bytes(ipaddress.IPv6Address(ip).packed, 'big')
                i = bisect.bisect_right(self._v6starts, num) - 1
                if i >= 0 and num <= self._v6ends[i]:
                    return self._loc(self._v6locs[i])
            else:
                parts = ip.split('.')
                if len(parts) != 4:
                    return '', ''
                num = 0
                for part in parts:
                    num = num * 256 + int(part)
                    if num > 0xFFFFFFFF:
                        return '', ''
                i = bisect.bisect_right(self._v4starts, num) - 1
                if i >= 0 and num <= self._v4ends[i]:
                    return self._loc(self._v4locs[i])
        except (ValueError, IndexError):
            pass
        return '', ''


class NullGeoDB:
    """Fallback when no database file is available: everything unknown."""

    def lookup(self, ip):
        return '', ''


def load_geo_db(path):
    try:
        return GeoDB(path)
    except (OSError, ValueError):
        return NullGeoDB()
