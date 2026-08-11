#!/usr/bin/env bash
# Remove JSF from a host, touching nothing else.
#
# This runs on a machine that hosts other people's work. Every path and
# unit below is checked against an explicit allowlist before it is removed,
# and the script verifies afterwards that the neighbour is still running.
#
# Deliberately NOT removed:
#   /opt/polymarketstrat  and any other directory
#   /swapfile, /swapfile2 - swapoff on a live box forces swapped pages back
#                           into RAM at once and can OOM-kill a neighbour
#   /etc/sysctl.d/99-jsf.conf - only sets vm.swappiness=20, which is a sane
#                           value the remaining workload now depends on
#
# Pull anything worth keeping FIRST (see --keep-check below); this deletes
# the recorded book history, which took days to accumulate.
#
#   bash deploy/uninstall_jsf.sh --dry-run    # show what would go
#   bash deploy/uninstall_jsf.sh --yes        # actually remove it
set -euo pipefail

DIR=/opt/jsf
DRY=1
for arg in "$@"; do
  case "$arg" in
    --yes) DRY=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
say() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else echo "[remove]  $*"; fi; }
run() { if [ "$DRY" = 1 ]; then echo "          would run: $*"; else "$@"; fi; }

# --- what else is on this box, before we start ----------------------------
echo "=== neighbours (recorded so we can confirm they survive) ==="
BEFORE=$(ls -d /opt/*/ 2>/dev/null | grep -v '^/opt/jsf/$' || true)
echo "${BEFORE:-  (none)}"
# Count the neighbour's processes WITHOUT counting ourselves. `pgrep -f`
# matches any command line containing the pattern, and the shell running
# this script mentions it -- a self-match would report the neighbour as
# alive no matter what actually happened to it, which is the one thing
# this check exists to detect.
neigh_count() {
  local n=0 pid
  for pid in $(pgrep -f '/opt/polymarketstrat' 2>/dev/null || true); do
    grep -qa 'uninstall_jsf' "/proc/$pid/cmdline" 2>/dev/null && continue
    n=$((n + 1))
  done
  echo "$n"
}
NEIGH_PROCS=$(neigh_count)
echo "  polymarketstrat processes: $NEIGH_PROCS"
echo

# --- data worth keeping ---------------------------------------------------
if [ -d "$DIR/data/live/books" ]; then
  N=$(ls "$DIR/data/live/books" 2>/dev/null | wc -l)
  SZ=$(du -sh "$DIR/data/live/books" 2>/dev/null | cut -f1)
  echo "!! $DIR/data/live/books holds $N distilled book files ($SZ)."
  echo "   That is the ask-survival evidence -- it cannot be re-fetched,"
  echo "   the venue serves no book history. Copy it off first:"
  echo "     scp -r root@\$(hostname -I | awk '{print \$1}'):$DIR/data/live/books ./books_old_droplet"
  echo
fi

# --- units ----------------------------------------------------------------
# Only ever unit names beginning jsf-, resolved from what systemd has loaded.
UNITS=$(systemctl list-unit-files --no-legend 'jsf-*' 2>/dev/null \
        | awk '{print $1}' || true)
INSTANCES=$(systemctl list-units --no-legend --all 'jsf-*' 2>/dev/null \
        | awk '{print $1}' || true)
ALL=$(printf '%s\n%s\n' "$UNITS" "$INSTANCES" | sed '/^$/d' | sort -u)
if [ -n "$ALL" ]; then
  echo "=== systemd units ==="
  for u in $ALL; do
    case "$u" in
      jsf-*) ;;
      *) echo "REFUSING: '$u' is not a jsf unit" >&2; exit 3 ;;
    esac
    say "stop + disable $u"
    run systemctl disable --now "$u" 2>/dev/null || true
  done
  # jsf.slice has no hyphen, so the jsf-* glob alone would leave it behind
  say "remove /etc/systemd/system/jsf-* and jsf.slice"
  if [ "$DRY" = 1 ]; then
    ls -d /etc/systemd/system/jsf-* /etc/systemd/system/jsf.slice \
      2>/dev/null | sed 's/^/          /' || true
  else
    systemctl stop jsf.slice 2>/dev/null || true
    rm -f /etc/systemd/system/jsf-* /etc/systemd/system/jsf.slice
    systemctl daemon-reload
    systemctl reset-failed 2>/dev/null || true
  fi
  echo
fi

# --- the directory --------------------------------------------------------
if [ -d "$DIR" ]; then
  SZ=$(du -sh "$DIR" 2>/dev/null | cut -f1)
  say "delete $DIR ($SZ)"
  run rm -rf "$DIR"
else
  echo "$DIR is already gone"
fi

# --- confirm we broke nothing --------------------------------------------
echo
echo "=== after ==="
AFTER=$(ls -d /opt/*/ 2>/dev/null | grep -v '^/opt/jsf/$' || true)
if [ "$BEFORE" != "$AFTER" ]; then
  echo "!! /opt changed beyond removing jsf -- investigate" >&2
  echo "before: $BEFORE"
  echo "after:  $AFTER"
  exit 4
fi
echo "${AFTER:-  (nothing else in /opt)}"
NOW=$(neigh_count)
echo "  polymarketstrat processes: $NOW (was $NEIGH_PROCS)"
if [ "$DRY" = 0 ] && [ "$NEIGH_PROCS" -gt 0 ] && [ "$NOW" -eq 0 ]; then
  echo "!! the neighbour is no longer running -- this script did not stop it,"
  echo "   but check it before walking away" >&2
fi
free -m | head -2
echo
if [ "$DRY" = 1 ]; then
  echo "DRY RUN -- nothing was changed. Re-run with --yes to remove."
else
  echo "JSF removed. Swap, sysctl and every other directory left as they were."
fi
