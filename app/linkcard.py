"""
linkcard.py -- Fetch Open Graph metadata to build a Bluesky link card.

Bluesky does not unfurl links itself. When a post has a link but NO image, the
otherwise-unused embed slot can hold an external link card
(`app.bsky.embed.external`) with a title, description, and thumbnail. This module
fetches and normalizes that metadata from the target URL.

This runs server-side by necessity: a browser cannot fetch arbitrary
cross-origin pages (CORS), so the client cannot gather Open Graph tags. Mastodon
needs none of this -- it generates its own preview cards server-side.

Everything here is BEST-EFFORT. Any failure returns None (or a card without a
thumbnail) so a post is never blocked by a card that could not be built. The
worst case degrades to a clickable link with no card -- the prior behavior.

Note on server-side imaging: the app resizes post images client-side, but a
link-card thumbnail is fetched on the server, so it is downscaled here with
Pillow -- the one server-side imaging case the spec explicitly permits.
"""

from __future__ import annotations

import io
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image


_TIMEOUT = 10
# Don't download a whole large page just to read tags in <head>.
_MAX_HTML_BYTES = 1_000_000
# Refuse to pull down an enormous og:image before we even downscale it.
_MAX_IMAGE_FETCH_BYTES = 10_000_000
# Keep the prepared thumbnail comfortably under Bluesky's ~976KB blob ceiling.
_THUMB_MAX_BYTES = 900_000
_THUMB_MAX_DIM = 1000
# A polite, honest User-Agent; some sites serve no Open Graph tags to unknown UAs.
_UA = "Mozilla/5.0 (compatible; Broadside/1.0; +https://github.com/cruftbox/broadside)"


class _OGParser(HTMLParser):
    """Collect meta tags (Open Graph + name) and the <title> from an HTML head.

    Using the stdlib HTML parser keeps this dependency-light and far more robust
    than regexing meta tags. We keep the FIRST value seen for each key, which is
    what Open Graph consumers conventionally do.
    """

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self._in_title = False
        self.title_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            content = a.get("content")
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_text += data


def fetch_card(url: str) -> dict[str, Any] | None:
    """Fetch link-card metadata for ``url``.

    Returns a dict with ``uri``, ``title``, ``description`` and -- when an
    og:image was found and successfully prepared -- ``thumb_bytes`` and
    ``thumb_mime``. Returns None only when the page itself could not be read.
    """
    try:
        resp = requests.get(
            url, timeout=_TIMEOUT, headers={"User-Agent": _UA}, stream=True
        )
        if not resp.ok:
            return None
        # Read at most _MAX_HTML_BYTES, decoding any content-encoding (gzip).
        raw = resp.raw.read(_MAX_HTML_BYTES, decode_content=True)
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
        resp.close()
    except Exception:
        # Network failure, timeout, bad URL: no card, but the post still posts.
        return None

    parser = _OGParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    m = parser.meta
    # Prefer Open Graph; fall back to <title> / meta description / the domain.
    title = (m.get("og:title") or parser.title_text.strip() or urlparse(url).netloc or url)
    description = m.get("og:description") or m.get("description") or ""

    card: dict[str, Any] = {
        "uri": url,
        "title": title[:300],
        "description": description[:1000],
    }

    img_url = m.get("og:image") or m.get("twitter:image")
    if img_url:
        thumb = _fetch_thumb(img_url, base=url)
        if thumb:
            card["thumb_bytes"], card["thumb_mime"] = thumb

    return card


def _fetch_thumb(img_url: str, base: str) -> tuple[bytes, str] | None:
    """Download and downscale an og:image into a Bluesky-sized JPEG thumbnail."""
    try:
        # og:image may be relative; resolve it against the page URL.
        img_url = urljoin(base, img_url)
        r = requests.get(img_url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        if not r.ok or len(r.content) > _MAX_IMAGE_FETCH_BYTES:
            return None
        return _prepare_thumb(r.content)
    except Exception:
        return None


def _prepare_thumb(raw: bytes) -> tuple[bytes, str] | None:
    """Re-encode arbitrary image bytes as a JPEG under the blob size limit.

    Flattening to RGB JPEG drops transparency, which is fine for a small preview
    thumbnail and guarantees broad compatibility. Quality steps down until the
    result fits under the ceiling.
    """
    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        im.thumbnail((_THUMB_MAX_DIM, _THUMB_MAX_DIM))
        quality = 88
        data = b""
        while quality >= 40:
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= _THUMB_MAX_BYTES:
                return data, "image/jpeg"
            quality -= 12
        # Last attempt, even if marginally over -- better a thumb than none.
        return data, "image/jpeg"
    except Exception:
        return None


def first_url(facets: list[dict[str, Any]]) -> str | None:
    """Pull the first link URL out of a computed facets list, or None.

    Bluesky link cards represent a single URL; convention is to use the first
    link in the text. The facets already carry the normalized (scheme-prefixed)
    URI, so we reuse it rather than re-scanning the text.
    """
    for f in facets:
        for feature in f.get("features", []):
            if feature.get("$type") == "app.bsky.richtext.facet#link":
                return feature.get("uri")
    return None
