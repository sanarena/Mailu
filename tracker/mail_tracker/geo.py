"""Offline GeoIP city/country lookup over a compact binary database.

The database is built from DB-IP Lite CSV (see tools/build_geo.py;
data (c) DB-IP, CC-BY-4.0, see README) and shipped in the image.

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

The file is ~180MB; it is mmapped and bisected in place so lookups
cost kilobytes of RAM instead of hundreds of megabytes of Python
objects. Unknown/private IPs miss.
"""
import bisect
import ipaddress
import mmap
import struct

MAGIC = b'TRKG'
VERSION = 1


class _Starts:
    """Read-only bisectable view over the start column of an entry table."""

    def __init__(self, mem, base, count, stride, size):
        self._mem = mem
        self._base = base
        self._count = count
        self._stride = stride
        self._size = size

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in
                    range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        off = self._base + index * self._stride
        if self._size == 4:
            return struct.unpack_from('>I', self._mem, off)[0]
        return int.from_bytes(self._mem[off:off + 16], 'big')


class GeoDB:
    def __init__(self, path):
        self._file = open(path, 'rb')
        self._mem = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        mem = self._mem
        if bytes(mem[0:4]) != MAGIC or \
                struct.unpack_from('>H', mem, 4)[0] != VERSION:
            raise ValueError('bad geo database (magic/version)')
        off = 6
        v4count = struct.unpack_from('>I', mem, off)[0]
        off += 4
        self._v4starts = _Starts(mem, off, v4count, 12, 4)
        self._v4base = off
        off += v4count * 12
        v6count = struct.unpack_from('>I', mem, off)[0]
        off += 4
        self._v6starts = _Starts(mem, off, v6count, 36, 16)
        self._v6base = off
        off += v6count * 36
        stab_len = struct.unpack_from('>I', mem, off)[0]
        off += 4
        self._stab_base = off
        self._stab_end = off + stab_len

    def close(self):
        try:
            self._mem.close()
        finally:
            self._file.close()

    def _loc(self, offset):
        base = self._stab_base + offset
        cc = bytes(self._mem[base:base + 2]).decode('ascii', 'replace')
        end = self._mem.find(b'\x00', base + 3, self._stab_end)
        city = bytes(self._mem[base + 3:end]).decode('utf-8', 'replace')
        return cc, city

    def lookup(self, ip):
        """Return (country_code, city); ('', '') when unknown."""
        try:
            if ':' in ip:
                num = int.from_bytes(
                    ipaddress.IPv6Address(ip).packed, 'big')
                i = bisect.bisect_right(self._v6starts, num) - 1
                if i >= 0:
                    off = self._v6base + i * 36
                    end = int.from_bytes(self._mem[off + 16:off + 32], 'big')
                    if num <= end:
                        loc = struct.unpack_from(
                            '>I', self._mem, off + 32)[0]
                        return self._loc(loc)
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
                if i >= 0:
                    off = self._v4base + i * 12
                    end, loc = struct.unpack_from('>II', self._mem, off + 4)
                    if num <= end:
                        return self._loc(loc)
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
