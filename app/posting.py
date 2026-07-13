"""
posting.py -- Fan-out orchestration: turn one composed post into many.

This module owns the model in spec section 9. A post is an ordered list of
entries (text + images with alt text). Posting fans the whole sequence out to
each selected account, where each account maintains its OWN independent
self-reply chain.

Key rules implemented here, all from spec section 9:

  * Serial across accounts. Accounts are posted one at a time, in config order,
    never concurrently. Nothing is time-critical and serial keeps the logic
    simple and rate-limit exposure low.
  * Independent chains. Account A's entry 2 replies to Account A's entry 1,
    never to any other account's post.
  * Stop-on-failure. If an entry fails partway through a chain, that chain
    stops there (a later entry had nothing to reply to). Other accounts are
    unaffected and continue.
  * Retry policy (spec section 11). Exactly one silent retry for transient
    failures only: an expired Bluesky token (refresh + retry), a network blip,
    or a 429 (after honoring retry-after). Everything else is reported at once.

The output is a list of per-target result dicts -- one per selected account --
that the composer renders as per-target status lines, and that logstore records.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Callable

from . import bluesky, logstore, mastodon
from .config import find_account
from .errors import ApiError


# Upper bound on how long we will sleep honoring a rate-limit Retry-After before
# the single retry. Prevents a hostile/misconfigured header from stalling the
# whole post for minutes.
_MAX_RETRY_AFTER = 30


def _decode_image(image: dict[str, Any]) -> tuple[bytes, str, str]:
    """Turn one client image payload into (bytes, mime, alt).

    The browser sends each already-resized image as base64 plus its mime and
    enforced alt text. We validate alt text presence again here as defense in
    depth: the client blocks empty alt text, but the server must never post an
    image without it (spec section 10), regardless of what the client sent.
    """
    alt = (image.get("alt") or "").strip()
    if not alt:
        # A hard block, matching the client. This should be unreachable via the
        # UI, but the enforcement is a primary reason the app exists.
        raise ApiError("rejected", "Every image must have alt text.", raw=image.get("mime"))
    data = image.get("data", "")
    # The client may send a bare base64 string or a full data URL; handle both.
    if "," in data and data.strip().startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    return raw, image.get("mime", "image/jpeg"), alt


def _sleep_for_retry(err: ApiError) -> None:
    """Honor a rate-limit Retry-After (bounded) before the single retry."""
    if err.kind == "ratelimit" and err.retry_after:
        time.sleep(min(err.retry_after, _MAX_RETRY_AFTER))


def _attempt(action: Callable[[], Any], on_expired: Callable[[], None] | None = None) -> Any:
    """Run ``action`` with the spec's exactly-once retry policy.

    ``action`` performs one platform operation and may raise ``ApiError``.
    Transient errors get a single silent retry:

      * expired  -> call ``on_expired`` (refresh the Bluesky session) then retry.
      * ratelimit-> sleep for retry-after, then retry.
      * unreachable -> retry immediately.

    Any non-transient error, or a second failure, propagates to the caller,
    which stops that account's chain. A retried-unchanged 422 would just fail
    again and waste the user's time, so it is never retried (spec section 11).
    """
    try:
        return action()
    except ApiError as first:
        if not first.is_transient:
            raise
        # Prepare for the one retry based on the kind of transient failure.
        if first.kind == "expired":
            if on_expired is None:
                # No way to refresh (e.g. this op has no session to renew):
                # treat as a normal auth failure so the user re-authenticates.
                raise ApiError("auth", "Credentials expired; re-authenticate.", raw=first.raw)
            on_expired()
        else:
            _sleep_for_retry(first)
        # The single retry. If THIS fails, let it propagate unretried.
        return action()


# --- Bluesky chain ----------------------------------------------------------
def _post_bluesky_chain(account: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Post the full entry sequence to one Bluesky account as a reply chain."""
    service = account.get("service", "https://bsky.social")
    handle = account["handle"]

    # Session state is mutable so the expired-token retry can swap in a fresh
    # accessJwt mid-chain without unwinding progress.
    session = {"access": None, "refresh": None, "did": account.get("did")}

    def open_session() -> None:
        s = bluesky.create_session(handle, account["app_password"], service)
        session["access"] = s["accessJwt"]
        session["refresh"] = s["refreshJwt"]
        session["did"] = s["did"]

    def refresh() -> None:
        # Called by the retry policy on ExpiredToken: get a new accessJwt.
        s = bluesky.refresh_session(session["refresh"], service)
        session["access"] = s["accessJwt"]
        session["refresh"] = s.get("refreshJwt", session["refresh"])

    result = _new_result(account, "bluesky", len(entries))

    try:
        # Establishing the session is itself an attempt subject to retry (a
        # network blip here should not doom the whole account).
        _attempt(open_session)
    except ApiError as err:
        # Could not even start: whole chain fails at entry 0.
        return _finish_failed(result, account, "bluesky", err, entries_posted=0)

    root_ref = None
    parent_ref = None

    for index, entry in enumerate(entries):
        try:
            # Upload this entry's images first (each retried independently).
            images = []
            for img in entry.get("images", []):
                raw, mime, alt = _decode_image(img)
                blob = _attempt(
                    lambda raw=raw, mime=mime: bluesky.upload_blob(service, session["access"], raw, mime),
                    on_expired=refresh,
                )
                images.append({"blob": blob, "alt": alt})

            # Build the reply refs for entries past the first (spec section 9).
            reply = None
            if parent_ref is not None:
                reply = {"root": root_ref, "parent": parent_ref}

            created = _attempt(
                lambda images=images, reply=reply: bluesky.create_record(
                    service, session["access"], session["did"], entry.get("text", ""), images, reply
                ),
                on_expired=refresh,
            )

            # Advance the chain: first post becomes the root; every post becomes
            # the next parent.
            ref = bluesky.strong_ref(created)
            if root_ref is None:
                root_ref = ref
            parent_ref = ref

            result["posts"].append(
                {"index": index, "url": bluesky.post_web_url(handle, created["uri"])}
            )

        except ApiError as err:
            # Stop this chain here; a later entry had nothing to reply to.
            return _finish_failed(result, account, "bluesky", err, entries_posted=index)

    return _finish_ok(result, account, "bluesky")


# --- Mastodon chain ---------------------------------------------------------
def _post_mastodon_chain(account: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Post the full entry sequence to one Mastodon account as a reply chain."""
    instance = account["instance_url"]
    token = account["access_token"]

    result = _new_result(account, "mastodon", len(entries))
    prev_status_id = None

    for index, entry in enumerate(entries):
        try:
            # Upload media with alt text (in the description field). v2 media may
            # process asynchronously; the client polls until ready internally.
            media_ids = []
            for img in entry.get("images", []):
                raw, mime, alt = _decode_image(img)
                media_id = _attempt(
                    lambda raw=raw, mime=mime, alt=alt: mastodon.upload_media(instance, token, raw, mime, alt)
                )
                media_ids.append(media_id)

            created = _attempt(
                lambda media_ids=media_ids: mastodon.post_status(
                    instance, token, entry.get("text", ""), media_ids, prev_status_id
                )
            )
            # Mastodon uses a single parent pointer: the next entry replies here.
            prev_status_id = created["id"]
            result["posts"].append({"index": index, "url": created.get("url")})

        except ApiError as err:
            return _finish_failed(result, account, "mastodon", err, entries_posted=index)

    return _finish_ok(result, account, "mastodon")


# --- Result assembly + logging ---------------------------------------------
def _new_result(account: dict[str, Any], platform: str, total: int) -> dict[str, Any]:
    """Start a per-target result record for one account."""
    return {
        "account_id": account["id"],
        "platform": platform,
        "display_name": account.get("display_name", account.get("handle")),
        "total_entries": total,
        "posts": [],       # filled with {index, url} as each entry succeeds
        "status": None,    # "ok" | "partial" | "failed"
        "error": None,     # translated error dict on failure
    }


def _finish_ok(result: dict[str, Any], account: dict[str, Any], platform: str) -> dict[str, Any]:
    """Mark a fully-successful chain and log it."""
    result["status"] = "ok"
    logstore.record(
        platform, result["display_name"], "ok",
        entries_posted=len(result["posts"]), total_entries=result["total_entries"],
    )
    return result


def _finish_failed(
    result: dict[str, Any],
    account: dict[str, Any],
    platform: str,
    err: ApiError,
    entries_posted: int,
) -> dict[str, Any]:
    """Mark a stopped chain (fully or partially) and log it.

    "partial" means some entries in a thread posted before the failure (spec
    section 9: "posted entries 1 to 2, failed on entry 3"). "failed" means
    nothing posted. ``is_auth`` is surfaced so the UI can offer the one-click
    re-auth path (spec section 11).
    """
    result["status"] = "partial" if entries_posted > 0 else "failed"
    result["error"] = err.to_dict()
    result["error"]["is_auth"] = err.is_auth
    result["failed_on_entry"] = entries_posted  # zero-based index of the failed entry
    logstore.record(
        platform, result["display_name"], result["status"],
        entries_posted=entries_posted, total_entries=result["total_entries"],
        error=result["error"],
    )
    return result


# --- Public entry point -----------------------------------------------------
def post_all(cfg: dict[str, Any], account_ids: list[str], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fan a composed post out to every selected account, serially.

    Returns one result dict per selected account, in the order the accounts were
    selected. Each account is fully independent: one account failing never stops
    another (spec section 9). This is the single function the HTTP layer calls.
    """
    results: list[dict[str, Any]] = []
    for account_id in account_ids:
        platform, account = find_account(cfg, account_id)
        if account is None:
            # A selected id that no longer exists (edited away in another tab).
            results.append(
                {
                    "account_id": account_id,
                    "platform": "unknown",
                    "display_name": account_id,
                    "total_entries": len(entries),
                    "posts": [],
                    "status": "failed",
                    "error": {"kind": "rejected", "message": "Account not found.", "raw": None, "is_auth": False},
                }
            )
            continue

        if platform == "bluesky":
            results.append(_post_bluesky_chain(account, entries))
        else:
            results.append(_post_mastodon_chain(account, entries))

    return results
