"""
server.py -- The Flask app: routing, validation, and the JSON API.

This is the only layer the browser talks to. It enforces the security boundary
from spec section 3: the browser sends non-secret data (post text, resized image
bytes, account selection) and receives non-secret data (sanitized account lists,
per-target status). Raw credentials enter only through the wizard's add/edit
endpoints and never leave again.

Routes fall into three groups:

  * Pages       -- GET / (composer or first-run redirect) and GET /wizard.
  * Account API -- list, add (with live validation), edit, remove.
  * Post + log  -- POST a composed post and fan it out; read the history log.

The app factory (``create_app``) builds the Flask instance; a module-level
``app`` is exposed for gunicorn (``app.server:app``).
"""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for

from . import bluesky, config, logstore, mastodon, posting, updatecheck
from .errors import ApiError


# Browser-sent images are base64, which inflates payloads ~33%, and a thread can
# carry several images. 64 MB is comfortably generous for a single-user tool
# while still rejecting anything absurd outright.
_MAX_CONTENT_LENGTH = 64 * 1024 * 1024

# Directory bind-mounted from the host where the app drops an update-request
# flag. A host-side watcher (watch-update.sh, run from cron) notices the flag
# and runs update.sh -- the container cannot rebuild its own image from inside
# itself, so the actual rebuild happens on the host.
_CONTROL_DIR = os.environ.get("BROADSIDE_CONTROL_DIR", "/control")


def create_app() -> Flask:
    """Construct and configure the Flask application."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

    # --- Pages --------------------------------------------------------------
    @app.route("/")
    def index():
        """Composer, or a redirect to the wizard on first run (spec section 5)."""
        if not config.is_configured():
            return redirect(url_for("wizard"))
        return render_template("composer.html")

    @app.route("/wizard")
    def wizard():
        """The setup wizard: first-run experience and ongoing settings screen."""
        return render_template("wizard.html")

    # --- Account API --------------------------------------------------------
    @app.route("/api/config")
    def get_config():
        """Return the SANITIZED account list -- never any secrets (section 3)."""
        return jsonify(config.public_view())

    @app.route("/api/accounts/bluesky", methods=["POST"])
    def add_bluesky():
        """Validate a Bluesky account live, then store it (spec section 5).

        Validation calls createSession; the account is saved ONLY if that
        succeeds, and the returned DID is cached. If an ``id`` is supplied we are
        editing/re-authenticating an existing account in place.
        """
        body = request.get_json(force=True, silent=True) or {}
        handle = (body.get("handle") or "").strip()
        app_password = (body.get("app_password") or "").strip()
        service = (body.get("service") or "https://bsky.social").strip()
        account_id = body.get("id") or config.new_account_id("bluesky")

        if not handle or not app_password:
            return _error_response("Handle and app password are required.", 400)

        try:
            session = bluesky.create_session(handle, app_password, service)
        except ApiError as err:
            # Do not store the account; report the specific reason (section 5).
            return _api_error_response(err)

        account = {
            "id": account_id,
            "handle": handle,
            "app_password": app_password,
            "service": service,
            "did": session["did"],
            # Prefer the server-resolved handle for display when available.
            "display_name": session.get("handle", handle),
        }
        config.upsert_account("bluesky", account)
        return jsonify({"ok": True, "account": _public_account("bluesky", account)})

    @app.route("/api/accounts/mastodon", methods=["POST"])
    def add_mastodon():
        """Validate a Mastodon account live, then store it with limits (section 5).

        On success we cache the ``@user@instance`` display name AND immediately
        discover and cache the instance limits (spec section 8).
        """
        body = request.get_json(force=True, silent=True) or {}
        instance_url = (body.get("instance_url") or "").strip()
        access_token = (body.get("access_token") or "").strip()
        account_id = body.get("id") or config.new_account_id("mastodon")

        if not instance_url or not access_token:
            return _error_response("Instance URL and access token are required.", 400)
        if not instance_url.startswith(("http://", "https://")):
            instance_url = "https://" + instance_url

        try:
            account_info = mastodon.verify_credentials(instance_url, access_token)
            limits = mastodon.get_instance_limits(instance_url, access_token)
        except ApiError as err:
            return _api_error_response(err)

        account = {
            "id": account_id,
            "instance_url": instance_url,
            "access_token": access_token,
            "display_name": mastodon.account_display_name(instance_url, account_info),
            "limits": limits,
        }
        config.upsert_account("mastodon", account)
        return jsonify({"ok": True, "account": _public_account("mastodon", account)})

    @app.route("/api/accounts/<account_id>", methods=["DELETE"])
    def delete_account(account_id: str):
        """Remove an account (spec section 5). Idempotent-ish: 404 if absent."""
        removed = config.remove_account(account_id)
        if not removed:
            return _error_response("Account not found.", 404)
        return jsonify({"ok": True})

    # --- Post + log ---------------------------------------------------------
    @app.route("/api/post", methods=["POST"])
    def post():
        """Fan a composed post out to the selected accounts (spec section 9).

        Request body::

            {
              "account_ids": ["bsky-...", "masto-..."],
              "entries": [{"text": "...", "images": [{"data","mime","alt"}]}]
            }

        Returns ``{"results": [<per-target status>, ...]}``. The HTTP status is
        200 even when some targets fail -- the per-target results carry the
        real outcomes, because the composer never collapses to a single
        success/failure (spec section 11).
        """
        body = request.get_json(force=True, silent=True) or {}
        account_ids = body.get("account_ids") or []
        entries = body.get("entries") or []

        # Basic shape validation. Deeper rules (alt text, limits) are enforced
        # client-side and re-checked in the posting layer.
        problem = _validate_post(account_ids, entries)
        if problem:
            return _error_response(problem, 400)

        cfg = config.load_config()
        results = posting.post_all(cfg, account_ids, entries)
        return jsonify({"results": results})

    @app.route("/api/log")
    def get_log():
        """Return recent post-attempt history, newest first (spec section 11)."""
        return jsonify({"entries": logstore.read_recent()})

    # --- Self-update --------------------------------------------------------
    @app.route("/api/version")
    def api_version():
        """Report the running commit vs the latest on GitHub.

        Drives the composer's "update available" banner. Never errors: an
        unreachable GitHub simply yields update_available=false.
        """
        return jsonify(updatecheck.status())

    @app.route("/api/update", methods=["POST"])
    def api_update():
        """Request a rebuild by dropping a flag the host watcher polls.

        The app can't rebuild its own container from inside itself, so it drops
        a flag file in the bind-mounted control dir; watch-update.sh (host cron)
        runs update.sh when it sees it. If the control dir isn't mounted (e.g. a
        dev run), say so plainly rather than pretending it worked.
        """
        try:
            os.makedirs(_CONTROL_DIR, exist_ok=True)
            with open(os.path.join(_CONTROL_DIR, "update_requested"), "w", encoding="utf-8") as fh:
                fh.write(updatecheck.latest_version() or "")
        except OSError as exc:
            return _error_response(f"Update channel unavailable: {exc}", 500)
        return jsonify({"ok": True, "queued": True})

    return app


# --- Small helpers ----------------------------------------------------------
def _validate_post(account_ids: list[Any], entries: list[Any]) -> str | None:
    """Return an error message if the post request is malformed, else None."""
    if not account_ids:
        return "Select at least one account."
    if not entries:
        return "There is nothing to post."
    for i, entry in enumerate(entries):
        text = (entry.get("text") or "").strip()
        images = entry.get("images") or []
        # An entry must carry either text or an image. Text-only entries are
        # allowed (spec section 12), but a wholly empty entry is not postable.
        if not text and not images:
            return f"Entry {i + 1} is empty."
        # Alt text is a hard block on every image (spec section 10).
        for img in images:
            if not (img.get("alt") or "").strip():
                return f"An image in entry {i + 1} is missing alt text."
    return None


def _public_account(platform: str, account: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single stored account for return to the browser."""
    view = config.public_view({"bluesky_accounts": [], "mastodon_accounts": []})
    # Reuse the same field selection public_view applies, for one account.
    if platform == "bluesky":
        return {
            "id": account["id"],
            "platform": "bluesky",
            "handle": account["handle"],
            "service": account["service"],
            "display_name": account["display_name"],
            "limits": config.BLUESKY_LIMITS,
        }
    return {
        "id": account["id"],
        "platform": "mastodon",
        "instance_url": account["instance_url"],
        "display_name": account["display_name"],
        "limits": account.get("limits", {}),
    }


def _error_response(message: str, status: int):
    """A plain JSON error for validation failures."""
    return jsonify({"ok": False, "error": {"message": message}}), status


def _api_error_response(err: ApiError):
    """Turn an ``ApiError`` into a JSON body carrying friendly + raw detail.

    Used by the wizard endpoints so a failed live validation shows the specific
    reason (spec section 5) with the raw detail available underneath.
    """
    payload = {"ok": False, "error": err.to_dict()}
    payload["error"]["is_auth"] = err.is_auth
    # 502 for unreachable (upstream problem), 400 for a genuine rejection.
    http_status = 502 if err.kind == "unreachable" else 400
    return jsonify(payload), http_status


# Module-level app for gunicorn: ``gunicorn app.server:app``.
app = create_app()


if __name__ == "__main__":
    # Local development entry point: ``python -m app.server``.
    # Binds to all interfaces so it is reachable from other LAN devices, which
    # is the intended deployment posture (spec section 3). threaded=True lets a
    # slow post (async media polling) not block a second request.
    import os

    port = int(os.environ.get("BROADSIDE_PORT", "8080"))
    app.run(host="0.0.0.0", port=port, threaded=True)
