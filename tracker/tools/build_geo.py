#!/usr/bin/env python3
"""Build the compact tracker GeoIP database from DB-IP Lite CSV.

Usage: python3 build_geo.py <dbip-city-lite.csv[.gz]> <out.dat>

Input rows: ip_start,ip_end,continent,country,stateprov,city,lat,lon
(Data (c) DB-IP, CC-BY-4.0 - attribution in tracker README.)

Adjacent ranges with the same (country, city) are merged to keep the
database small. Rows with no country information are dropped.
"""
import csv
import gzip
import ipaddress
import struct
import sys

MAGIC = b'TRKG'
VERSION = 1


def parse_ip(text):
    text = text.strip().strip('"')
    try:
        return int(ipaddress.IPv4Address(text)), 4
    except ValueError:
        return int(ipaddress.IPv6Address(text)), 6


def load_rows(path):
    opener = gzip.open if path.endswith('.gz') else open
    v4, v6 = [], []
    with opener(path, 'rt', newline='') as fh:
        for row in csv.reader(fh):
            if len(row) < 6:
                continue
            try:
                start, ver = parse_ip(row[0])
                end, ver2 = parse_ip(row[1])
            except ValueError:
                continue
            if ver != ver2 or end < start:
                continue
            cc = row[3].strip().strip('"').upper()
            city = row[5].strip().strip('"')[:64]
            if not cc or cc == 'ZZ':
                continue
            if ver == 4:
                v4.append((start, end, cc, city))
            else:
                v6.append((start, end, cc, city))
    return v4, v6


def merge(entries):
    entries.sort()
    out = []
    for start, end, cc, city in entries:
        if out and out[-1][2] == cc and out[-1][3] == city \
                and start <= out[-1][1] + 1:
            if end > out[-1][1]:
                out[-1][1] = end
        else:
            out.append([start, end, cc, city])
    return out


def build(path, out_path):
    v4, v6 = load_rows(path)
    v4 = merge(v4)
    v6 = merge(v6)
    loc_index = {}
    stab = bytearray()  # noqa: F841  (populated by loc_id below)

    def loc_id(cc, city):
        nonlocal stab
        key = (cc, city)
        if key not in loc_index:
            loc_index[key] = len(stab)
            stab += cc.encode('ascii', 'replace')[:2].ljust(2, b'?')
            stab += b'\x00' + city.encode('utf-8', 'replace') + b'\x00'
        return loc_index[key]

    blob = bytearray()
    blob += MAGIC + struct.pack('>H', VERSION)
    blob += struct.pack('>I', len(v4))
    for start, end, cc, city in v4:
        blob += struct.pack('>III', start, end, loc_id(cc, city))
    blob += struct.pack('>I', len(v6))
    for start, end, cc, city in v6:
        blob += start.to_bytes(16, 'big') + end.to_bytes(16, 'big')
        blob += struct.pack('>I', loc_id(cc, city))
    blob += struct.pack('>I', len(stab)) + stab
    with open(out_path, 'wb') as fh:
        fh.write(blob)
    print(f'v4={len(v4)} v6={len(v6)} locs={len(loc_index)}'
          f' bytes={len(blob)} -> {out_path}')


if __name__ == '__main__':
    build(sys.argv[1], sys.argv[2])
