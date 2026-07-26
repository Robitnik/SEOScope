#!/usr/bin/env bash
# One-time cleanup of everything this container left root-owned while it still ran as root.
#
# Why this exists: api/worker now run as the operator's own uid (compose `user:`), but that only
# stops NEW damage. Files already on disk are root-owned and no amount of restarting fixes them —
# and one of them, AISeoTools' `state/locks/main.download.lock`, hard-blocks every host-side
# export (`flock` needs O_RDWR; `-rw-r--r-- root root` denies it to the operator forever, while
# root goes on opening the operator's locks fine).
#
# Everything here is either a zero-byte lock file, a disposable download, a build cache or a
# volume this container owns outright. Nothing carries data worth keeping, which is why plain
# `rm`/`chown` is the whole remedy.
#
#   bash scripts/derootify.sh            # show what would change, touch nothing
#   sudo bash scripts/derootify.sh --go  # actually do it
#
# Run it from the repo root. Re-running is harmless: it re-checks and reports "clean".
set -euo pipefail

GO=0
[ "${1:-}" = "--go" ] && GO=1

# The operator, not whoever invoked sudo. `SUDO_UID` is set when this runs under sudo; the plain
# `id -u` fallback covers the dry run, where we are already the operator.
UID_TARGET="${SUDO_UID:-$(id -u)}"
GID_TARGET="${SUDO_GID:-$(id -g)}"
OPERATOR_HOME="$(getent passwd "$UID_TARGET" | cut -d: -f6)"
SEO_RUNTIME="${SEOTOOLS_ROOT:-$OPERATOR_HOME/.local/share/seo-audit}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$UID_TARGET" = "0" ]; then
  echo "Refusing to run: the target operator resolves to root (uid 0)." >&2
  echo "Use 'sudo bash scripts/derootify.sh --go' from your normal user, not a root shell." >&2
  exit 1
fi

echo "operator : $UID_TARGET:$GID_TARGET ($OPERATOR_HOME)"
echo "runtime  : $SEO_RUNTIME"
echo "repo     : $REPO"
echo "mode     : $([ "$GO" = 1 ] && echo 'APPLYING' || echo 'dry run — pass --go to apply')"
echo

found=0

run() {
  found=1
  echo "  \$ $*"
  [ "$GO" = 1 ] && "$@"
  return 0
}

# ── 1. AISeoTools shared locks ─────────────────────────────────────────────────
# Zero-byte flock files. Deleting one is always safe: it holds no state, and the next process to
# want it recreates it (AISeoTools chmods 0666 on every open now, so the recreated one is shared).
echo "[1/4] AISeoTools shared locks — $SEO_RUNTIME/state/locks"
if [ -d "$SEO_RUNTIME/state/locks" ]; then
  while IFS= read -r -d '' f; do
    echo "  root-owned lock: $f"
    run rm -f "$f"
  done < <(find "$SEO_RUNTIME/state/locks" -maxdepth 1 -type f ! -uid "$UID_TARGET" -print0)
else
  echo "  (no locks directory yet)"
fi

# ── 2. AISeoTools shared download drop ─────────────────────────────────────────
# The host Chrome writes exports here and the container reads them back. Disposable scratch, but a
# root-owned SUBDIRECTORY (0755) is one the host Chrome cannot write into at all.
echo "[2/4] AISeoTools download drop — $SEO_RUNTIME/downloads"
if [ -d "$SEO_RUNTIME/downloads" ] && find "$SEO_RUNTIME/downloads" ! -uid "$UID_TARGET" -print -quit | grep -q .; then
  run chown -R "$UID_TARGET:$GID_TARGET" "$SEO_RUNTIME/downloads"
else
  echo "  clean"
fi

# ── 3. This repository's build caches and scratch output ───────────────────────
echo "[3/4] repo build caches — $REPO"
for d in .mypy_cache .pytest_cache .ruff_cache out app_data; do
  target="$REPO/$d"
  [ -e "$target" ] || continue
  if find "$target" ! -uid "$UID_TARGET" -print -quit | grep -q .; then
    run chown -R "$UID_TARGET:$GID_TARGET" "$target"
  fi
done
[ "$found" = 1 ] || echo "  clean"

# ── 4. Named volumes ───────────────────────────────────────────────────────────
# Not chowned here: they need the compose stack DOWN, and doing it silently under a running
# container risks a half-written SQLite WAL. entrypoint.sh refuses to start and prints this same
# command if the volume is still root-owned, so this is a reminder, not a hidden step.
echo "[4/4] docker named volumes"
echo "  Run with the stack stopped (entrypoint.sh will tell you if it is still needed):"
echo "    docker compose down"
echo "    docker run --rm -v theyovthclientsreport_app_data:/v -v theyovthclientsreport_artifacts:/a \\"
echo "      -v theyovthclientsreport_browser_profiles:/b alpine chown -R $UID_TARGET:$GID_TARGET /v /a /b"
echo "    docker compose up -d --build"

echo
if [ "$GO" = 1 ]; then
  echo "Done. Verify the host CLI is unblocked:"
else
  echo "Dry run only — nothing changed. Re-run with: sudo bash scripts/derootify.sh --go"
  echo "Then verify the host CLI is unblocked:"
fi
echo "  python -c \"from seo_audit.browser.profile import BrowserProfile as B;"
echo "             p=B('ahrefs');"
echo "             [__import__('contextlib').ExitStack().enter_context(l()) for l in (p.busy_lock,p.window_lock,p.download_lock)];"
echo "             print('locks OK')\""
