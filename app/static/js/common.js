/*
 * common.js -- Helpers shared by the composer and the wizard.
 *
 * Everything here is framework-free (spec section 2). The four things both
 * pages need live on a single global `Broadside` object:
 *
 *   - api()            : fetch wrapper that speaks Broadside's JSON error shape
 *   - graphemeCount()  : Bluesky-accurate character counting (spec section 7)
 *   - computeLimits()  : the "minimum across all selected accounts" math
 *   - resizeImage()    : client-side canvas resize/re-encode (spec section 10)
 *
 * Keeping image bytes on the client until they are resized means the raw,
 * full-size image never transits the backend (spec sections 2 and 3).
 */

const Broadside = (() => {
  "use strict";

  /* ------------------------------------------------------------------ *
   * API helper
   * ------------------------------------------------------------------ */

  /**
   * Call a JSON endpoint. Resolves with the parsed body on success. On an
   * error status it throws an Error whose `.detail` carries the server's
   * translated error object ({kind, message, raw, ...}) so callers can show
   * "friendly up top, raw underneath" (spec section 11).
   */
  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    let data = null;
    try {
      data = await resp.json();
    } catch (_) {
      /* Some responses (e.g. transport failures) may not carry JSON. */
    }
    if (!resp.ok || (data && data.ok === false)) {
      const detail = (data && data.error) || { message: `Request failed (${resp.status}).` };
      const err = new Error(detail.message || "Request failed.");
      err.detail = detail;
      throw err;
    }
    return data;
  }

  /* ------------------------------------------------------------------ *
   * Grapheme-accurate character counting (spec section 7)
   * ------------------------------------------------------------------ */

  // Intl.Segmenter counts user-perceived characters (graphemes), matching how
  // Bluesky actually counts. Raw string length disagrees whenever an emoji or
  // other multi-code-unit character is present, so we never use it.
  const _segmenter =
    typeof Intl !== "undefined" && Intl.Segmenter
      ? new Intl.Segmenter(undefined, { granularity: "grapheme" })
      : null;

  /** Count graphemes in `text`. Falls back to spread length if Segmenter is absent. */
  function graphemeCount(text) {
    if (!text) return 0;
    if (_segmenter) {
      let n = 0;
      for (const _ of _segmenter.segment(text)) n += 1;
      return n;
    }
    // Fallback: spread respects surrogate pairs (better than .length), though
    // it does not merge combining marks. Modern browsers all have Segmenter.
    return [...text].length;
  }

  /* ------------------------------------------------------------------ *
   * Bluesky link shortening (spec section 9 addendum)
   *
   * Bluesky's own app never posts a link's full typed text: before every
   * post it rewrites each URL's VISIBLE text to `host + truncated-path +
   * "..."` (stripping the scheme), while the underlying link still points at
   * the full URL -- see `toShortUrl`/`shortenLinks` in bluesky-social/
   * social-app (src/lib/strings/url-helpers.ts, rich-text-manip.ts). It does
   * this unconditionally, and its OWN live character counter counts the
   * shortened length. Mirroring that here is what keeps this counter from
   * blocking a post that bsky.app would post successfully at a shorter
   * effective length -- the same input, counted the same way upstream counts
   * it. The server applies the identical rule (facets.py) to what actually
   * gets posted.
   * ------------------------------------------------------------------ */

  const _URL_RE = /(^|[\s(])(https?:\/\/[^\s]+|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?:\/[^\s]*)?)/gi;
  const _TRAILING = ".,;:!?\"')]}>";

  /** Port of bsky.app's toShortUrl: shorten one URL for display only. */
  function toShortUrl(url) {
    try {
      const u = new URL(url);
      if (u.protocol !== "http:" && u.protocol !== "https:") return url;
      const tail = (u.pathname === "/" ? "" : u.pathname) + u.search + u.hash;
      return tail.length > 15 ? u.host + tail.slice(0, 13) + "..." : u.host + tail;
    } catch (_) {
      // Not an absolute http(s) URL (e.g. a bare domain typed with no
      // scheme) -- left exactly as typed, matching upstream.
      return url;
    }
  }

  /** Replace every URL in `text` with its shortened display form. */
  function shortenBskyLinks(text) {
    return text.replace(_URL_RE, (whole, pre, url) => {
      let urlText = url;
      while (urlText && _TRAILING.includes(urlText[urlText.length - 1])) {
        urlText = urlText.slice(0, -1);
      }
      if (!urlText) return whole;
      return pre + toShortUrl(urlText) + url.slice(urlText.length);
    });
  }

  /* ------------------------------------------------------------------ *
   * Binding limits: the minimum across all selected accounts (spec section 7)
   * ------------------------------------------------------------------ */

  /**
   * Given the list of currently-selected account objects (each with a
   * `.limits` block and a `.platform`), return the binding limits and a short
   * human explanation of which limit is binding and why.
   *
   * The binding value for every dimension is the MINIMUM across the selection:
   * the tightest selected account wins, regardless of platform.
   */
  function computeLimits(selected) {
    if (!selected.length) {
      return { maxChars: 0, maxImageBytes: 0, maxAttachments: 0, reason: "no accounts selected" };
    }

    let maxChars = Infinity;
    let maxImageBytes = Infinity;
    let maxAttachments = Infinity;
    let bindingAccount = null; // the account setting the character ceiling

    for (const acct of selected) {
      const lim = acct.limits || {};
      if (typeof lim.max_characters === "number" && lim.max_characters < maxChars) {
        maxChars = lim.max_characters;
        bindingAccount = acct;
      }
      if (typeof lim.max_image_size_bytes === "number") {
        maxImageBytes = Math.min(maxImageBytes, lim.max_image_size_bytes);
      }
      if (typeof lim.max_attachments === "number") {
        maxAttachments = Math.min(maxAttachments, lim.max_attachments);
      }
    }

    // Build the "300, Bluesky selected" style explanation so the number is
    // never mysterious (spec section 7).
    let reason;
    if (bindingAccount && bindingAccount.platform === "bluesky") {
      reason = "Bluesky selected";
    } else if (bindingAccount) {
      reason = bindingAccount.display_name || "Mastodon";
    } else {
      reason = "";
    }

    return {
      maxChars: maxChars === Infinity ? 0 : maxChars,
      maxImageBytes: maxImageBytes === Infinity ? 0 : maxImageBytes,
      maxAttachments: maxAttachments === Infinity ? 0 : maxAttachments,
      reason,
    };
  }

  /* ------------------------------------------------------------------ *
   * Client-side image resize / re-encode (spec section 10)
   * ------------------------------------------------------------------ */

  /**
   * Heuristically detect an animated GIF from its bytes. GIF animation is
   * signalled by more than one Graphic Control Extension block (0x21 0xF9).
   * A single-frame GIF has at most one. This is a heuristic, but reliable
   * enough to drive the "will be flattened to a still frame" warning
   * (spec section 10).
   */
  function isAnimatedGif(bytes) {
    // Must start with "GIF8".
    if (bytes.length < 6 || bytes[0] !== 0x47 || bytes[1] !== 0x49 || bytes[2] !== 0x46) {
      return false;
    }
    let frames = 0;
    for (let i = 0; i < bytes.length - 3; i++) {
      if (bytes[i] === 0x21 && bytes[i + 1] === 0xf9) {
        frames += 1;
        if (frames > 1) return true;
      }
    }
    return false;
  }

  /**
   * Resize/re-encode a File so it satisfies `targetBytes`, and report whether
   * it is an animated GIF (so the caller can warn).
   *
   * Strategy:
   *   - If the file is already within the target size, keep the ORIGINAL bytes.
   *     This preserves PNG transparency and GIF animation whenever the binding
   *     limit is generous enough (typically Mastodon-only selections).
   *   - Otherwise draw it onto a canvas at decreasing quality/dimensions and
   *     export JPEG until it fits. Canvas re-encoding flattens animation, which
   *     is exactly what the GIF warning is about.
   *
   * EXIF orientation is respected via createImageBitmap({imageOrientation})
   * so phone photos are not posted rotated (spec section 10).
   *
   * Returns { base64, mime, animated, bytes }.
   */
  async function resizeImage(file, targetBytes) {
    const buffer = new Uint8Array(await file.arrayBuffer());
    const animated = isAnimatedGif(buffer);

    // Fast path: already small enough -> send original bytes untouched.
    if (file.size <= targetBytes) {
      return {
        base64: await _bytesToBase64(buffer),
        mime: file.type || "image/jpeg",
        animated,
        bytes: file.size,
      };
    }

    // Decode with EXIF orientation applied.
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });

    // Step dimensions and quality down until the JPEG fits under the target.
    // Screenshots and photos both survive JPEG well; this reliably clears
    // Bluesky's ~976KB blob ceiling.
    let width = bitmap.width;
    let height = bitmap.height;
    let quality = 0.92;

    for (let attempt = 0; attempt < 12; attempt++) {
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(width));
      canvas.height = Math.max(1, Math.round(height));
      const ctx = canvas.getContext("2d");
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);

      const blob = await _canvasToBlob(canvas, "image/jpeg", quality);
      if (blob.size <= targetBytes) {
        bitmap.close && bitmap.close();
        return {
          base64: await _bytesToBase64(new Uint8Array(await blob.arrayBuffer())),
          mime: "image/jpeg",
          animated,
          bytes: blob.size,
        };
      }

      // Not small enough yet: first drop quality, then start shrinking pixels.
      if (quality > 0.5) {
        quality -= 0.1;
      } else {
        width *= 0.85;
        height *= 0.85;
        quality = 0.85; // reset quality after a dimension step
      }
    }

    // Give up gracefully after the loop with the smallest attempt we can make.
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width));
    canvas.height = Math.max(1, Math.round(height));
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await _canvasToBlob(canvas, "image/jpeg", 0.5);
    bitmap.close && bitmap.close();
    return {
      base64: await _bytesToBase64(new Uint8Array(await blob.arrayBuffer())),
      mime: "image/jpeg",
      animated,
      bytes: blob.size,
    };
  }

  /** Promise wrapper around canvas.toBlob. */
  function _canvasToBlob(canvas, type, quality) {
    return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
  }

  /** Encode raw bytes as base64 without blowing the call stack on large inputs. */
  function _bytesToBase64(bytes) {
    return new Promise((resolve) => {
      let binary = "";
      const chunk = 0x8000; // process in 32KB slices to avoid arg-length limits
      for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
      }
      resolve(btoa(binary));
    });
  }

  /* ------------------------------------------------------------------ *
   * Tiny DOM helper: escape text for safe insertion into innerHTML.
   * ------------------------------------------------------------------ */
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  return {
    api,
    graphemeCount,
    computeLimits,
    resizeImage,
    isAnimatedGif,
    escapeHtml,
    toShortUrl,
    shortenBskyLinks,
  };
})();
