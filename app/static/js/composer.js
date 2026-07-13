/*
 * composer.js -- The compose-and-post screen (spec sections 6, 7, 10, 11).
 *
 * Responsibilities:
 *   - Load the sanitized account list and render the selection (all checked by
 *     default), grouped by platform.
 *   - Manage an ordered list of entries. One entry = a single post; more =
 *     a thread. Add / remove / reorder controls, with the button label flipping
 *     between "Post" and "Post Thread".
 *   - Per-entry: grapheme-accurate character counter against the binding limit,
 *     an image drop zone (drag/drop, click-to-browse, paste), and an alt-text
 *     field per image that auto-focuses the moment an image lands.
 *   - Enforce, before posting: at least one account, no empty entry, alt text on
 *     every image, and the binding character limit. Warn on animated GIFs.
 *   - Render per-target results with real links, expandable raw errors, and a
 *     one-click re-auth link on auth failures.
 *
 * State lives in `state`; structural changes (add/remove/reorder/image change)
 * re-render the affected regions, and text/alt edits update counters in place
 * without a rebuild so focus is never lost.
 */

(() => {
  "use strict";
  const { api, graphemeCount, computeLimits, resizeImage, escapeHtml } = Broadside;

  // ------------------------------------------------------------------ //
  // State
  // ------------------------------------------------------------------ //
  // Id source and entry factory are declared BEFORE `state` because `state`
  // initializes its first entry by calling newEntry() -- referencing `seq`
  // earlier would hit its temporal dead zone and throw at load time.
  let seq = 0; // monotonic id source for entries and images
  function uid() { return `x${seq++}`; }
  function newEntry() { return { id: uid(), text: "", images: [] }; }

  const state = {
    accounts: [],          // flat list of {id, platform, display_name, limits, ...}
    selected: new Set(),   // ids currently checked (defaults to all)
    entries: [newEntry()], // ordered list; starts as a single entry
    posting: false,        // guards against double-submits
  };

  // ------------------------------------------------------------------ //
  // Boot
  // ------------------------------------------------------------------ //
  async function init() {
    try {
      const cfg = await api("GET", "/api/config");
      state.accounts = [...cfg.bluesky_accounts, ...cfg.mastodon_accounts];
    } catch (err) {
      document.getElementById("account-groups").textContent =
        "Could not load accounts. Is the app configured? Visit Settings.";
      return;
    }
    // Default target is all accounts (spec section 6).
    state.selected = new Set(state.accounts.map((a) => a.id));

    renderAccounts();
    renderEntries();
    updateBinding();
    wireControls();
  }

  // ------------------------------------------------------------------ //
  // Account selection
  // ------------------------------------------------------------------ //
  function renderAccounts() {
    const host = document.getElementById("account-groups");
    host.innerHTML = "";
    if (!state.accounts.length) {
      host.innerHTML = `<p class="muted">No accounts yet. <a href="/wizard">Add one in Settings.</a></p>`;
      return;
    }
    for (const platform of ["bluesky", "mastodon"]) {
      const group = state.accounts.filter((a) => a.platform === platform);
      if (!group.length) continue;

      const wrap = document.createElement("div");
      wrap.className = "account-group";
      wrap.innerHTML = `<h3>${platform === "bluesky" ? "Bluesky" : "Mastodon"}</h3>`;

      for (const acct of group) {
        const label = document.createElement("label");
        label.className = "account-check";
        const checked = state.selected.has(acct.id) ? "checked" : "";
        label.innerHTML =
          `<input type="checkbox" value="${escapeHtml(acct.id)}" ${checked}>` +
          `<span>${escapeHtml(acct.display_name || acct.id)}</span>`;
        label.querySelector("input").addEventListener("change", (e) => {
          if (e.target.checked) state.selected.add(acct.id);
          else state.selected.delete(acct.id);
          // Selection drives the binding limits and image constraints (spec 7/10).
          updateBinding();
        });
        wrap.appendChild(label);
      }
      host.appendChild(wrap);
    }
  }

  function selectedAccounts() {
    return state.accounts.filter((a) => state.selected.has(a.id));
  }

  // ------------------------------------------------------------------ //
  // Entries
  // ------------------------------------------------------------------ //
  function renderEntries() {
    syncFromDom(); // capture any in-progress text/alt before we rebuild
    const host = document.getElementById("entries");
    host.innerHTML = "";
    state.entries.forEach((entry, index) => host.appendChild(renderEntry(entry, index)));
    updatePostButton();
  }

  function renderEntry(entry, index) {
    const isThread = state.entries.length > 1;
    const card = document.createElement("div");
    card.className = "entry card";
    card.dataset.entryId = entry.id;

    // Header row: entry number (only shown in a thread) + reorder/remove.
    const header = document.createElement("div");
    header.className = "entry-header";
    if (isThread) {
      header.innerHTML = `<span class="entry-num">Entry ${index + 1}</span>`;
      const controls = document.createElement("div");
      controls.className = "entry-controls";
      // Move up/down and remove are visually quiet -- rarely used (spec 6).
      controls.appendChild(iconBtn("↑", "Move up", index === 0, () => moveEntry(index, -1)));
      controls.appendChild(iconBtn("↓", "Move down", index === state.entries.length - 1, () => moveEntry(index, +1)));
      controls.appendChild(iconBtn("✕", "Remove entry", false, () => removeEntry(index)));
      header.appendChild(controls);
    }
    card.appendChild(header);

    // Text area + live counter.
    const ta = document.createElement("textarea");
    ta.className = "entry-text";
    ta.rows = 4;
    ta.placeholder = "What's happening?";
    ta.value = entry.text;
    ta.addEventListener("input", () => {
      entry.text = ta.value;
      updateCounter(card, entry); // update this entry's counter in place
      // Re-evaluate post blockers live so the empty/over-limit reasons and the
      // Post button's enabled state track what the user is typing.
      updateBlockers();
    });
    card.appendChild(ta);

    const counter = document.createElement("div");
    counter.className = "counter";
    counter.dataset.role = "counter";
    card.appendChild(counter);

    // Image drop zone + attached images.
    card.appendChild(renderDropZone(entry, index));
    const imageList = document.createElement("div");
    imageList.className = "image-list";
    imageList.dataset.role = "images";
    entry.images.forEach((img) => imageList.appendChild(renderImage(entry, img)));
    card.appendChild(imageList);

    // Populate the counter now that the card exists.
    setTimeout(() => updateCounter(card, entry), 0);
    return card;
  }

  function renderDropZone(entry, index) {
    const zone = document.createElement("div");
    zone.className = "dropzone";
    zone.innerHTML = `<span>Drop images anywhere, paste, or <button type="button" class="linkbutton">browse</button></span>`;

    // Hidden file input for the click-to-browse fallback (spec section 10).
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = "image/*";
    fileInput.multiple = true;
    fileInput.hidden = true;
    zone.appendChild(fileInput);

    zone.querySelector("button").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      handleFiles(index, fileInput.files);
      fileInput.value = "";
    });

    // NOTE: drag-and-drop is handled page-wide at the document level (see
    // wireDragAndDrop) so an image can be dropped anywhere on the compose page,
    // not only onto this box. Paste is likewise document-level. The drop is
    // routed to whichever entry it lands over, so no per-zone handler is needed.
    zone.dataset.entryIndex = String(index);
    return zone;
  }

  function renderImage(entry, img) {
    const item = document.createElement("div");
    item.className = "image-item";

    const thumb = document.createElement("img");
    thumb.className = "thumb";
    thumb.src = `data:${img.mime};base64,${img.base64}`;
    thumb.alt = "";
    item.appendChild(thumb);

    const right = document.createElement("div");
    right.className = "image-fields";

    // Alt text field -- opens focused when the image lands (spec section 10).
    const altWrap = document.createElement("label");
    altWrap.className = "alt-field";
    altWrap.innerHTML = `<span>Alt text (required)</span>`;
    const alt = document.createElement("textarea");
    alt.rows = 2;
    alt.maxLength = 1500; // cap matches spec section 10
    alt.value = img.alt || "";
    alt.dataset.role = "alt";
    alt.dataset.imageId = img.id;
    alt.placeholder = "Describe the image";
    alt.addEventListener("input", () => {
      img.alt = alt.value;
      updateBlockers();
    });
    altWrap.appendChild(alt);
    right.appendChild(altWrap);

    // Animated-GIF warning (spec section 10): flattened to a still frame.
    if (img.animated) {
      const warn = document.createElement("div");
      warn.className = "warn";
      warn.textContent = "Animated GIF — will post as a single still frame.";
      right.appendChild(warn);
    }

    // Remove-image control.
    const rm = iconBtn("✕", "Remove image", false, () => {
      entry.images = entry.images.filter((x) => x.id !== img.id);
      renderEntries();
      updateBinding();
    });
    rm.classList.add("image-remove");
    right.appendChild(rm);

    item.appendChild(right);
    return item;
  }

  // ------------------------------------------------------------------ //
  // Image intake + resize
  // ------------------------------------------------------------------ //
  async function handleFiles(entryIndex, fileList) {
    const entry = state.entries[entryIndex];
    if (!entry) return;
    const files = Array.from(fileList).filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;

    const limits = computeLimits(selectedAccounts());
    const cap = limits.maxAttachments || 4;
    const targetBytes = limits.maxImageBytes || 1_000_000;

    for (const file of files) {
      // Enforce the per-entry attachment cap; say why rather than silently
      // dropping (spec section 10).
      if (entry.images.length >= cap) {
        flash(`This entry is limited to ${cap} image${cap === 1 ? "" : "s"} for the current selection.`);
        break;
      }
      try {
        const resized = await resizeImage(file, targetBytes);
        entry.images.push({
          id: uid(),
          base64: resized.base64,
          mime: resized.mime,
          alt: "",
          animated: resized.animated,
        });
      } catch (err) {
        flash(`Could not process an image: ${err.message}`);
      }
    }

    renderEntries();
    updateBinding();
    // Auto-focus the alt-text field of the last image just added, so the user
    // is typing alt text the instant the image lands (spec section 10).
    focusLastAlt(entryIndex);
  }

  function focusLastAlt(entryIndex) {
    const cards = document.querySelectorAll(".entry");
    const card = cards[entryIndex];
    if (!card) return;
    const alts = card.querySelectorAll('textarea[data-role="alt"]');
    if (alts.length) alts[alts.length - 1].focus();
  }

  // ------------------------------------------------------------------ //
  // Entry structural ops
  // ------------------------------------------------------------------ //
  function addEntry() {
    syncFromDom();
    state.entries.push(newEntry());
    renderEntries();
    updateBinding();
  }

  function removeEntry(index) {
    syncFromDom();
    state.entries.splice(index, 1);
    if (!state.entries.length) state.entries.push(newEntry());
    renderEntries();
    updateBinding();
  }

  function moveEntry(index, delta) {
    syncFromDom();
    const target = index + delta;
    if (target < 0 || target >= state.entries.length) return;
    const [item] = state.entries.splice(index, 1);
    state.entries.splice(target, 0, item);
    renderEntries();
    updateBinding();
  }

  // Copy live DOM values back into state before any structural rebuild, so
  // in-progress text and alt edits are never lost.
  function syncFromDom() {
    const cards = document.querySelectorAll(".entry");
    cards.forEach((card, i) => {
      const entry = state.entries[i];
      if (!entry) return;
      const ta = card.querySelector(".entry-text");
      if (ta) entry.text = ta.value;
      card.querySelectorAll('textarea[data-role="alt"]').forEach((altEl) => {
        const img = entry.images.find((x) => x.id === altEl.dataset.imageId);
        if (img) img.alt = altEl.value;
      });
    });
  }

  // ------------------------------------------------------------------ //
  // Counters, binding limits, blockers, post button
  // ------------------------------------------------------------------ //
  function updateBinding() {
    // Recompute every entry's counter against the new binding limit and refresh
    // the blockers and post button.
    document.querySelectorAll(".entry").forEach((card, i) => {
      const entry = state.entries[i];
      if (entry) updateCounter(card, entry);
    });
    updateBlockers();
    updatePostButton();
  }

  function updateCounter(card, entry) {
    const el = card.querySelector('[data-role="counter"]');
    if (!el) return;
    const limits = computeLimits(selectedAccounts());
    const count = graphemeCount(entry.text || "");
    const max = limits.maxChars || 0;
    const over = max > 0 && count > max;
    el.classList.toggle("over", over);
    // Show the count, the binding limit, and WHY it is binding (spec section 7).
    const why = limits.reason ? `, ${limits.reason}` : "";
    el.textContent = max > 0 ? `${count} / ${max}${why}` : `${count}`;
  }

  function updatePostButton() {
    const btn = document.getElementById("post-button");
    // Label flips with entry count (spec section 6).
    btn.textContent = state.entries.length > 1 ? "Post Thread" : "Post";
  }

  // Compute the reasons (if any) that posting is currently blocked, and show
  // them. The post button is disabled while any blocker stands.
  function updateBlockers() {
    const reasons = collectBlockers();
    const host = document.getElementById("post-blockers");
    host.innerHTML = "";
    for (const r of reasons) {
      const div = document.createElement("div");
      div.className = "blocker";
      div.textContent = r;
      host.appendChild(div);
    }
    document.getElementById("post-button").disabled = reasons.length > 0 || state.posting;
  }

  function collectBlockers() {
    const reasons = [];
    if (state.selected.size === 0) reasons.push("Select at least one account.");

    const limits = computeLimits(selectedAccounts());
    state.entries.forEach((entry, i) => {
      const hasText = (entry.text || "").trim().length > 0;
      const hasImages = entry.images.length > 0;
      if (!hasText && !hasImages) reasons.push(`Entry ${i + 1} is empty.`);
      // Alt text is a hard block on every image (spec section 10).
      for (const img of entry.images) {
        if (!(img.alt || "").trim()) {
          reasons.push(`An image in entry ${i + 1} needs alt text.`);
          break;
        }
      }
      // Character limit is enforced before sending (spec section 7).
      if (limits.maxChars > 0 && graphemeCount(entry.text || "") > limits.maxChars) {
        reasons.push(`Entry ${i + 1} is over the ${limits.maxChars}-character limit.`);
      }
    });
    return reasons;
  }

  // ------------------------------------------------------------------ //
  // Posting
  // ------------------------------------------------------------------ //
  async function doPost() {
    syncFromDom();
    updateBlockers();
    if (collectBlockers().length) return; // guarded, but double-check

    // Warn once about animated GIFs before sending (spec section 10).
    const anyAnimated = state.entries.some((e) => e.images.some((i) => i.animated));
    if (anyAnimated) {
      const ok = window.confirm(
        "One or more animated GIFs will be posted as a single still frame. Continue?"
      );
      if (!ok) return;
    }

    const payload = {
      account_ids: [...state.selected],
      entries: state.entries.map((e) => ({
        text: e.text || "",
        images: e.images.map((i) => ({ data: i.base64, mime: i.mime, alt: i.alt })),
      })),
    };

    state.posting = true;
    setPosting(true);
    try {
      const resp = await api("POST", "/api/post", payload);
      renderResults(resp.results);
    } catch (err) {
      renderResults(null, err);
    } finally {
      state.posting = false;
      setPosting(false);
    }
  }

  function setPosting(on) {
    const btn = document.getElementById("post-button");
    btn.disabled = on;
    btn.textContent = on ? "Posting…" : state.entries.length > 1 ? "Post Thread" : "Post";
  }

  function renderResults(results, topError) {
    const panel = document.getElementById("results");
    const list = document.getElementById("results-list");
    panel.hidden = false;
    list.innerHTML = "";

    if (topError) {
      // A whole-request failure (e.g. bad payload). Per-target results were
      // never produced.
      const div = document.createElement("div");
      div.className = "result failed";
      div.textContent = topError.message;
      list.appendChild(div);
      return;
    }

    for (const r of results) {
      list.appendChild(renderOneResult(r));
    }
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderOneResult(r) {
    const div = document.createElement("div");
    div.className = `result ${r.status}`;

    const who = `${r.platform === "bluesky" ? "Bluesky" : "Mastodon"}, ${escapeHtml(r.display_name)}`;

    if (r.status === "ok") {
      // Success: show a working link to the posted item (spec section 11). For
      // a thread, link the first post of the chain.
      const link = r.posts.length ? r.posts[0].url : null;
      div.innerHTML =
        `<span class="result-line">${who}: posted` +
        (r.total_entries > 1 ? ` (${r.posts.length}/${r.total_entries} entries)` : "") +
        `</span>` +
        (link ? ` <a href="${escapeHtml(link)}" target="_blank" rel="noopener">view</a>` : "");
      return div;
    }

    // Failure or partial: friendly line up top, raw error one click away.
    const err = r.error || {};
    const posted =
      r.status === "partial"
        ? `posted entries 1 to ${r.posts.length}, failed on entry ${(r.failed_on_entry ?? r.posts.length) + 1}`
        : "failed";
    div.innerHTML = `<span class="result-line">${who}: ${posted} — ${escapeHtml(err.message || "error")}</span>`;

    // Re-auth shortcut on auth failures (spec sections 9 and 11): links
    // straight to this account in the wizard, pre-identified.
    if (err.is_auth) {
      const reauth = document.createElement("a");
      reauth.href = `/wizard?reauth=${encodeURIComponent(r.account_id)}`;
      reauth.textContent = "re-authenticate";
      reauth.className = "reauth-link";
      div.appendChild(document.createTextNode(" "));
      div.appendChild(reauth);
    }

    // Distinguish rejected vs unreachable in the wording (spec section 11).
    if (err.kind === "unreachable") {
      const note = document.createElement("div");
      note.className = "result-note";
      note.textContent = "Could not reach it — try again.";
      div.appendChild(note);
    }

    // Expandable raw detail. Never hidden, never led with (spec section 11).
    if (err.raw != null || err.status != null) {
      const details = document.createElement("details");
      details.className = "raw";
      details.innerHTML =
        `<summary>Raw error</summary><pre>${escapeHtml(formatRaw(err))}</pre>`;
      div.appendChild(details);
    }
    return div;
  }

  function formatRaw(err) {
    const parts = [];
    if (err.status != null) parts.push(`HTTP ${err.status}`);
    if (err.kind) parts.push(`kind: ${err.kind}`);
    if (err.raw != null) {
      parts.push(typeof err.raw === "string" ? err.raw : JSON.stringify(err.raw, null, 2));
    }
    return parts.join("\n");
  }

  // ------------------------------------------------------------------ //
  // History (server-side log) drawer
  // ------------------------------------------------------------------ //
  async function showLog() {
    const panel = document.getElementById("log-panel");
    const list = document.getElementById("log-list");
    panel.hidden = false;
    list.textContent = "Loading…";
    try {
      const resp = await api("GET", "/api/log");
      if (!resp.entries.length) {
        list.textContent = "No posts yet.";
        return;
      }
      list.innerHTML = "";
      for (const e of resp.entries) {
        const row = document.createElement("div");
        row.className = `log-row ${e.outcome}`;
        const when = new Date(e.ts).toLocaleString();
        let text = `${when} — ${e.platform}, ${e.account}: ${e.outcome}`;
        if (e.total_entries > 1) text += ` (${e.entries_posted}/${e.total_entries})`;
        if (e.error && e.error.message) text += ` — ${e.error.message}`;
        row.textContent = text;
        list.appendChild(row);
      }
    } catch (err) {
      list.textContent = `Could not load history: ${err.message}`;
    }
  }

  // ------------------------------------------------------------------ //
  // Wiring
  // ------------------------------------------------------------------ //
  function wireControls() {
    document.getElementById("add-entry").addEventListener("click", addEntry);
    document.getElementById("post-button").addEventListener("click", doPost);
    document.getElementById("show-log").addEventListener("click", showLog);
    document.getElementById("close-log").addEventListener("click", () => {
      document.getElementById("log-panel").hidden = true;
    });

    // Global paste handler: route pasted images to the entry whose field is
    // focused, else the first entry. Screenshots are the expected common case.
    document.addEventListener("paste", (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const files = [];
      for (const it of items) {
        if (it.kind === "file" && it.type.startsWith("image/")) files.push(it.getAsFile());
      }
      if (!files.length) return;
      e.preventDefault();
      handleFiles(focusedEntryIndex(), files);
    });

    wireDragAndDrop();
  }

  // Page-wide drag-and-drop. An image dropped ANYWHERE on the compose page is
  // accepted (not just onto a drop box) and routed to the entry it lands over,
  // falling back to the focused/first entry. A full-page highlight shows while
  // a file is being dragged in.
  function wireDragAndDrop() {
    // A single overlay element gives the "drop anywhere" affordance. It is
    // pointer-events:none so it never becomes the drop target -- that lets us
    // read the real element under the cursor to pick the right entry.
    const overlay = document.createElement("div");
    overlay.className = "drag-overlay";
    overlay.hidden = true;
    overlay.innerHTML = "<span>Drop image to add it</span>";
    document.body.appendChild(overlay);

    // dragenter/dragleave fire per element, so a depth counter tracks whether
    // the pointer is still somewhere over the window before hiding the overlay.
    let depth = 0;
    const isFileDrag = (e) =>
      e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");

    document.addEventListener("dragenter", (e) => {
      if (!isFileDrag(e)) return; // ignore text/selection drags
      depth += 1;
      overlay.hidden = false;
    });
    document.addEventListener("dragover", (e) => {
      if (!isFileDrag(e)) return;
      e.preventDefault(); // required so the drop is allowed
    });
    document.addEventListener("dragleave", (e) => {
      if (!isFileDrag(e)) return;
      depth -= 1;
      if (depth <= 0) {
        depth = 0;
        overlay.hidden = true;
      }
    });
    document.addEventListener("drop", (e) => {
      if (!e.dataTransfer) return;
      const files = Array.from(e.dataTransfer.files || []).filter((f) => f.type.startsWith("image/"));
      // Always prevent the browser's default "open the dropped image" behavior.
      e.preventDefault();
      depth = 0;
      overlay.hidden = true;
      if (!files.length) return;
      handleFiles(entryIndexFromNode(e.target), files);
    });
  }

  // Pick the entry a drop landed on. If the drop point is inside an entry card,
  // use that entry; otherwise fall back to the focused (or first) entry.
  function entryIndexFromNode(node) {
    const card = node && node.closest ? node.closest(".entry") : null;
    if (card) {
      const idx = Array.from(document.querySelectorAll(".entry")).indexOf(card);
      if (idx >= 0) return idx;
    }
    return focusedEntryIndex();
  }

  // Determine which entry currently has focus, so pasted images land there.
  function focusedEntryIndex() {
    const active = document.activeElement;
    const card = active && active.closest ? active.closest(".entry") : null;
    if (card) {
      const cards = Array.from(document.querySelectorAll(".entry"));
      const idx = cards.indexOf(card);
      if (idx >= 0) return idx;
    }
    return 0;
  }

  // ------------------------------------------------------------------ //
  // Small UI helpers
  // ------------------------------------------------------------------ //
  function iconBtn(glyph, title, disabled, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "icon-btn";
    b.textContent = glyph;
    b.title = title;
    b.setAttribute("aria-label", title);
    b.disabled = !!disabled;
    b.addEventListener("click", onClick);
    return b;
  }

  // Transient, non-blocking notice (attachment cap hit, image error, etc.).
  function flash(message) {
    const bar = document.getElementById("post-blockers");
    const div = document.createElement("div");
    div.className = "blocker flash";
    div.textContent = message;
    bar.appendChild(div);
    setTimeout(() => div.remove(), 4000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
