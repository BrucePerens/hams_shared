# SPDX-License-Identifier: AGPL-3.0-or-later
"""Is a failure of a unit in the isolated verification VM EXPECTED (no network) or REAL (the restored system is wrong)?

The VM has no network. Units that call outside services (sync daemons, mail, the Cloudflare tunnel, ACME) must start, try the network and fail
to connect: that is expected and proves they were restored as production has them. Anything else that fails (missing file, permission,
missing module or package, configuration error, a crash, a traceback that is not a network error, refusal of a connection to this very
machine) is real. The decision uses the unit's own journal text and exit status.
"""
import re

NETWORK_SIGNATURES = re.compile(
    r"Network is unreachable|No route to host|Temporary failure in name resolution|Name or service not known|"
    r"nodename nor servname|Could not resolve host|Could not resolve|Failed to resolve|getaddrinfo|socket\.gaierror|"
    r"Failed to establish a new connection|Max retries exceeded|NameResolutionError|dial tcp|no such host|lookup [\w.-]+ on|"
    r"ENETUNREACH|EHOSTUNREACH|ECONNREFUSED|Connection refused|Connection timed out|timed out|Timeout|TimeoutError|ConnectionError|"
    r"ConnectTimeout|Connection reset|Unable to establish connection|failed to connect|Failed to connect|Cannot assign requested address|"
    r"connect: network|temporary failure|resolve.*failed|ServerNotFoundError|Unable to connect|SMTPServerDisconnected|"
    r"SMTPConnectError|MaxRetryError|URLError|HTTPSConnectionPool|HTTPConnectionPool|No address associated with hostname",
    re.IGNORECASE)
LOOPBACK = re.compile(r"127\.\d+\.\d+\.\d+|localhost|\[?::1\]?|/var/run/|/run/\S+\.sock|\.s\.PGSQL|unix socket", re.IGNORECASE)
HARD_FAILURES = re.compile(
    r"status=(?:20\d|21\d|22\d|23\d|24\d)/|code=exited, status=(?:20\d|21\d|22\d|23\d|24\d)|"
    r"ModuleNotFoundError|ImportError|command not found|Permission denied|Operation not permitted|Read-only file system|"
    r"ConditionPath\w* failed|Failed to load environment files|EnvironmentFile|No such file or directory|not found|"
    r"Unit .* not found|Failed to start .*: Unit|start-limit-hit|Start request repeated too quickly|"
    r"KeyError|AttributeError|SyntaxError|NameError|Segmentation fault|core-dump|"
    r"FATAL:|invalid configuration|Invalid config|configuration error|unknown option|Unknown key|syntax error",
    re.IGNORECASE)
NETWORK_NAMED_FILE = re.compile(r"No such file or directory.*(?:\.sock|:\d+)", re.IGNORECASE)

OK, EXPECTED, REAL, NOT_STARTED = "ok", "expected_network_failure", "real_failure", "not_started"


def line_is_loopback_refusal(line):
    return bool(NETWORK_SIGNATURES.search(line)) and bool(LOOPBACK.search(line))


def traceback_final_line(journal):
    """The exception line that closes the LAST Python traceback in the journal text, or None when there is no traceback."""
    lines = journal.splitlines()
    starts = [i for i, ln in enumerate(lines) if "Traceback (most recent call last)" in ln]
    if not starts:
        return None
    for ln in lines[starts[-1] + 1:]:
        if ln and not ln[0].isspace():
            return ln
    return ""


def classify(state, journal):
    """Category for one unit. `state` is a dict with ActiveState, SubState, Result, ExecMainStatus, ExecMainStartTimestamp, NRestarts (strings).

    Returns (category, reason). Units that are running or finished cleanly are ok; not started at all is its own failure."""
    active, result = state.get("ActiveState", ""), state.get("Result", "success")
    started = bool(state.get("ExecMainStartTimestamp")) or bool(state.get("InvocationID")) or active in ("active", "activating", "failed")
    failed = active == "failed" or result not in ("success", "") or state.get("SubState") == "auto-restart"
    if not started and not failed:
        return NOT_STARTED, "never started (state %s/%s)" % (active, state.get("SubState"))
    lines = journal.splitlines()
    evidence = [ln for ln in lines if NETWORK_SIGNATURES.search(ln)]
    external = [ln for ln in evidence if not LOOPBACK.search(ln)]
    local_refused = [ln for ln in evidence if LOOPBACK.search(ln)]
    if not failed:
        if local_refused and not external:
            return REAL, "connection to this machine refused: " + local_refused[-1].strip()[:200]
        return OK, "running or completed" + (" (network attempt seen)" if external else "")
    hard = [ln for ln in lines if HARD_FAILURES.search(ln) and not NETWORK_NAMED_FILE.search(ln) and not NETWORK_SIGNATURES.search(ln)]
    if hard and not external:
        return REAL, hard[-1].strip()[:200]
    if local_refused and not external:
        return REAL, "connection to this machine refused: " + local_refused[-1].strip()[:200]
    final = traceback_final_line(journal)
    if final is not None and not NETWORK_SIGNATURES.search(final):
        return REAL, "traceback ends with: " + final.strip()[:180]
    if external:
        return EXPECTED, external[-1].strip()[:200]
    if hard:
        return REAL, hard[-1].strip()[:200]
    return REAL, "failed with no explanation in the journal (result %s, status %s)" % (result, state.get("ExecMainStatus"))


def network_attempt_seen(journal):
    """True when the journal shows an attempt to reach something outside this machine (it failed, as it must in the isolated VM)."""
    return any(NETWORK_SIGNATURES.search(ln) and not LOOPBACK.search(ln) for ln in journal.splitlines())
