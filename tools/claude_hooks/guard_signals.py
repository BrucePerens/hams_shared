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
  * `kill` whose pids are not literal numbers (from `$(...)`, backticks, variables, a pipe): `kill $(pgrep ...)`, `... | xargs kill`.
It allows `kill -TERM 123`, `kill 123`, `kill -0 123`, `kill -l`, and everything else (grep, ps, cargo, git, sleep loops, and
`agent_procs.py wait`). A literal-pid kill is allowed with a reminder to print the ps line first.

The escape for a human: `HAMS_ALLOW_SIGNALS=1` in the command itself (`HAMS_ALLOW_SIGNALS=1 kill -9 123`) lets it through and
appends a line to `~/.cache/hams-jobs/guard.log`. An agent must never set it on its own.

Failure policy: if the hook itself crashes, a command that does not look like a signal command is allowed (a broken hook must not
block ordinary work), and one that does look like one is denied. Invalid input is allowed. Exit status is always 0; a denial is
the JSON `permissionDecision: deny` on standard output. Documentation of every function: tools/README.md
"Guard hook for signals (`claude_hooks/guard_signals.py`)".
"""

import dataclasses
import json
import os
import re
import sys
import time

MAX_DEPTH = 8
ESCAPE_VARIABLE = "HAMS_ALLOW_SIGNALS"
_ESCAPE_RE = re.compile(r"(?:^|[\s;&|(])" + ESCAPE_VARIABLE + r"=1(?=[\s;&|)]|$)")
_CRUDE_RE = re.compile(r"kill|pgrep|pidof|xargs|-9\b|SIGKILL|fuser", re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_KILL_RE = re.compile(
    r"SIGKILL|os\.killpg|\.kill\([^)]*,\s*9\s*\)|\[\s*['\"](?:pkill|killall)['\"]|\[\s*['\"]pgrep['\"]\s*,\s*['\"]-\w*f|"
    r"os\.system\(\s*['\"][^'\"]*(?:pkill|killall|kill\s+-9|pgrep\s+-\w*f)")
RESERVED = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "time", "fi", "done", "esac", "coproc"}
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
    "unbuffer": set(), "time": set(),
}
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
    """One simple command: its `words` (including leading assignments and wrappers) and the bodies of the heredocs attached
    to it (`bodies`, raw text, never parsed unless the command is a shell or an interpreter)."""

    words: list
    bodies: list = dataclasses.field(default_factory=list)


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
    ( )` and newline (command boundaries), redirections (the file word and a leading fd digit are dropped) and heredocs (the
    body is attached to the command). Raises `ValueError` when the nesting is deeper than `MAX_DEPTH`."""
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
                    pending.append((word, want_delim, len(bodies)))
                    bodies.append("")
                    want_delim = None
                elif drop_next:
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
                drop_next = True
            else:
                if op == "\n" and pending:
                    for delimiter, strip_tabs, index in pending:
                        body, i = read_heredoc(text, i, delimiter, strip_tabs)
                        bodies[index] = body
                    pending = []
                if words or bodies:
                    commands.append(current)
                words, bodies, drop_next, want_delim = [], [], False, None
                current = Command(words, bodies)
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
    if started and not drop_next and want_delim is None:
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
        if text in value_options:
            i += 1
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
        if name not in WRAPPER_VALUE_OPTIONS:
            return name, words, via_xargs
        via_xargs = via_xargs or name == "xargs"
        index = skip_options(words, 1, WRAPPER_VALUE_OPTIONS[name])
        if name == "command" and any(w.text in ("-v", "-V") for w in words[1:index]):
            return None, [], via_xargs  # `command -v pkill` only looks a program up
        index += POSITIONAL_AFTER_OPTIONS.get(name, 0)
        if name == "env":
            while index < len(words) and _ASSIGNMENT_RE.match(words[index].text):
                index += 1
        words = words[index:]


# [@ANCHOR: guard_signals:signal_is_kill]
def signal_is_kill(spec):
    """True when the signal spec text (`9`, `KILL`, `SIGKILL`, any case, with or without a leading `-`) names SIGKILL."""
    spec = spec.lstrip("-").upper()
    spec = spec[3:] if spec.startswith("SIG") else spec
    return spec in ("9", "KILL")


# [@ANCHOR: guard_signals:check_kill]
def check_kill(words, via_xargs, verdict):
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
            if signal_is_kill(text):
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
        if not literal and word.text != "$!":
            verdict.reasons.append(f"kill needs literal pid numbers, not {word.text!r}: a pid from `$(...)`, a variable or a pipe "
                                   "can name the wrong process")
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
def check_command(command, verdict, depth):
    """Judge one `Command` and add what is wrong to `verdict`; recurses into `bash -c`, `eval`, `ssh host '...'`, `find -exec`,
    `watch`, shell heredocs and interpreter `-c`/`-e` strings and heredocs (`depth` guards recursion)."""
    name, words, via_xargs = resolve(command.words)
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
    elif name in SHELLS or name in ("su", "runuser", "sg"):
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
    elif name == "find" and any(r in ("-exec", "-execdir", "-ok", "-okdir") for r in rest):
        index = next(i for i, r in enumerate(rest) if r in ("-exec", "-execdir", "-ok", "-okdir"))
        analyze(" ".join(rest[index + 1:]).replace(";", " ; "), depth + 1, verdict)
    elif re.match(r"(python|perl|ruby|node|php)", name):
        texts = [words[i + 1].text for i, w in enumerate(words[:-1]) if w.text in ("-c", "-e", "-r")] + command.bodies
        if any(_PYTHON_KILL_RE.search(text) for text in texts):
            verdict.reasons.append("an interpreter one-liner sends SIGKILL or signals by pattern: use agent_procs.py")


# [@ANCHOR: guard_signals:analyze]
def analyze(text, depth=0, verdict=None):
    """Parse shell `text` and judge every command in it. Returns the `Verdict` (the same object passed in, when given).
    Raises `ValueError` for text nested deeper than `MAX_DEPTH`."""
    verdict = verdict if verdict is not None else Verdict()
    if depth > MAX_DEPTH:
        raise ValueError("shell text nested too deeply")
    for command in tokenize(text, [], depth):
        check_command(command, verdict, depth)
    return verdict


# [@ANCHOR: guard_signals:log_path]
def log_path(environ=None):
    """`<jobs dir>/guard.log`: `HAMS_JOBS_DIR` or `~/.cache/hams-jobs`. The directory is created (mode 0700) by `log_event`."""
    environ = os.environ if environ is None else environ
    base = environ.get("HAMS_JOBS_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "hams-jobs")
    return os.path.join(base, "guard.log")


# [@ANCHOR: guard_signals:log_event]
def log_event(kind, command, detail, session_id="", environ=None):
    """Append one JSON line (`time`, `kind` of override/denied/error, `session`, `detail`, the command cut to 2000
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
    """The text an agent reads when a command is denied: the reasons, the replacement tools and the human-only escape."""
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_procs.py")
    return ("BLOCKED by guard_signals: " + "; ".join(dict.fromkeys(reasons)) + ". Never find processes by a command-line pattern "
            "and signal the result. Instead: wait for a process with `python3 " + tool + " wait --pid PID --start-time T` "
            "(or `--job NAME`, or `--file F --contains RE`); start long work with `... run NAME -- COMMAND`; stop it with "
            "`... stop NAME` (SIGTERM to its process group; `--force` for SIGKILL on that verified group); signal one exact "
            "pid with `... kill-pid PID --start-time T`; list candidates with `... find --cmd-contains TEXT` (it never lists "
            "itself or its ancestors and flags protected processes). `kill -TERM <literal pid>` is allowed. Only a human may "
            "override, with HAMS_ALLOW_SIGNALS=1 in the command (logged); do not set it yourself.")


# [@ANCHOR: guard_signals:decide]
def decide(command, session_id="", environ=None):
    """The hook's decision for one Bash command text. Returns `None` to allow silently, or the dict to print as the hook's
    JSON output: a deny (`permissionDecision: deny` with `deny_message`) or an allow-with-reminder (`additionalContext` only,
    no permission decision, so the normal permission flow is untouched).

    The human escape `HAMS_ALLOW_SIGNALS=1` allows a command that would be denied and logs it. A crash inside the analysis
    allows a command that has no signal-looking word and denies one that has (and logs the error)."""
    try:
        verdict = analyze(command)
    except Exception as error:  # fail open for ordinary commands only
        log_event("error", command, repr(error), session_id, environ)
        if _CRUDE_RE.search(command) and not _ESCAPE_RE.search(command):
            verdict = Verdict(["the hook could not parse a command that looks like it sends signals"])
        else:
            return None
    if verdict.reasons:
        if _ESCAPE_RE.search(command):
            log_event("override", command, verdict.reasons, session_id, environ)
            return None
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
