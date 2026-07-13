/*
 * wizard.js -- Setup wizard / settings screen (spec section 5).
 *
 * Adds, edits, re-validates, and removes accounts. Every add or edit posts to
 * the backend, which validates the credentials LIVE and only stores the account
 * on success -- so the status shown here reflects a real round-trip to Bluesky
 * or the Mastodon instance, not a guess.
 *
 * Secrets are never sent back to the browser (spec section 3): editing an
 * account pre-fills only its non-secret fields (handle, service, instance URL)
 * and requires the user to re-enter the app password or access token. That
 * doubles as the re-authentication path -- an auth failure in the composer
 * links here with ?reauth=<id>, which scrolls to and pre-identifies the right
 * account.
 */

(() => {
  "use strict";
  const { api, escapeHtml } = Broadside;

  let accounts = []; // flat sanitized list from /api/config

  async function init() {
    await refresh();
    wireForms();
    handleReauthParam();
  }

  // ------------------------------------------------------------------ //
  // Existing accounts
  // ------------------------------------------------------------------ //
  async function refresh() {
    const cfg = await api("GET", "/api/config");
    accounts = [...cfg.bluesky_accounts, ...cfg.mastodon_accounts];
    renderList();
  }

  function renderList() {
    const host = document.getElementById("account-list");
    host.innerHTML = "";
    if (!accounts.length) {
      host.innerHTML = `<p class="muted">No accounts configured yet. Add one below.</p>`;
      return;
    }
    for (const acct of accounts) {
      const row = document.createElement("div");
      row.className = "account-row";
      row.id = `acct-${acct.id}`;

      const platform = acct.platform === "bluesky" ? "Bluesky" : "Mastodon";
      const sub = acct.platform === "bluesky" ? acct.service : acct.instance_url;
      row.innerHTML =
        `<div class="account-meta">` +
        `<span class="badge ${acct.platform}">${platform}</span> ` +
        `<strong>${escapeHtml(acct.display_name || acct.id)}</strong>` +
        `<div class="muted small">${escapeHtml(sub || "")}</div>` +
        `</div>`;

      const actions = document.createElement("div");
      actions.className = "account-actions";

      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "ghost";
      edit.textContent = "Edit / re-validate";
      edit.addEventListener("click", () => loadIntoForm(acct));
      actions.appendChild(edit);

      const del = document.createElement("button");
      del.type = "button";
      del.className = "ghost danger";
      del.textContent = "Remove";
      del.addEventListener("click", () => removeAccount(acct));
      actions.appendChild(del);

      row.appendChild(actions);
      host.appendChild(row);
    }
  }

  // Fill the appropriate form with an account's non-secret fields for editing.
  // The secret field stays empty and must be re-entered (spec section 3).
  function loadIntoForm(acct) {
    if (acct.platform === "bluesky") {
      const f = document.getElementById("bluesky-form");
      f.elements["id"].value = acct.id;
      f.elements["handle"].value = acct.handle || "";
      f.elements["service"].value = acct.service || "https://bsky.social";
      f.elements["app_password"].value = "";
      setStatus(f, "Re-enter the app password to re-validate.", "info");
      f.elements["app_password"].focus();
      f.scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      const f = document.getElementById("mastodon-form");
      f.elements["id"].value = acct.id;
      f.elements["instance_url"].value = acct.instance_url || "";
      f.elements["access_token"].value = "";
      setStatus(f, "Re-enter the access token to re-validate.", "info");
      f.elements["access_token"].focus();
      f.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  async function removeAccount(acct) {
    if (!window.confirm(`Remove ${acct.display_name || acct.id}?`)) return;
    try {
      await api("DELETE", `/api/accounts/${encodeURIComponent(acct.id)}`);
      await refresh();
    } catch (err) {
      window.alert(`Could not remove: ${err.message}`);
    }
  }

  // ------------------------------------------------------------------ //
  // Add / edit forms
  // ------------------------------------------------------------------ //
  function wireForms() {
    const bsky = document.getElementById("bluesky-form");
    bsky.addEventListener("submit", (e) => {
      e.preventDefault();
      submitForm(bsky, "/api/accounts/bluesky", {
        id: bsky.elements["id"].value || undefined,
        handle: bsky.elements["handle"].value,
        app_password: bsky.elements["app_password"].value,
        service: bsky.elements["service"].value,
      });
    });

    const masto = document.getElementById("mastodon-form");
    masto.addEventListener("submit", (e) => {
      e.preventDefault();
      submitForm(masto, "/api/accounts/mastodon", {
        id: masto.elements["id"].value || undefined,
        instance_url: masto.elements["instance_url"].value,
        access_token: masto.elements["access_token"].value,
      });
    });
  }

  async function submitForm(form, url, body) {
    setStatus(form, "Verifying…", "info");
    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    try {
      const resp = await api("POST", url, body);
      // On success show a green confirmation with the resolved identity, so the
      // user sees exactly which account it will post as (spec section 5).
      setStatus(form, `Verified: ${resp.account.display_name}`, "ok");
      form.reset();
      form.elements["id"].value = "";
      if (form.id === "bluesky-form") form.elements["service"].value = "https://bsky.social";
      await refresh();
    } catch (err) {
      // On failure show the specific reason and do NOT store (spec section 5).
      setStatus(form, err.message, "error");
    } finally {
      submitBtn.disabled = false;
    }
  }

  function setStatus(form, message, kind) {
    const el = form.querySelector('[data-role="status"]');
    el.textContent = message;
    el.className = `form-status ${kind}`;
  }

  // ------------------------------------------------------------------ //
  // Re-auth deep link (spec sections 9 and 11)
  // ------------------------------------------------------------------ //
  // The composer links here as /wizard?reauth=<account_id> after an auth
  // failure. Scroll to that account and pre-load it into its form so recovery
  // is one action rather than a hunt.
  function handleReauthParam() {
    const params = new URLSearchParams(window.location.search);
    const id = params.get("reauth");
    if (!id) return;
    const acct = accounts.find((a) => a.id === id);
    if (acct) loadIntoForm(acct);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
