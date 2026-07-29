#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract a battle event stream (actions, skills, damage, targets) from a .mhw replay.

Builds on the container format decoded by ``mhw_dump.py`` and decodes the
message layer far enough to answer "who used what skill on whom for how
much damage".

Message layer (verified against the edition 236 finals, 22,042 packets):

    packet := opcode(2) | length(uint16 LE) | payload(length) | 11 00 00

Payloads whose first byte is 0xe5 carry the battle semantics, dispatched
on the second byte:

    subop 0x93 -- action::

        e5 93 | subject(1) | skill_id(uint32 LE) | flags(uint16) |
        count(1) | affected(uint16 LE * count) |
        name_len(1) | name(GBK) | 00 00 00 00

    The affected-unit list is solid: the hit-point packets that follow an
    action name exactly those units, in the same order. ``subject`` is the
    primary affected unit, NOT the attacker -- treating it as the attacker
    puts 96% of damage inside one team, which a 5v5 match rules out. Which
    unit dealt a hit is therefore still undecoded.

    subop 0x6c -- unit stat delta (opcode 2a42)::

        e5 6c | unit(1) | kind(1) | delta(int32 LE) | ...

        kind 0 is hit points: delta < 0 is damage, delta > 0 is healing.

Skill names appear on only some actions, so names learned from those are
backfilled onto every action sharing the same skill id. Unit names come
from the definition packet preceding each ``176e`` registration; ids 1..20
are the two 5v5 teams with their pets, higher ids are entities summoned or
transformed mid-battle.

Examples:
    python mhw_events.py replay.mhw --report        # human-readable battle log
    python mhw_events.py replay.mhw                 # writes replay.events.json
    python mhw_events.py replay.mhw --jsonl         # one event per line
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import struct
import sys
from pathlib import Path

MAGIC = b"RRAW"
HEADER_SIZE = 16
TERMINATOR = b"\x11\x00\x00"
BATTLE_TAG = 0xE5
ACTION_SUBOP = 0x93
STAT_SUBOP = 0x6C
STAT_OPCODE = "2a42"
REGISTER_OPCODE = "176e"
STATUS_OPCODE = "07fa"
HP_KIND = 0
MAX_TARGETS = 24
GBK_RE = re.compile(rb"(?:[\xa1-\xf7][\xa1-\xfe])+")
# How many packets after an action its damage is still attributed to it.
ATTRIBUTION_WINDOW = 60


def read_records(path: Path) -> list[tuple[int, int, bytes]]:
    data = path.read_bytes()
    if data[:4] != MAGIC:
        raise ValueError(f"{path}: missing RRAW magic, not a Wushentan replay")
    out, pos, total = [], HEADER_SIZE, len(data)
    while pos + 4 <= total:
        (length,) = struct.unpack_from("<i", data, pos)
        if length == 0:
            raise ValueError(f"{path}: zero-length record at offset {pos}")
        size = -length if length < 0 else length
        payload = data[pos + 4 : pos + 4 + size]
        if len(payload) != size:
            raise ValueError(f"{path}: truncated record at offset {pos}")
        out.append((pos, length, payload))
        pos += 4 + size
    if pos != total:
        raise ValueError(f"{path}: trailing {total - pos} bytes at offset {pos}")
    return out


def gbk_strings(blob: bytes, min_chars: int = 2) -> list[str]:
    found = []
    for match in GBK_RE.finditer(blob):
        try:
            text = match.group().decode("gbk")
        except UnicodeDecodeError:
            continue
        if len(text) >= min_chars:
            found.append(text)
    return found


def body_of(payload: bytes) -> bytes | None:
    """Return the message body if the packet matches the framing, else None."""
    if len(payload) < 7 or payload[-3:] != TERMINATOR:
        return None
    if struct.unpack_from("<H", payload, 2)[0] != len(payload) - 7:
        return None
    return payload[4:-3]


def build_unit_table(packets: list[bytes]) -> dict[int, str]:
    table: dict[int, str] = {}
    for index, payload in enumerate(packets):
        if payload[:2].hex() != REGISTER_OPCODE or len(payload) < 5 or index == 0:
            continue
        uid = payload[4]
        if uid in table:
            continue
        names = gbk_strings(packets[index - 1])
        table[uid] = names[0] if names else f"#{uid}"
    return table


def parse_action(body: bytes) -> dict | None:
    """Decode a 0xe5 0x93 action body, accepting only a self-consistent parse.

    A few opcodes prefix the body with a spare byte, so the tag is searched
    for in the first few positions rather than assumed at zero.
    """
    for start in range(4):
        if len(body) < start + 11:
            break
        if body[start] != BATTLE_TAG or body[start + 1] != ACTION_SUBOP:
            continue
        view = body[start:]
        count = view[9]
        end = 10 + count * 2
        if count > MAX_TARGETS or len(view) <= end:
            continue
        targets = [struct.unpack_from("<H", view, 10 + i * 2)[0] for i in range(count)]
        name_len = view[end]
        tail = view[end + 1 :]
        if name_len > len(tail):
            continue
        rest = tail[name_len:]
        # Anything past the name is zero padding, or a trailing JSON blob.
        if any(rest) and b"{" not in rest:
            continue
        name = None
        if name_len:
            try:
                name = tail[:name_len].decode("gbk")
            except UnicodeDecodeError:
                continue
        extra = None
        if b"{" in rest:
            blob = rest[rest.index(b"{") :]
            blob = blob[: blob.rindex(b"}") + 1] if b"}" in blob else b""
            try:
                extra = json.loads(blob.decode("gbk"))
            except (ValueError, UnicodeDecodeError):
                extra = None
        return {
            "subject": view[2],
            "skill_id": struct.unpack_from("<I", view, 3)[0],
            "flags": struct.unpack_from("<H", view, 7)[0],
            "affected": targets,
            "skill_name": name,
            "extra": extra,
        }
    return None


def extract_events(records: list[tuple[int, int, bytes]]) -> dict:
    packets = [p for _, ln, p in records if ln > 0]
    units = build_unit_table(packets)
    name_of = lambda uid: units.get(uid, f"#{uid}")

    events: list[dict] = []
    skills: dict[int, str] = {}
    stats = collections.Counter()

    for index, payload in enumerate(packets):
        opcode = payload[:2].hex()
        if len(payload) < 7 or payload[-3:] != TERMINATOR:
            continue
        body = payload[4:-3]

        # Status notices ("被封印", "气血不足") sit outside the 0xe5 families.
        if opcode == STATUS_OPCODE and len(body) > 5:
            names = gbk_strings(body)
            if names:
                events.append(
                    {
                        "seq": index,
                        "kind": "status",
                        "unit": body[5],
                        "unit_name": name_of(body[5]),
                        "status": names[0],
                    }
                )
                stats["status"] += 1
            continue

        action = parse_action(body)
        if action is not None:
            if action["skill_name"]:
                skills.setdefault(action["skill_id"], action["skill_name"])
            event = {
                "seq": index,
                "kind": "action",
                "subject": action["subject"],
                "subject_name": name_of(action["subject"]),
                "skill_id": action["skill_id"],
                "skill_name": action["skill_name"],
                "affected": action["affected"],
                "affected_names": [name_of(t) for t in action["affected"]],
            }
            if action["extra"] is not None:
                event["extra"] = action["extra"]
            events.append(event)
            stats["action"] += 1

        elif len(body) >= 8 and body[0] == BATTLE_TAG and body[1] == STAT_SUBOP and opcode == STAT_OPCODE:
            if body[3] != HP_KIND:
                stats["stat_other"] += 1
                continue
            delta = struct.unpack_from("<i", body, 4)[0]
            uid = body[2]
            events.append(
                {
                    "seq": index,
                    "kind": "heal" if delta > 0 else "damage",
                    "unit": uid,
                    "unit_name": name_of(uid),
                    "hp_delta": delta,
                }
            )
            stats["heal" if delta > 0 else "damage"] += 1

    # Backfill names learned from other casts of the same skill.
    for event in events:
        if event["kind"] == "action" and not event["skill_name"]:
            event["skill_name"] = skills.get(event["skill_id"])

    # Link each hp change to the action that named its unit as affected.
    linked = 0
    recent: list[dict] = []
    for event in events:
        if event["kind"] == "action":
            recent.append(event)
            recent[:] = [a for a in recent if event["seq"] - a["seq"] <= ATTRIBUTION_WINDOW]
        elif event["kind"] in ("damage", "heal"):
            for action in reversed(recent):
                if event["unit"] in action["affected"]:
                    event["skill_id"] = action["skill_id"]
                    event["skill_name"] = action["skill_name"]
                    event["action_seq"] = action["seq"]
                    linked += 1
                    break
    stats["hp_linked_to_skill"] = linked

    return {
        "units": {str(k): v for k, v in sorted(units.items())},
        "skills": {str(k): v for k, v in sorted(skills.items())},
        "packet_count": len(packets),
        "event_count": len(events),
        "stats": dict(stats),
        "events": events,
    }


def print_report(doc: dict) -> None:
    events = doc["events"]
    stats = doc["stats"]
    dmg = [e for e in events if e["kind"] == "damage"]
    print(f"units {len(doc['units'])}   packets {doc['packet_count']:,}   events {len(events):,}")
    print(
        f"  actions {stats.get('action', 0):,}   damage {stats.get('damage', 0):,}   "
        f"heals {stats.get('heal', 0):,}   status {stats.get('status', 0):,}"
    )
    linked = stats.get("hp_linked_to_skill", 0)
    print(f"  hp changes linked to a skill: {linked:,} of {len(dmg) + stats.get('heal', 0):,}")
    print(f"  skill names recovered: {len(doc['skills'])} (ids seen: {len({e['skill_id'] for e in events if e['kind'] == 'action'})})")

    taken = collections.Counter()
    for event in dmg:
        taken[event["unit_name"]] += -event["hp_delta"]
    print("\nmost damage taken:")
    for name, total in taken.most_common(10):
        print(f"  {total:>8,}  {name}")

    by_skill = collections.Counter()
    for event in dmg:
        key = event.get("skill_name") or (
            f"skill#{event['skill_id']}" if "skill_id" in event else "(unlinked)"
        )
        by_skill[key] += -event["hp_delta"]
    print("\nmost damage by skill:")
    for name, total in by_skill.most_common(10):
        print(f"  {total:>8,}  {name}")

    print("\nbiggest single hits:")
    for event in sorted(dmg, key=lambda e: e["hp_delta"])[:8]:
        skill = event.get("skill_name") or f"skill#{event.get('skill_id', '?')}"
        print(f"  {-event['hp_delta']:>7,}  {event['unit_name']:<14} ({skill})")

    named = collections.Counter(
        e["skill_name"] for e in events if e["kind"] == "action" and e["skill_name"]
    )
    print("\nnamed skills cast:")
    for name, count in named.most_common(12):
        print(f"  {count:>4}x  {name}")

    by_status = collections.Counter(e["status"] for e in events if e["kind"] == "status")
    if by_status:
        print("\nstatus notices:")
        for name, count in by_status.most_common(8):
            print(f"  {count:>4}x  {name}")

    print("\nsample event stream:")
    for event in events[:14]:
        if event["kind"] == "action":
            skill = event["skill_name"] or f"skill#{event['skill_id']}"
            print(f"  #{event['seq']:<6} action {skill:<22} -> {event['affected_names']}")
        elif event["kind"] == "status":
            print(f"  #{event['seq']:<6} status {event['unit_name']} {event['status']}")
        else:
            skill = event.get("skill_name") or f"skill#{event.get('skill_id', '?')}"
            print(f"  #{event['seq']:<6} {event['kind']:<6} {event['unit_name']:<14} {event['hp_delta']:+,}  ({skill})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mhw", type=Path, help=".mhw replay file")
    parser.add_argument("-o", "--output", type=Path, help="output path")
    parser.add_argument("--jsonl", action="store_true", help="write one event per line")
    parser.add_argument("--report", action="store_true", help="print a battle summary")
    args = parser.parse_args()

    doc = extract_events(read_records(args.mhw))
    if args.report:
        print_report(doc)
        return 0
    if args.jsonl:
        output = args.output or args.mhw.with_suffix(".events.jsonl")
        with output.open("w", encoding="utf-8") as handle:
            for event in doc["events"]:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    else:
        output = args.output or args.mhw.with_suffix(".events.json")
        output.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {output} ({output.stat().st_size:,} bytes, {doc['event_count']:,} events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
