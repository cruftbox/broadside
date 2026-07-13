#!/bin/sh
# watch-update.sh -- Host-side watcher that performs app-requested updates.
#
# The Broadside container cannot rebuild its own image from inside itself (doing
# so would kill the very process running the rebuild). So when the in-app
# "Update now" button is pressed, the app drops a flag file in the bind-mounted
# control directory, and this watcher -- run from cron on the host, once a
# minute -- notices the flag, removes it, and runs update.sh to pull + rebuild.
#
# It ships in the repo next to update.sh and is a no-op (fast exit) when no
# update has been requested, so running it every minute is cheap.
#
# Install (QNAP, survives reboots):
#   echo '* * * * * /share/CACHEDEV1_DATA/apps/broadside/watch-update.sh' >> /etc/config/crontab
#   crontab /etc/config/crontab
# Remove: delete that line from /etc/config/crontab and reload with crontab.

set -eu

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
FLAG="$APP_DIR/.deploy/control/update_requested"
LOG="$APP_DIR/.deploy/watch.log"

# Nothing requested -> exit immediately (the common case, once a minute).
[ -f "$FLAG" ] || exit 0

# Remove the flag first so a rebuild that restarts things can't re-trigger, and
# so a fresh request during this run is honored on the next tick.
rm -f "$FLAG"

echo "$(date '+%Y-%m-%d %H:%M:%S') update requested via app; running update.sh --force" >> "$LOG"

# --force because the user explicitly asked to update; rebuild even if the SHA
# comparison is momentarily equal. update.sh writes its own detailed log.
# Invoked via `sh` so it works regardless of update.sh's executable bit (which
# a git checkout on some filesystems may not preserve).
if sh "$APP_DIR/update.sh" --force >> "$LOG" 2>&1; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') update complete" >> "$LOG"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') update FAILED (see update log)" >> "$LOG"
fi
