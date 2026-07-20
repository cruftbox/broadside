"""
facets.py -- Compute Bluesky URL link facets so links are clickable.

Bluesky does not auto-linkify anything. To make a URL clickable, the post
record must carry a ``facets`` array where each facet annotates a byte range of
the text and attaches a link feature (spec sections 1 and 9). Mastodon needs
none of this -- it linkifies URLs server-side on its own -- so this module is
Bluesky-only.

THE CRITICAL DETAIL (spec section 9): facet offsets are measured in UTF-8
BYTES, not JavaScript string indices and not Python character indices. If we
computed offsets from ``len(text[:i])`` (a character count) any post containing
an emoji or accented character *before* a URL would highlight the wrong span.
So every offset here is derived from the UTF-8 encoding of the text.

Scope is deliberately limited to URLs (spec section 1). ``@handle`` mentions and
``#hashtags`` are left as plain text -- resolving a mention would require a
per-mention identity lookup, which we chose not to build.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


# Match either an explicit http(s) URL or a bare domain (e.g. "example.com/x").
# The leading group captures the character before the URL (start of string,
# whitespace, or an opening paren) so we never match a URL glued to the middle
# of a word. This mirrors the detection Bluesky's own docs recommend.
_URL_RE = re.compile(
    r"(?P<pre>^|[\s(])"
    r"(?P<url>"
    r"https?://[^\s]+"                                  # explicit scheme
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"          # or a bare domain:
    r"[a-z]{2,}(?:/[^\s]*)?"                            #   labels + TLD + path
    r")",
    re.IGNORECASE,
)

# Punctuation that commonly trails a URL in prose ("see example.com.") and
# should NOT be part of the link. We trim these from the end of a match.
_TRAILING = ".,;:!?\"')]}>"


def _byte_len(s: str) -> int:
    """Length of ``s`` in UTF-8 bytes -- the unit Bluesky facet offsets use."""
    return len(s.encode("utf-8"))


def detect_facets(text: str) -> list[dict[str, Any]]:
    """Return the ``facets`` list for ``text``, or ``[]`` if it has no URLs.

    Each returned facet has the shape Bluesky expects::

        {
          "index": {"byteStart": <int>, "byteEnd": <int>},
          "features": [
            {"$type": "app.bsky.richtext.facet#link", "uri": "<full url>"}
          ]
        }

    ``index`` points at the VISIBLE text span (so the highlighted, clickable
    text is exactly what the user typed). ``uri`` is the link target, which may
    differ from the visible text: a bare domain like "example.com" is shown as
    typed but linked as "https://example.com".
    """
    facets: list[dict[str, Any]] = []

    for m in _URL_RE.finditer(text):
        # The character offset where the URL itself starts (after the captured
        # leading whitespace/paren, which is not part of the link).
        char_start = m.start("url")
        url_text = m.group("url")

        # Trim trailing prose punctuation so "example.com." links "example.com".
        while url_text and url_text[-1] in _TRAILING:
            url_text = url_text[:-1]
        if not url_text:
            continue

        # A bare domain gets an https:// scheme for the link target, while the
        # visible span (and thus the byte offsets) stays exactly as typed.
        uri = url_text if url_text.lower().startswith(("http://", "https://")) else "https://" + url_text

        # Convert character offsets to UTF-8 BYTE offsets. This is the whole
        # point of the module: everything before the URL is measured as bytes.
        byte_start = _byte_len(text[:char_start])
        byte_end = byte_start + _byte_len(url_text)

        facets.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [
                    {"$type": "app.bsky.richtext.facet#link", "uri": uri}
                ],
            }
        )

    return facets


# --- Matching Bluesky's own link-shortening behavior -----------------------
#
# Bluesky's official app (web, iOS, Android) never posts a link's full typed
# text. Before every post, it rewrites each link facet's VISIBLE text to
# `host + truncated-path + "..."` while leaving the facet's `uri` (the actual
# link target) untouched -- see `toShortUrl`/`shortenLinks` in
# bluesky-social/social-app (src/lib/strings/url-helpers.ts and
# rich-text-manip.ts). It does this unconditionally, not only when a post
# would otherwise be too long, and its own live character counter counts the
# SHORTENED length. A post built from the literal typed text (as this app
# used to do) can therefore look "over 300" here while bsky.app posts the
# same typed text successfully at a shorter effective length -- the same URL
# ends up counted differently, not counted incorrectly.
#
# The two constants below and the truncation rule in ``to_short_url`` are a
# direct port of upstream's ``toShortUrl``, so Broadside's posted text and its
# character counter both match what bsky.app would have done with the same
# input.
_SHORTEN_PATH_MAX = 15
_SHORTEN_PATH_KEEP = 13


def to_short_url(url: str) -> str:
    """Shorten one URL for DISPLAY the way Bluesky's own app does.

    Strips the scheme; if the remaining path+query+fragment is longer than
    ``_SHORTEN_PATH_MAX`` characters, keeps only the first ``_SHORTEN_PATH_KEEP``
    followed by an ellipsis. Only applies to ``http``/``https`` URLs -- a bare
    domain typed without a scheme is returned unchanged, matching upstream's
    behavior of leaving anything it can't parse as an absolute URL untouched.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url
    path = parts.path if parts.path != "/" else ""
    query = f"?{parts.query}" if parts.query else ""
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    tail = path + query + fragment
    if len(tail) > _SHORTEN_PATH_MAX:
        tail = tail[:_SHORTEN_PATH_KEEP] + "..."
    return parts.netloc + tail


def shorten_link_facets(text: str, facets: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Shorten every link facet's visible text in ``text``, Bluesky-app style.

    Returns ``(new_text, new_facets)``. Each link facet's ``index`` is updated
    to point at the shortened span; its ``uri`` feature -- the real link
    target -- is left exactly as detected. Facets are processed left to right
    with a running byte-offset delta so multiple links in one post shift
    correctly. Non-link facets are returned unchanged (there are none today --
    only URLs are faceted -- but this stays correct if that ever changes).
    """
    link_indices = [
        i
        for i, f in enumerate(facets)
        if any(feat.get("$type") == "app.bsky.richtext.facet#link" for feat in f.get("features", []))
    ]
    if not link_indices:
        return text, facets

    text_bytes = bytearray(text.encode("utf-8"))
    delta = 0
    for i in sorted(link_indices, key=lambda i: facets[i]["index"]["byteStart"]):
        idx = facets[i]["index"]
        start = idx["byteStart"] + delta
        end = idx["byteEnd"] + delta
        old_span = bytes(text_bytes[start:end]).decode("utf-8")
        short = to_short_url(old_span)
        if short == old_span:
            continue
        short_bytes = short.encode("utf-8")
        text_bytes[start:end] = short_bytes
        idx["byteStart"] = start
        idx["byteEnd"] = start + len(short_bytes)
        delta += len(short_bytes) - (end - start)

    return text_bytes.decode("utf-8"), facets
