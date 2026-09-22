#!/usr/bin/env bash
# ADR-0102. Installs this repo's tracked git hooks into .git/hooks/, which every worktree of the
# repo shares (hooks live in the common .git directory, not per-worktree) -- so this only needs to
# run once per repo checkout, not once per worktree. Safe to re-run; idempotent.
set -euo pipefail

common_git_dir=$(git rev-parse --git-common-dir)
hooks_src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/git-hooks" && pwd)"

for hook in "$hooks_src_dir"/*; do
    name=$(basename "$hook")
    dest="$common_git_dir/hooks/$name"
    cp "$hook" "$dest"
    chmod +x "$dest"
    echo "installed $name -> $dest"
done
