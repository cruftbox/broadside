"""
config.py -- Load, persist, and mutate Broadside's account configuration.

Broadside keeps everything it needs in a single JSON file on a mounted Docker
volume so the configuration survives container restarts (spec sections 2 and
4). This module is the ONLY place that reads or writes that file. Concentrating
all config access here is what makes the "credentials live server-side" rule
(spec section 3) enforceable in one auditable location.

Two shapes of data leave this module:

  * The FULL config, including secrets (app passwords, access tokens), is used
    internally by the posting logic (posting.py), which runs server-side where
    it is allowed to read credentials.
  * A SANITIZED "public" view (see ``public_view``) is the only thing the
    browser is ever allowed to receive. It omits ``app_password`` and
    ``access_token`` entirely, so raw credentials are never returned to the
    client after setup.

All mutations go through a module-level lock so that two concurrent requests
cannot clobber each other's writes (read-modify-write races). Writes are
atomic: we write a temp file and ``os.replace`` it into place, so a crash
mid-write can never leave a half-written config on the volume.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any


# --- Where the config lives -------------------------------------------------
#
# The directory is configurable via the BROADSIDE_DATA_DIR environment variable
# so the same code runs both in the container (where the volume is mounted at
# /data) and in local development (where we point it at ./data). Everything
# Broadside persists -- config and the post log -- lives under this one dir.
DATA_DIR = os.environ.get("BROADSIDE_DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# Restrictive permissions for the config file: owner read/write only (0600).
# On Linux (the container) this actually protects the secrets on the volume.
# On Windows (local dev) chmod is largely a no-op, which is harmless.
_CONFIG_MODE = 0o600

# Serialize all read-modify-write sequences. Flask may serve requests on
# multiple threads, and two account edits landing at once must not race.
_lock = threading.RLock()


# --- The empty/default config shape ----------------------------------------
def _empty_config() -> dict[str, Any]:
    """Return the structure of a brand-new, unconfigured Broadside install."""
    return {"bluesky_accounts": [], "mastodon_accounts": []}


def load_config() -> dict[str, Any]:
    """Read and return the full config (including secrets).

    If the file does not exist yet (first run), return an empty structure
    rather than raising, so first-run detection (spec section 5) is a simple
    "are there any accounts?" check against a valid dict.
    """
    with _lock:
        if not os.path.exists(CONFIG_PATH):
            return _empty_config()
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Defend against an older/partial file missing a top-level key.
        base = _empty_config()
        base.update(data)
        return base


def save_config(cfg: dict[str, Any]) -> None:
    """Persist the full config atomically with restrictive permissions.

    We write to a temporary file in the same directory and then atomically
    replace the real file. Same-directory replace guarantees the rename is
    atomic on POSIX filesystems, so readers see either the old or the new file,
    never a truncated one.
    """
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = CONFIG_PATH + ".tmp"
        # Open with the restrictive mode from the start so the secrets are
        # never briefly world-readable between creation and chmod.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _CONFIG_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, ensure_ascii=False)
        except Exception:
            # Don't leave a stray temp file behind if serialization failed.
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        os.replace(tmp_path, CONFIG_PATH)
        # Re-assert the mode on the final path in case the OS umask altered it.
        try:
            os.chmod(CONFIG_PATH, _CONFIG_MODE)
        except OSError:
            # Non-fatal on platforms (Windows) where chmod is limited.
            pass


def is_configured() -> bool:
    """True once at least one account of either platform exists.

    Drives first-run routing (spec section 5): no accounts -> send the user to
    the wizard; any account -> the composer is usable.
    """
    cfg = load_config()
    return bool(cfg["bluesky_accounts"]) or bool(cfg["mastodon_accounts"])


# --- The sanitized view the browser is allowed to see ----------------------
def public_view(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of the config with every secret stripped out.

    This is what /api/config serves. It carries exactly what the composer and
    wizard need to render -- ids, display names, service/instance hosts, and
    the cached limits -- and NOTHING that could repost on the user's behalf.
    The app password and access token never appear here (spec section 3).
    """
    if cfg is None:
        cfg = load_config()

    bsky = [
        {
            "id": a["id"],
            "platform": "bluesky",
            "handle": a.get("handle"),
            "service": a.get("service"),
            "display_name": a.get("display_name", a.get("handle")),
            # Bluesky limits are fixed and known (spec section 8), surfaced here
            # so the client's binding-limit math treats both platforms uniformly.
            "limits": BLUESKY_LIMITS,
        }
        for a in cfg["bluesky_accounts"]
    ]
    masto = [
        {
            "id": a["id"],
            "platform": "mastodon",
            "instance_url": a.get("instance_url"),
            "display_name": a.get("display_name"),
            "limits": a.get("limits", {}),
        }
        for a in cfg["mastodon_accounts"]
    ]
    return {"bluesky_accounts": bsky, "mastodon_accounts": masto}


# Bluesky's limits are fixed and documented (spec section 8): 300 graphemes,
# up to 4 images, and an image blob ceiling of ~976KB (1,000,000 bytes). We
# express them in the same shape as the discovered Mastodon limits so the
# selection-driven "minimum across all selected accounts" logic (spec section
# 7) can treat every account identically.
BLUESKY_LIMITS = {
    "max_characters": 300,
    "max_image_size_bytes": 1_000_000,
    "max_attachments": 4,
    "supported_mime_types": ["image/jpeg", "image/png", "image/webp", "image/gif"],
}


# --- Account lookup and mutation helpers -----------------------------------
def new_account_id(platform: str) -> str:
    """Mint a stable, unique internal id for a new account.

    The id (e.g. ``bsky-a1b2c3``) is what selection and per-target status
    reporting key on (spec section 4), so it must never change once assigned
    and must be unique across a platform.
    """
    prefix = "bsky" if platform == "bluesky" else "masto"
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _account_list_key(platform: str) -> str:
    return "bluesky_accounts" if platform == "bluesky" else "mastodon_accounts"


def find_account(cfg: dict[str, Any], account_id: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    """Locate an account by id across both platforms.

    Returns ``(platform, account_dict)`` or ``(None, None)`` if not found.
    Callers get the platform for free so they don't have to re-derive it.
    """
    for a in cfg["bluesky_accounts"]:
        if a["id"] == account_id:
            return "bluesky", a
    for a in cfg["mastodon_accounts"]:
        if a["id"] == account_id:
            return "mastodon", a
    return None, None


def upsert_account(platform: str, account: dict[str, Any]) -> dict[str, Any]:
    """Insert a new account or replace an existing one (matched by id).

    Used by the wizard after a live validation succeeds (spec section 5). The
    whole read-modify-write runs under the lock so concurrent edits are safe.
    Returns the saved account.
    """
    key = _account_list_key(platform)
    with _lock:
        cfg = load_config()
        for i, existing in enumerate(cfg[key]):
            if existing["id"] == account["id"]:
                cfg[key][i] = account
                break
        else:
            cfg[key].append(account)
        save_config(cfg)
    return account


def remove_account(account_id: str) -> bool:
    """Delete an account by id. Returns True if something was removed."""
    with _lock:
        cfg = load_config()
        for key in ("bluesky_accounts", "mastodon_accounts"):
            before = len(cfg[key])
            cfg[key] = [a for a in cfg[key] if a["id"] != account_id]
            if len(cfg[key]) != before:
                save_config(cfg)
                return True
    return False
