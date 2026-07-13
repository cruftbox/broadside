"""
logstore.py -- Structured, append-only history of every post attempt.

The composer's per-target status is momentary; once the page is gone, so is the
record of what went where. Spec section 11 calls for a durable server-side log
so the operator can spot intermittent patterns (a flaky instance, recurring
rate limits) without reading raw container logs.

Format is JSON Lines (one JSON object per line) on the same volume as the
config. Append-only writing is cheap and crash-safe: a partial final line is
the worst case, and the reader simply skips any line it cannot parse.

Each attempt writes ONE line per target (spec section 11): timestamp, platform,
account, outcome, and -- on failure -- the raw error.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any

from .config import DATA_DIR


LOG_PATH = os.path.join(DATA_DIR, "post_log.jsonl")

# Appends are serialized so interleaved writes from concurrent posts cannot
# corrupt a line. Posting is serial per request, but two browser tabs could
# post at once.
_lock = threading.Lock()


def record(
    platform: str,
    account: str,
    outcome: str,
    entries_posted: int,
    total_entries: int,
    error: dict[str, Any] | None = None,
) -> None:
    """Append one attempt line for a single target.

    Parameters mirror the per-target status the composer shows, plus a UTC
    timestamp. ``outcome`` is one of "ok", "partial", or "failed". ``error`` is
    the translated error dict (from ``ApiError.to_dict``) on failure, else None.
    """
    line = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "platform": platform,
        "account": account,
        "outcome": outcome,
        "entries_posted": entries_posted,
        "total_entries": total_entries,
        "error": error,
    }
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent log lines, newest first.

    Powers the readable history view. Unparseable lines (e.g. a torn final line
    from a crash mid-write) are skipped rather than failing the whole read.
    """
    if not os.path.exists(LOG_PATH):
        return []
    with _lock:
        with open(LOG_PATH, "r", encoding="utf-8") as fh:
            raw_lines = fh.readlines()

    parsed: list[dict[str, Any]] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    # Newest first, capped to the requested limit.
    parsed.reverse()
    return parsed[:limit]
