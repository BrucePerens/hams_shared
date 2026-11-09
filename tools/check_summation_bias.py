#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Summation Bias Checker
----------------------
Detects if a file's size has been reduced by more than 5% compared to HEAD.
This helps prevent AI agents from summarizing and dropping critical nuance during edits.

During a merge commit (MERGE_HEAD present, `--staged` only) a file is judged against what the merge should have
produced rather than against the first parent, so a rewording that arrived from the merged-in branch is not a shrink
the author made; a shrink the author adds on top of it, or a conflict resolution that drops lines, still is. See
reference_size(). Ordinary commits are judged exactly as before.

With `--staged` (what git-hooks/pre-commit runs) only the changes staged for the commit are compared, and the new size is
the staged content's, so an unrelated edit another session has left in a shared working tree cannot block a commit.
"""

import os
import subprocess
import sys
import tempfile

def _git_out(args, cwd, check=True):
    return subprocess.run(["git", *args], capture_output=True, check=check, cwd=cwd).stdout


def _blob_size(spec, repo_root):
    """Size in bytes of the blob `spec` (e.g. HEAD:path), or None when it does not exist."""
    try:
        return int(_git_out(["cat-file", "-s", spec], repo_root).decode().strip())
    except subprocess.CalledProcessError:
        return None


def _blob_id(spec, repo_root):
    try:
        return _git_out(["rev-parse", "--verify", "-q", spec], repo_root).decode().strip()
    except subprocess.CalledProcessError:
        return None


def _merge_heads(repo_root):
    """Commits being merged into HEAD (the contents of MERGE_HEAD), empty for an ordinary commit."""
    try:
        path = _git_out(["rev-parse", "--git-path", "MERGE_HEAD"], repo_root).decode().strip()
        with open(os.path.join(repo_root, path), encoding="ascii") as f:
            return [line.strip() for line in f if line.strip()]
    except (subprocess.CalledProcessError, OSError):
        return []


def _clean_merge_size(filepath, heads, repo_root):
    """Size of what git's own three-way merge of the two sides gives for this file, or None when it
    conflicts or cannot be computed (an octopus merge, no merge base, a side without the file)."""
    if len(heads) != 1:
        return None
    try:
        base = _git_out(["merge-base", "HEAD", heads[0]], repo_root).decode().split()[0]
        ours = _git_out(["show", f"HEAD:{filepath}"], repo_root)
        theirs = _git_out(["show", f"{heads[0]}:{filepath}"], repo_root)
        base_content = _git_out(["show", f"{base}:{filepath}"], repo_root)
    except (subprocess.CalledProcessError, IndexError):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base_content), ("theirs", theirs)):
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], "wb") as f:
                f.write(data)
        res = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True, cwd=repo_root)
    return len(res.stdout) if res.returncode == 0 else None


def reference_size(filepath, old_size, heads, new_blob, repo_root):
    """The size the staged content is judged against, or None when this file is not to be judged.

    Ordinary commit: the size at HEAD. Merge commit: HEAD is the branch author's side and the merged-in commit is
    main's, so a file main reworded arrives 5-8 percent smaller through no act of the author. A file whose staged
    content is exactly what a merged-in parent has came from that parent and is not judged. Otherwise the reference
    is what git's own three-way merge of the two sides produces when it merges cleanly (so a shrink the author adds
    on top of main's is still caught), and when it conflicts, the smaller of the parents' versions (so a resolution
    that drops lines below both sides is still caught).
    """
    if not heads:
        return old_size
    sizes = [old_size]
    for head in heads:
        if _blob_id(f"{head}:{filepath}", repo_root) == new_blob:
            return None
        other = _blob_size(f"{head}:{filepath}", repo_root)
        if other is not None:
            sizes.append(other)
    clean = _clean_merge_size(filepath, heads, repo_root)
    return clean if clean is not None else min(sizes)


def _has_no_head():
    """True inside a git work tree that has no commit yet (the first commit)."""
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], capture_output=True, text=True)
    return inside.returncode == 0 and head.returncode != 0


def main():
    staged = "--staged" in sys.argv[1:]
    try:
        # `git rev-parse --show-toplevel` resolves correctly regardless of the CURRENT working
        # directory, as long as it's anywhere inside the repo -- unlike the naive relative-path
        # reads below, which is exactly the real bug this fixes (see comment at the
        # os.path.exists/os.path.getsize call sites).
        repo_root_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        repo_root = repo_root_res.stdout.strip()

        # Get list of modified files against HEAD
        diff_command = ["git", "diff", "--name-only"] + (["--cached"] if staged else ["HEAD"])
        res = subprocess.run(diff_command, capture_output=True, text=True, check=True)
        modified_files = [f for f in res.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError as error:
        # A repository with no commit yet has no HEAD to compare with: nothing to measure a reduction against, which is the
        # one legitimate "no diff". Anything else (not a git repository, a diff git cannot compute) means the checker could not
        # look, so it is not a pass (exit 2, with git's own message).
        if _has_no_head():
            sys.exit(0)
        detail = (error.stderr or "").strip() if isinstance(error.stderr, str) else ""
        print(f"[!] check_summation_bias: git could not list the modified files ({' '.join(error.cmd)}: {detail or 'exit ' + str(error.returncode)}); "
              "nothing was compared, so this is not a pass. Next: run it inside a git work tree.", file=sys.stderr)
        sys.exit(2)

    violation = False

    for filepath in modified_files:
        # Real bug found 2026-09-10: `git diff --name-only` always reports paths relative to
        # the REPO ROOT, regardless of the process's own current working directory -- but
        # os.path.exists/os.path.getsize resolve a relative path against the CURRENT working
        # directory instead. Running this script from anywhere other than the exact repo root
        # (any subdirectory -- entirely realistic: nothing about how this script is invoked
        # pins the caller's cwd to the repo root) made every real, non-deleted, modified file
        # resolve to a nonexistent path from that subdirectory's own point of view, silently
        # hitting the "file was deleted" branch below for EVERY file and reporting zero
        # violations -- completely defeating this exact anti-summation-bias check, with no
        # warning, on a script whose whole purpose is guarding against silent content loss.
        abs_filepath = os.path.join(repo_root, filepath)
        if not os.path.exists(abs_filepath):
            continue  # file was deleted, that's fine (or at least handled by other PR reviews)

        # We specifically care about prompt files, python files, and docs
        if not (filepath.endswith(".md") or filepath.endswith(".py") or filepath.endswith(".json")):
            continue

        try:
            old_size_res = subprocess.run(
                ["git", "cat-file", "-s", f"HEAD:{filepath}"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo_root,
            )
            old_size = int(old_size_res.stdout.strip())
        except subprocess.CalledProcessError:
            # File might be new (not in HEAD)
            continue

        heads = _merge_heads(repo_root) if staged else []
        if heads:
            ref_size = reference_size(filepath, old_size, heads, _blob_id(f":{filepath}", repo_root), repo_root)
            if ref_size is None:
                continue  # the staged content is a merged-in parent's own version of the file
            old_size = ref_size

        if staged:
            try:
                new_size = int(
                    subprocess.run(
                        ["git", "cat-file", "-s", f":{filepath}"],
                        capture_output=True, text=True, check=True, cwd=repo_root,
                    ).stdout.strip()
                )
            except subprocess.CalledProcessError:
                continue  # the path is no longer in the index: a staged deletion
        else:
            new_size = os.path.getsize(abs_filepath)
        
        if old_size > 0 and new_size < old_size:
            reduction_ratio = (old_size - new_size) / old_size
            if reduction_ratio > 0.05:
                print(f"❌ SUMMATION BIAS DETECTED in {filepath}")
                print(f"   - File size reduced by {reduction_ratio:.1%} (from {old_size} to {new_size} bytes).")
                print("   - CRITICAL REQUIREMENT: Did you summarize or drop critical rules/nuance during your edit?")
                print("   - If this deletion was intentional, you MUST manually bypass or ignore this error,")
                print("     AND you MUST explicitly explain the reduction in your response to assure the User")
                print("     and the Reviewer that it was not summation bias.")
                violation = True

    if violation:
        sys.exit(1)
    
if __name__ == "__main__":
    main()
