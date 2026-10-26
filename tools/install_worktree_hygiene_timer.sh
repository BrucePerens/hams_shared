#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Installs a systemd user timer that runs worktree_hygiene.py --apply on the given repositories every 30 minutes and writes the
# report to ~/.cache/worktree_hygiene.txt (STALE lines mean someone must commit, open a PR and merge, unfinished or not).
# Usage: install_worktree_hygiene_timer.sh REPO...   (default: ~/workspace/hams_open ~/workspace/hams_com and its hams_shared)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repos=("$@")
[ ${#repos[@]} -gt 0 ] || repos=("$HOME/workspace/hams_open" "$HOME/workspace/hams_com" "$HOME/workspace/hams_open/hams_shared")
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir" "$HOME/.cache"
cat > "$unit_dir/worktree-hygiene.service" <<UNIT
[Unit]
Description=Remove merged worktrees and branches, report stale ones

[Service]
Type=oneshot
ExecStart=/bin/bash -c '/usr/bin/python3 $here/worktree_hygiene.py --apply ${repos[*]} > $HOME/.cache/worktree_hygiene.txt 2>&1; true'
UNIT
cat > "$unit_dir/worktree-hygiene.timer" <<UNIT
[Unit]
Description=Worktree hygiene every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now worktree-hygiene.timer
systemctl --user list-timers worktree-hygiene.timer --no-pager | head -3
