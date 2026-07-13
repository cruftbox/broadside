"""
updatecheck.py -- Report whether a newer Broadside is available on GitHub.

The running image has its source commit baked in at build time as the
BROADSIDE_VERSION env var (set from the deployed SHA by the Docker build arg,
which update.sh passes through). This module compares that against the latest
commit on the tracked GitHub branch so the UI can offer a one-click update when
the deployment is behind.

The GitHub result is cached briefly so repeated page loads do not burn through
the unauthenticated API rate limit (60 requests/hour per IP). Every failure is
soft: if GitHub can't be reached we simply report "no update known" rather than
erroring, so the composer is never blocked by an update check.
"""

from __future__ import annotations

import os
import threading
import time

import requests


REPO = os.environ.get("BROADSIDE_REPO", "cruftbox/broadside")
BRANCH = os.environ.get("BROADSIDE_BRANCH", "main")
# The commit this image was built from. "unknown" for a local/dev run that did
# not pass the build arg -- in that case we never nag about updates.
CURRENT = os.environ.get("BROADSIDE_VERSION", "unknown")

_CACHE_TTL = 600  # seconds; keeps well under GitHub's 60/hour anonymous limit
_lock = threading.Lock()
_cache: dict[str, object] = {"latest": None, "at": 0.0}


def current_version() -> str:
    """The commit SHA this running image was built from (or 'unknown')."""
    return CURRENT


def latest_version() -> str | None:
    """The latest commit SHA on the tracked branch, cached, or None if unknown.

    On any network/API failure returns the last cached value (possibly None) so
    a transient GitHub hiccup never turns into an error for the caller.
    """
    with _lock:
        if _cache["latest"] and time.time() - float(_cache["at"]) < _CACHE_TTL:
            return _cache["latest"]  # type: ignore[return-value]

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=8,
        )
        if not resp.ok:
            return _cache["latest"]  # type: ignore[return-value]
        sha = resp.json().get("sha")
    except (requests.RequestException, ValueError):
        return _cache["latest"]  # type: ignore[return-value]

    if sha:
        with _lock:
            _cache["latest"] = sha
            _cache["at"] = time.time()
    return sha


def status() -> dict:
    """Return {current, latest, update_available} for the /api/version endpoint.

    ``update_available`` is true only when both SHAs are known and differ, and
    the current version is a real baked SHA (not 'unknown'), so a dev run never
    shows the update banner.
    """
    cur = current_version()
    latest = latest_version()
    available = bool(latest and cur != "unknown" and cur != latest)
    return {"current": cur, "latest": latest, "update_available": available}
