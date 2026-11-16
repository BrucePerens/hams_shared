#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fixes the mechanical lint breakage of a branch before merge_pr.py sees it.

    python3 hams_shared/tools/lint_autofix.py [--tree DIR] [--base origin/main] [--dry-run] [--confirm-claim CLAIM.md ...]

Run it in your worktree (any of hams_com, hams_open, hams_shared). It looks only at files changed against `--base` (committed,
staged or not) and:
  - runs `cargo fmt` in each touched crate;
  - removes unused imports (F401) and the `f` prefix of f-strings without placeholders (F541) in changed .py files (not `__init__.py`);
    a file whose fix would not parse is left as it was;
  - LISTS stale claim hashes, anchor-docs-standard findings and missing function-test anchors that name a changed file or anchor.
It never confirms a claim by itself: a claim's hash only says the code changed, and only a reader can say the wording is still
true. Read the claim and the code; when the wording is still true, `--confirm-claim <claim.md>` rewrites that claim's code_hash
(and `hash_algorithm: v2`) to the current one. When it is not, rewrite the claim with write_claim.py.
It prints what it changed and what remains; exit 0 when nothing remains, 1 when findings are left for you.
"""
import argparse
import ast
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ANCHOR_RE = re.compile(r"@ANCHOR(?:-BEGIN)?:\s*([A-Za-z0-9_:.\-]+)")
UNUSED_RE = re.compile(r"'([^']+)' imported but unused")
FLAKE_LINE_RE = re.compile(r"^(.*?):(\d+):(\d+): (\w+) (.*)$")


def run(argv, cwd, timeout=900):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as error:
        return subprocess.CompletedProcess(argv, 124, "", str(error))


# [@ANCHOR: lint_autofix_changed_files]
# Verified by [@ANCHOR: test_changed_files_lists_committed_and_uncommitted_work]
def changed_files(tree, base="origin/main"):
    """Paths (relative to `tree`) of files that exist and differ from the merge base with `base`, plus untracked and uncommitted ones."""
    names = set()
    commands = (["git", "diff", "--name-only", "--diff-filter=d", base + "...HEAD"], ["git", "diff", "--name-only", "--diff-filter=d", "HEAD"],
                ["git", "ls-files", "--others", "--exclude-standard"])
    for argv in commands:
        result = run(argv, tree)
        if result.returncode == 0:
            names.update(line for line in result.stdout.splitlines() if line)
    return sorted(name for name in names if os.path.isfile(os.path.join(tree, name)))


# [@ANCHOR: lint_autofix_crates]
# Verified by [@ANCHOR: test_touched_crates_are_the_nearest_cargo_toml_directories]
def touched_crates(tree, files):
    """Directories (relative to `tree`) of the nearest Cargo.toml above each changed file."""
    crates = set()
    for name in files:
        directory = os.path.dirname(name)
        while directory:
            if os.path.exists(os.path.join(tree, directory, "Cargo.toml")):
                crates.add(directory)
                break
            directory = os.path.dirname(directory)
    return sorted(crates)


def _flake8_name(node, alias):
    """The name pyflakes prints for an unused import: `os.path`, `x.a`, `.m.a`, with ` as b` when renamed."""
    if isinstance(node, ast.Import):
        full = alias.name
    else:
        module = "." * node.level + (node.module or "")
        full = module + alias.name if module.endswith(".") else module + "." + alias.name
    return full + (" as " + alias.asname if alias.asname and alias.asname != alias.name else "")


# [@ANCHOR: lint_autofix_remove_unused]
# Verified by [@ANCHOR: test_unused_imports_are_removed_whole_or_by_name]
def remove_unused_imports(source, unused):
    """`source` with the imports flake8 called unused removed. `unused` maps a line number to the set of unused names flake8 printed
    for it. A statement whose every name is unused goes; otherwise the statement is rewritten without them (one line)."""
    lines = source.split("\n")
    tree = ast.parse(source)
    edits = []  # (first line, last line, replacement lines), applied bottom up
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.lineno not in unused:
            continue
        gone = unused[node.lineno]
        keep = [a for a in node.names if _flake8_name(node, a) not in gone]
        if keep:
            indent = re.match(r"\s*", lines[node.lineno - 1]).group(0)
            parts = [a.name + (" as " + a.asname if a.asname else "") for a in keep]
            head = "import " if isinstance(node, ast.Import) else "from %s%s import " % ("." * node.level, node.module or "")
            edits.append((node.lineno, node.end_lineno, [indent + head + ", ".join(parts)]))
        else:
            edits.append((node.lineno, node.end_lineno, []))
    for first, last, replacement in sorted(edits, reverse=True):
        lines[first - 1:last] = replacement
    return "\n".join(lines)


# [@ANCHOR: lint_autofix_strip_f]
# Verified by [@ANCHOR: test_f_prefix_is_dropped_only_where_flake8_says_so]
def strip_f_prefix(source, positions):
    """`source` with the `f` removed from the string prefix starting at each (line, column) in `positions` (1-based line, 0-based column)."""
    lines = source.split("\n")
    for line_no, col in sorted(positions, reverse=True):
        text = lines[line_no - 1]
        match = re.match(r"[rRbBuUfF]*", text[col:])
        prefix = match.group(0)
        if "f" in prefix.lower():
            new = re.sub("[fF]", "", prefix, count=1)
            lines[line_no - 1] = text[:col] + new + text[col + len(prefix):]
    return "\n".join(lines)


def flake8_findings(tree, files):
    result = run(["flake8", "--select=F401,F541", "--per-file-ignores=__init__.py:F401"] + files, tree)
    found = {}
    for line in result.stdout.splitlines():
        match = FLAKE_LINE_RE.match(line)
        if match:
            found.setdefault(match.group(1), []).append((int(match.group(2)), int(match.group(3)), match.group(4), match.group(5)))
    return found


# [@ANCHOR: lint_autofix_fix_python]
# Verified by [@ANCHOR: test_python_files_are_fixed_and_unparsable_results_are_reverted]
def fix_python(tree, files, dry_run=False, say=print):
    """Applies the F401/F541 fixes to the changed .py files; returns the list of files changed."""
    targets = [f for f in files if f.endswith(".py") and os.path.basename(f) != "__init__.py"]
    changed = []
    if not targets:
        return changed
    for name, findings in sorted(flake8_findings(tree, targets).items()):
        path = os.path.join(tree, name)
        with open(path, encoding="utf-8") as handle:
            original = handle.read()
        unused, positions = {}, []
        for line_no, col, code, message in findings:
            if code == "F401":
                match = UNUSED_RE.search(message)
                if match:
                    unused.setdefault(line_no, set()).add(match.group(1))
            elif code == "F541":
                positions.append((line_no, col - 1))
        try:
            fixed = strip_f_prefix(original, positions) if positions else original
            if unused:
                fixed = remove_unused_imports(fixed, unused)  # an f-prefix edit never adds or removes a line, so line numbers still hold
            ast.parse(fixed)
        except SyntaxError:
            say("left alone (the fix would not parse): " + name)
            continue
        if fixed != original:
            if not dry_run:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(fixed)
            changed.append(name)
            say("%s %s (%d unused import(s), %d f-string(s))" % ("would fix" if dry_run else "fixed", name, sum(map(len, unused.values())), len(positions)))
    return changed


def cargo_fmt(tree, crates, dry_run=False, say=print):
    """Runs `cargo fmt` in each crate; returns the crates whose files changed."""
    changed = []
    for crate in crates:
        argv = ["cargo", "fmt", "--", "--check"] if dry_run else ["cargo", "fmt"]
        result = run(argv, os.path.join(tree, crate))
        if (result.returncode != 0 and dry_run) or (not dry_run and result.returncode == 0 and _dirty(tree, crate)):
            changed.append(crate)
            say("%s cargo fmt in %s" % ("would run" if dry_run else "ran", crate))
        elif result.returncode != 0 and not dry_run:
            say("cargo fmt failed in %s: %s" % (crate, (result.stderr or result.stdout).strip()[-300:]))
    return changed


def _dirty(tree, crate):
    return bool(run(["git", "status", "--porcelain", "--", crate], tree).stdout.strip())


# [@ANCHOR: lint_autofix_needles]
# Verified by [@ANCHOR: test_needles_hold_changed_paths_and_their_anchors]
def needles_for(tree, files):
    """The changed paths plus every anchor named in a changed non-Markdown file: what a finding must mention to be this branch's."""
    needles = set(files)
    for name in files:
        if name.endswith(".md"):
            continue
        try:
            with open(os.path.join(tree, name), encoding="utf-8", errors="replace") as handle:
                needles.update(ANCHOR_RE.findall(handle.read()))
        except OSError:
            continue
    return needles


# [@ANCHOR: lint_autofix_scoped_lines]
# Verified by [@ANCHOR: test_scoped_lines_keep_only_lines_naming_a_needle]
def scoped_lines(output, needles):
    """The lines of a checker's output that mention one of the needles."""
    return [line for line in output.splitlines() if any(needle in line for needle in needles)]


def listed_checks(tree, repo_tools, needles):
    """[(title, [lines])] for the findings that are not auto-fixable and belong to this branch."""
    py = sys.executable
    checks = [
        ("claim hashes stale (read the claim; if still true: --confirm-claim <claim.md>; else rewrite with write_claim.py)",
         [py, os.path.join(repo_tools, "check_claims_freshness.py"), "."]),
        ("anchor docs standard (write the missing documentation by hand)", [py, os.path.join(repo_tools, "check_anchor_docs_standard.py"), "."]),
        ("function-test anchors (add the '# Verified by [@ANCHOR: ...]' test anchor)", [py, os.path.join(repo_tools, "check_function_test_anchors.py"), "."]),
    ]
    out = []
    for title, argv in checks:
        result = run(argv, tree)
        if result.returncode != 0:
            lines = scoped_lines(result.stdout + "\n" + result.stderr, needles)
            if result.returncode not in (0, 1):
                print("note: %s could not run here (exit %d); merge_pr.py --dry-run covers it" % (os.path.basename(argv[1]), result.returncode))
            if lines:
                out.append((title, lines))
    return out


# [@ANCHOR: lint_autofix_confirm_claim]
# Verified by [@ANCHOR: test_confirm_claim_rewrites_only_the_hash_lines]
def confirm_claim(tree, claim, say=print):
    """Rewrites `claim`'s code_hash (and hash_algorithm: v2) to the anchored code's current v2 hash; True when it was changed. The
    caller has read the claim and decided its wording is still true."""
    import check_claims_freshness as ccf
    import verify_anchors as va

    path = claim if os.path.isabs(claim) else os.path.join(tree, claim)
    with open(path, encoding="utf-8") as handle:
        content = handle.read()
    fields = ccf.parse_claim_frontmatter(content)
    if not fields or "anchor" not in fields or "code_hash" not in fields:
        say("%s: no anchor/code_hash frontmatter, not changed" % claim)
        return False
    info = ccf.build_anchor_index(tree, {va._clean(fields["anchor"])}).get(va._clean(fields["anchor"]))
    if info is None:
        say("%s: anchor %s not found (orphaned claim: retire or re-anchor it by hand)" % (claim, fields["anchor"]))
        return False
    new = re.sub(r"^code_hash:.*$", "code_hash: sha256:" + info.hashes.v2, content, count=1, flags=re.M)
    if re.search(r"^hash_algorithm:", new, flags=re.M):
        new = re.sub(r"^hash_algorithm:.*$", "hash_algorithm: v2", new, count=1, flags=re.M)
    else:
        new = re.sub(r"^(code_hash:.*)$", r"\1\nhash_algorithm: v2", new, count=1, flags=re.M)
    if new == content:
        say("%s: already current" % claim)
        return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(new)
    say("confirmed %s (hash rewritten; you vouched that its wording is still true)" % claim)
    return True


def find_tools(tree):
    for sub in ("tools", os.path.join("hams_shared", "tools")):
        if os.path.exists(os.path.join(tree, sub, "check_claims_freshness.py")):
            return os.path.join(tree, sub)
    return HERE


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fix the mechanical lint findings of the files this branch changed.")
    parser.add_argument("--tree", default=".")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-claim", action="append", default=[], metavar="CLAIM.md")
    args = parser.parse_args(argv)
    top = run(["git", "rev-parse", "--show-toplevel"], os.path.abspath(args.tree))
    if top.returncode != 0:
        print("not a git tree: " + args.tree)
        return 2
    tree = top.stdout.strip()
    for claim in args.confirm_claim:
        confirm_claim(tree, claim)
    files = changed_files(tree, args.base)
    print("%d changed file(s) against %s" % (len(files), args.base))
    fix_python(tree, files, args.dry_run)
    cargo_fmt(tree, touched_crates(tree, files), args.dry_run)
    if not args.dry_run:
        files = changed_files(tree, args.base)
    remaining = listed_checks(tree, find_tools(tree), needles_for(tree, files))
    for title, lines in remaining:
        print("\nREMAINS, for you: " + title)
        for line in lines[:40]:
            print("    " + line)
    if not remaining:
        print("nothing remains that lint_autofix can see; run merge_pr.py --dry-run for the full cheap check")
    return 1 if remaining else 0


if __name__ == "__main__":
    sys.exit(main())
