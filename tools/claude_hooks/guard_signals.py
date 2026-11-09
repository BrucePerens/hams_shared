#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PreToolUse hook for the Bash tool: refuses the signal commands that took the shared PostgreSQL cluster down on 2026-11-08 and
points at tools/agent_procs.py instead (Bruce, 2026-11-08).

What it denies (after parsing the shell text into commands, so a word inside a quoted string, a commit message, a heredoc of
data or a `grep` pattern is NOT a command):
  * `kill -9`, `kill -KILL`, `kill -s KILL`, `kill --signal=9`, `os.kill(pid, 9)` in a `python -c`: no SIGKILL by hand.
  * `pkill`, `killall`, `skill`, `fuser -k`, `systemctl kill`.
  * `pgrep -f` / `pgrep --full` (the pattern is matched against every command line, including the shell that holds it; the
    `[x]yz` bracket trick does not help: it matched a PostgreSQL backend). `pgrep` without -f (name only) is allowed.
  * `kill` whose pids are not literal numbers (from `$(...)`, backticks, variables, a pipe): `kill $(pgrep ...)`, `... | xargs kill`;
    a pid derived from `ps`/`ppid` is such a case, and `$PPID`, `$$`, `0`, `-1`, `-- -PGID` (parents and process groups) are named.
  * `kill` of a literal pid that, looked up in /proc when the hook runs, is the hook's own ancestry (the agent, its shell, the
    systemd --user that started it), pid 1, another user's or root's process, or a protected program (agent_procs.PROTECTED_WORDS:
    postgres, systemd, sshd, redis-server, rabbitmq, ...); and a `kill` word that itself names such a program.
(2026-11-09: an agent ran `kill $pp` on its own parent, `pp=$(ps -o ppid= -p N)`, because this hook was not active in a hams_shared
worktree; the repository now tracks .claude/settings.json in all three checkouts and `check_claude_hook_registration.py` enforces it.)
It allows `kill -TERM 123`, `kill 123`, `kill -0 123`, `kill -l`, and everything else (grep, ps, cargo, git, sleep loops, and
`agent_procs.py wait`). A literal-pid kill is allowed with a reminder to print the ps line first.

There is NO escape for an agent (Bruce, 2026-11-09): no environment variable, flag or marker in a command lets a denied command
through (the former `HAMS_ALLOW_SIGNALS=1` escape was removed; a command containing it is denied like any other). Only Bruce, in his
own terminal outside the agent session, signals freely. Every denial is appended to `~/.cache/hams-jobs/guard.log`.

Failure policy: if the hook itself crashes, a command that does not look like a signal command is allowed (a broken hook must not
block ordinary work), and one that does look like one is denied. Invalid input is allowed. Exit status is always 0; a denial is
the JSON `permissionDecision: deny` on standard output. Documentation of every function: tools/README.md
"Guard hook for signals (`claude_hooks/guard_signals.py`)".
"""

import codecs
import dataclasses
import json
import os
import re
import shlex
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
try:
    import agent_procs  # the one list of protected programs (PROTECTED_WORDS) and the /proc reader
except Exception:  # a broken import must not break the hook: the textual rules still apply
    agent_procs = None

MAX_DEPTH = 8
_CRUDE_RE = re.compile(r"kill|pgrep|pidof|xargs|-9\b|SIGKILL|fuser", re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_KILL_RE = re.compile(
    r"SIGKILL|os\.killpg|\.kill\([^)]*,\s*9\s*\)|\[\s*['\"](?:pkill|killall)['\"]|\[\s*['\"]pgrep['\"]\s*,\s*['\"]-\w*f|"
    r"os\.system\(\s*['\"][^'\"]*(?:pkill|killall|kill\s+-9|pgrep\s+-\w*f)")
RESERVED = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "fi", "done", "esac", "coproc"}
EXEC_OPTIONS = ("-exec", "-execdir", "-ok", "-okdir")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
KILLERS = {"pkill": "pkill", "killall": "killall", "skill": "skill", "killall5": "killall5"}
OPERATORS = ("<<<", "<<-", "&>>", "<<", ">>", "&>", ">&", "<&", ">|", "<>", "&&", "||", ";;", "|&", "<", ">", ";", "&", "|",
             "(", ")", "\n")
REDIRECTS = {"<<<", "<<-", "&>>", "<<", ">>", "&>", ">&", "<&", ">|", "<>", "<", ">"}
WRAPPER_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-T", "-U", "-D", "-R"}, "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p", "-P", "-u"}, "timeout": {"-s", "-k"},
    "stdbuf": {"-i", "-o", "-e"}, "xargs": {"-I", "-n", "-P", "-d", "-E", "-L", "-s", "-a", "-l"}, "flock": {"-w", "-E"},
    "exec": {"-a"}, "nohup": set(), "setsid": set(), "command": set(), "builtin": set(), "chrt": set(), "taskset": set(),
    "unbuffer": set(), "time": set(), "pkexec": {"--user"}, "busybox": set(), "runuser": {"-u", "--user", "-g", "--group", "-G"},
}
# Long options with a value, by wrapper (`sudo --user odoo pkill x`); a short cluster ending in a value option (`sudo -nu odoo`) takes a value too.
WRAPPER_VALUE_OPTIONS["sudo"] |= {"--user", "--group", "--chdir", "--host", "--prompt", "--role", "--type", "--other-user", "--chroot",
                                  "--close-from", "--command-timeout"}
WRAPPER_VALUE_OPTIONS["env"] |= {"--unset", "--chdir", "--split-string"}
WRAPPER_VALUE_OPTIONS["timeout"] |= {"--signal", "--kill-after"}
SPLIT_STRING_OPTIONS = ("-S", "--split-string")
_VARIABLE_RE = re.compile(r"^\$(?:(\w+)|\{(\w+)(?::?-([^}]*))?\})$")
POSITIONAL_AFTER_OPTIONS = {"timeout": 1, "chrt": 1, "taskset": 1, "flock": 1}


# [@ANCHOR: guard_signals:Word]
@dataclasses.dataclass(frozen=True)
class Word:
    """One shell word after quote removal. `text` is the word without quotes and backslashes; `expands` is True when an
    unquoted or double-quoted `$` expansion or a backtick was seen in it (its value is not known when the hook runs)."""

    text: str
    expands: bool = False


# [@ANCHOR: guard_signals:Command]
@dataclasses.dataclass
class Command:
    """One simple command: its `words` (including leading assignments and wrappers), the bodies of the heredocs attached
    to it (`bodies`, raw text, never parsed unless the command is a shell or an interpreter) and whether it is `piped` into."""

    words: list
    bodies: list = dataclasses.field(default_factory=list)
    piped: bool = False  # True when the command's standard input is a pipe from the command before it (`a | THIS`)


# [@ANCHOR: guard_signals:Verdict]
@dataclasses.dataclass
class Verdict:
    """What `analyze` found: `reasons` (each a sentence naming a forbidden construct; empty means allowed) and `notes`
    (reminders for allowed constructs, such as a literal-pid kill)."""

    reasons: list = dataclasses.field(default_factory=list)
    notes: list = dataclasses.field(default_factory=list)


# [@ANCHOR: guard_signals:find_paren_end]
def find_paren_end(text, start):
    """Index of the `)` that closes the `(` at `text[start - 1]`, scanning from `start`; nesting, single and double quotes and
    backslashes are honoured. Returns `len(text)` when it is never closed (the rest is then taken as the inside)."""
    depth, i, n = 1, start, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            i = text.find("'", i + 1)
            i = n if i < 0 else i + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


# [@ANCHOR: guard_signals:find_backtick_end]
def find_backtick_end(text, start):
    """Index of the closing backtick of a command substitution whose body starts at `start` (a backslash escapes the next
    character); `len(text)` when there is none."""
    i, n = start, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return i
        i += 1
    return n


# [@ANCHOR: guard_signals:read_heredoc]
def read_heredoc(text, start, delimiter, strip_tabs):
    """Read a heredoc body that begins at `text[start]` (just after the newline). Returns `(body, next_index)`: the lines up
    to, not including, the line equal to `delimiter` (leading tabs ignored when `strip_tabs`), and the index after that line."""
    lines, i, n = [], start, len(text)
    while i < n:
        end = text.find("\n", i)
        end = n if end < 0 else end
        line = text[i:end]
        i = min(end + 1, n)
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            break
        lines.append(line)
    return "\n".join(lines), i


# [@ANCHOR: guard_signals:tokenize]
def tokenize(text, commands, depth=0):
    """Split shell `text` into simple commands and append them (`Command`) to the list `commands`, including the commands
    inside `$(...)`, backticks, `<(...)`, `>(...)` (parsed recursively, `depth` guards against absurd nesting).

    Handles single quotes, double quotes (with `$(...)` inside), backslash escapes, `#` comments, the operators `; & | && ||
    ( )` and newline (command boundaries), redirections (the file word and a leading fd digit are dropped) and heredocs and here-strings (the
    body or string is attached to the command). Raises `ValueError` when the nesting is deeper than `MAX_DEPTH`."""
    if depth > MAX_DEPTH:
        raise ValueError("shell text nested too deeply")
    words, buf, started, expands = [], [], False, False
    bodies, pending, drop_next, want_delim = [], [], False, None
    i, n = 0, len(text)
    current = Command(words, bodies)
    while i < n:
        c = text[i]
        op = next((o for o in OPERATORS if text.startswith(o, i)), None) if c in " \t\n;&|()<>" else None
        if op in ("<", ">") and text.startswith("(", i + 1):
            end = find_paren_end(text, i + 2)
            tokenize(text[i + 2:end], commands, depth + 1)
            started, expands = True, True
            buf.append("$(...)")
            i = end + 1
            continue
        if c in " \t" or op:
            if started:
                word = "".join(buf)
                if want_delim is not None:
                    pending.append((word, want_delim, bodies, len(bodies)))  # the list of THIS command: a pipe may start the next one before the body is read
                    bodies.append("")
                    want_delim = None
                elif drop_next:
                    if drop_next == "here":
                        bodies.append(word)  # a here-string is the command's standard input, like a heredoc body
                    drop_next = False
                elif op in REDIRECTS and word.isdigit():
                    pass  # a file descriptor number as in `2>/dev/null`
                else:
                    words.append(Word(word, expands))
                buf, started, expands = [], False, False
            if op is None:
                i += 1
                continue
            i += len(op)
            if op in ("<<", "<<-"):
                want_delim = op == "<<-"
            elif op in REDIRECTS:
                drop_next = "here" if op == "<<<" else True
            else:
                if op == "\n" and pending:
                    for delimiter, strip_tabs, owner, index in pending:
                        body, i = read_heredoc(text, i, delimiter, strip_tabs)
                        owner[index] = body
                    pending = []
                if words or bodies:
                    commands.append(current)
                words, bodies, drop_next, want_delim = [], [], False, None
                current = Command(words, bodies, piped=op in ("|", "|&"))
            continue
        if c == "#" and not started:
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        started = True
        if c == "\\":
            buf.append(text[i + 1:i + 2] if text[i + 1:i + 2] != "\n" else "")
            i += 2
        elif c == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            buf.append(text[i + 1:end])
            i = end + 1
        elif text.startswith("$'", i):  # ANSI-C quoting: `$'kill'` is the word kill
            end = i + 2
            while end < n and text[end] != "'":
                end += 2 if text[end] == "\\" else 1
            raw = text[i + 2:end]
            try:
                raw = codecs.decode(raw, "unicode_escape")
            except ValueError:
                pass
            buf.append(raw)
            i = end + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                elif text.startswith("$(", i):
                    end = find_paren_end(text, i + 2)
                    tokenize(text[i + 2:end], commands, depth + 1)
                    buf.append("$(...)")
                    expands = True
                    i = end + 1
                elif text[i] == "`":
                    end = find_backtick_end(text, i + 1)
                    tokenize(text[i + 1:end], commands, depth + 1)
                    buf.append("`...`")
                    expands = True
                    i = end + 1
                else:
                    expands = expands or (text[i] == "$" and text[i + 1:i + 2] not in ("", '"', " "))
                    buf.append(text[i])
                    i += 1
            i += 1
        elif text.startswith("$(", i):
            end = find_paren_end(text, i + 2)
            tokenize(text[i + 2:end], commands, depth + 1)
            buf.append("$(...)")
            expands = True
            i = end + 1
        elif c == "`":
            end = find_backtick_end(text, i + 1)
            tokenize(text[i + 1:end], commands, depth + 1)
            buf.append("`...`")
            expands = True
            i = end + 1
        else:
            expands = expands or (c == "$" and text[i + 1:i + 2] not in ("", " "))
            buf.append(c)
            i += 1
    if started and drop_next == "here":
        bodies.append("".join(buf))
    elif started and not drop_next and want_delim is None:
        words.append(Word("".join(buf), expands))
    if words or bodies:
        commands.append(current)
    return commands


# [@ANCHOR: guard_signals:skip_options]
def skip_options(words, start, value_options):
    """Index of the first word at or after `start` that is not an option of a wrapper command. Words starting with `-` are
    options; those in `value_options` also consume the next word; `--` ends the options."""
    i = start
    while i < len(words) and words[i].text.startswith("-") and len(words[i].text) > 1:
        text = words[i].text
        i += 1
        if text == "--":
            break
        if text in value_options or (not text.startswith("--") and len(text) > 2 and "-" + text[-1] in value_options):
            i += 1  # the option takes a value; a short cluster takes it for its last letter (`-nu odoo`)
    return i


# [@ANCHOR: guard_signals:resolve]
def resolve(words):
    """Strip what comes before the real command: assignments, reserved words (`until`, `do`, `!`, `{`) and wrappers (`sudo`,
    `env`, `nice`, `timeout N`, `nohup`, `xargs`, ...). Returns `(name, rest, via_xargs)`: the program's base name, its words
    (name first) and whether an `xargs` took the pids from a pipe. `name` is `None` when no command is left."""
    via_xargs = False
    while True:
        while words and (words[0].text in RESERVED or _ASSIGNMENT_RE.match(words[0].text)):
            words = words[1:]
        if not words:
            return None, [], via_xargs
        name = os.path.basename(words[0].text)
        if name == "function":  # `function f { kill -9 1; }`: what runs is the body after the brace
            braces = [i for i, w in enumerate(words) if w.text == "{"]
            if not braces:
                return None, [], via_xargs
            words = words[braces[0]:]
            continue
        if name == "runuser" and not any(w.text in ("-u", "--user") for w in words[1:]):
            return name, words, via_xargs  # the `-c SCRIPT` form is read by check_command
        if name not in WRAPPER_VALUE_OPTIONS:
            return name, words, via_xargs
        via_xargs = via_xargs or name == "xargs"
        index = skip_options(words, 1, WRAPPER_VALUE_OPTIONS[name])
        if name == "command" and any(w.text in ("-v", "-V") for w in words[1:index]):
            return None, [], via_xargs  # `command -v pkill` only looks a program up
        if name == "env":  # `env -S 'kill -9 1'` runs the split string as a command line
            for k in range(1, index):
                text = words[k].text
                if text in SPLIT_STRING_OPTIONS and k + 1 < len(words):
                    return "eval", [Word("eval"), words[k + 1]], via_xargs
                if text.startswith("--split-string="):
                    return "eval", [Word("eval"), Word(text.split("=", 1)[1])], via_xargs
                if text.startswith("-S") and len(text) > 2:
                    return "eval", [Word("eval"), Word(text[2:])], via_xargs
        index += POSITIONAL_AFTER_OPTIONS.get(name, 0)
        if name == "flock" and index + 1 < len(words) and words[index].text in ("-c", "--command"):
            return "sh", [Word("sh"), Word("-c"), words[index + 1]], via_xargs  # `flock LOCK -c 'kill -9 1'` runs a shell script
        if name == "env":
            while index < len(words) and _ASSIGNMENT_RE.match(words[index].text):
                index += 1
        words = words[index:]


# [@ANCHOR: guard_signals:signal_is_kill]
def signal_is_kill(spec):
    """True when the signal spec text (`9`, `09`, `KILL`, `SIGKILL`, any case, with or without a leading `-`) names SIGKILL."""
    spec = spec.lstrip("-").upper()
    spec = spec[3:] if spec.startswith("SIG") else spec
    return spec == "KILL" or (spec.isascii() and spec.isdigit() and int(spec) == 9)


# [@ANCHOR: guard_signals:protected_words]
def protected_words():
    """`agent_procs.PROTECTED_WORDS` (lower case tuple), or a built-in copy when agent_procs cannot be imported."""
    if agent_procs is not None:
        return tuple(agent_procs.PROTECTED_WORDS)
    return ("postgres", "postmaster", "redis-server", "rabbitmq", "beam.smp", "epmd", "systemd", "sshd", "gdm", "gnome",
            "claude-desktop")


# [@ANCHOR: guard_signals:literal_pid_reason]
def literal_pid_reason(pid, table=None):
    """Why signalling the literal `pid` is forbidden, looked up in /proc now, or `None` (also when the pid does not exist,
    when agent_procs cannot be imported or /proc cannot be read: this check only adds to the textual rules). Forbidden: the
    hook's own ancestry (the agent and the shells above it), pid 1, another user's or root's process, a protected program.
    `table` is a pid -> ProcInfo snapshot (default: read now)."""
    if agent_procs is None or pid <= 0:
        return None
    try:
        table = agent_procs.snapshot() if table is None else table
        info = table.get(pid)
        if info is None:
            return None
        family = set(agent_procs.ancestry(table, os.getpid())) | {os.getpid()}
        return agent_procs.protected_reason(info, family=family)
    except Exception:
        return None


# [@ANCHOR: guard_signals:check_kill]
def check_kill(words, via_xargs, verdict, table=None):
    """Judge a `kill` command (`words[0]` is kill): SIGKILL is denied, every pid must be a literal number (or `%job`, or
    `$!`), and pids supplied by an `xargs` pipe are denied. `kill -l` and a `kill` with no pid are allowed. A literal pid
    adds a reminder note to print its ps line first."""
    pids, i, listing = [], 1, False
    while i < len(words):
        text = words[i].text
        if text in ("-s", "-n", "--signal", "-sig") and i + 1 < len(words):
            if signal_is_kill(words[i + 1].text):
                verdict.reasons.append("SIGKILL (kill -s KILL) is forbidden")
            i += 2
            continue
        if text.startswith("--signal="):
            if signal_is_kill(text.split("=", 1)[1]):
                verdict.reasons.append("SIGKILL (kill --signal=KILL) is forbidden")
        elif text in ("-l", "-L", "--list", "--table"):
            listing = True
        elif text == "--":
            pids.extend(words[i + 1:])
            break
        elif re.fullmatch(r"-(\d+|[A-Za-z][A-Za-z0-9]*)", text):
            if signal_is_kill(text) or (text[:2] in ("-s", "-n") and signal_is_kill(text[2:])):
                verdict.reasons.append("SIGKILL (kill -9 / -KILL) is forbidden: a SIGKILLed PostgreSQL backend crashes the "
                                       "whole shared cluster")
        else:
            pids.append(words[i])
        i += 1
    if listing:
        return
    if via_xargs:
        verdict.reasons.append("`xargs kill`: the pids come from a pipe and cannot be verified")
    for word in pids:
        literal = re.fullmatch(r"-?\d+|%\d+", word.text) and not word.expands
        if word.text in ("$PPID", "${PPID}", "$$", "${$}") or re.fullmatch(r"-?0|1|-\d+", word.text):
            verdict.reasons.append(f"kill of {word.text!r} signals a parent, init, a process group or every process: never signal "
                                   "parents or groups (stop a job with `agent_procs.py stop NAME`)")
        elif not literal and word.text != "$!":
            verdict.reasons.append(f"kill needs literal pid numbers, not {word.text!r}: a pid from `$(...)`, a variable, "
                                   "`ps`/`ppid` or a pipe can name the wrong process, even your own parent")
            break
        elif literal and word.text.isdigit():
            reason = literal_pid_reason(int(word.text), table)
            if reason:
                verdict.reasons.append(f"kill of pid {word.text} is refused: {reason}")
    names = [w.text for w in words[1:] if not w.text.startswith("-")]
    protected = protected_words()
    for text in names:
        hit = next((p for p in protected if p in text.lower()), None)
        if hit:
            verdict.reasons.append(f"kill names a shared service ({hit!r}): a signal can crash everyone's work")
            break
    if pids and not verdict.reasons:
        verdict.notes.append("kill of a literal pid: print its ps line first (`ps -o pid,user,lstart,args -p PID`); prefer "
                             "agent_procs.py kill-pid PID --start-time T or agent_procs.py stop NAME")


# [@ANCHOR: guard_signals:check_pgrep]
def check_pgrep(words, verdict):
    """Deny `pgrep` with `-f` (also inside a cluster such as `-af`) or `--full`: the pattern is matched against full command
    lines, including the shell that holds the pattern. `pgrep NAME` (name only) is allowed."""
    for word in words[1:]:
        text = word.text
        if text == "--full" or (text.startswith("-") and not text.startswith("--") and "f" in text[1:]):
            verdict.reasons.append("`pgrep -f` matches full command lines, including your own `bash -c` and other agents' or "
                                   "services' processes (the `[x]yz` trick does not help)")
            return


# [@ANCHOR: guard_signals:shell_script]
def shell_script(words):
    """The script text of `sh -c SCRIPT` / `bash -lc SCRIPT` / `su -c SCRIPT`, or `None` when `words` has no `-c`."""
    for i, word in enumerate(words[1:], 1):
        if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word.text) and i + 1 < len(words):
            return words[i + 1].text
    return None


# [@ANCHOR: guard_signals:check_command]
def check_command(command, verdict, depth, assigned=None):
    """Judge one `Command` and add what is wrong to `verdict`; recurses into `bash -c`, `eval`, `ssh host '...'`, `find -exec`,
    `watch`, shell heredocs and interpreter `-c`/`-e` strings and heredocs (`depth` guards recursion). `assigned` is the
    `NAME -> value` table of earlier assignments on the same line, used by `expand_program`."""
    name, words, via_xargs = resolve(expand_program(command.words, assigned))
    if name is None:
        return
    rest = [w.text for w in words[1:]]
    if name in KILLERS:
        verdict.reasons.append(f"`{KILLERS[name]}` signals by name pattern and can hit services, other agents and your own shell")
    elif name == "kill":
        check_kill(words, via_xargs, verdict)
    elif name == "pgrep":
        check_pgrep(words, verdict)
    elif name == "fuser" and any(re.fullmatch(r"-[A-Za-z]*k[A-Za-z]*", r) for r in rest):
        verdict.reasons.append("`fuser -k` kills every process using a file")
    elif name == "systemctl" and "kill" in rest:
        verdict.reasons.append("`systemctl kill` sends signals to a service's processes")
    elif name in SHELLS or name in ("su", "runuser", "sg", "script"):
        script = shell_script(words)
        if script is not None:
            analyze(script, depth + 1, verdict)
        for body in command.bodies:
            if name in SHELLS:
                analyze(body, depth + 1, verdict)
    elif name == "eval":
        analyze(" ".join(rest), depth + 1, verdict)
    elif name in ("watch", "ssh"):
        for i in range(1, len(words)):
            analyze(" ".join(w.text for w in words[i:]), depth + 1, verdict)
    elif name == "find" and any(r in EXEC_OPTIONS for r in rest):
        index = 0
        while index < len(rest):
            if rest[index] in EXEC_OPTIONS:  # every -exec, each up to its `;` or `+`, words re-quoted so `sh -c 'kill -9 $0'` stays one script
                end = next((j for j in range(index + 1, len(rest)) if rest[j] in (";", "+")), len(rest))
                analyze(" ".join(shlex.quote(r) for r in rest[index + 1:end]), depth + 1, verdict)
                index = end
            index += 1
    elif re.match(r"(python|perl|ruby|node|php)", name):
        texts = [words[i + 1].text for i, w in enumerate(words[:-1]) if w.text in ("-c", "-e", "-r")] + command.bodies
        if any(_PYTHON_KILL_RE.search(text) for text in texts):
            verdict.reasons.append("an interpreter one-liner sends SIGKILL or signals by pattern: use agent_procs.py")


# [@ANCHOR: guard_signals:expand_program]
def expand_program(words, assigned):
    """`words` with the program name replaced when it is a variable whose value is known from the same line: `K=kill; $K -9 1`,
    `${K:-kill} -9 1`. Looks past leading assignments and reserved words; `$NAME` or `${NAME}` takes the value assigned earlier
    on the line, `${NAME:-default}` / `${NAME-default}` that value or else the default; the value is split on white space into
    words. A variable with no known value (it may come from the environment) is left as it is, so `$CARGO test` stays allowed."""
    start = 0
    while start < len(words) and (words[start].text in RESERVED or _ASSIGNMENT_RE.match(words[start].text)):
        start += 1
    if start >= len(words):
        return words
    match = _VARIABLE_RE.match(words[start].text)
    if not match:
        return words
    name = match.group(1) or match.group(2)
    value = (assigned or {}).get(name) or match.group(3)
    if not value:
        return words
    try:
        pieces = shlex.split(value)
    except ValueError:
        pieces = value.split()
    return words[:start] + [Word(piece) for piece in pieces] + words[start + 1:]


# [@ANCHOR: guard_signals:piped_shell_sources]
def piped_shell_sources(commands, index):
    """The texts a shell reads when `commands[index]` is a shell with no script of its own whose input is a pipe (`echo 'kill -9 1' |
    sh`, `printf ... | bash`, `cat <<EOF | bash`): the arguments (options of echo and printf left out) and heredoc bodies of every
    earlier command of the same pipeline. Returns `[]` for any other command. `bash script.sh`, `bash -c ...` and a shell that is
    not piped into are not stdin shells."""
    command = commands[index]
    if not command.piped:
        return []
    name, words, _via = resolve(command.words)
    if name not in SHELLS or shell_script(words) is not None or any(not w.text.startswith("-") for w in words[1:]):
        return []
    texts, j = [], index
    while j > 0 and commands[j].piped:
        j -= 1
        source = commands[j]
        source_name, source_words, _ = resolve(source.words)
        args = [w.text for w in source_words[1:] if not (source_name in ("echo", "printf") and w.text.startswith("-"))]
        if args:
            texts.append(" ".join(args))
            if source_name == "printf" and len(args) > 1:
                texts.extend(args)  # printf FORMAT ARG...: the format word is not part of the script
        texts.extend(source.bodies)
    return texts


# [@ANCHOR: guard_signals:analyze]
def analyze(text, depth=0, verdict=None):
    """Parse shell `text` and judge every command in it. Returns the `Verdict` (the same object passed in, when given).
    Raises `ValueError` for text nested deeper than `MAX_DEPTH`."""
    verdict = verdict if verdict is not None else Verdict()
    if depth > MAX_DEPTH:
        raise ValueError("shell text nested too deeply")
    commands = tokenize(text, [], depth)
    assigned = {}
    for index, command in enumerate(commands):
        if command.words and all(_ASSIGNMENT_RE.match(w.text) for w in command.words):
            for word in command.words:
                key, _, value = word.text.partition("=")
                assigned[key] = value
        check_command(command, verdict, depth, assigned)
        for source in piped_shell_sources(commands, index):
            analyze(source, depth + 1, verdict)
    return verdict


# [@ANCHOR: guard_signals:log_path]
def log_path(environ=None):
    """`<jobs dir>/guard.log`: `HAMS_JOBS_DIR` or `~/.cache/hams-jobs`. The directory is created (mode 0700) by `log_event`."""
    environ = os.environ if environ is None else environ
    base = environ.get("HAMS_JOBS_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "hams-jobs")
    return os.path.join(base, "guard.log")


# [@ANCHOR: guard_signals:log_event]
def log_event(kind, command, detail, session_id="", environ=None):
    """Append one JSON line (`time`, `kind` of denied/error, `session`, `detail`, the command cut to 2000
    characters) to the guard log. Never raises: a log that cannot be written must not block or crash the hook."""
    try:
        path = log_path(environ)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "session": session_id,
                                     "detail": detail, "command": command[:2000]}) + "\n")
    except OSError:
        pass


# [@ANCHOR: guard_signals:deny_message]
def deny_message(reasons):
    """The text an agent reads when a command is denied: the reasons, the replacement tools and the statement that there is no escape."""
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_procs.py")
    return ("BLOCKED by guard_signals: " + "; ".join(dict.fromkeys(reasons)) + ". Never find processes by a command-line pattern "
            "and signal the result. Instead: wait for a process with `python3 " + tool + " wait --pid PID --start-time T` "
            "(or `--job NAME`, or `--file F --contains RE`); start long work with `... run NAME -- COMMAND`; stop it with "
            "`... stop NAME` (SIGTERM to its process group; `--force` for SIGKILL on that verified group); signal one exact "
            "pid with `... kill-pid PID --start-time T`; list candidates with `... find --cmd-contains TEXT` (it never lists "
            "itself or its ancestors and flags protected processes). `kill -TERM <literal pid>` is allowed. There is no escape "
            "and no environment variable or flag bypasses this guard: use agent_procs.py, or ask Bruce to run the command in his own "
            "terminal.")


# [@ANCHOR: guard_signals:decide]
def decide(command, session_id="", environ=None):
    """The hook's decision for one Bash command text. Returns `None` to allow silently, or the dict to print as the hook's
    JSON output: a deny (`permissionDecision: deny` with `deny_message`) or an allow-with-reminder (`additionalContext` only,
    no permission decision, so the normal permission flow is untouched).

    There is no escape: nothing in the command text allows a denied command. A crash inside the analysis
    allows a command that has no signal-looking word and denies one that has (and logs the error)."""
    try:
        verdict = analyze(command)
    except Exception as error:  # fail open for ordinary commands only
        log_event("error", command, repr(error), session_id, environ)
        if _CRUDE_RE.search(command):
            verdict = Verdict(["the hook could not parse a command that looks like it sends signals"])
        else:
            return None
    if verdict.reasons:
        log_event("denied", command, verdict.reasons, session_id, environ)
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": deny_message(verdict.reasons)}}
    if verdict.notes:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": verdict.notes[0]}}
    return None


# [@ANCHOR: guard_signals:main]
def main(stdin=None, stdout=None, environ=None):
    """Hook entry point: read the hook JSON from `stdin` (default standard input), print the decision JSON (if any) on
    `stdout`. Returns 0 always. Input that is not JSON, is not a Bash call, or has no command is allowed."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        data = json.load(stdin)
        if data.get("tool_name") != "Bash":
            return 0
        command = (data.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            return 0
    except (ValueError, AttributeError):
        return 0
    result = decide(command, str(data.get("session_id", "")), environ)
    if result:
        json.dump(result, stdout)
        stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
