"""
mastodon.py -- Thin Mastodon client for posting and instance discovery.

Every network call to a Mastodon instance lives here, and each translates
Mastodon's HTTP errors into the shared ``ApiError`` taxonomy (errors.py).

Two Mastodon-specific wrinkles the rest of the app relies on this module to
hide:

  * Instance limits are per-instance and must be DISCOVERED, not guessed (spec
    section 8). ``get_instance_limits`` fetches and normalizes them.
  * Media processing can be ASYNCHRONOUS. ``POST /api/v2/media`` may return a
    202 while the instance is still processing the image; the status cannot be
    posted until the media is ready. ``upload_media`` polls until ready or a
    timeout, and a timeout is reported distinctly (kind="media_timeout") because
    it is the Mastodon quirk most likely to confuse (spec sections 9 and 11).

Threading model (spec section 9): Mastodon uses a single ``in_reply_to_id``
parent pointer -- no separate root reference like Bluesky.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .errors import ApiError


_TIMEOUT = 30

# How long to wait for async media processing before giving up, and how often
# to poll while waiting. Bounded so a stuck instance surfaces a clear
# "processing timed out" rather than hanging the whole post.
_MEDIA_POLL_TIMEOUT = 60
_MEDIA_POLL_INTERVAL = 2

# Sensible fallbacks if an instance does not report a given limit. 500 chars is
# the Mastodon default; the image ceiling and attachment count match common
# defaults. Real values overwrite these whenever the instance reports them.
_DEFAULT_LIMITS = {
    "max_characters": 500,
    "max_image_size_bytes": 16_777_216,   # 16 MB, the common default
    "max_attachments": 4,
    "supported_mime_types": ["image/jpeg", "image/png", "image/gif", "image/webp"],
}


def _base(instance_url: str) -> str:
    """Normalize an instance base URL (strip trailing slash)."""
    return instance_url.rstrip("/")


def _auth(token: str) -> dict[str, str]:
    """Bearer auth header for the access token."""
    return {"Authorization": f"Bearer {token}"}


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP request mapping transport failures to kind='unreachable'."""
    try:
        return requests.request(method, url, timeout=_TIMEOUT, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise ApiError("unreachable", "Could not reach the instance; try again.", raw=str(exc))


def _raise_for_mastodon(resp: requests.Response) -> dict[str, Any]:
    """Return JSON on success, or raise a classified ``ApiError``.

    Maps Mastodon's HTTP statuses to kinds per spec section 11:
      401 -> auth (dead/revoked token, route to re-auth)
      422 -> rejected (content problem: text too long, media not ready, ...)
      429 -> ratelimit (honor retry-after, retry once)
      5xx / connection -> unreachable (instance problem, not the user's fault)
    """
    if resp.ok:
        return resp.json() if resp.content else {}

    body = _safe_json(resp)
    # Mastodon error bodies often carry {"error": "...", "error_description": "..."}.
    detail = ""
    if isinstance(body, dict):
        detail = body.get("error_description") or body.get("error") or ""

    if resp.status_code == 401:
        raise ApiError("auth", "Access token is invalid or revoked.", raw=body, status=401)
    if resp.status_code == 422:
        raise ApiError("rejected", detail or "The instance rejected the post.", raw=body, status=422)
    if resp.status_code == 429:
        raise ApiError(
            "ratelimit",
            "The instance is rate limiting; will retry.",
            raw=body,
            status=429,
            retry_after=_parse_retry_after(resp),
        )
    if resp.status_code >= 500:
        raise ApiError("unreachable", "Instance server error; try again.", raw=body, status=resp.status_code)

    raise ApiError("rejected", detail or f"Request failed ({resp.status_code}).", raw=body, status=resp.status_code)


def _parse_retry_after(resp: requests.Response) -> float | None:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


# --- Setup-time calls -------------------------------------------------------
def verify_credentials(instance_url: str, token: str) -> dict[str, Any]:
    """Validate a token and return the account (spec section 5).

    On success the caller caches ``@user@instance`` as the display name.
    """
    resp = _request(
        "GET",
        f"{_base(instance_url)}/api/v1/accounts/verify_credentials",
        headers=_auth(token),
    )
    return _raise_for_mastodon(resp)


def account_display_name(instance_url: str, account: dict[str, Any]) -> str:
    """Build the ``@user@instance`` display string from a verified account.

    ``account['acct']`` is bare (``user``) for local accounts, so we append the
    instance host to get the fully-qualified form used in the UI.
    """
    acct = account.get("acct", account.get("username", "unknown"))
    host = _base(instance_url).split("://", 1)[-1]
    return f"@{acct}@{host}" if "@" not in acct else f"@{acct}"


def get_instance_limits(instance_url: str, token: str) -> dict[str, Any]:
    """Discover and normalize an instance's real limits (spec section 8).

    Prefers the v2 instance endpoint (where limits live under
    ``configuration``) and falls back to v1. Any limit the instance does not
    report keeps its sensible default. Called right after a token validates and
    again whenever the account is re-validated.
    """
    limits = dict(_DEFAULT_LIMITS)

    # Try v2 first; it is the current shape. Fall back to v1 on older instances.
    body: dict[str, Any] | None = None
    for path in ("/api/v2/instance", "/api/v1/instance"):
        resp = _request("GET", f"{_base(instance_url)}{path}", headers=_auth(token))
        if resp.ok:
            body = resp.json()
            break
    if not body:
        # Could not read limits at all; return defaults rather than failing the
        # whole account -- posting still works, just with conservative limits.
        return limits

    config = body.get("configuration", {}) or {}
    statuses = config.get("statuses", {}) or {}
    media = config.get("media_attachments", {}) or {}

    if isinstance(statuses.get("max_characters"), int):
        limits["max_characters"] = statuses["max_characters"]
    if isinstance(statuses.get("max_media_attachments"), int):
        limits["max_attachments"] = statuses["max_media_attachments"]
    if isinstance(media.get("image_size_limit"), int):
        limits["max_image_size_bytes"] = media["image_size_limit"]
    if isinstance(media.get("supported_mime_types"), list) and media["supported_mime_types"]:
        limits["supported_mime_types"] = media["supported_mime_types"]

    return limits


# --- Posting ----------------------------------------------------------------
def upload_media(instance_url: str, token: str, image_bytes: bytes, mime: str, description: str) -> str:
    """Upload one image with alt text; return its media id once ready.

    Uses ``POST /api/v2/media``. A 200 means the media is ready immediately; a
    202 means the instance is processing asynchronously, so we poll
    ``GET /api/v1/media/:id`` until it is ready or the timeout hits. A timeout
    raises kind="media_timeout" so the UI can say "image processing timed out on
    <instance>" rather than a generic failure (spec section 11).

    The alt text rides in the ``description`` field -- this is how enforced alt
    text reaches Mastodon.
    """
    resp = _request(
        "POST",
        f"{_base(instance_url)}/api/v2/media",
        headers=_auth(token),
        files={"file": ("image", image_bytes, mime)},
        data={"description": description},
    )

    if resp.status_code == 200:
        # Ready immediately.
        return _raise_for_mastodon(resp)["id"]

    if resp.status_code == 202:
        # Processing asynchronously: capture the id and poll until ready.
        media_id = resp.json()["id"]
        return _poll_media_ready(instance_url, token, media_id)

    # Any other status is a real error (401/422/429/5xx handled by the mapper).
    return _raise_for_mastodon(resp)["id"]


def _poll_media_ready(instance_url: str, token: str, media_id: str) -> str:
    """Poll a processing media attachment until it is ready or times out."""
    deadline = time.monotonic() + _MEDIA_POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_MEDIA_POLL_INTERVAL)
        resp = _request(
            "GET",
            f"{_base(instance_url)}/api/v1/media/{media_id}",
            headers=_auth(token),
        )
        # 200 => processing finished and a URL is available; 206 => still working.
        if resp.status_code == 200:
            return media_id
        if resp.status_code >= 400:
            _raise_for_mastodon(resp)  # surfaces the real error and stops here
    raise ApiError(
        "media_timeout",
        f"Image processing timed out on {_base(instance_url).split('://', 1)[-1]}.",
        raw={"media_id": media_id},
    )


def post_status(
    instance_url: str,
    token: str,
    status_text: str,
    media_ids: list[str],
    in_reply_to_id: str | None = None,
) -> dict[str, Any]:
    """Post one status and return its record (including ``id`` and ``url``).

    ``in_reply_to_id`` is set for every entry past the first in a thread, making
    this status a reply to this account's previous status (spec section 9).
    """
    payload: dict[str, Any] = {"status": status_text}
    if media_ids:
        payload["media_ids[]"] = media_ids
    if in_reply_to_id:
        payload["in_reply_to_id"] = in_reply_to_id

    resp = _request(
        "POST",
        f"{_base(instance_url)}/api/v1/statuses",
        headers=_auth(token),
        data=payload,
    )
    return _raise_for_mastodon(resp)


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
