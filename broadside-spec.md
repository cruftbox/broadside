# Broadside

A self-hosted cross-poster for Bluesky and Mastodon. Compose once, with proper alt text, and post the same image and text to multiple accounts across both platforms in a single action.

This document is the build specification. It is written to be handed to Claude Code as the source of truth for implementation.

---

## 1. Purpose and scope

Broadside solves one problem: posting the same content to Bluesky and to one or more Mastodon accounts without visiting each platform individually, while guaranteeing that alt text is always present.

The author posts text plus one or more images. Alt text is always written. Broadside makes that workflow a single form and a single button, and it will not let a post go out without alt text on every image.

### In scope

- Multiple Bluesky accounts and multiple Mastodon accounts.
- Posts consist of text plus image attachments. Images are the only attachment type.
- Threads: an ordered sequence of entries posted as a self-reply chain.
- Per-account selection, defaulting to all accounts.
- Enforced alt text on every image.
- Client-side image resizing to satisfy platform limits.
- Live-validated setup wizard with per-account re-authentication.
- Clear per-target success and error reporting.
- Clickable links on Bluesky via URL link facets (section 9). URLs in the text are auto-detected and made clickable. Mentions and hashtags are not faceted.
- Bluesky link preview cards (unfurls) for entries that have a link but no image. Because a Bluesky post has a single embed slot, a card is only added when the entry has no image; when an image is present the image takes the slot and there is no card. Mastodon generates its own preview cards server-side, so no work is needed there.

### Explicitly out of scope

- Video and any non-image attachment type.
- Link preview cards on entries that also carry an image (not possible: the image occupies the post's single embed slot).
- Mention and hashtag facets on Bluesky. Only URL link facets are computed (section 9). `@handle` and `#tag` are posted as plain text and are not resolved or notified on Bluesky. Mastodon resolves both server-side on its own.
- Scheduling, drafts persistence across restarts, and analytics.
- Public internet exposure. See section 3.
- Undo or send-delay. Not wanted.

---

## 2. Technology

- Backend: Python with Flask.
- Packaging: Docker, single container.
- Config storage: a JSON file on a mounted Docker volume, so it survives container restarts.
- Image processing: client-side in the browser using a canvas for resize and re-encode. Server-side Pillow is acceptable as a fallback if a client-side path proves unreliable, but client-side is preferred so the raw image never needs to transit the backend.
- Frontend: plain HTML, CSS, and JavaScript is sufficient. No framework is required. A light framework is acceptable if it earns its place, but the app is small enough not to need one.
- HTTP: the app calls the Bluesky and Mastodon APIs directly. `requests` on the backend, or `fetch` from the browser, are both acceptable per operation. Note that credentials must stay server-side after setup (section 3), which affects where each call runs.

---

## 3. Deployment and security posture

Broadside holds live posting credentials for multiple accounts. The single hard requirement is that it is never exposed to the public internet. It binds to a local port and is reached over the local network exactly like any other homelab service. How the network is reached (direct LAN, and optionally a private overlay if off-network access is ever wanted) is the operator's existing setup and is not a dependency of the app.

This is a single-user tool. There is no application-level login or authentication. Reachability on the local network is the entire access control: anyone who can reach the port can compose posts and open the setup wizard. This is a deliberate choice consistent with the LAN-only posture. The wizard never displays stored credentials back to the browser (see below), so LAN reachability grants the ability to post and to add or remove accounts, but not to read existing tokens.

Credential handling:

- The setup wizard collects credentials and writes them to the config file on the volume.
- After setup, credentials live server-side and are never sent back to the browser. The composer UI never receives the raw tokens or app passwords. When the app needs to post, the posting logic runs where it can read the config, and only non-secret data (post text, image bytes, account selection) moves between browser and backend as needed.
- The config file is written with restrictive file permissions, owned by the container user.
- Config is stored in plaintext on the volume. This is a deliberate choice for a single-user tool on the operator's own hardware. Encryption at rest is not used because it would break "set once and just post" by requiring an unlock on every restart, and it would guard against an attacker who would already have larger access.

---

## 4. Accounts and the config model

Broadside supports an arbitrary number of accounts on each platform. Do not hardcode a fixed count.

### Bluesky account

- `handle`: the account handle, for example `name.bsky.social`.
- `app_password`: a Bluesky app password (not the account password). Generated by the user in Bluesky settings.
- `service`: the PDS or service host. Default `https://bsky.social`.
- Derived at setup and cached: the account DID, obtained from a successful session creation.

### Mastodon account

- `instance_url`: the base URL of the instance, for example `https://mastodon.social`.
- `access_token`: an access token minted by the user via the instance's Preferences, Development, New Application flow.
- Derived at setup and cached: the resolved account name for display (for example `@you@instance.social`), and the instance limits (section 8).

### Config file shape (illustrative)

```json
{
  "bluesky_accounts": [
    {
      "id": "bsky-1",
      "handle": "name.bsky.social",
      "app_password": "xxxx-xxxx-xxxx-xxxx",
      "service": "https://bsky.social",
      "did": "did:plc:...",
      "display_name": "name.bsky.social"
    }
  ],
  "mastodon_accounts": [
    {
      "id": "masto-1",
      "instance_url": "https://mastodon.social",
      "access_token": "...",
      "display_name": "@you@mastodon.social",
      "limits": {
        "max_characters": 500,
        "max_image_size_bytes": 16777216,
        "max_attachments": 4,
        "supported_mime_types": ["image/jpeg", "image/png", "image/gif", "image/webp"]
      }
    }
  ]
}
```

Each account has a stable internal `id` used for selection and status reporting.

---

## 5. Setup wizard

The wizard is the first-run experience and also the ongoing settings screen. It is fully re-runnable.

### First-run detection

On startup the app checks for a valid config. If config is missing or incomplete, the user is routed to the wizard. If config is present and valid, the user goes straight to the composer. The same wizard is reachable at any time from a settings link so accounts can be added, edited, re-validated, or removed.

### Adding a Bluesky account

1. User enters handle and app password.
2. The app validates live by calling `com.atproto.server.createSession` with the handle and app password.
3. On success: store the account, cache the returned DID, show a green confirmation with the resolved handle.
4. On failure: show the specific reason (bad credentials, unknown handle, service unreachable) and do not store the account.

### Adding a Mastodon account

1. User enters the instance URL and the access token.
2. The app validates live by calling `GET /api/v1/accounts/verify_credentials` with the token.
3. On success: store the account, cache the display name (`@user@instance`), and immediately fetch and cache the instance limits (section 8). Show a green confirmation displaying the account it will post as.
4. On failure: show the specific reason (invalid or revoked token, bad instance URL, instance unreachable) and do not store the account.

### Validation is required, not optional

The wizard's core value is catching bad credentials at setup rather than at post time. An account is not saved until its live validation succeeds. The wizard should be able to report "all accounts verified" before the user leaves it.

### Editing and re-authentication

- Any account can be edited (new token, new app password) and re-validated in place.
- Any account can be removed.
- The wizard is the destination for the re-auth path described in section 9. When a post fails on an account due to an auth error, the app links the user directly to that account's entry in the wizard, pre-identified, so recovery is one action.

---

## 6. Composer

### Default state

The composer opens with a single entry and no thread affordance in view. A single post should not look like a thread. The default target is all accounts.

A single entry contains:

- A text area with a live character counter (section 7).
- An image drop zone (section 10).
- For each attached image: a thumbnail preview and its own alt text field.

### Account selection

- All configured accounts are listed at the top of the composer, grouped or labeled by platform, each with a checkbox.
- All accounts are checked by default.
- Selection applies to the whole post or thread, not per entry. The user never selects accounts per entry.
- The current selection drives the binding character limit and the binding image constraints (sections 7 and 8).

### Threads

- Below the entry (or entries) sits one quiet control labeled "+ Add to thread." It is not touched for a normal single post.
- Clicking it appends a new entry below the current one and reveals the thread structure.
- Each entry past the first has a remove control so a single entry can be backed out without discarding the whole thread.
- Each entry has move-up and move-down controls for reordering. These are expected to be rarely used, so they are visually quiet and do not compete with the compose fields. Entries are an ordered list, not an append-only stack.
- When the composer collapses back to a single entry, the thread affordance recedes.

### Post button

- Reads "Post" when there is exactly one entry.
- Reads "Post Thread" when there is more than one entry.

### Text and images per entry

- Each entry has its own text and its own images with their own alt text.
- Most posts have text in addition to the image. Text plus image is the normal case.
- Whether a thread entry may be text-only is an open decision, see section 12.

---

## 7. Character counting and limits

- The counter is grapheme-accurate. Use `Intl.Segmenter` with `granularity: 'grapheme'` so the count matches how Bluesky actually counts. Do not use raw string length, which disagrees with Bluesky whenever an emoji or other multi-code-unit character is present.
- Each entry has its own independent counter. Thread entries do not pool their character budgets.
- The binding limit is the **minimum** across all currently selected accounts. When several accounts are selected, the tightest one wins, regardless of platform. Concretely:
  - If any Bluesky account is selected: the limit is 300, because Bluesky's 300 is the tightest ceiling in play.
  - If the selection is Mastodon-only: the limit is the smallest `max_characters` among the selected Mastodon accounts, taken from their cached instance limits (defaulting to 500 if a limit is not yet known). Two Mastodon accounts with limits of 500 and 5000 bind at 500.
- This same minimum-across-selected rule governs image size and attachment count in sections 8 and 10, not just the character count.
- The counter should show which limit is currently binding and why (for example, "300, Bluesky selected"), so the number is never mysterious.
- The limit is enforced. A post that exceeds the binding limit for any selected target is blocked before it is sent, with a clear message.

---

## 8. Instance limits discovery

Mastodon limits are per-instance, not fixed. They must be discovered, not guessed.

- During setup, right after a Mastodon token validates, call the instance information endpoint (`/api/v1/instance`, or the v2 equivalent) and cache the instance's real limits per account: maximum characters, maximum image size, maximum attachments, and supported image mime types.
- These cached limits drive character enforcement (section 7), image sizing and acceptance (section 10), and the per-entry attachment cap.
- Refresh the cached limits when an account is re-validated in the wizard.

Bluesky limits are fixed and known: 300 characters, up to 4 images per post, and an image blob size ceiling of approximately 976KB per image.

---

## 9. Posting and fan-out

### Model

A post is an ordered list of one or more entries. Each entry has text and zero or more images with alt text. Posting fans the whole sequence out to each selected account, where each account maintains its own independent self-reply chain.

Threads on both platforms are chains of individual posts, each pointing at the previous one as its parent. There is no first-class thread object on either platform.

### Fan-out is serial across accounts

Accounts are posted one at a time, in order, not concurrently. Finish an account's entire chain before starting the next account's. Nothing here is time-critical, and serial posting keeps the logic simple, keeps rate-limit exposure low, and makes the per-target status lines fill in one at a time as each account completes. Ordering within a single account's chain is always strict regardless (entry 2 cannot post until entry 1 returns its identifiers); the serial rule is specifically that different accounts do not overlap.

### Per account, walk the entries in order

For each selected account, post entry 1, capture the identifiers from the response, then post entry 2 as a reply to entry 1, and so on. Each account's chain is independent. Account A's entry 2 replies to Account A's entry 1, never to any other account's post.

### Bluesky posting

- Session: use the cached credentials to create a session (`com.atproto.server.createSession`) and obtain `accessJwt` and `refreshJwt`.
- Image upload: for each image, upload the (already resized) blob via `com.atproto.repo.uploadBlob` and keep the returned blob reference.
- Post: create the record via `com.atproto.repo.createRecord` in the `app.bsky.feed.post` collection, with the text, `createdAt`, an `app.bsky.embed.images` embed, and computed link facets (below). Each embedded image carries its `alt` text and its blob reference.
- Link facets: detect URLs in the entry's text and attach a `facets` array so they render as clickable links. Each facet has an `index` with `byteStart` and `byteEnd`, plus a feature of type `app.bsky.richtext.facet#link` carrying the `uri`. **Offsets are UTF-8 byte offsets, not JavaScript string indices and not grapheme counts.** Compute them against the UTF-8 encoding of the text, or any post containing an emoji or accented character before a URL will highlight the wrong span. Only URLs are faceted; `@handle` and `#tag` are left as plain text (section 1). This runs per entry, so each post in a thread carries the facets for its own text.
- Link card (unfurl): for an entry that has a link but no image, add an `app.bsky.embed.external` embed for the first URL. Fetch the URL's Open Graph metadata server-side (title, description, and `og:image`), downscale the thumbnail under the blob ceiling and upload it as a blob for the card's `thumb`. This shares the single embed slot with images, so it is only built when the entry has no image. It is best-effort: if the fetch or thumbnail fails, the post still goes out with the clickable link and no card. The link facet is applied regardless, so the URL is both clickable and (when imageless) carries a card.
- Threading: for entry N past the first, set the record's `reply` field with a `root` reference (the first post in this account's chain) and a `parent` reference (the immediately previous post in this account's chain). Both are strong references, each a URI plus a `cid`, taken from the create responses.

### Mastodon posting

- Media: for each image, upload via `POST /api/v2/media` with the image and the alt text in the `description` field. Note that v2 media processing can be asynchronous and may return a 202 while processing. Poll until the media reports ready, or until a timeout, before posting the status. Keep the returned media id.
- Post: `POST /api/v1/statuses` with the `status` text and the `media_ids`.
- Threading: for entry N past the first, set `in_reply_to_id` to the id of this account's previous status. Mastodon uses a single parent pointer, with no separate root reference.

### Thread visibility note

This is not a bug to chase. On Mastodon, because of federation, a remote instance only displays the parts of a thread it has actually received, so a self-reply chain can look partial to some viewers. Broadside cannot fix this and should not try. Documented here so it is not mistaken for a defect in the posting logic.

### Failure within a chain

If an entry fails partway through an account's chain, stop that account's chain at the failure. A later entry was meant to reply to the failed one and has nothing to attach to. Report where it stopped, for example "posted entries 1 to 2, failed on entry 3," and let the user decide whether to retry the tail. Other accounts' chains are independent and continue regardless.

---

## 10. Images

Images are the only attachment type. Most posts pair an image with text.

### Input methods, all on the same drop zone per entry

- Drag and drop is the primary gesture. The drop zone listens for `dragover` and `drop` and pulls files off `dataTransfer.files`.
- Click to browse is a fallback on the same zone, using a file input. This covers cases where dragging is awkward or unavailable.
- Paste from clipboard is supported. A `paste` listener grabs image data the same way the drop handler does. This is expected to be heavily used for screenshots.

All three paths feed the same downstream pipeline.

### Alt text is enforced and prompted immediately

- When an image is dropped, browsed, or pasted onto an entry, its alt text field opens automatically with the cursor focused in it, so the user is typing alt text the instant the image lands.
- Alt text is enforced before posting. A post will not go out while any attached image lacks alt text. This enforcement is a primary reason the app exists. It is a hard block, not a warning.
- Alt text is capped at 1500 characters per image, which sits under both platforms' limits. This is a soft ceiling on input, not a reason to block an otherwise-valid post; keep the field from exceeding it.

### Ordering and binding

- Images within an entry are an ordered list. Order is preserved from how they arrived and determines post order. If several images are dropped at once, present them in a clear sequence with each image's alt text field directly attached to its own thumbnail, so alt text always binds to the correct image.
- Each image's thumbnail, alt text field, and data stay bound together through the whole flow.

### Animated GIFs

Images are mainly photos and screenshots. If an animated GIF is dropped, browsed, or pasted, warn the user before posting that it will be flattened to a single still frame, because the client-side canvas resize captures one frame and discards the animation. This is a warning the user can accept and proceed past, not a hard block. Detect the animation by inspecting the GIF for more than one image frame.

### Resizing

- Resize is client-side using a canvas: draw the image smaller, export with `canvas.toBlob()` at a quality setting, and step quality or dimensions down until the result satisfies the target size.
- Respect EXIF orientation when drawing to the canvas (for example via `createImageBitmap` with `imageOrientation: 'from-image'`), so photos from phones are not posted rotated. Screenshots are unaffected.
- Resize to satisfy the tightest selected target. If any Bluesky account is selected, compress to under approximately 976KB, which also clears any normal Mastodon instance limit. If the selection is Mastodon-only, use the cached per-instance size ceiling, with more headroom and no need to crush the image.

### Attachment count

- Per entry, cap the number of images at the tightest selected target's maximum. Bluesky allows 4. Mastodon's maximum comes from the cached instance limits. Reject an image beyond the cap on any single entry and say why, rather than silently dropping it.

---

## 11. Error handling and reporting

### Per-target status, always

The composer never collapses to a single success or failure. Every selected target gets its own status line on every post: the platform and account, the outcome, and on success a real link to the posted item.

- Success: show the account and a working link to the post (for example "Bluesky, name.bsky.social: posted, view"). Construct or retrieve the post's web URL from the create response.
- For a thread, report per account how far the chain got.

### Separate rejection from unreachable

Distinguish two fundamentally different failure classes, because the user's response to each differs:

- Rejected: the server received the request and said no. HTTP 4xx with a body, or an AT Protocol error object. Show the specific reason.
- Unreachable: the request could not be completed at all. Network timeout, DNS failure, connection refused, or 5xx. Show this as "could not reach it, try again," not as a content problem.

### Translate the raw errors

- AT Protocol returns a JSON body with an `error` name and a `message`. Switch on the `error` name. Handle `ExpiredToken` and `InvalidToken` as the normal access-token lifecycle, see retry policy below. Handle `RateLimitExceeded` using the accompanying headers. Surface an oversized-blob error as an image size problem.
- Mastodon returns an HTTP status plus a body, sometimes with `error` and `error_description`. Read the status: 401 is a dead or revoked token (route to re-auth), 422 is a content rejection (show which, for example text too long or media not ready), 429 is rate limiting, 5xx or a connection failure is an instance problem worth marking as unreachable rather than the user's fault.
- Handle the Mastodon async media case explicitly. If media never becomes ready before the timeout, report "image processing timed out on instance.social," not a generic failure. This is the Mastodon quirk most likely to produce a confusing error.

### Show friendly up top, raw underneath

Each status shows a clean human-readable line, with the raw error (status code, AT Protocol `error` name, response body) reachable one click away in an expandable detail. Do not hide the raw error, and do not lead with it.

### Retry policy

- Auto-retry exactly once, silently, for clearly transient cases only:
  - An expired Bluesky access token: use the `refreshJwt` to get a new session and retry once before surfacing anything. Only if the refresh itself fails is "credentials expired, re-authenticate" shown.
  - A network blip or timeout.
  - A 429, after honoring its retry-after.
- Everything else is reported immediately, with no auto-retry. A 422 malformed post retried unchanged just fails again and wastes the user's time.
- Retrying a target re-runs only that target (and for a thread, only the failed tail), so the accounts that already succeeded are never double-posted.

### Re-auth path

An auth failure on an account (Bluesky refresh failure, Mastodon 401) links the user directly to that account in the setup wizard, pre-identified, so re-authentication is one action rather than a hunt across accounts.

### Server-side log

Every post attempt writes a structured server-side log line per target: timestamp, platform, account, outcome, and on failure the raw error. This provides history the composer's momentary view does not, and reveals intermittent patterns (a flaky instance, recurring rate limits) that a single attempt cannot. Surface this as a plain readable list of what went where and when, so the operator has a record without reading container logs.

---

## 12. Resolved decisions

These were the two behaviors previously left open. Both are now decided.

1. Text-only thread entries are allowed. An entry may carry text only, with no image, anywhere in a thread including the first. Alt-text enforcement still applies to any image that is attached; it simply does not force an image onto an entry that has none.
2. Drafts are not persisted. If the tab closes or the container restarts mid-compose, the in-progress post is lost. This is a conscious choice, not an oversight, consistent with the no-scheduling, no-analytics scope. A lightweight `beforeunload` warning may be used to make the loss non-surprising, but nothing is written to disk.

---

## 13. Suggested build order

Prove the whole pipeline on the smallest surface first, then repeat patterns.

1. Setup wizard with live validation for one Bluesky account and one Mastodon account, including instance-limits discovery for the Mastodon account. Config written to the volume with tight permissions, credentials held server-side.
2. A single-account, single-entry, single-image, text-plus-alt post, end to end, to one target. This proves auth, client-side resize, enforced alt text, the posting call, success-with-link, and the error and retry handling on the minimal surface.
3. The second platform's single post, proving the other API's media and status flow and its error translation.
4. Multiple accounts and the account selection UI, with binding-limit logic driven by selection.
5. Threading: the ordered-entry composer, the add and reorder controls, the "Post" versus "Post Thread" label, and the independent per-account self-reply chains with stop-on-failure.
6. The server-side log and its readable view.

Everything after step 3 is repetition of patterns already proven, applied across more accounts and more entries.

---

## 14. Fixed decisions summary

For quick reference, the settled behaviors:

- Images are the only attachment type. No video and no link preview cards.
- Bluesky posts get URL link facets so links are clickable; mentions and hashtags are not faceted. Mastodon links auto-resolve server-side.
- Bluesky link preview cards are added for entries with a link and no image (a post has one embed slot; images take precedence). Best-effort, server-side Open Graph fetch; never blocks the post.
- Alt text is enforced as a hard block on every image, and capped at 1500 characters. The alt text field auto-opens and focuses when an image lands.
- Character limit: grapheme-accurate, and the minimum across all selected accounts. 300 when any Bluesky account is selected; the smallest selected Mastodon instance maximum (default 500) when Mastodon-only. Enforced.
- Instance limits are discovered per Mastodon account at setup and cached. Image size and attachment count also bind to the minimum across all selected accounts.
- Images arrive by drag-and-drop, click-to-browse, or paste, on one drop zone per entry. Resized client-side to the tightest selected target, honoring EXIF orientation. Animated GIFs warn that they will be flattened to a still frame.
- Default is a single post to all accounts. Threads are opt-in via a quiet control. Post button reads "Post" or "Post Thread." Thread entries may be text-only.
- Fan-out is serial across accounts, each with its own independent self-reply chain. A chain stops at its first failed entry.
- Drafts are not persisted.
- Auto-retry exactly once for transient failures only (token refresh, network blip, 429 after retry-after). Everything else is reported immediately.
- Per-target status always, separating rejection from unreachable, friendly message with raw error underneath, success carrying a real link.
- Auth failures route to a per-account re-auth path in the wizard.
- Server-side per-attempt log provides history.
- Not exposed to the public internet. Credentials held server-side, never returned to the browser after setup. Plaintext config on a permission-restricted volume.
- No undo and no send-delay.
