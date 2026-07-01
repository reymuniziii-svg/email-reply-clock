#!/usr/bin/env python3
"""Build data/reply_clock.json from local Thunderbird mail.

Thunderbird stores each folder as an mbox file (no extension) somewhere under a
profile directory, e.g.

    ~/.thunderbird/<profile>.default/Mail/<account>/Inbox
    ~/.thunderbird/<profile>.default/ImapMail/<account>/Sent
    ~/.thunderbird/<profile>.default/ImapMail/<account>/Archives.sbd/2025

This script walks those mbox files, indexes every message by Message-ID, then
finds the replies *you* sent (a message authored by you that carries an
In-Reply-To / References header pointing at a message we have). For each reply
it records:

    hour_of_day     decimal hour 0-24 the reply was sent (in the Date header's
                    own timezone, i.e. your local clock when you hit send)
    latency_minutes minutes between the parent arriving and your reply
    day_index       relative day offset from your first reply (animation order)

and emits the exact same JSON shape app.js / render_video.py already consume.
Pure standard library; no names, addresses, subjects or bodies are written out.

Usage:
    python3 thunderbird_import.py                         # auto-find profile
    python3 thunderbird_import.py --profile ~/.thunderbird/abc.default
    python3 thunderbird_import.py --me me@example.com --me old@example.com
    python3 thunderbird_import.py --max-days 30 -o data/reply_clock.json
"""

from __future__ import annotations

import argparse
import glob
import mailbox
import math
import os
import sys
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime

# Visualization rings (latency_minutes, label) — fixed, matches the original.
RINGS = [[1, "1 min"], [10, "10 min"], [60, "1 hour"], [1440, "1 day"], [10080, "1 week"]]
CAP_MINUTES = 10080  # 7 days; used by the radius log-scale in the renderer

# Folder names (any case, several languages) that imply "messages I sent".
SENT_HINTS = ("sent", "envoy", "gesendet", "enviado", "inviat", "verzonden")


def find_default_profile() -> str | None:
    """Return a likely Thunderbird profile directory, or None."""
    roots = [
        os.path.expanduser("~/.thunderbird"),
        os.path.expanduser("~/.mozilla-thunderbird"),
        os.path.expanduser("~/Library/Thunderbird/Profiles"),
        os.path.expanduser("~/AppData/Roaming/Thunderbird/Profiles"),
    ]
    candidates: list[str] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and (
                "default" in name.lower()
                or os.path.isdir(os.path.join(p, "Mail"))
                or os.path.isdir(os.path.join(p, "ImapMail"))
            ):
                candidates.append(p)
    # Prefer a profile that actually has a mail store.
    candidates.sort(
        key=lambda p: os.path.isdir(os.path.join(p, "ImapMail"))
        or os.path.isdir(os.path.join(p, "Mail")),
        reverse=True,
    )
    return candidates[0] if candidates else None


def looks_like_mbox(path: str) -> bool:
    """True if `path` is a Thunderbird mbox file (not an index/metadata file)."""
    if not os.path.isfile(path):
        return False
    base = os.path.basename(path)
    if base.startswith("."):
        return False
    # Skip Thunderbird sidecar/index files and obvious non-mail files.
    skip_ext = (".msf", ".dat", ".sqlite", ".json", ".html", ".log", ".ini", ".txt")
    if base.lower().endswith(skip_ext):
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
        return head[:5] == b"From "
    except OSError:
        return False


def discover_mboxes(profile: str) -> list[str]:
    """Find every mbox file under a Thunderbird profile's mail stores."""
    found: list[str] = []
    search_dirs = []
    for sub in ("Mail", "ImapMail"):
        d = os.path.join(profile, sub)
        if os.path.isdir(d):
            search_dirs.append(d)
    # If the path given *is* a mail store or a single mbox, handle that too.
    if not search_dirs:
        if os.path.isfile(profile):
            return [profile] if looks_like_mbox(profile) else []
        search_dirs = [profile]
    for d in search_dirs:
        for path in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if looks_like_mbox(path):
                found.append(path)
    return sorted(set(found))


def is_sent_folder(path: str) -> bool:
    """Heuristic: does this mbox live in a 'Sent'-like folder?"""
    low = os.path.basename(path).lower()
    return any(h in low for h in SENT_HINTS)


def parse_date(msg) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return dt  # keep tz-aware; we compare in absolute UTC and read local hour


def first_reference(msg) -> str | None:
    """The Message-ID this message is replying to."""
    irt = msg.get("In-Reply-To")
    if irt:
        ids = irt.split()
        if ids:
            return ids[0].strip()
    refs = msg.get("References")
    if refs:
        ids = refs.split()
        if ids:
            return ids[-1].strip()  # last reference == the immediate parent
    return None


def from_addresses(msg) -> set[str]:
    return {
        addr.lower()
        for _, addr in getaddresses(msg.get_all("From", []))
        if addr
    }


def build(args) -> dict:
    me = {a.lower() for a in (args.me or [])}
    mboxes = discover_mboxes(args.profile)
    if not mboxes:
        sys.exit(f"No mbox files found under: {args.profile}")
    print(f"Scanning {len(mboxes)} mbox folder(s)…", file=sys.stderr)

    # Pass 1: index every message by Message-ID -> earliest send/receive date.
    # Also stash candidate replies so we don't parse everything twice.
    msg_date: dict[str, datetime] = {}
    candidates: list[tuple[datetime, str]] = []  # (reply_dt, parent_msgid)
    scanned = 0

    for path in mboxes:
        sent_like = is_sent_folder(path)
        try:
            box = mailbox.mbox(path, create=False)
        except Exception as exc:  # noqa: BLE001 - skip unreadable folders
            print(f"  skip {path}: {exc}", file=sys.stderr)
            continue
        for msg in box:
            scanned += 1
            mid = msg.get("Message-ID")
            dt = parse_date(msg)
            if mid:
                mid = mid.strip()
                if dt and (mid not in msg_date or dt < msg_date[mid]):
                    msg_date[mid] = dt
            if dt is None:
                continue
            parent = first_reference(msg)
            if not parent:
                continue
            # Is this a reply *I* authored? Either From matches one of my
            # addresses, or it lives in a Sent-style folder.
            mine = sent_like or (bool(me) and bool(from_addresses(msg) & me))
            if mine:
                candidates.append((dt, parent))
        box.close()

    print(f"  {scanned} messages indexed, {len(candidates)} candidate replies",
          file=sys.stderr)

    # Pass 2: resolve each reply against its parent's date.
    replies: list[tuple[datetime, float]] = []  # (reply_dt, latency_minutes)
    no_parent = skipped = 0
    cap_max = args.max_days * 1440 if args.max_days else None
    for reply_dt, parent in candidates:
        pdt = msg_date.get(parent)
        if pdt is None:
            no_parent += 1
            continue
        latency = (reply_dt - pdt).total_seconds() / 60.0
        if latency <= 0:  # clock skew, or reply we matched to itself
            skipped += 1
            continue
        if cap_max is not None and latency > cap_max:
            skipped += 1
            continue
        replies.append((reply_dt, round(latency, 1)))

    if not replies:
        sys.exit(
            "Found 0 usable replies. Try passing --me <your address(es)> so "
            "replies can be identified, or point --profile at the right profile."
        )

    replies.sort(key=lambda r: r[0])
    first_day = replies[0][0].date()

    rows: list[list] = []
    hours: list[float] = []
    latencies: list[float] = []
    year_boundaries: dict[str, int] = {}
    for reply_dt, latency in replies:
        # decimal local hour from the Date header's own timezone
        hour = reply_dt.hour + reply_dt.minute / 60.0 + reply_dt.second / 3600.0
        day_index = (reply_dt.date() - first_day).days
        rows.append([round(hour, 2), latency, day_index])
        hours.append(hour)
        latencies.append(latency)
        year = str(reply_dt.year)
        if year not in year_boundaries:
            year_boundaries[year] = day_index

    n = len(rows)
    latencies_sorted = sorted(latencies)
    median = latencies_sorted[n // 2] if n % 2 else (
        (latencies_sorted[n // 2 - 1] + latencies_sorted[n // 2]) / 2
    )
    mean = sum(latencies) / n
    within_hour = sum(1 for x in latencies if x <= 60) / n * 100
    under5 = sum(1 for x in latencies if x <= 5) / n * 100
    archive_day_count = rows[-1][2]
    mean_hour = sum(hours) / n

    print(
        f"  {n} replies kept "
        f"({no_parent} parent unknown, {skipped} skipped)",
        file=sys.stderr,
    )

    return {
        "meta": {
            "total": n,
            "median_minutes": round(median, 1),
            "mean_minutes": round(mean, 1),
            "within_hour_pct": round(within_hour, 1),
            "under5_pct": round(under5, 1),
            "archive_day_count": archive_day_count,
            "mean_hour": round(mean_hour, 2),
            "year_boundaries": year_boundaries,
            "rings": RINGS,
            "cap_minutes": CAP_MINUTES,
        },
        "keys": ["hour", "latency_minutes", "day_index"],
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", help="Thunderbird profile dir (auto-detected if omitted)")
    ap.add_argument("--me", action="append", metavar="ADDRESS",
                    help="An email address you send from (repeatable). "
                         "If omitted, replies are taken from Sent-style folders.")
    ap.add_argument("--max-days", type=float, default=None,
                    help="Drop replies whose latency exceeds this many days "
                         "(filters auto-replies to ancient threads).")
    ap.add_argument("-o", "--output", default=None,
                    help="Output JSON path (default: data/reply_clock.json next to this script)")
    args = ap.parse_args()

    if not args.profile:
        args.profile = find_default_profile()
        if not args.profile:
            sys.exit("Could not auto-detect a Thunderbird profile; pass --profile.")
        print(f"Using profile: {args.profile}", file=sys.stderr)

    if not args.output:
        here = os.path.dirname(os.path.abspath(__file__))
        args.output = os.path.join(here, "data", "reply_clock.json")

    payload = build(args)

    import json
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    m = payload["meta"]
    print(
        f"Wrote {args.output}: {m['total']} replies, "
        f"median {m['median_minutes']} min, {m['within_hour_pct']}% within the hour.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
