#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
A change to backup encryption, stanza creation, key generation or a secret-file overwrite needs Bruce's recorded approval (Bruce,
2026-11-09: an agent must never change the backup key or encryption so that a backup cannot be restored).

    check_backup_encryption_changes.py [REPO_DIR] [--base REF]      exit 0 when clean, 1 when a change lacks an approval

The check reads the diff of `REPO_DIR` (default: the checkout this script lives in) against `merge-base(REF, HEAD)` (REF defaults to
`origin/main`), including uncommitted and untracked files, and looks only at provisioning and backup code (RELEVANT_RE; tests, docs and the
guard hooks are not scanned). It reports:
  * `cipher`      a changed (added OR removed) line that names a pgBackRest cipher setting: `repoN-cipher-type`, `cipher-pass`, `CIPHER_PASS`;
  * `stanza`      a changed line that creates, upgrades or deletes a pgBackRest stanza;
  * `kopia-repo`  a changed line that runs `kopia repository create|connect|change-password|set-parameters`;
  * `keygen`      an added block that generates a secret (`secrets.token_*`, `os.urandom`, `openssl rand`, `Fernet.generate_key`, `ssh-keygen`, ...)
                  and names a backup key (HAMS_CRYPTO_KEY, hams_crypto.secret, KOPIA_PASSWORD, a cipher passphrase, ...);
  * `secret-write` an added block that writes, truncates, replaces or copies onto a file that holds such a key.
Each finding needs an entry ADDED IN THE SAME CHANGE to `docs/APPROVED_BACKUP_ENCRYPTION_CHANGES.md` (or, where `docs` is a link into another
repository, `APPROVED_BACKUP_ENCRYPTION_CHANGES.md` at the repository root) of the form

    - <file path or glob> | <rule names, comma separated, or *> | approved by Bruce on YYYY-MM-DD | <why>

with a real date that is not in the future. An older entry licenses nothing new. The check cannot tell a true approval from a typed
one: the reviewer of the pull request must see Bruce's words. If git or the base ref is unavailable the check says so and passes (a lint
machine without history must not block everything); the pull-request gate always has them. Documentation: tools/README.md
"Backup encryption change check".
"""

import datetime
import fnmatch
import os
import re
import subprocess
import sys

MARKER = os.path.join("docs", "APPROVED_BACKUP_ENCRYPTION_CHANGES.md")
ROOT_MARKER = "APPROVED_BACKUP_ENCRYPTION_CHANGES.md"
RELEVANT_RE = re.compile(
    r"(?:^|/)(?:provision[^/]*\.(?:py|sh)|infrastructure\.py|b2_backup\.py|[^/]*backup[^/]*\.(?:py|service|timer|conf|sh)|pgbackrest[^/]*|"
    r"hams1_[^/]*\.(?:py|service|timer)|mirror_repos_to_b2\.py|host_requirements\.py)$|^(?:backup_management|daemon_key_manager)/")
EXCLUDED_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test\.py|tests/[^/]*|claude_hooks/[^/]*|check_backup_encryption_changes\.py|[^/]*\.(?:md|html|csv|xml|js|json|po|pot|css|scss|png|svg))$|/tests/|/docs/|/static/")
CIPHER_RE = re.compile(r"repo\d*-cipher|cipher-(?:type|pass)|CIPHER_PASS|CIPHER_TYPE", re.IGNORECASE)
STANZA_RE = re.compile(r"stanza[-_](?:create|upgrade|delete)")
KOPIA_RE = re.compile(r"""kopia["',\s]+(?:repository|repo)["',\s]+(?:create|connect|change-password|set-parameters)|repository\s+(?:create|change-password|set-parameters)""")
GENERATOR_RE = re.compile(r"secrets\.token_|os\.urandom|token_bytes|token_hex|token_urlsafe|openssl\s+rand|generate_key|ssh-keygen|age-keygen|gpg\s+--(?:gen|full-gen)|genpass|SystemRandom")
SECRET_NAME_RE = re.compile(r"HAMS_CRYPTO_KEY|hams_crypto|ODOO_BACKUP_CRYPTO_KEY|KOPIA_PASSWORD|kopia_password|kopia_repo|cipher|pgbackrest\.conf|b2_backup\.env|B2_APPLICATION_KEY|application_key", re.IGNORECASE)
WRITE_RE = re.compile(r"""["'](?:w|a|wb|ab|w\+|r\+)["']|O_TRUNC|O_WRONLY|os\.replace|os\.rename|shutil\.(?:copy|move)|write_text|write_bytes|\.write\(|\btee\b|\bmv\b|\bcp\b|install\s+-m|>\s*\S""")
APPROVAL_RE = re.compile(r"^\s*[-*]\s*(?P<path>\S+)\s*\|\s*(?P<rules>[\w*,\- ]+?)\s*\|\s*approved by Bruce on (?P<date>\d{4}-\d{2}-\d{2})\b.*$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
RULES = ("cipher", "stanza", "kopia-repo", "keygen", "secret-write")


def run_git(repo, *args):
    """`git -C repo ARGS` as `(returncode, stdout)`; `(127, "")` when git cannot be run."""
    try:
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


# [@ANCHOR: check_backup_encryption_changes:parse_diff]
def parse_diff(text):
    """Parse `git diff -U0` text into `{path: [(added_lines, removed_lines), ...]}`, one tuple per hunk (the lines without their `+`/`-`)."""
    files, path, added, removed = {}, None, None, None
    old_left = new_left = 0  # lines of the current hunk still to come: its body is counted, never guessed from a `+++ ` or `--- ` prefix

    def close():
        if path and (added or removed):
            files.setdefault(path, []).append((added, removed))
    for line in text.splitlines():
        if old_left > 0 and line.startswith("-"):
            removed.append(line[1:])
            old_left -= 1
        elif new_left > 0 and line.startswith("+"):
            added.append(line[1:])
            new_left -= 1
        elif line.startswith("+++ "):
            close()
            # git ends the path with a tab when it holds a space
            path, added, removed = (line[6:].rstrip("\t") if line.startswith("+++ b/") else None), [], []
        elif line.startswith("@@"):
            close()
            added, removed = [], []
            counts = HUNK_RE.match(line)
            old_left, new_left = (int(counts.group(1) or 1), int(counts.group(2) or 1)) if counts else (0, 0)
    close()
    return files


# [@ANCHOR: check_backup_encryption_changes:relevant_path]
def relevant_path(path):
    """True for provisioning and backup code that this check scans (RELEVANT_RE) and not for tests, docs, data files and the guard hooks."""
    return bool(RELEVANT_RE.search(path)) and not EXCLUDED_RE.search(path)


# [@ANCHOR: check_backup_encryption_changes:findings_for_hunks]
def findings_for_hunks(path, hunks):
    """The `(path, rule, line)` findings of one file's hunks. cipher, stanza and kopia-repo look at added and removed lines; keygen and
    secret-write at an added block that holds both the action and a backup key name."""
    found = []
    for added, removed in hunks:
        for line in added + removed:
            for rule, pattern in (("cipher", CIPHER_RE), ("stanza", STANZA_RE), ("kopia-repo", KOPIA_RE)):
                if pattern.search(line):
                    found.append((path, rule, line.strip()))
        block = "\n".join(added)
        if SECRET_NAME_RE.search(block):
            for rule, pattern in (("keygen", GENERATOR_RE), ("secret-write", WRITE_RE)):
                match = pattern.search(block)
                if match:
                    found.append((path, rule, next((ln.strip() for ln in added if pattern.search(ln)), block[:80])))
    return found


# [@ANCHOR: check_backup_encryption_changes:parse_approvals]
def parse_approvals(added_lines, today=None):
    """The approvals in the marker file's added lines: `[(path glob, set of rules, date)]`. A line needs `approved by Bruce on YYYY-MM-DD` with a
    real date that is not after `today` (default now); rule names must be in RULES or `*`. Lines that do not parse are not approvals."""
    today = today or datetime.date.today()
    approvals = []
    for line in added_lines:
        match = APPROVAL_RE.match(line)
        if not match:
            continue
        try:
            date = datetime.date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        rules = {r.strip() for r in match.group("rules").split(",") if r.strip()}
        if date <= today and rules and (rules == {"*"} or rules <= set(RULES)):
            approvals.append((match.group("path"), rules, date))
    return approvals


# [@ANCHOR: check_backup_encryption_changes:is_approved]
def is_approved(path, rule, approvals):
    """True when an approval's path glob matches `path` (or is a suffix of it) and its rules include `rule` or `*`."""
    return any((fnmatch.fnmatch(path, glob) or path.endswith("/" + glob.lstrip("/")) or path == glob) and ("*" in rules or rule in rules)
               for glob, rules, _ in approvals)


# [@ANCHOR: check_backup_encryption_changes:check_repo]
def check_repo(repo, base="origin/main", today=None):
    """The problems of the change in `repo` against `merge-base(base, HEAD)`: a list of sentences (empty: clean). An unavailable git or base
    is reported on standard error by `main` and gives no problems."""
    code, merge_base = run_git(repo, "merge-base", base, "HEAD")
    if code != 0 or not merge_base.strip():
        return []
    code, diff = run_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", merge_base.strip())
    if code != 0:
        return []
    files = parse_diff(diff)
    code, untracked = run_git(repo, "ls-files", "--others", "--exclude-standard")
    for name in untracked.splitlines() if code == 0 else []:
        try:
            with open(os.path.join(repo, name), encoding="utf-8") as handle:
                files.setdefault(name, []).append((handle.read().splitlines(), []))
        except (OSError, UnicodeDecodeError):
            continue
    approvals = []
    for marker in (MARKER, ROOT_MARKER):
        for added, _ in files.get(marker, []):
            approvals.extend(parse_approvals(added, today))
    problems = []
    for path, hunks in sorted(files.items()):
        if not relevant_path(path):
            continue
        for found_path, rule, line in findings_for_hunks(path, hunks):
            if not is_approved(found_path, rule, approvals):
                problems.append(f"{found_path}: `{rule}` change without an approval entry added in this change: {line[:100]!r}. Backup keys and encryption "
                                f"are never changed on an agent's own authority; add `- {found_path} | {rule} | approved by Bruce on YYYY-MM-DD | <why>` to "
                                f"{MARKER} only with Bruce's words in the pull request")
    return problems


# [@ANCHOR: check_backup_encryption_changes:main]
def main(argv=None, out=None):
    """Check the repository named in `argv` (default: the one this script is in) against `--base REF`; print the problems; return the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = out or sys.stdout
    base = "origin/main"
    if "--base" in argv:
        index = argv.index("--base")
        base = argv[index + 1] if index + 1 < len(argv) else base
        del argv[index:index + 2]
    repo = argv[0] if argv else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if run_git(repo, "merge-base", base, "HEAD")[0] != 0:
        print(f"check_backup_encryption_changes: no merge-base with {base} in {repo}; not checked", file=sys.stderr)
        return 0
    problems = check_repo(repo, base)
    for problem in problems:
        print(f"🚨 {problem}", file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
