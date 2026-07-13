#!/bin/sh
# update.sh -- Update Broadside on the server from GitHub and rebuild it.
#
# This runs ON the server (e.g. bandersnatch), not from a dev machine: copying
# files over SSH from a laptop proved fragile. It pulls the repository as a
# tarball via the GitHub API -- no `git` binary required, and it works for the
# private repo when given a token -- then rebuilds with docker compose.
#
# It is safe to run by hand (you or an operator over SSH) and safe to invoke
# programmatically: it is idempotent and does nothing when already up to date
# unless --force is given.
#
# USAGE
#   ./update.sh            Update only if GitHub is ahead of what is deployed.
#   ./update.sh --force    Rebuild from the latest commit even if unchanged.
#   ./update.sh --check    Report status only; make no changes. Exit code:
#                            0 = up to date, 10 = update available, 1 = error.
#
# AUTH
#   The broadside repo is public, so a token is OPTIONAL -- the script works
#   unauthenticated. Provide one only for a private fork, or to raise GitHub's
#   unauthenticated API rate limit (60 requests/hour). It is read from (in
#   order): the GITHUB_TOKEN env var, or the file $APP_DIR/.deploy/github_token
#   (a fine-grained PAT with Contents: Read, chmod 600). The token never leaves
#   the server.
#
# CONFIG (override via environment)
#   BROADSIDE_REPO      GitHub owner/repo         (default cruftbox/broadside)
#   BROADSIDE_BRANCH    branch to track           (default main)
#   DOCKER              docker binary             (default: PATH, else QNAP path)

set -eu

# --- Resolve paths ----------------------------------------------------------
# APP_DIR is the directory this script lives in, which is also the compose
# project directory (the script ships inside the repo).
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="$APP_DIR/.deploy"
TOKEN_FILE="$DEPLOY_DIR/github_token"
STATE_FILE="$DEPLOY_DIR/deployed_sha"
LOG_FILE="$DEPLOY_DIR/update.log"

REPO="${BROADSIDE_REPO:-cruftbox/broadside}"
BRANCH="${BROADSIDE_BRANCH:-main}"

# Docker isn't on the PATH under QNAP Container Station, so fall back to its
# known location. Override with the DOCKER env var elsewhere.
if [ -n "${DOCKER:-}" ]; then
  :
elif command -v docker >/dev/null 2>&1; then
  DOCKER="docker"
else
  DOCKER="/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker"
fi

mkdir -p "$DEPLOY_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }
die() { log "ERROR: $*"; exit 1; }

# --- Token ------------------------------------------------------------------
# Token is optional (the repo is public). When present it is used for a private
# fork or a higher rate limit. Strip whitespace/newlines so a token pasted into
# the file (which usually gains a trailing newline) still forms a valid header.
TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$TOKEN_FILE" ]; then
  TOKEN="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"
fi

API="https://api.github.com/repos/$REPO"
ACCEPT="Accept: application/vnd.github+json"

# curl wrapper that adds the Authorization header only when a token is set, so
# the same calls work authenticated (private fork) or anonymous (public repo).
gh_curl() {
  if [ -n "$TOKEN" ]; then
    curl -fsSL -H "Authorization: Bearer $TOKEN" -H "$ACCEPT" "$@"
  else
    curl -fsSL -H "$ACCEPT" "$@"
  fi
}

# --- Determine latest and current SHAs -------------------------------------
# Latest commit on the tracked branch. Parse the first "sha" from the commit
# object without depending on jq (which QNAP may not have).
latest_sha() {
  gh_curl "$API/commits/$BRANCH" \
    | grep -m1 '"sha"' \
    | sed -E 's/.*"sha" *: *"([0-9a-f]+)".*/\1/'
}

LATEST="$(latest_sha)" || die "could not reach GitHub (check network; a private fork needs a token)"
[ -n "$LATEST" ] || die "could not parse latest commit SHA from GitHub"
CURRENT="$(cat "$STATE_FILE" 2>/dev/null || echo '')"

MODE="update"
case "${1:-}" in
  --check) MODE="check" ;;
  --force) MODE="force" ;;
  "") ;;
  *) die "unknown argument: $1" ;;
esac

short() { echo "$1" | cut -c1-7; }

if [ "$MODE" = "check" ]; then
  if [ "$CURRENT" = "$LATEST" ]; then
    log "up to date at $(short "$LATEST")"
    exit 0
  fi
  log "update available: deployed $(short "${CURRENT:-none}") -> latest $(short "$LATEST")"
  exit 10
fi

if [ "$CURRENT" = "$LATEST" ] && [ "$MODE" != "force" ]; then
  log "already up to date at $(short "$LATEST"); nothing to do (use --force to rebuild)"
  exit 0
fi

# --- Download and extract the tarball --------------------------------------
log "updating $REPO@$BRANCH: $(short "${CURRENT:-none}") -> $(short "$LATEST")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

gh_curl "$API/tarball/$BRANCH" -o "$TMP/src.tgz" \
  || die "tarball download failed"
mkdir -p "$TMP/src"
tar xzf "$TMP/src.tgz" -C "$TMP/src" || die "tarball extract failed"

# GitHub wraps everything in a single top-level dir (owner-repo-<sha>/).
SRC="$(find "$TMP/src" -mindepth 1 -maxdepth 1 -type d | head -n1)"
[ -n "$SRC" ] || die "extracted source directory not found"

# --- Sync into the app dir --------------------------------------------------
# Never touch the runtime data or the deploy state. The tarball never contains
# data/ or .deploy/ (both gitignored), so preserving them is automatic; the
# excludes below make it explicit and let rsync prune files deleted upstream.
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude='/data' --exclude='/.deploy' --exclude='/.docker' \
    "$SRC"/ "$APP_DIR"/ || die "rsync into app dir failed"
else
  # Fallback without rsync: copy contents over the app dir. This adds/updates
  # files but will not remove files deleted upstream.
  cp -R "$SRC"/. "$APP_DIR"/ || die "copy into app dir failed"
fi

# --- Rebuild ----------------------------------------------------------------
# HOME/DOCKER_CONFIG are redirected into the app dir because QNAP's docker
# compose otherwise tries to create a per-user config dir the service user
# cannot write (mkdir .../container-station/homes/<user>: permission denied).
log "rebuilding container with docker compose"
(
  cd "$APP_DIR"
  HOME="$APP_DIR" DOCKER_CONFIG="$APP_DIR/.docker" \
    "$DOCKER" compose up -d --build
) >>"$LOG_FILE" 2>&1 || die "docker compose build/up failed (see $LOG_FILE)"

# --- Record the deployed SHA ------------------------------------------------
echo "$LATEST" > "$STATE_FILE"
log "updated to $(short "$LATEST")"
