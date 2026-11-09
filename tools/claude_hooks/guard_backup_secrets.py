#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PreToolUse hook (Bash and the file tools) that stops an agent from changing, deleting, moving, overwriting, regenerating or printing
backup key material or backup encryption settings (Bruce, 2026-11-09: "an agent changes the backup key or encryption so that a
backup cannot be restored").

What it protects (the lists are PATH_RULES, SAMPLES, MENTION_RE and SECRET_VARIABLES below):
  * files: /var/lib/odoo/hams_crypto.secret, /etc/pgbackrest/ and any pgbackrest.conf, /opt/hams/etc/b2_backup/, backup/B2/kopia/crypto/
    custody entries of /opt/hams/etc/keys/ and of ~/.secrets (the Backblaze key, the pgBackRest cipher passphrase, the kopia repository
    password, the custody kit and its sheets), kopia's repository config, /opt/hams/etc/{core,db,odoo}.env (HAMS_CRYPTO_KEY lives
    there), /proc/PID/environ;
  * programs: `pgbackrest stanza-create|stanza-upgrade|stanza-delete|expire` and any `--repoN-cipher-*` / `--repoN-s3-key*` option; `kopia
    repository create|connect|disconnect|change-password|set-parameters|...`, `kopia snapshot delete|expire`, `kopia blob|content delete`,
    `kopia --password`; the `b2` command-line tool except its read-only commands; `aws s3 rm|rb|mv` on the backup bucket; `b2_backup.py
    gen-password|init`; `provision.py` without --plan/--test/--test-environment (production mode generates secrets);
  * variables: setting HAMS_CRYPTO_KEY, ODOO_BACKUP_CRYPTO_KEY, KOPIA_PASSWORD, PGBACKREST_REPO*_CIPHER_*, PGBACKREST_REPO*_S3_KEY* in a
    command, expanding them (`echo $HAMS_CRYPTO_KEY`), and `printenv`/`env`/`set`/`export -p`/`declare -p`/`ps e`/`systemctl show|cat` of a
    backup unit that would print them.
Any command that has a protected path as an operand is denied unless the command only looks (ls, stat, test, file, du, wc, sha256sum,
readlink, find without -exec, echo, cd, ...): reading metadata and `pgbackrest info|check` stay allowed. The shell text is parsed with the
tokenizer of guard_signals.py (quotes, heredocs, here-strings, `$(...)`, `bash -c`, `sudo`, `env`, `ssh host '...'`, `find -exec`), variable
assignments and `for` loops of the same line are substituted, base64/hex/`\\x` blobs are decoded and judged too, glob and brace patterns are
matched against SAMPLES, an interpreter one-liner or heredoc (`python3 -c`, perl, ruby, node, php) that names a protected path and does
anything but stat it is denied, and a protected path reached through a variable the hook cannot see is denied when a `$(...)` operand sits
next to a protected word.

The file tools (Write, Edit, MultiEdit, NotebookEdit, Read, Grep, Artifact) are judged by their path arguments: a protected path is denied;
Grep also refuses a directory that contains one.

There is no escape (Bruce, 2026-11-09, as for guard_signals): no environment variable, flag, marker file or comment lets a denied command
through, because the agent could write it. A human who really must change a key runs the command in his own terminal, where this hook does not run. Failure policy as guard_signals: a
crash inside the hook allows a command that has no backup-looking word and denies one that has; invalid input is allowed. Exit status is
always 0; a denial is the JSON `permissionDecision: deny` on standard output, and every denial and error is appended to
~/.cache/hams-jobs/guard.log. Documentation of every function: tools/README.md "Guard hook for backup secrets
(`claude_hooks/guard_backup_secrets.py`)".
"""

import base64
import binascii
import dataclasses
import fnmatch
import itertools
import json
import os
import re
import shlex
import sys

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if HOOK_DIR not in sys.path:
    sys.path.insert(0, HOOK_DIR)
import guard_signals as gs  # noqa: E402  the shell tokenizer, wrapper resolution and the guard log

MAX_DEPTH = 8
MAX_VARIANTS = 12
BACKUP_TOKENS = r"backup|b2|backblaze|kopia|pgbackrest|crypto|custody|cipher|repo_?pass|repository"

# Absolute-path rules; a path is protected when one of them matches the normalized path.
PATH_RULES = [
    (re.compile(r"(?:^|/)hams_crypto[^/]*$"), "the Odoo encryption key file (hams_crypto.secret)"),
    (re.compile(r"(?:^|/)pgbackrest\.conf[^/]*$"), "a pgbackrest.conf (repository cipher passphrase and storage keys)"),
    (re.compile(r"^/etc/pgbackrest(?:/|$)"), "/etc/pgbackrest (pgBackRest configuration)"),
    (re.compile(r"^/opt/hams/etc/b2_backup(?:/|$)"), "the b2_backup configuration, secrets and password fingerprint"),
    (re.compile(r"^/opt/hams/etc/keys/.*(?:" + BACKUP_TOKENS + ")"), "a backup-related key under /opt/hams/etc/keys"),
    (re.compile(r"^/opt/hams/etc/(?:core|db|odoo)\.env$"), "a provisioned env file that carries HAMS_CRYPTO_KEY"),
    (re.compile(r"^(?:/home/[^/]+|/root)/\.secrets/.*(?:" + BACKUP_TOKENS + ")"), "a backup, B2, kopia or custody-kit secret in ~/.secrets"),
    (re.compile(r"^(?:/home/[^/]+|/root)/\.b2_account_info$"), "the Backblaze B2 account info"),
    (re.compile(r"(?:^|/)\.kopia-configs(?:/|$)"), "a kopia repository connection config"),
    (re.compile(r"/\.config/kopia(?:/|$)"), "kopia's repository config"),
    (re.compile(r"(?:^|/)(?:b2_application_key|b2_key|b2_account)[^/]*$"), "a Backblaze B2 application key file"),
    (re.compile(r"custody[-_]?kit"), "a custody-kit item"),
    (re.compile(r"^/proc/[^/]+/environ$"), "a process environment (it holds HAMS_CRYPTO_KEY and other secrets)"),
]
# Representative protected files: parents of these, and glob/dynamic words that match them, count as touching a protected path.
SAMPLES = [
    "/var/lib/odoo/hams_crypto.secret", "/etc/pgbackrest/pgbackrest.conf", "/etc/pgbackrest/conf.d/repo2.conf",
    "/opt/hams/etc/b2_backup/b2_backup.env", "/opt/hams/etc/b2_backup/config.json", "/opt/hams/etc/b2_backup/password.fingerprint",
    "/opt/hams/etc/keys/backup_management/backup_management.key", "/opt/hams/etc/keys/b2_backup/b2.key",
    "/opt/hams/etc/core.env", "/opt/hams/etc/db.env", "/opt/hams/etc/odoo.env",
    "/var/lib/odoo/backups/.kopia-configs/config_1.config", "/proc/1/environ",
    "HOME/.secrets/pgbackrest_repo2_cipher_pass", "HOME/.secrets/backblaze/hams1_files_repository.password",
    "HOME/.secrets/b2_application_key.env", "HOME/.secrets/custody_kit/items/kopia_repo_password",
    "HOME/.secrets/custody_sheets_2026-10-05.pdf", "HOME/.config/kopia/repository.config", "HOME/.b2_account_info",
]
# A text that names one of these (path words, variable names) is a backup-secret text; used for interpreter text, decoded blobs and `$(...)`.
MENTION_RE = re.compile(
    r"hams_crypto|pgbackrest\.conf|/etc/pgbackrest|b2_backup|kopia-configs|\.secrets\b|/opt/hams/etc/keys|/opt/hams/etc/(?:core|db|odoo)\.env|custody|"
    r"cipher[-_]pass|backblaze|KOPIA_PASSWORD|HAMS_CRYPTO_KEY|ODOO_BACKUP_CRYPTO_KEY|/proc/[^\s/]*/environ|\.b2_account_info|b2_application_key",
    re.IGNORECASE)
# Directories whose names alone, with a dynamic part after them, may lead to a protected file.
PARENT_TOKEN_RE = re.compile(r"/var/lib/odoo|/opt/hams/etc|\.secrets|/etc/pgbackrest|\$HOME|~/")
SECRET_NAMES = (r"HAMS_CRYPTO_KEY|ODOO_BACKUP_CRYPTO_KEY|KOPIA_PASSWORD|PGBACKREST_REPO\d*_(?:CIPHER_\w+|S3_KEY(?:_SECRET)?)|"
                r"B2_APPLICATION_KEY(?:_ID)?|B2_ACCOUNT_INFO")
SECRET_VARIABLES = re.compile(r"\$\{?(?:" + SECRET_NAMES + r"|AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID)(?![A-Za-z0-9_])")
SECRET_ASSIGN_RE = re.compile(r"^(?:" + SECRET_NAMES + r")=")
CRUDE_RE = re.compile(
    r"hams_crypto|pgbackrest|kopia|cipher|\.secrets|custody|b2_|backblaze|etc/keys|stanza|HAMS_CRYPTO|KOPIA_|ODOO_BACKUP|printenv|environ|"
    r"\bb2\b|/opt/hams/etc|/var/lib/odoo", re.IGNORECASE)
PATHLIKE_RE = re.compile(r"^[\w./~@:+=,%*?\[\]{}$`()!-]+$")

# Commands that only look: a protected operand is fine for them.
METADATA = {"ls", "stat", "test", "[", "[[", "file", "du", "wc", "sha256sum", "sha512sum", "sha1sum", "md5sum", "b2sum", "cksum", "namei",
            "readlink", "realpath", "getfacl", "lsattr", "df", "dirname", "basename", "id", "cd", "pushd", "popd", "echo", "printf", "true", ":",
            "false", "exit", "return", "sleep", "which", "type", "command", "ldd", "diff", "cmp"}
# Programs whose bare `$(...)` operands (a commit message) are not file names.
NON_FILE_COMMANDS = {"git", "gh", "cargo", "make", "npm", "docker"}
# Commands that change only permissions or create: allowed on a directory that merely contains a protected file, denied on the file itself.
PERMISSION_COMMANDS = {"chmod", "chown", "chgrp", "setfacl", "chattr", "touch", "mkdir", "rmdir", "install -d"}
SHELLS = gs.SHELLS | {"su", "runuser", "sg", "script"}
INTERPRETER_RE = re.compile(r"^(?:python[0-9.]*|perl[0-9.]*|ruby|node|nodejs|php[0-9.]*|lua|osascript|tclsh|Rscript)$")
SCRIPT_FIRST = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "zgrep", "sed", "awk", "gawk", "mawk", "jq", "expr"}
ACCESS_VERBS = re.compile(
    r"open|read|write|unlink|remove|rename|replace|copy|move|rmtree|chmod|chown|truncate|system|popen|subprocess|exec|check_output|"
    r"\.run\(|\.call\(|fdopen|environ|getenv|cat |load|touch|mkdir|symlink|link\(|dump|delete|File\.|IO\.|sendfile|shutil|spawn|`", re.IGNORECASE)
OBFUSCATION_RE = re.compile(r"chr\(|fromhex|b64|base64|decode|join\(|\+\s*['\"]|%\s*\(|format\(|reversed|\[::-1\]|getattr|__import__|eval\(|exec\(")
ENV_DUMP_PRINT_RE = re.compile(r"(?:print|dumps?|pprint|echo|write)\w*\([^)]*environ(?!\s*[\[.])")

PGBACKREST_DENIED = {"stanza-create", "stanza-upgrade", "stanza-delete", "expire"}
PGBACKREST_OPTION_RE = re.compile(r"^--(?:no-)?(?:repo\d*-)(?:cipher|s3-key)")
KOPIA_REPOSITORY_DENIED = {"create", "connect", "disconnect", "change-password", "set-parameters", "set-client", "upgrade", "sync-to",
                           "repair", "connect-server"}
KOPIA_OTHER_DENIED = {"snapshot": {"delete", "expire"}, "blob": {"delete", "rm", "gc", "rewrite"}, "content": {"delete", "rm", "rewrite"},
                      "maintenance": {"set"}}
B2_READ_ONLY = {"ls", "version", "--version", "help", "--help", "get-bucket", "get_bucket", "list-buckets", "list_buckets", "list-keys", "list_keys",
                "list-file-names", "list_file_names", "list-file-versions", "list_file_versions", "get-account-info", "get_account_info",
                "ls-long", "get-url", "get_url"}
B2_GROUP_READ_ONLY = {"info", "list", "get", "ls", "url", "show"}
AWS_DENIED_VERBS = {"rm", "rb", "mv", "delete-object", "delete-objects", "delete-bucket", "put-bucket-lifecycle-configuration",
                    "put-bucket-lifecycle", "put-object-lock-configuration", "delete-object-version", "put-object", "put-bucket-versioning"}
BACKUP_BUCKET_RE = re.compile(r"hams-com-prod-backups|hams_prod_?(?:enc|plain)?\b|backblazeb2", re.IGNORECASE)
BACKUP_UNIT_RE = re.compile(r"backup|pgbackrest|kopia|b2|wal-receive|hams1-|odoo|hams", re.IGNORECASE)
SAFE_SYSTEMD_PROPERTIES = {"activestate", "substate", "loadstate", "unitfilestate", "result", "execmainstatus", "nrestarts", "mainpid",
                           "description", "activeentertimestamp", "inactiveexittimestamp", "activeexittimestamp", "id", "names", "type",
                           "fragmentpath", "canstart", "canstop", "triggeredby", "triggers", "nextelapsetimestamprealtime",
                           "lasttriggerusec", "execmainstarttimestamp", "execmainexittimestamp"}
PROVISION_SAFE_FLAGS = {"--plan", "--help", "-h", "--test", "--test-environment"}
PROVISION_SECRET_FLAG_RE = re.compile(r"^--(?:regenerate|rotate|new|reset|reinit|reissue|renew|force)[-_a-z]*(?:secret|key|password|passphrase|crypto|cipher)")
B2_BACKUP_DENIED = {"gen-password", "init"}

ASSIGN_RE = re.compile(r"""(?:^|[;&|(\s])(?:(?:export|declare(?:\s+-\w+)*|local|readonly|typeset)\s+)?([A-Za-z_]\w*)=("(?:[^"\\]|\\.)*"|'[^']*'|[^\s;&|()]*)""")
FOR_RE = re.compile(r"\bfor\s+([A-Za-z_]\w*)\s+in\s+([^;\n]*?)\s*(?:;|\n)\s*do\b")
WRITE_REDIRECT_RE = re.compile(r"""(?<![<>&\\])(?:\d*|&)(?:>>|>\||>)(?![&(>])\s*((?:"[^"]*"|'[^']*'|\\.|[^\s;&|()<>])+)""")
READ_REDIRECT_RE = re.compile(r"""(?<![<>&])<(?![<(&>=])\s*((?:"[^"]*"|'[^']*'|\\.|[^\s;&|()<>])+)""")
BASE64_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")
HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2}){12,}")
HEXOCT_RE = re.compile(r"\\x([0-9a-fA-F]{2})|\\([0-7]{3})")
DYNAMIC_RE = re.compile(r"\$\([^)]*\)|`[^`]*`|\$\{[^}]*\}|\$\w+|\$[@*#?!$-]")


@dataclasses.dataclass
class Context:
    """What the checks need besides the command: the working directory used for relative paths, the home directory, the `NAME -> value`
    assignments seen so far on the line, and whether the whole text names a backup secret (`mentions`)."""

    cwd: str
    home: str
    assigned: dict = dataclasses.field(default_factory=dict)
    mentions: bool = False


# [@ANCHOR: guard_backup_secrets:to_absolute]
def to_absolute(text, cwd, home):
    """`text` as a normalized absolute path: `~`, `~/x`, `$HOME` and `${HOME}` become the home directory, a relative path is joined to
    `cwd`, `..` and `//` are resolved without touching the file system."""
    text = text.replace("${HOME}", home).replace("$HOME", home)
    if text == "~" or text.startswith("~/"):
        text = home + text[1:]
    elif text.startswith("~") and not text.startswith("~="):
        text = "/home/" + text[1:]
    if not text.startswith("/"):
        text = os.path.join(cwd, text)
    return os.path.normpath(text)


# [@ANCHOR: guard_backup_secrets:samples_for]
def samples_for(home):
    """SAMPLES with `HOME` replaced by the home directory (and /root)."""
    return [home + s[4:] if s.startswith("HOME") else s for s in SAMPLES]


# [@ANCHOR: guard_backup_secrets:protected_reason]
def protected_reason(path):
    """What a normalized absolute `path` is, when it is a protected file or directory (a path rule matches), else `None`."""
    for pattern, what in PATH_RULES:
        if pattern.search(path):
            return what
    return None


# [@ANCHOR: guard_backup_secrets:contains_protected]
def contains_protected(path, home):
    """True when `path` is a directory above a protected sample (`/`, `/var/lib/odoo`, `~`, `~/.secrets`, `/opt/hams/etc/keys`, ...): deleting,
    moving, copying or recursively reading it reaches the protected file."""
    prefix = path.rstrip("/") + "/"
    return any(s.startswith(prefix) for s in samples_for(home))


# [@ANCHOR: guard_backup_secrets:brace_expand]
def brace_expand(text, limit=32):
    """The words `{a,b}c{1,2}` expands to (comma lists only, nested and several groups handled, at most `limit` results); a text with no
    brace group is returned as the one-element list."""
    match = re.search(r"\{([^{}]*,[^{}]*)\}", text)
    if not match or limit <= 1:
        return [text]
    results = []
    for option in match.group(1).split(","):
        results.extend(brace_expand(text[:match.start()] + option + text[match.end():], limit))
        if len(results) >= limit:
            break
    return results[:limit]


# [@ANCHOR: guard_backup_secrets:word_hits]
def word_hits(text, cwd, home):
    """How the shell word `text` touches protected paths: `"protected"` (a protected file or a directory rule), `"contains"` (a directory
    that holds one), `"pattern"` (a glob or a dynamic path such as `/var/lib/odoo/$f` that can name one) or `None`. Prefixes after `=`, `:` and `@`
    (`--config=P`, `of=P`, `host:P`, `-F f=@P`) are judged too; a word with spaces or odd characters (a script, a sentence) is no path."""
    if not text or text.startswith("-") and "=" not in text or not PATHLIKE_RE.match(text) and "=" not in text and ":" not in text:
        return None
    candidates = [text] + [text[i + 1:] for i, c in enumerate(text) if c in "=:@" and i + 1 < len(text)][:6]
    found = None
    for candidate in candidates:
        if not candidate or not PATHLIKE_RE.match(candidate):
            continue
        for variant in brace_expand(candidate):
            variant = variant.replace("${HOME}", home).replace("$HOME", home)
            dynamic = bool(DYNAMIC_RE.search(variant))
            pattern = DYNAMIC_RE.sub("*", variant) if dynamic else variant
            if not (re.match(r"^(?:/|~|\.{0,2}/)", pattern) or "/" in pattern):
                if not re.search(r"hams_crypto|pgbackrest\.conf", pattern):
                    continue
            path = to_absolute(pattern, cwd, home)
            if any(ch in path for ch in "*?["):
                prefix = re.split(r"[*?\[]", path, maxsplit=1)[0]
                if len(prefix) >= 2 and prefix.startswith("/") and (
                        protected_reason(path.replace("*", "x")) or any(fnmatch.fnmatchcase(s, path) for s in samples_for(home))):
                    return "pattern"
                continue
            if protected_reason(path):
                return "protected"
            if contains_protected(path, home):
                found = found or "contains"
    return found


# [@ANCHOR: guard_backup_secrets:path_reason]
def path_reason(path, cwd, home, include_parents=False):
    """Why a file-tool path argument is refused (a sentence) or `None`: a protected file, or with `include_parents` a directory that holds one."""
    hit = word_hits(path, cwd, home)
    if hit == "protected" or hit == "pattern":
        absolute = to_absolute(path, cwd, home)
        return f"{path!r} is {protected_reason(absolute) or 'a pattern that matches backup key material'}"
    if hit == "contains" and include_parents:
        return f"{path!r} is a directory that holds backup key material"
    return None


# [@ANCHOR: guard_backup_secrets:redirect_targets]
def redirect_targets(text):
    """`(writes, reads)`: the file words after `>`, `>>`, `>|`, `&>` and after `<` in the raw shell `text`, quotes and backslashes removed.
    Found by a pattern over the text, not by the tokenizer (which drops them), so a `>` inside a quoted string is also seen: that only adds."""
    def clean(word):
        try:
            return "".join(shlex.split(word)) or word
        except ValueError:
            return word.strip("\"'")
    return ([clean(m.group(1)) for m in WRITE_REDIRECT_RE.finditer(text)], [clean(m.group(1)) for m in READ_REDIRECT_RE.finditer(text)])


# [@ANCHOR: guard_backup_secrets:expand_variables]
def expand_variables(text):
    """The texts to judge for the shell `text`: itself, then itself with `$NAME` / `${NAME}` replaced by the value assigned earlier in the
    text (so `d=/var/lib/odoo; f=hams_crypto.secret; cat $d/$f` reads as the real command), once more for every value a `for NAME in A B; do`
    loop gives its variable. At most MAX_VARIANTS texts."""
    assigned = {}
    for match in ASSIGN_RE.finditer(text):
        value = match.group(2)
        if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        assigned[match.group(1)] = value
    loops = {m.group(1): m.group(2).split() for m in FOR_RE.finditer(text)}

    def substitute(source, values):
        for _ in range(4):
            new = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: values.get(m.group(1) or m.group(2), m.group(0)), source)
            if new == source:
                break
            source = new
        return source
    variants = [text]
    if assigned:
        variants.append(substitute(text, assigned))
    for name, values in loops.items():
        for value in values[:6]:
            variants.append(substitute(text, dict(assigned, **{name: value})))
    return list(dict.fromkeys(variants))[:MAX_VARIANTS]


# [@ANCHOR: guard_backup_secrets:decoded_texts]
def decoded_texts(text):
    """Texts hidden in `text`: base64 and hex blobs that decode to mostly printable bytes, and the text with `\\xHH` and `\\NNN` escapes
    decoded (what `printf` or `echo -e` would print). Each is judged like a command."""
    found = []
    for pattern, decode in ((BASE64_RE, lambda s: base64.b64decode(s + "=" * (-len(s) % 4), validate=True)), (HEX_RE, bytes.fromhex)):
        for match in pattern.finditer(text):
            try:
                raw = decode(match.group(0))
            except (binascii.Error, ValueError):
                continue
            if raw and sum(32 <= b < 127 or b in (9, 10) for b in raw) >= 0.9 * len(raw):
                found.append(raw.decode("ascii", "replace"))
    if len(HEXOCT_RE.findall(text)) >= 4:
        found.append(HEXOCT_RE.sub(lambda m: chr(int(m.group(1), 16) if m.group(1) else int(m.group(2), 8)), text))
    return found


# [@ANCHOR: guard_backup_secrets:positionals]
def positionals(words):
    """The texts of `words` that do not start with `-`."""
    return [w.text for w in words if not w.text.startswith("-")]


# [@ANCHOR: guard_backup_secrets:check_secret_words]
def check_secret_words(command, base, verdict):
    """Deny setting a backup secret variable in a command (`HAMS_CRYPTO_KEY=x cmd`, `export KOPIA_PASSWORD=...`, `env PGBACKREST_REPO2_CIPHER_PASS=..`)
    and expanding one (`echo $HAMS_CRYPTO_KEY`, `cat <<EOF` of one) in anything but a `[`/`test` test."""
    assignments_only = all(re.match(r"^[A-Za-z_]\w*=", w.text) for w in command.words)
    for word in command.words:
        if SECRET_ASSIGN_RE.match(word.text):
            verdict.reasons.append(f"setting {word.text.split('=')[0]} changes backup key material for that command")
        elif word.expands and SECRET_VARIABLES.search(word.text) and base not in ("[", "[[", "test") and not assignments_only:
            verdict.reasons.append("expanding a backup secret variable (" + SECRET_VARIABLES.search(word.text).group(0) + ") would print or pass the key")
    if base not in ("[", "[[", "test") and any(SECRET_VARIABLES.search(body) for body in command.bodies):
        verdict.reasons.append("a heredoc or here-string expands a backup secret variable")


# [@ANCHOR: guard_backup_secrets:check_environment_dump]
def check_environment_dump(command, name, base, rest, verdict):
    """Deny the commands that print the whole environment (it holds HAMS_CRYPTO_KEY on the test box): a bare `env` (also `sudo env`), `printenv`
    with no name or a secret's name, `export`/`declare`/`typeset`/`readonly` listing (`-p`, `-x`, no arguments), a bare `set`, `ps e` /
    `ps ewww`, `compgen -e`, `systemctl show-environment`."""
    peeled, _, _ = gs.resolve(command.words + [gs.Word("__END__")])
    if peeled == "__END__" and any(os.path.basename(w.text) == "env" for w in command.words):
        verdict.reasons.append("a bare `env` prints every environment variable, HAMS_CRYPTO_KEY included (use `printenv NAME` for one harmless variable)")
    texts = [w.text for w in rest]
    if base == "printenv" and (not texts or any(re.fullmatch(SECRET_NAMES + "|.*(?:KEY|PASS|SECRET|TOKEN).*", t) for t in texts)):
        verdict.reasons.append("`printenv` with no name or a secret's name prints the environment")
    elif base in ("export", "declare", "typeset", "readonly") and (not texts or any(re.fullmatch(r"-\w*[pxf]\w*", t) for t in texts)) and not any("=" in t for t in texts):
        verdict.reasons.append(f"`{base}` with no variable or with -p/-x prints every exported variable")
    elif base == "set" and not texts:
        verdict.reasons.append("a bare `set` prints every shell variable")
    elif base == "compgen" and "-e" in texts:
        verdict.reasons.append("`compgen -e` lists the environment")
    elif base == "ps" and any(re.fullmatch(r"[A-Za-z]*e[A-Za-z]*", t) for t in texts):
        verdict.reasons.append("`ps e` (BSD style) appends every process's environment")


# [@ANCHOR: guard_backup_secrets:check_systemctl]
def check_systemctl(rest, verdict):
    """Deny `systemctl show-environment`, `systemctl cat UNIT` and `systemctl show UNIT` (no `-p`, or a property that is not in
    SAFE_SYSTEMD_PROPERTIES) for a backup, odoo or hams unit or for no unit at all: they print Environment= lines. `status`, `is-active`,
    `list-units` and `show -p ActiveState` stay allowed."""
    texts = [w.text for w in rest]
    verbs = [t for t in texts if not t.startswith("-")]
    if "show-environment" in texts:
        verdict.reasons.append("`systemctl show-environment` prints the manager's environment")
        return
    if not verbs or verbs[0] not in ("show", "cat"):
        return
    properties, units, skip = [], [], False
    for index, text in enumerate(texts):
        if skip:
            skip = False
        elif text in ("-p", "--property"):
            properties.extend(texts[index + 1].split(",") if index + 1 < len(texts) else [])
            skip = True
        elif text.startswith("--property="):
            properties.extend(text.split("=", 1)[1].split(","))
        elif text.startswith("-p") and len(text) > 2 and not text.startswith("--"):
            properties.extend(text[2:].split(","))
        elif not text.startswith("-") and text not in ("show", "cat"):
            units.append(text)
    safe = verbs[0] == "show" and properties and all(p.lower() in SAFE_SYSTEMD_PROPERTIES for p in properties)
    if not safe and (not units or any(BACKUP_UNIT_RE.search(u) for u in units)):
        verdict.reasons.append("`systemctl show|cat` of a backup, odoo or hams unit prints its Environment= (keys); ask for a safe property, "
                               "e.g. `systemctl show -p ActiveState UNIT`, or `systemctl status UNIT`")


# [@ANCHOR: guard_backup_secrets:check_pgbackrest]
def check_pgbackrest(rest, verdict):
    """Deny `pgbackrest stanza-create|stanza-upgrade|stanza-delete|expire` and any `--repoN-cipher-*` / `--repoN-s3-key*` option; `info`, `check`,
    `verify`, `backup`, `restore`, `version` stay allowed."""
    for word in rest:
        if PGBACKREST_OPTION_RE.match(word.text):
            verdict.reasons.append(f"pgbackrest option {word.text.split('=')[0]} sets a repository cipher or storage key")
    for text in positionals(rest):
        if text in PGBACKREST_DENIED:
            verdict.reasons.append(f"`pgbackrest {text}` can re-create the repository stanza or expire backups so that they cannot be restored")


# [@ANCHOR: guard_backup_secrets:check_kopia]
def check_kopia(rest, verdict):
    """Deny `kopia repository create|connect|disconnect|change-password|set-parameters|...`, `kopia snapshot delete|expire`, `kopia blob|content
    delete`, `kopia maintenance set` and `--password`; `snapshot list`, `snapshot create`, `restore`, `repository status` stay allowed."""
    if any(w.text == "--password" or w.text.startswith("--password=") for w in rest):
        verdict.reasons.append("kopia --password passes the repository password on the command line")
    texts = positionals(rest)
    for index, text in enumerate(texts):
        later = set(texts[index + 1:])
        if text in ("repository", "repo") and later & KOPIA_REPOSITORY_DENIED:
            verdict.reasons.append("`kopia repository " + sorted(later & KOPIA_REPOSITORY_DENIED)[0] + "` creates, re-keys or reconnects the backup repository")
        elif text in KOPIA_OTHER_DENIED and later & KOPIA_OTHER_DENIED[text]:
            verdict.reasons.append(f"`kopia {text} {sorted(later & KOPIA_OTHER_DENIED[text])[0]}` deletes backup data or changes its maintenance")


# [@ANCHOR: guard_backup_secrets:check_b2_and_aws]
def check_b2_and_aws(base, rest, verdict):
    """Deny the Backblaze `b2` command-line tool except its read-only commands (B2_READ_ONLY, and the `info|list|get` of `bucket|file|key`), and
    `aws` verbs that delete or reconfigure (AWS_DENIED_VERBS) when a word names the backup bucket or a B2 endpoint."""
    texts = positionals(rest)
    if base in ("b2", "b2v3", "b2v4", "backblaze-b2"):
        if texts and texts[0] in ("bucket", "file", "key", "account"):
            if len(texts) < 2 or texts[1] not in B2_GROUP_READ_ONLY:
                verdict.reasons.append(f"`b2 {' '.join(texts[:2])}` changes B2 keys, buckets or files")
        elif texts and texts[0] not in B2_READ_ONLY:
            verdict.reasons.append(f"`b2 {texts[0]}` authorizes, creates or deletes B2 keys, buckets or files (read-only commands are allowed)")
    elif base == "aws" and any(BACKUP_BUCKET_RE.search(w.text) for w in rest) and AWS_DENIED_VERBS & set(texts):
        verdict.reasons.append(f"`aws ... {sorted(AWS_DENIED_VERBS & set(texts))[0]}` on the backup bucket deletes or reconfigures backups")


# [@ANCHOR: guard_backup_secrets:check_provisioning]
def check_provisioning(script, rest, verdict):
    """Deny `provision.py` / `provision_test_environment.py` unless `--plan`, `--help`, `--test` or `--test-environment` is given (production mode
    generates secrets, among them HAMS_CRYPTO_KEY, when none exists), any provisioning flag that spells out regenerating a secret, and
    `b2_backup.py gen-password|init`."""
    texts = [w.text for w in rest]
    if script in ("provision.py", "provision_test_environment.py", "provision_build_host.sh"):
        if not PROVISION_SAFE_FLAGS & set(texts):
            verdict.reasons.append(f"`{script}` without --plan/--test/--test-environment is production provisioning: it generates and writes secret files; a human runs it")
        for text in texts:
            if PROVISION_SECRET_FLAG_RE.match(text):
                verdict.reasons.append(f"`{script} {text}` regenerates secrets")
    elif script == "b2_backup.py" and B2_BACKUP_DENIED & set(positionals(rest)):
        verdict.reasons.append(f"`b2_backup.py {sorted(B2_BACKUP_DENIED & set(positionals(rest)))[0]}` creates a repository password or the repository itself")


# [@ANCHOR: guard_backup_secrets:check_find]
def check_find(rest, ctx, verdict):
    """Judge `find`: when a start path is protected or holds a protected file, `-delete`, `-fprint*`, `-fls` and any `-exec`/`-ok` command that
    is not a metadata command (ls, stat, sha256sum, ...) are denied; plain listing is allowed."""
    texts = [w.text for w in rest]
    starts = []
    for text in texts:
        if text.startswith("-") or text in ("(", "!"):
            break
        starts.append(text)
    if not any(word_hits(s, ctx.cwd, ctx.home) for s in starts):
        return
    if any(t in ("-delete",) or t.startswith("-fprint") or t == "-fls" for t in texts):
        verdict.reasons.append("find -delete/-fprint on a path that holds backup key material")
    index = 0
    while index < len(texts):
        if texts[index] in gs.EXEC_OPTIONS:
            end = next((j for j in range(index + 1, len(texts)) if texts[j] in (";", "+")), len(texts))
            program = os.path.basename(texts[index + 1]) if index + 1 < len(texts) else ""
            if program not in METADATA:
                verdict.reasons.append(f"find -exec {program} on a path that holds backup key material")
            index = end
        index += 1


# [@ANCHOR: guard_backup_secrets:check_interpreter]
def check_interpreter(texts, verdict):
    """Judge the program text of an interpreter (`python3 -c`, perl -e, a heredoc piped into python): it is denied when it names a protected path or
    secret variable AND does anything but look (ACCESS_VERBS), when it names a directory of secrets together with a way of building strings at
    run time (OBFUSCATION_RE), or when it prints the whole environment. A script that only `os.stat`s or `os.path.exists` a protected path is allowed."""
    for text in texts:
        if ENV_DUMP_PRINT_RE.search(text):
            verdict.reasons.append("an interpreter prints the whole environment")
        if MENTION_RE.search(text) and ACCESS_VERBS.search(text):
            verdict.reasons.append("an interpreter program names backup key material (" + MENTION_RE.search(text).group(0) + ") and could read, change or print it; "
                                   "use the file tools or a metadata command, or ask a human")
        elif PARENT_TOKEN_RE.search(text) and ACCESS_VERBS.search(text) and OBFUSCATION_RE.search(text):
            verdict.reasons.append("an interpreter program builds a path under a secrets directory at run time")


# [@ANCHOR: guard_backup_secrets:check_operands]
def check_operands(base, words, ctx, verdict):
    """Deny a command that has a protected path (or a glob or `$VAR` path that can name one, or a directory that holds one) as an operand unless
    it only looks (METADATA); permission commands may name a directory that holds one but not the file itself; the first operand of grep, sed,
    awk and the like is a pattern and is skipped. A bare `$(...)` or `$VAR` operand is denied when the command line names a protected word."""
    if base in METADATA:
        return
    operands = list(words[1:])
    if base in SCRIPT_FIRST and not any(w.text in ("-e", "-f", "--expression", "--file", "--regexp") for w in operands):
        first = next((i for i, w in enumerate(operands) if not w.text.startswith("-")), None)
        if first is not None:
            del operands[first]
    for word in operands:
        hit = word_hits(word.text, ctx.cwd, ctx.home)
        if hit == "contains" and base in PERMISSION_COMMANDS:
            continue
        if hit:
            what = protected_reason(to_absolute(word.text, ctx.cwd, ctx.home)) or "backup key material"
            verdict.reasons.append(f"`{base}` on {word.text!r} ({what})" if hit != "contains" else f"`{base}` on {word.text!r}, a directory that holds backup key material")
        elif ctx.mentions and base not in NON_FILE_COMMANDS and word.expands and re.fullmatch(r"\$\(.*\)|`.*`|\$\{?\w+\}?", word.text):
            verdict.reasons.append(f"`{base}` is given a path built at run time ({word.text!r}) in a command line that names backup key material")


# [@ANCHOR: guard_backup_secrets:script_name]
def script_name(base, rest):
    """The file name that counts for the tool-specific checks: the first non-option word for an interpreter (`python3 tools/provision.py`), else `base`."""
    if INTERPRETER_RE.match(base) or base in ("sh", "bash"):
        for word in rest:
            if not word.text.startswith("-"):
                return os.path.basename(word.text)
        return ""
    return base


# [@ANCHOR: guard_backup_secrets:check_command]
def check_command(command, verdict, depth, ctx, piped_texts=()):
    """Judge one `Command` and add what is wrong to `verdict`: secret variables, environment dumps, and by program name pgbackrest, kopia, b2, aws,
    systemctl, provisioning tools, find, interpreters, shells, `eval`, `ssh host '...'` (recursing into their script text), then the operand rule
    (`check_operands`). `piped_texts` are the programs piped into an interpreter."""
    name, words, via_xargs = gs.resolve(gs.expand_program(command.words, ctx.assigned))
    base = os.path.basename(name or "")
    rest = words[1:]
    check_secret_words(command, base, verdict)
    check_environment_dump(command, name, base, rest, verdict)
    if name is None:
        return
    script = script_name(base, rest)
    check_provisioning(script, rest[1:] if script != base else rest, verdict)
    if base == "pgbackrest":
        check_pgbackrest(rest, verdict)
        return
    if base == "kopia":
        check_kopia(rest, verdict)
        return
    if base == "systemctl":
        check_systemctl(rest, verdict)
        return
    check_b2_and_aws(base, rest, verdict)
    if via_xargs and base not in METADATA and ctx.mentions:
        verdict.reasons.append(f"`xargs {base}` takes its files from a pipe in a command line that names backup key material")
    if base == "find":
        check_find(rest, ctx, verdict)
        for index, text in enumerate(w.text for w in rest):
            if text in gs.EXEC_OPTIONS:
                end = next((j for j in range(index + 1, len(rest)) if rest[j].text in (";", "+")), len(rest))
                analyze(" ".join(shlex.quote(w.text) for w in rest[index + 1:end]), depth + 1, verdict, ctx)
        return
    if base in SHELLS:
        script_text = gs.shell_script(words)
        if script_text is not None:
            analyze(script_text, depth + 1, verdict, ctx)
        for body in command.bodies:
            if base in gs.SHELLS:
                analyze(body, depth + 1, verdict, ctx)
    elif base == "eval":
        analyze(" ".join(w.text for w in rest), depth + 1, verdict, ctx)
    elif base in ("watch", "ssh", "sshpass"):
        options_with_value = {"-p", "-i", "-l", "-o", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-S", "-w", "-W"}
        index, host_seen = 0, False
        while index < len(rest):
            text = rest[index].text
            if base == "ssh" and not host_seen:
                if text in options_with_value:
                    index += 2
                    continue
                if text.startswith("-"):
                    index += 1
                    continue
                host_seen = True
                index += 1
                continue
            break
        analyze(" ".join(w.text for w in rest[index:]), depth + 1, verdict, ctx)
        for body in command.bodies:
            analyze(body, depth + 1, verdict, ctx)
    elif INTERPRETER_RE.match(base):
        texts = [words[i + 1].text for i, w in enumerate(words[:-1]) if w.text in ("-c", "-e", "-r", "-E")] + command.bodies + list(piped_texts)
        check_interpreter(texts, verdict)
    check_operands(base, words, ctx, verdict)


# [@ANCHOR: guard_backup_secrets:piped_sources]
def piped_sources(commands, index):
    """The texts an interpreter reads on standard input when `commands[index]` is piped into: the arguments (echo/printf options left out) and heredoc
    bodies of every earlier command of the same pipeline (`echo "import os; ..." | python3`). `[]` when the command is not piped into."""
    texts, j = [], index
    while j > 0 and commands[j].piped:
        j -= 1
        name, words, _ = gs.resolve(commands[j].words)
        texts.append(" ".join(w.text for w in words[1:] if not (name in ("echo", "printf") and w.text.startswith("-"))))
        texts.extend(commands[j].bodies)
    return [t for t in texts if t]


# [@ANCHOR: guard_backup_secrets:analyze]
def analyze(text, depth=0, verdict=None, ctx=None):
    """Judge shell `text`: every variant (`expand_variables`) and every hidden text (`decoded_texts`) is tokenized and each command checked; redirections
    onto or from a protected path are denied; and a text whose decoded blob names backup key material is denied outright. Returns the Verdict
    (`gs.Verdict`, `reasons` empty means allowed). Raises ValueError for text nested deeper than MAX_DEPTH."""
    verdict = verdict if verdict is not None else gs.Verdict()
    ctx = ctx or Context(os.getcwd(), os.path.expanduser("~"))
    if depth > MAX_DEPTH:
        raise ValueError("shell text nested too deeply")
    for hidden in decoded_texts(text):
        if MENTION_RE.search(hidden):
            verdict.reasons.append("an encoded or escaped text names backup key material (" + MENTION_RE.search(hidden).group(0) + ")")
        if depth < MAX_DEPTH:
            analyze(hidden, depth + 1, verdict, ctx)
    for variant in expand_variables(text):
        ctx = dataclasses.replace(ctx, mentions=bool(MENTION_RE.search(variant)), assigned={})
        writes, reads = redirect_targets(variant)
        for target in writes:
            hit = word_hits(target, ctx.cwd, ctx.home)
            if hit in ("protected", "pattern"):
                verdict.reasons.append(f"a redirection writes to {target!r} ({protected_reason(to_absolute(target, ctx.cwd, ctx.home)) or 'backup key material'})")
        for target in reads:
            if word_hits(target, ctx.cwd, ctx.home) in ("protected", "pattern"):
                verdict.reasons.append(f"a redirection reads {target!r} (backup key material)")
        commands = gs.tokenize(variant, [], depth)
        for index, command in enumerate(commands):
            if command.words and all(gs._ASSIGNMENT_RE.match(w.text) for w in command.words):
                for word in command.words:
                    key, _, value = word.text.partition("=")
                    ctx.assigned[key] = value
            check_command(command, verdict, depth, ctx, piped_texts=piped_sources(commands, index))
            for source in gs.piped_shell_sources(commands, index):
                analyze(source, depth + 1, verdict, ctx)
    return verdict


# [@ANCHOR: guard_backup_secrets:deny_message]
def deny_message(reasons):
    """The text an agent reads when a command or edit is denied: the reasons, what stays allowed, and that there is no escape for an agent."""
    return ("BLOCKED by guard_backup_secrets: " + "; ".join(dict.fromkeys(reasons)) + ". Bruce (2026-11-09): no agent may change, delete, move, "
            "overwrite, regenerate or print a backup key, passphrase or encryption setting; a backup that cannot be restored is worse than none. "
            "Allowed: `ls`/`stat`/`test` of those paths, `pgbackrest info` and `pgbackrest check`, `kopia snapshot list`, reading and editing "
            "repository source and docs. There is no escape: nothing in the command text, no environment variable, flag, marker file or comment allows a "
            "denied command; do not rewrite the command to get around this. If the key work is really needed, stop and tell Bruce the exact command; he runs it in his own terminal.")


# [@ANCHOR: guard_backup_secrets:decide_command]
def decide_command(command, session_id="", cwd=None, environ=None, home=None):
    """The decision for one shell command text: `None` to allow, or the deny dict (`hookSpecificOutput.permissionDecision: deny`). A crash in the
    analysis allows a command with no backup-looking word (CRUDE_RE) and denies one with such a word, and is logged."""
    cwd = cwd or os.getcwd()
    home = home or os.path.expanduser("~")
    try:
        verdict = analyze(command, 0, None, Context(cwd, home))
    except Exception as error:
        gs.log_event("error", command, repr(error), session_id, environ)
        if not CRUDE_RE.search(command):
            return None
        verdict = gs.Verdict(["the hook could not parse a command that mentions backup key material"])
    return deny(verdict.reasons, command, session_id, environ)


# [@ANCHOR: guard_backup_secrets:tool_paths]
def tool_paths(tool_input):
    """Every path-like string of a file tool's input: the values of `file_path`, `notebook_path`, `path`, `paths`, `file_paths` and `files` (a mapping's
    values and each value's `from` / `path`, a list's items, recursively)."""
    found = []

    def collect(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            if "from" in value or "path" in value:
                for key in ("from", "path"):
                    if key in value:
                        collect(value[key])
            else:
                for item in value.values():
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    for key in ("file_path", "notebook_path", "path", "paths", "file_paths", "files"):
        if key in tool_input:
            collect(tool_input[key])
    return found


# [@ANCHOR: guard_backup_secrets:decide_tool]
def decide_tool(tool_name, tool_input, session_id="", cwd=None, environ=None, home=None):
    """The decision for a file tool call (Write, Edit, MultiEdit, NotebookEdit, Read, Grep, Artifact, ...): denied when a path argument is
    protected; Grep and Glob-style searches also when it is a directory that holds protected files. A crash allows the call unless a path mentions a
    backup-looking word."""
    cwd = cwd or os.getcwd()
    home = home or os.path.expanduser("~")
    paths = []
    try:
        paths = tool_paths(tool_input)
        reasons = [r for r in (path_reason(p, cwd, home, include_parents=tool_name in ("Grep", "Glob")) for p in paths) if r]
    except Exception as error:
        gs.log_event("error", f"{tool_name} {paths}", repr(error), session_id, environ)
        if not any(CRUDE_RE.search(p) for p in paths):
            return None
        reasons = [f"the hook could not judge a {tool_name} path that mentions backup key material"]
    return deny(reasons, f"{tool_name} {paths}", session_id, environ)


# [@ANCHOR: guard_backup_secrets:deny]
def deny(reasons, subject, session_id="", environ=None):
    """`None` when `reasons` is empty, else the deny dict, after logging the denial."""
    if not reasons:
        return None
    gs.log_event("denied", subject, reasons, session_id, environ)
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": deny_message(reasons)}}


# [@ANCHOR: guard_backup_secrets:main]
def main(stdin=None, stdout=None, environ=None):
    """Hook entry point: read the hook JSON from `stdin`, print the decision JSON (if any) on `stdout`. Returns 0 always. A tool call with a string
    `command` (Bash, a terminal tool) is judged as shell text, any other call by its path arguments; invalid input is allowed."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        data = json.load(stdin)
        tool_name = data.get("tool_name")
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return 0
    except (ValueError, AttributeError):
        return 0
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else None
    session = str(data.get("session_id", ""))
    if isinstance(tool_input.get("command"), str):
        result = decide_command(tool_input["command"], session, cwd, environ)
    else:
        result = decide_tool(tool_name, tool_input, session, cwd, environ)
    if result:
        json.dump(result, stdout)
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
