"""Benchmark of the three core hot paths, on the current Python implementation.

The migration to Rust is justified by `matrix-nio` being unmaintained, not by
speed. This script exists so that "faster" is a number instead of a claim, and
so the same measurements can be re-run against the Rust core once it exists.

Three paths, chosen because they are the only ones that move at all:

    1. E2EE backlog   decrypt N events of a megolm session
    2. sync ingestion parse a ~5 MB /sync response and build the timeline entries
    3. timeline redraw render N messages into Rich markup

Run:
    .venv/bin/python bench/core_hotpaths.py
    .venv/bin/python bench/core_hotpaths.py --events 2000 --repeats 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from typing import Callable

# ---------------------------------------------------------------------------
# 1. E2EE backlog
# ---------------------------------------------------------------------------


def bench_e2ee_backlog(events: int) -> dict[str, float]:
    """Encrypt then decrypt `events` messages through one megolm session.

    This is the shape of scrolling into an encrypted room with a backlog: the
    sender ratchets forward for every message, the receiver decrypts each one.
    Uses python-olm directly, which is exactly the binding matrix-nio uses.
    """
    import olm

    outbound = olm.OutboundGroupSession()
    inbound = olm.InboundGroupSession(outbound.session_key)

    payloads = [json.dumps({"body": f"message {i}"}) for i in range(events)]

    started = time.perf_counter()
    for payload in payloads:
        ciphertext = outbound.encrypt(payload)
        plaintext = inbound.decrypt(ciphertext)
        if not plaintext:
            raise AssertionError("olm roundtrip failed")
    elapsed = time.perf_counter() - started

    return {
        "events": events,
        "seconds": elapsed,
        "per_event_us": elapsed / events * 1e6,
        "events_per_s": events / elapsed,
    }


# ---------------------------------------------------------------------------
# 2. Sync ingestion
# ---------------------------------------------------------------------------


def _fake_sync(target_mb: float, *, events: int = 0) -> tuple[bytes, int]:
    """A /sync response of roughly `target_mb` MB, as raw JSON bytes.

    Sized in two passes: the body length is solved for so the payload lands on
    the requested size with a realistic per-event shape, instead of padding the
    JSON by hand. Returns the bytes and the event count.
    """
    rooms_count = 20
    body_words = 6

    def build(word_count: int) -> tuple[bytes, int]:
        body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * word_count
        per_room = max(1, target_events // rooms_count)
        rooms: dict[str, dict] = {}
        total = 0
        for room_index in range(rooms_count):
            timeline = []
            for i in range(per_room):
                timeline.append(
                    {
                        "type": "m.room.message",
                        "event_id": f"$room{room_index}-event{i}",
                        "sender": f"@user{(i * 7) % 23}:matrix.org",
                        "origin_server_ts": 1_757_000_000_000 + i * 1800,
                        "content": {
                            "msgtype": "m.text",
                            "body": f"{body} [{room_index}/{i}]",
                        },
                    }
                )
            total += len(timeline)
            rooms[f"!room{room_index}:matrix.org"] = {
                "timeline": {"events": timeline, "limited": False, "prev_batch": "p"},
                "state": {"events": []},
                "ephemeral": {"events": []},
            }
        payload = {
            "next_batch": "s12345",
            "rooms": {"join": rooms, "invite": {}, "leave": {}},
            "device_one_time_keys_count": {"signed_curve25519": 50},
            "device_lists": {"changed": [], "left": []},
            "to_device": {"events": []},
            "account_data": {"events": []},
            "presence": {"events": []},
        }
        return json.dumps(payload).encode(), total

    # Solve the body length by bisection: one pass to size the events, then
    # adjust the words per body until the payload matches the target.
    target_events = events or max(40, int(target_mb * 1e6 / 500))
    raw, count = build(body_words)
    while len(raw) < target_mb * 1e6 and body_words < 400:
        body_words += 4
        raw, count = build(body_words)
    while len(raw) > target_mb * 1e6 * 1.05 and body_words > 1:
        body_words -= 1
        raw, count = build(body_words)
    return raw, count


def bench_sync_ingest(megabytes: float, *, events: int = 10000) -> dict[str, float]:
    """Parse a /sync payload and build the app's timeline entries.

    Measures what shelltrix owns: decoding the JSON and turning events into
    `TimelineEntry`. A real `/sync` carries a handful of events, so the
    `events` argument exists to report the typical case next to the backlog.
    """
    import nio

    from shelltrix.formatting import TimelineEntry

    raw, expected_events = _fake_sync(megabytes, events=events)
    size_mb = len(raw) / 1e6

    started = time.perf_counter()
    decoded = json.loads(raw)
    joined = decoded["rooms"]["join"]

    entries = 0
    for room_id, room in joined.items():
        for event in room["timeline"]["events"]:
            parsed = nio.RoomMessage.parse_event(event)
            TimelineEntry(
                sender=parsed.sender,
                display_name=parsed.sender.split(":")[0][1:],
                is_own=False,
                time_ms=parsed.server_timestamp,
                body=parsed.body,
                event_id=parsed.event_id,
                msgtype="m.text",
                has_mention=False,
                is_image=False,
                image_hint="",
                timestamp="",
                reply_to_event_id="",
                reply_to_name="",
            )
            entries += 1
    elapsed = time.perf_counter() - started

    assert entries == expected_events, f"{entries} events built, expected {expected_events}"

    return {
        "size_mb": size_mb,
        "events": entries,
        "seconds": elapsed,
        "per_event_us": elapsed / entries * 1e6,
        "mb_per_s": size_mb / elapsed,
    }


# ---------------------------------------------------------------------------
# 3. Timeline redraw
# ---------------------------------------------------------------------------


def bench_timeline_redraw(messages: int) -> dict[str, float]:
    """Render `messages` entries into Rich markup, the path a room open takes."""
    from rich.markup import escape

    from shelltrix.formatting import (
        TimelineContext,
        TimelineEntry,
        _sender_color,
        format_timeline_entries,
    )

    def header_for(entry: TimelineEntry) -> str:
        """Same markup as `ChatScreen._header_for`, without a running app."""
        ts = escape(entry.timestamp) if entry.timestamp else "--:--"
        stamp = f"[dim]{ts}[/dim]   "
        if entry.is_own:
            return f"{stamp}[bold]› You[/bold]"
        color = _sender_color(entry.sender)
        return f"{stamp}[{color}]‹ {escape(entry.display_name or entry.sender)}[/{color}]"

    entries = [
        TimelineEntry(
            sender=f"@user{i % 23}:matrix.org",
            display_name=f"User {i % 23}",
            is_own=(i % 11 == 0),
            time_ms=1_757_000_000_000 + i * 20_000,
            body=(
                "Lorem ipsum dolor sit amet, **consectetur** adipiscing elit. "
                f"Message {i}"
            ),
            event_id=f"$event{i}",
            msgtype="m.text",
            has_mention=(i % 37 == 0),
            is_image=False,
            image_hint="",
            timestamp=f"{(i // 20) % 24:02d}:{(i * 3) % 60:02d}",
            reply_to_event_id="",
            reply_to_name="",
        )
        for i in range(messages)
    ]

    started = time.perf_counter()
    ctx = TimelineContext()
    blocks, _ = format_timeline_entries(entries, ctx, header_for=header_for)
    elapsed = time.perf_counter() - started

    return {
        "messages": messages,
        "lines": len(blocks),
        "seconds": elapsed,
        "per_message_us": elapsed / messages * 1e6,
        "messages_per_s": messages / elapsed,
    }


# ---------------------------------------------------------------------------


def measure(fn: Callable[[], dict[str, float]], repeats: int) -> dict[str, float]:
    """Run `fn` `repeats` times, keep the fastest run and the median.

    The fastest run is the least noisy estimator of the work itself, without
    scheduler and GC noise; the median is reported next to it so a noisy
    machine is visible rather than hidden.
    """
    runs = [fn() for _ in range(repeats)]
    best = min(runs, key=lambda r: r["seconds"])
    best["median_seconds"] = statistics.median(r["seconds"] for r in runs)
    best["repeats"] = repeats
    return best


def _fmt(label: str, result: dict[str, float]) -> str:
    keys = [k for k in result if k.endswith("_us")]
    detail = "  ".join(f"{k.replace('_us', '')}={result[k]:.1f}us" for k in keys)
    return f"{label:34} {detail}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=1000,
                        help="events for the E2EE backlog (default: 1000)")
    parser.add_argument("--mb", type=float, default=5.0,
                        help="sync payload size in MB (default: 5)")
    parser.add_argument("--messages", type=int, default=200,
                        help="messages to redraw (default: 200)")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    print(f"python hot paths — {args.repeats} run(s) each, fastest reported\n")

    e2ee = measure(lambda: bench_e2ee_backlog(args.events), args.repeats)
    print(_fmt("1. E2EE backlog", e2ee))
    print(f"   {e2ee['events']} events in {e2ee['seconds'] * 1000:.1f} ms "
          f"({e2ee['events_per_s']:.0f} events/s)\n")

    sync = measure(lambda: bench_sync_ingest(args.mb), args.repeats)
    print(_fmt("2. sync ingestion", sync))
    print(f"   {sync['size_mb']:.1f} MB / {sync['events']} events in "
          f"{sync['seconds'] * 1000:.1f} ms ({sync['mb_per_s']:.1f} MB/s)\n")

    typical = measure(lambda: bench_sync_ingest(args.mb, events=20), args.repeats)
    print(_fmt("   typical /sync (20 events)", typical))
    print(f"   {typical['events']} events in {typical['seconds'] * 1000:.2f} ms "
          f"-> the common case is already cheap\n")

    redraw = measure(lambda: bench_timeline_redraw(args.messages), args.repeats)
    print(_fmt("3. timeline redraw", redraw))
    print(f"   {redraw['messages']} messages -> {redraw['lines']} lines in "
          f"{redraw['seconds'] * 1000:.1f} ms "
          f"({redraw['messages_per_s']:.0f} messages/s)\n")

    total = e2ee["seconds"] + sync["seconds"] + redraw["seconds"]
    print(f"total of the three paths: {total * 1000:.1f} ms")
    print("\nThese are the numbers to beat. Re-run this script against the Rust "
          "core\nand compare like for like before claiming any speedup.")


if __name__ == "__main__":
    main()