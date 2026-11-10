#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Result cache for the TEST steps of run_linters.py, keyed by the content of the files each step really read.

A test step (pytest sessions, the relay's structural cargo tests) runs under `strace`, which records every file it and its children open, every
program they execute and every socket they connect to. After a PASSING run the record becomes a manifest: the git blob id of every repository
file read and the listing of every repository directory read (as paths relative to the repository, so another worktree or a merge-queue tree
of the same content matches), the size and modification time of every file outside the repositories (interpreters, site-packages, the Cargo
registry, the toolchain), the paths looked for and not found, and the git state when the step read `.git`. A later run of the same step (same
name, command line, working directory and environment, all with the checkout paths replaced by the repository names; same tool versions)
whose every recorded input is unchanged is served from the cache: reported as a cache hit with the original pass's output.

Never cached (the step simply runs; listed so nobody has to guess):
  * a FAILED or TIMED OUT run: only passes are stored, and a lookup can only return a pass whose inputs are identical, so the cache never turns
    a failure into a pass;
  * a step that touched the NETWORK: a successful connect to a non-loopback address, or to a loopback port that was already listening
    before the step started (a host service: PostgreSQL, Redis, RabbitMQ, Odoo), or to a Unix socket the step did not bind itself, or that
    read the DNS resolver configuration;
  * a step that read HARDWARE or host ENVIRONMENT state: any /dev node except null, zero, full, random, urandom, tty, ptmx, pts, shm, fd and the
    std streams; any /proc or /sys file outside the process's own entries, cgroup limits, CPU topology and a few kernel constants
    (PROC_SYS_ALLOWED);
  * a RANDOMISED step: one that reads a Hypothesis example database (`.hypothesis`), since each run tries new examples;
  * a step whose inputs changed while it ran (an input's mtime is later than the step's start);
  * the steps in NEVER_CACHED, each with its reason (wall-clock time, machine load);
  * any step when strace is missing or cannot trace (macOS, a remote machine): it runs and nothing is stored;
  * a step that runs a setuid/setgid program or one with file capabilities (sudo: under ptrace, and under the no_new_privs flag strace's
    seccomp filter sets, it gets no privileges), or a tracer (TRACER_PROGRAMS); and a step whose traced run failed with a timeout but
    passed untraced. Such a step is rerun untraced at once (its verdict is the untraced one) and runs untraced for UNTRACEABLE_DAYS;
  * every step under `run_linters.py --no-cache`, HAMS_LINT_NO_CACHE=1 or HAMS_LINT_TRACE_CACHE=0.
A stored pass is used for at most MAX_AGE_SECONDS (24 h, HAMS_LINT_TRACE_CACHE_MAX_AGE), which bounds what a test that depends on the date and
is not in NEVER_CACHED could hide.

CLI: `lint_trace_cache.py run TRACE_FILE -- COMMAND...` runs a step traced (`run_traced`); `lint_trace_cache.py explain TRACE_FILE ROOT...` prints the manifest or the veto reasons a raw strace file would give (for tuning).
"""

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

SCHEMA = 1
MAX_AGE_SECONDS = 24 * 3600
KEEP_ENTRIES = 4

# Test steps that run under the trace and may be served from the cache (name prefixes).
TRACED_STEP_PREFIXES = ("pytest ", "relay structural tests")

# Explicitly never cached, with the reason. A prefix of the step name.
NEVER_CACHED = {
    "pytest daemons/relay_ca": "certificate tests: validity windows depend on the wall-clock date",
    "pytest daemons/auth_cert_renew": "certificate renewal: decisions depend on the wall-clock date",
    "pytest daemons/hams_simulated_bots": "idle-CPU and timing tests: results depend on the machine's load (XDIST_SERIAL)",
    "pytest tools/[host-bound]": "drives the real checkout and host state, outside any repository file",
    "pytest devbox_tools/": "talks to GitHub and reads the host's agent and job state",
}

# sendto/sendmsg are NOT traced: glibc sends on every TCP socket with sendto, so tracing them stopped the step at every write (a byte-drip
# timing test of relay_signer timed out under the trace). Remote contact is still seen at connect (TCP, and glibc's DNS, which connects
# its UDP socket); a datagram sent unconnected (dnspython) is caught by the read of the resolver configuration (RESOLVER_FILES).
STRACE_SYSCALLS = "?open,?openat,?openat2,?creat,execve,?execveat,connect,bind,?rename,?renameat,?renameat2,?mkdir,?mkdirat"
RESOLVER_FILES = ("/etc/resolv.conf", "/run/systemd/resolve/resolv.conf", "/run/systemd/resolve/stub-resolv.conf", "/run/resolvconf/resolv.conf")
# Programs that trace other programs: under strace they cannot attach (one tracer per process), so their steps run untraced.
TRACER_PROGRAMS = frozenset({"strace", "ltrace", "gdb", "py-spy", "perf", "valgrind"})
UNTRACEABLE_DAYS = 7

DEV_ALLOWED = ("/dev/shm", "/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/ptmx", "/dev/stdin", "/dev/stdout",
               "/dev/stderr")
DEV_ALLOWED_PREFIXES = ("/dev/pts/", "/dev/shm/", "/dev/fd/")
# Machine state that decides how a run is organised (memory and CPU budgets, the Cargo RAM cache, worker counts), not what a test checks:
# the process's own entries, kernel settings, load and memory figures. A test whose verdict depends on the load is flaky with or without
# the cache. Anything else under /proc or /sys is environment and vetoes.
PROC_SYS_ALLOWED = (
    re.compile(r"^/proc(/(self|thread-self|\d+)(/.*)?)?$"),
    re.compile(r"^/proc/sys(/|$)"),
    re.compile(r"^/proc/(filesystems|cpuinfo|meminfo|stat|loadavg|uptime|version|mounts|swaps)$"),
    re.compile(r"^/proc/pressure(/|$)"),
    re.compile(r"^/sys/fs/cgroup(/|$)"),
    re.compile(r"^/sys/devices/system/cpu(/|$)"),
    re.compile(r"^/sys/kernel/mm/transparent_hugepage(/|$)"),
)
# Unix sockets of the user database (glibc's NSS asks systemd-userdb or nscd who uid 1000 is): the same answer every time.
NSS_SOCKETS = ("/run/systemd/userdb/", "/var/run/nscd/socket", "/run/nscd/socket")
# Inside a repository: generated or derived content, never an input.
REPO_DERIVED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", "target", ".ruff_cache"})
# Volatile environment variables left out of the key (session, terminal, scheduling and scratch-path details).
ENV_IGNORED = re.compile(
    r"^(PWD|OLDPWD|SHLVL|_|TERM|TERM_.*|COLORTERM|SSH_.*|DISPLAY|WINDOWID|XDG_SESSION_.*|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|TMUX.*|STY|"
    r"COLUMNS|LINES|MAIL|LS_COLORS|CLAUDE.*|ANTHROPIC.*|OTEL_.*|TMPDIR|TEMP|TMP|INVOCATION_ID|JOURNAL_STREAM|SYSTEMD_.*|MANAGERPID|"
    r"HAMS_LINT_TIMEOUT_SCALE|HAMS_LINT_SLOT.*|HAMS_LINT_RUN_.*|HAMS_LOCK_HELD.*|HAMS_LINT_LOCK_.*|PYTEST_ADDOPTS|GIT_TRACE.*)$")

_LINE = re.compile(r"^(\d+) +(.*)$")
_CALL = re.compile(r"^(\w+)\((.*)$")
_RESUMED = re.compile(r"^<\.\.\. (\w+) resumed>(.*)$")
_RESULT = re.compile(r"\) += (-?\d+)(?:<(.*)>)?(?: ([A-Z0-9_]+) \(.*\))?$")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_DIRFD = re.compile(r"^(?:AT_FDCWD|\d+)<(.*?)>, ")
_INET = re.compile(r'sa_family=AF_INET6?, sin6?_port=htons\((\d+)\).*?(?:inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)")')
_MARKER = re.compile(r'"/proc/self/hams-lint-trace-(pause|resume)"')
_UNIX = re.compile(r'sa_family=AF_UNIX, sun_path=(@?)"((?:[^"\\]|\\.)*)"')


def _unescape(text):
    """strace's C escapes (\\n, \\t, \\", \\\\, \\xNN, \\NNN) back to the string."""
    if "\\" not in text:
        return text
    raw = bytearray()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 == len(text):
            raw += ch.encode("utf-8", "surrogateescape")
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "x" and re.match(r"[0-9a-fA-F]{2}", text[i + 2:i + 4]):
            raw.append(int(text[i + 2:i + 4], 16))
            i += 4
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", text[i + 1:]).group(0)
            raw.append(int(digits, 8) & 0xFF)
            i += 1 + len(digits)
        else:
            raw += {"n": b"\n", "t": b"\t", "r": b"\r", "v": b"\v", "f": b"\f", '"': b'"', "\\": b"\\"}.get(nxt, nxt.encode())
            i += 2
    return raw.decode("utf-8", "surrogateescape")


class Trace:
    """What one traced run touched: absolute paths read (`reads`), directories listed (`dirs`), looked for and absent (`absent`), written or
    created (`written`), programs executed (`execs`), Unix socket paths bound (`bound`), and network contacts (`contacts`, (kind, detail))."""

    def __init__(self):
        self.reads, self.dirs, self.absent, self.written, self.execs, self.bound = set(), set(), set(), set(), set(), set()
        self.contacts = []


# [@ANCHOR: lint_trace_cache:parse_trace]
def parse_trace(lines):
    """A `Trace` from the lines of `strace -f -qq -y` output (pid prefixes, `<unfinished ...>`/`resumed` pairs joined). A process's file
    reads between its `lint_cache.trace_marker("pause")` and `("resume")` are not recorded (the copy into a Cargo work checkout reads every
    file of the tree; what the build then reads from the copy is recorded). Network contacts are always recorded. Pure."""
    trace = Trace()
    pending = {}
    paused = set()
    for line in lines:
        line = line.rstrip("\n")
        match = _LINE.match(line)
        if not match:
            continue
        pid, rest = match.groups()
        if rest.startswith(("---", "+++")):
            continue
        resumed = _RESUMED.match(rest)
        if resumed:
            rest = pending.pop(pid, resumed.group(1) + "(") + resumed.group(2)
        if rest.endswith("<unfinished ...>"):
            pending[pid] = rest[: -len("<unfinished ...>")].rstrip()
            continue
        call = _CALL.match(rest)
        if not call:
            continue
        marker = _MARKER.search(call.group(2))
        if marker:
            (paused.add if marker.group(1) == "pause" else paused.discard)(pid)
            continue
        if pid in paused and call.group(1) not in ("connect", "bind", "execve", "execveat"):
            continue
        _record(trace, call.group(1), call.group(2))
    return trace


def _record(trace, name, args):
    result = _RESULT.search(args)
    if not result:
        return
    code, resolved, errno = int(result.group(1)), result.group(2), result.group(3)
    ok = code >= 0
    if name in ("open", "openat", "openat2", "creat", "execve", "execveat", "mkdir", "mkdirat", "rename", "renameat", "renameat2"):
        base = None
        dirfd = _DIRFD.match(args)
        if dirfd:
            base = dirfd.group(1)
        quoted = _QUOTED.findall(args[: result.start()])
        if not quoted:
            return
        path = _unescape(quoted[-1] if name.startswith("rename") else quoted[0])
        if not os.path.isabs(path):
            if base is None:
                return
            path = os.path.join(base, path)
        path = os.path.normpath(path)
        if name.startswith("rename"):
            if ok:
                trace.written.add(path)
            return
        if name.startswith("mkdir"):
            if ok:
                trace.written.add(path)
            return
        if name.startswith("execve"):
            if ok:
                trace.execs.add(path)
            elif errno == "ENOENT":
                trace.absent.add(path)
            return
        before = args[: result.start()]
        flags = before[before.rfind('"') + 1:]
        writes = name == "creat" or re.search(r"\bO_(WRONLY|RDWR|CREAT|TRUNC)\b", flags)
        if ok:
            real = resolved if resolved and os.path.isabs(resolved) else path
            if writes:
                trace.written.add(real)
                trace.written.add(path)
            elif "O_DIRECTORY" in args:
                trace.dirs.add(real)
            else:
                trace.reads.add(real)
        elif errno == "ENOENT" and not writes:
            trace.absent.add(path)
        return
    if name == "bind" and ok:
        unix = _UNIX.search(args)
        if unix:
            trace.bound.add(unix.group(1) + _unescape(unix.group(2)))
        return
    if name == "connect" and (ok or errno == "EINPROGRESS"):
        inet = _INET.search(args)
        if inet:
            trace.contacts.append(("inet", (inet.group(2) or inet.group(3), int(inet.group(1)))))
            return
        unix = _UNIX.search(args)
        if unix:
            trace.contacts.append(("unix", unix.group(1) + _unescape(unix.group(2))))


def _loopback(address):
    return address.startswith("127.") or address in ("::1", "::ffff:127.0.0.1", "0.0.0.0", "::")


def listening_ports(paths=("/proc/net/tcp", "/proc/net/tcp6")):
    """The local TCP ports in LISTEN state now (host services a step could talk to)."""
    ports = set()
    for path in paths:
        try:
            with open(path, encoding="ascii") as handle:
                next(handle, None)
                for row in handle:
                    fields = row.split()
                    if len(fields) > 3 and fields[3] == "0A":
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except (OSError, ValueError):
            continue
    return ports


# [@ANCHOR: lint_trace_cache:vetoes]
def vetoes(trace, listening_before=frozenset()):
    """Reasons the traced run must not be cached (network, host services, hardware, environment, randomness); [] when none. Pure."""
    reasons = []
    for kind, detail in trace.contacts:
        if kind == "inet":
            address, port = detail
            if not _loopback(address):
                reasons.append("network: contacted %s:%d" % (address, port))
            elif port in listening_before:
                reasons.append("host service: connected to loopback port %d, which was listening before the step started" % port)
        elif detail not in trace.bound and not detail.startswith(NSS_SOCKETS):
            reasons.append("host service: connected to the Unix socket %s" % detail)
    for path in sorted(trace.reads | trace.dirs):
        if path in trace.written:
            continue
        if path.startswith("/dev/"):
            if path not in DEV_ALLOWED and not path.startswith(DEV_ALLOWED_PREFIXES):
                reasons.append("hardware: read %s" % path)
        elif path.startswith(("/proc/", "/sys/")) or path in ("/proc", "/sys"):
            if not any(rule.match(path) for rule in PROC_SYS_ALLOWED):
                reasons.append("environment: read %s" % path)
        elif os.path.realpath(path) in RESOLVER_FILES or path in RESOLVER_FILES:
            reasons.append("network: read the DNS resolver configuration %s" % path)
        elif "/.hypothesis/" in path + "/":
            reasons.append("randomised: read the Hypothesis example database %s" % path)
    return sorted(set(reasons))


def privileged_program(path):
    """Why running `path` under the trace would not behave as it does untraced (setuid/setgid bits or file capabilities: no privilege under
    ptrace and no_new_privs; a tracer: one tracer per process), else None. Reads the file's mode and capability attribute."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & 0o6000:
        return "runs the setuid/setgid program %s" % path
    try:
        if os.getxattr(path, "security.capability"):
            return "runs %s, which has file capabilities" % path
    except (OSError, AttributeError):
        pass
    if os.path.basename(path) in TRACER_PROGRAMS:
        return "runs the tracer %s" % path
    return None


# [@ANCHOR: lint_trace_cache:trace_blockers]
def trace_blockers(trace):
    """Why the traced run cannot stand for an untraced one (`privileged_program` of every program it executed). [] when none."""
    return [reason for reason in (privileged_program(path) for path in sorted(trace.execs)) if reason]


def never_cached_reason(name):
    for prefix, reason in NEVER_CACHED.items():
        if name.startswith(prefix):
            return reason
    return None


def traced(name):
    return name.startswith(TRACED_STEP_PREFIXES) and never_cached_reason(name) is None


class Roots:
    """The repositories of this run: name -> real path (hams_com, hams_open, hams_shared), their git directories, and the fixed work copies
    of the Cargo builds (`lint_cache.work_checkout`: <cache>/hams-lint/work/<target>/<repo name>/...), which hold the same bytes."""

    def __init__(self, roots, work_base=None, scratch=()):
        self.roots = {name: os.path.realpath(path) for name, path in roots.items() if path and os.path.isdir(path)}
        self.ordered = sorted(self.roots.items(), key=lambda item: -len(item[1]))
        self.work_base = os.path.realpath(work_base) if work_base else None
        self.scratch = tuple(os.path.realpath(p).rstrip("/") + "/" for p in scratch if p)
        self.git_dirs = {}
        for name, path in self.roots.items():
            for arg in ("--git-dir", "--git-common-dir"):
                out = _git(path, "rev-parse", arg)
                if out:
                    self.git_dirs.setdefault(os.path.realpath(os.path.join(path, out)), name)

    def locate(self, path):
        """(repo name, relative path) of an absolute path inside a repository or one of its work copies, else None."""
        if self.work_base and path.startswith(self.work_base + "/"):
            parts = path[len(self.work_base) + 1:].split("/", 2)
            if len(parts) >= 2 and parts[1] in self.roots:
                return parts[1], parts[2] if len(parts) == 3 else ""
        for name, root in self.ordered:
            if path == root:
                return name, ""
            if path.startswith(root + "/"):
                return name, path[len(root) + 1:]
        return None

    def git_repo(self, path):
        for gitdir, name in self.git_dirs.items():
            if path == gitdir or path.startswith(gitdir + "/"):
                return name
        return None

    def is_scratch(self, path):
        return (path.rstrip("/") + "/").startswith(self.scratch)

    def path_of(self, name, rel):
        root = self.roots.get(name)
        return None if root is None else (os.path.join(root, rel) if rel else root)

    def normalise(self, text):
        """`text` with every repository path replaced by <name> (longest first), so the key does not depend on the checkout's location."""
        for name, root in self.ordered:
            text = text.replace(root, "<%s>" % name)
        return text


def _git(cwd, *args):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


class Hasher:
    """Git blob ids of files and listing digests of directories, memoised for the run by (device, inode, size, mtime). Thread-safe."""

    def __init__(self):
        self.memo = {}
        self.lock = threading.Lock()

    def blob(self, path):
        try:
            info = os.stat(path)
        except OSError:
            return None
        key = (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        with self.lock:
            if key in self.memo:
                return self.memo[key]
        try:
            if os.path.isdir(path):
                digest = self.listing(path)
            else:
                sha = hashlib.sha1(b"blob %d\0" % info.st_size)
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        sha.update(chunk)
                digest = sha.hexdigest()
        except OSError:
            return None
        with self.lock:
            self.memo[key] = digest
        return digest

    @staticmethod
    def listing(path):
        try:
            names = sorted(entry.name + ("/" if entry.is_dir(follow_symlinks=False) else "") for entry in os.scandir(path)
                           if entry.name not in REPO_DERIVED_PARTS)
        except OSError:
            return None
        return "dir:" + hashlib.sha256("\0".join(names).encode("utf-8", "surrogateescape")).hexdigest()


def git_state(root):
    """What a step that read `.git` may have seen: the tree of HEAD, the diff of the working tree against it, the untracked files and the
    commit of origin/main. None when git cannot tell."""
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if tree is None:
        return None
    try:
        diff = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=root, capture_output=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    untracked = _git(root, "ls-files", "--others", "--exclude-standard") or ""
    base = _git(root, "rev-parse", "--verify", "-q", "origin/main") or ""
    return hashlib.sha256(b"\0".join([tree.encode(), hashlib.sha256(diff).hexdigest().encode(), untracked.encode(), base.encode()])).hexdigest()


def _stat_key(path):
    try:
        info = os.stat(path)
    except OSError:
        return [-1, 0]
    return [info.st_size, info.st_mtime_ns]


# [@ANCHOR: lint_trace_cache:build_manifest]
def build_manifest(trace, roots, hasher, started_ns):
    """(manifest, problems) for a passing traced run. The manifest lists repository files by blob id, repository directories by listing,
    absent repository paths, outside files and directories by (size, mtime) and the git state of each repository whose .git was read.
    `problems` is non-empty (and the run must not be stored) when an input changed after the step started (`started_ns`, time.time_ns)."""
    files, dirs, absent, external, gits, problems = {}, {}, set(), {}, {}, []
    for path in sorted(trace.reads | trace.dirs | trace.execs | trace.absent):
        if path in trace.written or path.startswith(("/proc/", "/sys/", "/dev/")) or path in ("/proc", "/sys", "/dev"):
            continue
        git_name = roots.git_repo(path)
        if git_name:
            gits[git_name] = None
            continue
        place = roots.locate(path)
        if place is None and roots.is_scratch(path):
            continue
        if place is None:
            external[path] = _stat_key(path)
            if external[path][1] > started_ns and path not in trace.absent and not os.path.isdir(path):
                problems.append("changed while the step ran: %s" % path)
            continue
        name, rel = place
        if set(rel.split("/")) & REPO_DERIVED_PARTS:
            continue
        if path in trace.absent:
            if not os.path.lexists(path):
                absent.add((name, rel))
            continue
        try:
            if os.stat(path).st_mtime_ns > started_ns and not os.path.isdir(path):  # a directory's mtime moves with its __pycache__
                problems.append("changed while the step ran: %s/%s" % (name, rel))
        except OSError:
            continue
        digest = hasher.blob(path)
        if digest is None:
            continue
        (dirs if digest.startswith("dir:") else files)[(name, rel)] = digest
    for name in gits:
        gits[name] = git_state(roots.roots[name])
        if gits[name] is None:
            problems.append("git state of %s cannot be read" % name)
    manifest = {
        "files": sorted([n, r, d] for (n, r), d in files.items()),
        "dirs": sorted([n, r, d] for (n, r), d in dirs.items()),
        "absent": sorted([n, r] for n, r in absent),
        "external": sorted([p, s[0], s[1]] for p, s in external.items()),
        "git": sorted([n, s] for n, s in gits.items() if s),
    }
    return manifest, problems


# [@ANCHOR: lint_trace_cache:verify]
def verify(manifest, roots, hasher, git_memo=None):
    """None when every input of `manifest` is unchanged under `roots`, else the first difference (a string)."""
    git_memo = {} if git_memo is None else git_memo
    for path, size, mtime in manifest.get("external", ()):
        if _stat_key(path) != [size, mtime]:
            return "outside file changed: %s" % path
    for name, rel in manifest.get("absent", ()):
        path = roots.path_of(name, rel)
        if path is None or os.path.lexists(path):
            return "now exists: %s/%s" % (name, rel)
    for key in ("dirs", "files"):
        for name, rel, digest in manifest.get(key, ()):
            path = roots.path_of(name, rel)
            if path is None or hasher.blob(path) != digest:
                return "changed: %s/%s" % (name, rel)
    for name, state in manifest.get("git", ()):
        if name not in roots.roots:
            return "no repository %s" % name
        if name not in git_memo:
            git_memo[name] = git_state(roots.roots[name])
        if git_memo[name] != state:
            return "git state of %s changed" % name
    return None


def strace_usable(_memo={}):
    """True when strace exists and can trace a child here (not on macOS, not where ptrace is forbidden). Probed once per process."""
    if "ok" not in _memo:
        ok = False
        if shutil.which("strace") and sys.platform.startswith("linux"):
            try:
                ok = subprocess.run(["strace", "-f", "-qq", "--seccomp-bpf", "-e", "trace=openat", "-o", os.devnull, "true"],
                                    capture_output=True, timeout=30).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        _memo["ok"] = ok
    return _memo["ok"]


# [@ANCHOR: lint_trace_cache:trace_argv]
def trace_argv(argv, trace_file, python=sys.executable):
    """The command line that runs `argv` traced into `trace_file` (through `run_traced`, this file's `run` command)."""
    return [python, os.path.abspath(__file__), "run", trace_file, "--"] + list(argv)


ABORTED_STATUS = 125
_EXECVE = re.compile(r'^execve(?:at)?\((?:\S+, )?"((?:[^"\\]|\\.)*)"')


def _abort(proc, trace_file, reason):
    """Ends a traced run at once: strace (our own child) is stopped hard and its --kill-on-exit option makes the kernel end every process it
    traces. Records why in <trace_file>.abort and returns ABORTED_STATUS; the caller reruns the step untraced."""
    with open(trace_file + ".abort", "w", encoding="utf-8") as handle:
        handle.write(reason + "\n")
    proc.kill()
    proc.wait()
    return ABORTED_STATUS


def aborted(trace_file):
    """The reason `run_traced` ended a run early, else None."""
    try:
        with open(trace_file + ".abort", encoding="utf-8") as handle:
            return handle.read().strip() or "aborted"
    except OSError:
        return None


# [@ANCHOR: lint_trace_cache:run_traced]
def run_traced(trace_file, argv, poll=0.2):
    """Runs `argv` under `strace -f` writing `trace_file`, and returns its exit status as soon as the MAIN process has ended: a plain run does
    not wait for background processes the step leaves behind (they hold no pipe of ours), but strace waits for every tracee, so when the main
    process's exit appears in the trace strace is told to stop (SIGTERM: it detaches and the leftovers run on untraced, as without the trace).
    A main process killed by a signal gives 128 + the signal number, as a shell reports it. The moment any traced process starts a program
    that behaves differently under the trace (`privileged_program`: sudo and other setuid programs, tracers) the run is ended (`_abort`) and
    ABORTED_STATUS returned, so the caller loses seconds, not the whole step, before it reruns the step untraced."""
    command = ["strace", "-I", "1", "--kill-on-exit", "-f", "-q", "-y", "-s", "4096", "--seccomp-bpf", "-e", "trace=" + STRACE_SYSCALLS,
               "-o", trace_file, "--"] + list(argv)
    proc = subprocess.Popen(command)
    main_pid, status, offset, partial = None, None, 0, ""
    while status is None:
        ended = proc.poll() is not None
        try:
            with open(trace_file, encoding="utf-8", errors="surrogateescape") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        except OSError:
            chunk = ""
        lines = (partial + chunk).split("\n")
        partial = lines.pop()
        for line in lines:
            match = _LINE.match(line)
            if not match:
                continue
            if main_pid is None and match.group(2).startswith("execve("):
                main_pid = match.group(1)
            program = _EXECVE.match(match.group(2))
            reason = privileged_program(_unescape(program.group(1))) if program else None
            if reason:
                return _abort(proc, trace_file, reason)
            if match.group(1) == main_pid:
                done = re.match(r"\+\+\+ (?:exited with (\d+)|killed by (SIG[A-Z0-9]+))", match.group(2))
                if done and done.group(1) is not None:
                    status = int(done.group(1))
                elif done:
                    number = getattr(signal, done.group(2), None)
                    status = 128 + int(number) if number is not None else 1
        if ended and status is None:
            return proc.returncode if main_pid is None else (proc.returncode or 1)
        if status is None:
            time.sleep(poll)
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return status


class TraceCache:
    """The store: <directory>/<first two hex digits>/<step key>.json, each holding the newest KEEP_ENTRIES passes of that key."""

    def __init__(self, directory, roots, versions="", environ=None):
        self.directory = directory
        self.roots = roots
        self.versions = versions
        environ = os.environ if environ is None else environ
        self.max_age = max_age_seconds(environ)
        self.hasher = Hasher()
        self.git_memo = {}
        self._names = {}
        self.usable = strace_usable()

    @staticmethod
    def traced(name):
        return traced(name)

    # [@ANCHOR: lint_trace_cache:step_key]
    def step_key(self, step):
        """The step's identity: name, command line, working directory and environment (checkout paths replaced by repository names,
        volatile variables dropped: ENV_IGNORED) and the tool versions."""
        environ = step.env if step.env is not None else os.environ
        env = sorted((k, self.roots.normalise(v)) for k, v in environ.items() if not ENV_IGNORED.match(k))
        text = json.dumps([SCHEMA, step.name, [self.roots.normalise(a) for a in step.argv or ()],
                           self.roots.normalise(step.cwd or ""), env, self.versions])
        key = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
        self._names[key] = step.name
        return key

    def _path(self, key):
        return os.path.join(self.directory, key[:2], key + ".json")

    def _load(self, key):
        try:
            with open(self._path(key), encoding="utf-8") as handle:
                data = json.load(handle)
            return data.get("entries", []) if data.get("schema") == SCHEMA else []
        except (OSError, ValueError, AttributeError):
            return []

    def lookup(self, step, key, now=None):
        """The stored entry of a pass whose inputs are all unchanged, else None."""
        now = time.time() if now is None else now
        for entry in self._load(key):
            if now - entry.get("when", 0) > self.max_age or entry.get("returncode") != 0:
                continue
            if verify(entry.get("manifest", {}), self.roots, self.hasher, self.git_memo) is None:
                return entry
        return None

    def store(self, key, step, result, manifest, started):
        """Records a PASS (anything else is ignored). Concurrent writers serialise on a lock file."""
        if result.returncode != 0 or result.timed_out or result.cached:
            return False
        entry = {"when": started, "step": step.name, "returncode": 0, "seconds": round(result.seconds, 2), "output": result.output,
                 "tree": self.roots.normalise(step.cwd or ""), "manifest": manifest}
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = [e for e in self._load(key) if e.get("manifest") != manifest]
            entries = ([entry] + entries)[:KEEP_ENTRIES]
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"schema": SCHEMA, "entries": entries}, handle)
            os.replace(tmp, path)
        return True


    def _marker(self, key):
        """Kept per step NAME, not per step key: a step that runs sudo keeps doing so when its file list or environment changes, and
        finding it out again costs a traced start each time."""
        name = self._names.get(key, key)
        return os.path.join(self.directory, "untraceable", hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest() + ".json")

    def untraceable(self, key, now=None):
        """The reason the step must run untraced (a marker younger than UNTRACEABLE_DAYS), else None."""
        try:
            with open(self._marker(key), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        now = time.time() if now is None else now
        if not isinstance(data, dict) or now - data.get("when", 0) > UNTRACEABLE_DAYS * 86400:
            return None
        return data.get("reason") or "untraceable"

    def mark_untraceable(self, key, step, reasons):
        path = self._marker(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"when": time.time(), "step": step.name, "reason": "; ".join(reasons[:3])}, handle)
            os.replace(tmp, path)
        except OSError:
            pass
        self._not_stored(step, ["runs untraced from now on: " + "; ".join(reasons[:3])])

    def read_trace(self, trace_file):
        try:
            with open(trace_file, encoding="utf-8", errors="surrogateescape") as handle:
                return parse_trace(handle)
        except OSError:
            return None

    def record(self, step, key, result, trace, listening, started, started_ns):
        """Stores a passing traced run unless a veto or an input that changed while it ran forbids it; the reason why not goes to
        <directory>/not_stored.log (one line per run, for tuning). Returns True when stored."""
        if trace is None:
            return self._not_stored(step, ["trace unreadable"])
        reasons = vetoes(trace, listening)
        if reasons:
            return self._not_stored(step, reasons)
        manifest, problems = build_manifest(trace, self.roots, self.hasher, started_ns)
        if problems:
            return self._not_stored(step, problems)
        return self.store(key, step, result, manifest, started)

    def _not_stored(self, step, reasons):
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(os.path.join(self.directory, "not_stored.log"), "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), step.name, "; ".join(reasons[:5])))
        except OSError:
            pass
        return False


def roots_for(repo_root, tools_dir):
    """{repository name: path} of a run: the linted repository, its sibling (hams_com/hams_open) and hams_shared (the tools' parent)."""
    import lint_exec
    roots = {}
    name = lint_exec.repo_name(repo_root) or os.path.basename(os.path.realpath(repo_root))
    roots[name] = repo_root
    sibling = lint_exec.sibling_repo_root(repo_root)
    if sibling:
        roots[lint_exec.repo_name(sibling) or os.path.basename(os.path.realpath(sibling))] = sibling
    roots["hams_shared"] = os.path.dirname(os.path.realpath(tools_dir))
    return roots


def scratch_dirs(environ=None):
    """Directories whose contents are scratch, never inputs: the temporary directories and the lint Cargo target directories."""
    import tempfile
    environ = os.environ if environ is None else environ
    dirs = ["/tmp", "/var/tmp", "/dev/shm", tempfile.gettempdir()] + [environ[k] for k in ("TMPDIR",) if environ.get(k)]
    try:
        import lint_cache
        dirs += list(lint_cache.cargo_bases(environ))
    except Exception:  # the cargo bases are an optimisation of the key, never a reason to fail the run
        pass
    return dirs


def open_cache(repo_root, tools_dir, versions, environ=None):
    """The run's TraceCache, or None when it is turned off (HAMS_LINT_NO_CACHE=1, HAMS_LINT_TRACE_CACHE=0)."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_LINT_NO_CACHE") == "1" or environ.get("HAMS_LINT_TRACE_CACHE") == "0":
        return None
    import lint_cache
    base = os.path.join(lint_cache.cache_root(environ), "hams-lint")
    roots = Roots(roots_for(repo_root, tools_dir), work_base=os.path.join(base, "work"), scratch=scratch_dirs(environ))
    return TraceCache(os.path.join(base, "results"), roots, versions, environ)


def max_age_seconds(environ):
    """HAMS_LINT_TRACE_CACHE_MAX_AGE in seconds when it is a finite number of at least 0, else MAX_AGE_SECONDS ("inf" or "nan" would keep
    or reject every pass for ever)."""
    try:
        value = float(environ.get("HAMS_LINT_TRACE_CACHE_MAX_AGE", MAX_AGE_SECONDS))
    except ValueError:
        return MAX_AGE_SECONDS
    return value if math.isfinite(value) and value >= 0 else MAX_AGE_SECONDS


def cache_hit_note(entry):
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("when", 0)))
    return ("(result cache: every file this step read is unchanged since its pass of %s (%.0f s); output above is from that run; "
            "--no-cache reruns it)\n" % (when, entry.get("seconds", 0)))


def main(argv):
    if len(argv) >= 4 and argv[0] == "run" and argv[2] == "--":
        return run_traced(argv[1], argv[3:])
    if len(argv) >= 3 and argv[0] == "explain":
        with open(argv[1], encoding="utf-8", errors="surrogateescape") as handle:
            trace = parse_trace(handle)
        roots = Roots({os.path.basename(p.rstrip("/")): p for p in argv[2:]})
        reasons = vetoes(trace, listening_ports())
        manifest, problems = build_manifest(trace, roots, Hasher(), time.time_ns())
        print(json.dumps({"vetoes": reasons, "problems": problems, "counts": {k: len(v) for k, v in manifest.items()}}, indent=1))
        return 0
    print("usage: lint_trace_cache.py explain TRACE_FILE ROOT...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
