"""
bluesky.py -- Thin AT Protocol client for posting to Bluesky.

Every network call to Bluesky lives here. The functions are deliberately small
and translate Bluesky's wire errors into the shared ``ApiError`` taxonomy
(errors.py) so the posting orchestrator never has to know AT Protocol specifics.

Bluesky threading model (spec section 9): a thread is a chain of individual
records, each with a ``reply`` field carrying a ``root`` strong-reference (the
first post in the chain) and a ``parent`` strong-reference (the immediately
previous post). A "strong reference" is a ``{uri, cid}`` pair taken from a
create response.
"""

from __future__ import annotations

import datetime
from typing import Any

import requests

from .errors import ApiError
from .facets import detect_facets


# A generous but bounded timeout. Long enough for a slow PDS, short enough that
# an unreachable host surfaces as "could not reach it" rather than hanging.
_TIMEOUT = 30

# AT Protocol error names that mean the access token needs refreshing. These
# are a normal part of the token lifecycle, not a dead credential, so they map
# to kind="expired" (refresh + retry once) rather than kind="auth".
_EXPIRED_ERRORS = {"ExpiredToken", "InvalidToken"}


def _xrpc_url(service: str, method: str) -> str:
    """Build an XRPC endpoint URL, tolerating a trailing slash on ``service``."""
    return f"{service.rstrip('/')}/xrpc/{method}"


def _raise_for_atproto(resp: requests.Response) -> dict[str, Any]:
    """Return the JSON body on success, or raise a classified ``ApiError``.

    AT Protocol errors are a JSON object with an ``error`` name and a
    ``message`` (spec section 11). We switch on the ``error`` name to pick the
    right ``kind`` so the retry policy and the UI behave correctly.
    """
    if resp.ok:
        return resp.json() if resp.content else {}

    # Try to parse the structured AT Protocol error; fall back to raw text.
    try:
        body = resp.json()
    except ValueError:
        body = {"error": "Unknown", "message": resp.text}

    name = body.get("error", "Unknown")
    message = body.get("message", "") or name

    if name in _EXPIRED_ERRORS:
        # Signal the caller to refresh the session and retry once.
        raise ApiError("expired", "Access token expired.", raw=body, status=resp.status_code)
    if name == "RateLimitExceeded" or resp.status_code == 429:
        raise ApiError(
            "ratelimit",
            "Bluesky is rate limiting; will retry.",
            raw=body,
            status=resp.status_code,
            retry_after=_parse_retry_after(resp),
        )
    if name in ("AuthenticationRequired", "InvalidLogin", "AccountTakedown", "AuthFactorTokenRequired"):
        raise ApiError("auth", f"Authentication failed: {message}", raw=body, status=resp.status_code)
    if name in ("BlobTooLarge", "InvalidBlob") or "blob" in message.lower():
        raise ApiError("oversize", f"Image rejected: {message}", raw=body, status=resp.status_code)
    if resp.status_code >= 500:
        # Server-side wobble on the PDS: treat as unreachable, not the user's fault.
        raise ApiError("unreachable", "Bluesky service error; try again.", raw=body, status=resp.status_code)

    # Anything else is a genuine content rejection.
    raise ApiError("rejected", message, raw=body, status=resp.status_code)


def _parse_retry_after(resp: requests.Response) -> float | None:
    """Read a Retry-After header (seconds) if present, else None."""
    val = resp.headers.get("Retry-After") or resp.headers.get("ratelimit-reset")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """Perform an HTTP request, mapping transport failures to 'unreachable'.

    A DNS failure, refused connection, or timeout is fundamentally different
    from a server saying "no" (spec section 11), so we catch the requests-level
    exceptions here and raise kind="unreachable".
    """
    try:
        return requests.request(method, url, timeout=_TIMEOUT, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise ApiError("unreachable", "Could not reach Bluesky; try again.", raw=str(exc))


# --- Sessions ---------------------------------------------------------------
def create_session(handle: str, app_password: str, service: str = "https://bsky.social") -> dict[str, Any]:
    """Create a session with an app password (spec sections 5 and 9).

    Used both by the wizard's live validation and at the start of each posting
    chain. Returns the parsed body, which includes ``accessJwt``, ``refreshJwt``,
    ``did``, and ``handle``.
    """
    resp = _request(
        "POST",
        _xrpc_url(service, "com.atproto.server.createSession"),
        json={"identifier": handle, "password": app_password},
    )
    # createSession returns 401 for bad credentials; surface that as auth.
    if resp.status_code == 401:
        raise ApiError("auth", "Invalid handle or app password.", raw=_safe_json(resp), status=401)
    return _raise_for_atproto(resp)


def refresh_session(refresh_jwt: str, service: str = "https://bsky.social") -> dict[str, Any]:
    """Exchange a refresh token for a fresh session (spec section 11 retry).

    Called after an ``ExpiredToken`` to get a new ``accessJwt`` before the one
    silent retry. If this itself fails, the caller surfaces "credentials
    expired, re-authenticate".
    """
    resp = _request(
        "POST",
        _xrpc_url(service, "com.atproto.server.refreshSession"),
        headers={"Authorization": f"Bearer {refresh_jwt}"},
    )
    return _raise_for_atproto(resp)


# --- Media ------------------------------------------------------------------
def upload_blob(service: str, access_jwt: str, image_bytes: bytes, mime: str) -> dict[str, Any]:
    """Upload one already-resized image and return its blob reference.

    The blob ref (a ``$link`` object) is embedded into the post record so the
    image displays. Images arrive here already compressed under Bluesky's
    ~976KB ceiling by the client-side resize (spec section 10).
    """
    resp = _request(
        "POST",
        _xrpc_url(service, "com.atproto.repo.uploadBlob"),
        headers={"Authorization": f"Bearer {access_jwt}", "Content-Type": mime},
        data=image_bytes,
    )
    body = _raise_for_atproto(resp)
    return body["blob"]


# --- Records ----------------------------------------------------------------
def create_record(
    service: str,
    access_jwt: str,
    did: str,
    text: str,
    images: list[dict[str, Any]],
    reply: dict[str, Any] | None = None,
    external: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one ``app.bsky.feed.post`` record and return ``{uri, cid}``.

    Parameters
    ----------
    images : list of ``{"blob": <blob ref>, "alt": <str>}``
        Already-uploaded blobs paired with their enforced alt text. Every image
        carries alt text -- posting is blocked earlier if any is missing.
    reply : optional ``{"root": <ref>, "parent": <ref>}``
        Present for every entry past the first in a thread; each ref is a
        ``{uri, cid}`` strong reference from an earlier create response.
    external : optional link-card object ``{uri, title, description, thumb?}``
        A prepared ``app.bsky.embed.external`` payload. Used only for an entry
        with a link but no image (a post has a single embed slot, so images take
        precedence). ``thumb`` is an already-uploaded blob ref when present.
    """
    # createdAt must be an ISO-8601 timestamp with a timezone; Bluesky orders
    # and displays posts by it. UTC "Z" form is what the API expects.
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now,
    }

    # Compute URL link facets so any links in the text are clickable (spec
    # section 9). Empty when the text has no URLs, in which case we omit the key.
    facets = detect_facets(text)
    if facets:
        record["facets"] = facets

    # Attach an embed. A post has exactly one embed slot: images win when
    # present; otherwise a link card (external) is used when one was built.
    if images:
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": [
                {"alt": img["alt"], "image": img["blob"]} for img in images
            ],
        }
    elif external:
        record["embed"] = {"$type": "app.bsky.embed.external", "external": external}

    # Threading: link this post to the chain's root and its immediate parent.
    if reply:
        record["reply"] = reply

    resp = _request(
        "POST",
        _xrpc_url(service, "com.atproto.repo.createRecord"),
        headers={"Authorization": f"Bearer {access_jwt}"},
        json={"repo": did, "collection": "app.bsky.feed.post", "record": record},
    )
    return _raise_for_atproto(resp)


def strong_ref(create_response: dict[str, Any]) -> dict[str, Any]:
    """Extract a ``{uri, cid}`` strong reference from a create response.

    This is the exact shape the ``reply.root`` and ``reply.parent`` fields
    need, so a later entry can point back at an earlier one in the chain.
    """
    return {"uri": create_response["uri"], "cid": create_response["cid"]}


def post_web_url(handle: str, post_uri: str) -> str:
    """Build the public bsky.app URL for a post from its AT-URI.

    The create response returns an ``at://did/app.bsky.feed.post/<rkey>`` URI.
    The human-facing URL is ``https://bsky.app/profile/<handle>/post/<rkey>``,
    which is what the success status links to (spec section 11).
    """
    rkey = post_uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _safe_json(resp: requests.Response) -> Any:
    """Best-effort JSON parse used only when building an error's raw detail."""
    try:
        return resp.json()
    except ValueError:
        return resp.text
