#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump a Wushentan .mhw replay file into JSON (container level).

Reverse-engineered container format (verified on edition 236 finals replay):

    offset 0:  4 bytes  magic ``RRAW``
    offset 4: 12 bytes  header (version/timestamp fields, not yet decoded)
    offset 16:          record stream until EOF

Each record is a signed little-endian int32 length followed by payload:

    length > 0   game packet (recorded server->client protocol message)
    length < 0   sync frame of abs(length) bytes, carrying a float64 Unix
                 timestamp (seconds) at payload offset 5

Packet payloads contain GBK strings (player/pet/skill names, battle
messages) and binary fields whose semantics are not yet mapped.

Examples:
    python mhw_dump.py replay.mhw                 # writes replay.json
    python mhw_dump.py replay.mhw -o out.json
    python mhw_dump.py replay.mhw --no-raw        # omit hex payloads
    python mhw_dump.py replay.mhw --stats         # print summary only
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

MAGIC = b"RRAW"
HEADER_SIZE = 16
GBK_RE = re.compile(rb"(?:[\xa1-\xf7][\xa1-\xfe]){2,}")
ASCII_RE = re.compile(rb"[\x20-\x7e]{4,}")


def extract_strings(payload: bytes) -> list[str]:
    strings = []
    for match in GBK_RE.finditer(payload):
        try:
            strings.append(match.group().decode("gbk"))
        except UnicodeDecodeError:
            pass
    for match in ASCII_RE.finditer(payload):
        strings.append(match.group().decode("ascii"))
    return strings


def parse_mhw(path: Path, include_raw: bool = True) -> dict:
    data = path.read_bytes()
    if data[:4] != MAGIC:
        raise ValueError(f"{path}: missing RRAW magic, not a Wushentan replay")
    records = []
    pos = HEADER_SIZE
    total = len(data)
    while pos + 4 <= total:
        (length,) = struct.unpack_from("<i", data, pos)
        if length == 0:
            raise ValueError(f"{path}: zero-length record at offset {pos}")
        size = -length if length < 0 else length
        payload = data[pos + 4 : pos + 4 + size]
        if len(payload) != size:
            raise ValueError(f"{path}: truncated record at offset {pos}")
        record: dict = {
            "index": len(records),
            "offset": pos,
            "type": "sync" if length < 0 else "packet",
            "size": size,
        }
        if length > 0:
            record["head"] = payload[:4].hex()
            strings = extract_strings(payload)
            if strings:
                record["strings"] = strings
        elif size >= 13:
            (stamp,) = struct.unpack_from("<d", payload, 5)
            record["timestamp"] = stamp
            record["time_utc"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if include_raw:
            record["raw"] = payload.hex()
        records.append(record)
        pos += 4 + size
    if pos != total:
        raise ValueError(f"{path}: trailing {total - pos} bytes at offset {pos}")
    return {
        "file": path.name,
        "file_bytes": total,
        "magic": MAGIC.decode(),
        "header": data[4:HEADER_SIZE].hex(),
        "record_count": len(records),
        "packet_count": sum(1 for r in records if r["type"] == "packet"),
        "sync_count": sum(1 for r in records if r["type"] == "sync"),
        "records": records,
    }


def print_stats(doc: dict) -> None:
    print(f"{doc['file']}: {doc['file_bytes']:,} bytes")
    print(f"  packets: {doc['packet_count']:,}   sync frames: {doc['sync_count']}")
    stamps = [r["time_utc"] for r in doc["records"] if "time_utc" in r]
    if stamps:
        print(f"  recorded: {stamps[0]} .. {stamps[-1]}")
    heads = collections.Counter(
        r["head"][:4] for r in doc["records"] if r["type"] == "packet"
    )
    print("  top packet head bytes (candidate opcodes):")
    for head, count in heads.most_common(10):
        print(f"    {head}  x{count:,}")
    named = [r for r in doc["records"] if r.get("strings")]
    print(f"  packets containing strings: {len(named):,}")
    for record in named[:8]:
        print(f"    #{record['index']} @{record['offset']}: {record['strings'][:4]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mhw", type=Path, help=".mhw replay file")
    parser.add_argument("-o", "--output", type=Path, help="output JSON path")
    parser.add_argument(
        "--no-raw", action="store_true", help="omit hex payloads (smaller JSON)"
    )
    parser.add_argument(
        "--stats", action="store_true", help="print a summary instead of writing JSON"
    )
    args = parser.parse_args()

    doc = parse_mhw(args.mhw, include_raw=not args.no_raw)
    if args.stats:
        print_stats(doc)
        return 0
    output = args.output or args.mhw.with_suffix(".json")
    output.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {output} ({output.stat().st_size:,} bytes, {doc['record_count']:,} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
