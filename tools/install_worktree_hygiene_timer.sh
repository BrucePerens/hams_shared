#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Installs the systemd user timer hams-worktree-hygiene.timer: every 30 minutes it runs worktree_hygiene.py --apply on the given
# repositories and writes the report to ~/.cache/worktree_hygiene.txt (STALE lines mean someone must commit, open a PR and merge it,
# unfinished or not). Same shape as hams-merge-queue and hams-gate-canary: a hams- unit name, a oneshot service at nice 10 with a
# timeout, a timer that counts from the end of the previous run, units in ~/.config/systemd/user.
# Usage: install_worktree_hygiene_timer.sh [REPO...]   (default: ~/workspace/hams_open ~/workspace/hams_com and its hams_shared)
# The service runs the tool from the main checkout (HAMS_SHARED_TOOLS, default ~/workspace/hams_open/hams_shared/tools), never from a
# worktree, so it survives that worktree being removed.
set -euo pipefail
tools=${HAMS_SHARED_TOOLS:-$HOME/workspace/hams_open/hams_shared/tools}
[ -f "$tools/worktree_hygiene.py" ] || { echo "no worktree_hygiene.py in $tools (set HAMS_SHARED_TOOLS)" >&2; exit 1; }
repos=("$@")
[ ${#repos[@]} -gt 0 ] || repos=("$HOME/workspace/hams_open" "$HOME/workspace/hams_com" "$HOME/workspace/hams_open/hams_shared")
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.cache"
cat > "$unit_dir/hams-worktree-hygiene.service" <<UNIT
[Unit]
Description=Remove merged worktrees and branches, report stale ones

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=20min
# The tool exits 1 when anything is STALE (that is the report, not a failure of the unit).
ExecStart=/bin/bash -c '/usr/bin/nice -n 10 /usr/bin/python3 $tools/worktree_hygiene.py --apply ${repos[*]} > $HOME/.cache/worktree_hygiene.txt 2>&1; true'
UNIT
cat > "$unit_dir/hams-worktree-hygiene.timer" <<UNIT
[Unit]
Description=Run the hams worktree hygiene sweep every 30 minutes

[Timer]
OnBootSec=5min
OnUnitInactiveSec=30min

[Install]
WantedBy=timers.target
UNIT
# An earlier version of this script used the names without the hams- prefix.
systemctl --user disable --now worktree-hygiene.timer 2>/dev/null || true
rm -f "$unit_dir/worktree-hygiene.service" "$unit_dir/worktree-hygiene.timer"
systemctl --user daemon-reload
systemctl --user enable --now hams-worktree-hygiene.timer
systemctl --user list-timers hams-worktree-hygiene.timer --no-pager | head -3
