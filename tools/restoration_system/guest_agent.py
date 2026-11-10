#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs INSIDE the verification VM (copied into the VM's disk copy by verify.py, never into the image directory). Standard library only.

Boot order: the VM boots to hams-verify-gate.service ONLY (systemd.unit= on the kernel command line), so no production service has
started. This agent first proves the machine has no way out; if that proof fails it powers the machine off and no service is
ever started. Only then does it start multi-user.target (every production unit, enabled as on production), waits, runs the checks
and writes the results to the serial console as lines `HAMSVERIFY <json>`; the host parses them.
"""
import json
import os
import socket
import subprocess
import sys
import time

CONFIG = "/usr/local/lib/hams-verify/config.json"
TARGETS_TCP = [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 53), ("93.184.216.34", 80)]
CANARY_HOSTS = ["10.0.2.2", "10.0.2.3", "192.168.122.1", "172.17.0.1"]


EMIT_COUNTER = [0]
CHUNK = 120


def emit(phase, **data):
    """One result to the second serial port (ttyS1), which nothing else writes to. The JSON is base64 in short numbered lines so that no
    line limit or dropped character of the serial line can damage it: `HAMSVERIFY <id> <index>/<count> <chunk>`."""
    import base64
    data["phase"] = phase
    blob = base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()
    pieces = [blob[i:i + CHUNK] for i in range(0, len(blob), CHUNK)] or [""]
    EMIT_COUNTER[0] += 1
    lines = "".join("HAMSVERIFY %d %d/%d %s\n" % (EMIT_COUNTER[0], i + 1, len(pieces), piece) for i, piece in enumerate(pieces))
    try:
        fd = os.open("/dev/ttyS1", os.O_WRONLY | os.O_NOCTTY)
        try:
            import termios
            import tty
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (ImportError, OSError):
            pass
        data_bytes = lines.encode()
        while data_bytes:
            data_bytes = data_bytes[os.write(fd, data_bytes):]
        termios.tcdrain(fd)
        os.close(fd)
    except Exception as exc:  # audit-ignore-catch-all: the fallback carries the reason
        sys.stdout.write("HAMSVERIFY_PORT_ERROR %r\n" % (exc,) + lines)
        sys.stdout.flush()


def sh(argv, timeout=60, user=None):
    if user:
        argv = ["runuser", "-u", user, "--"] + argv
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)


def interfaces():
    return [n for n in sorted(os.listdir("/sys/class/net")) if n != "lo"]


def routes():
    out = []
    try:
        with open("/proc/net/route") as handle:
            for line in handle.read().splitlines()[1:]:
                f = line.split()
                if f and f[1] == "00000000":
                    out.append(f[0])
    except OSError:
        pass
    return out


def try_tcp(host, port, timeout=3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def try_udp(host, port, payload, timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(payload, (host, port))
        s.recvfrom(512)
        return True
    except OSError:
        return False
    finally:
        s.close()


def try_dns(name):
    try:
        socket.setdefaulttimeout(5)
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False


def mount_modules():
    """The host kernel's modules arrive on a second disk (the image has none: it is not a machine image); mounted so units can load nft, wireguard."""
    if os.path.exists("/dev/vdb"):
        target = "/lib/modules/" + os.uname().release
        os.makedirs(target, exist_ok=True)
        mounted = sh(["mount", "-o", "ro", "/dev/vdb", target])
        restarted = sh(["systemctl", "restart", "systemd-modules-load.service"])  # it ran in sysinit, before these modules existed
        emit("modules", mount=mounted, restart=restarted)


def isolation(cfg):
    """Every check must show NO way out. Any NIC is configured best-effort first, so a route that exists is measured, not assumed away."""
    checks = []
    nics = interfaces()
    for nic in nics:
        sh(["ip", "link", "set", nic, "up"])
        sh(["ip", "addr", "add", "10.0.2.15/24", "dev", nic])
        sh(["ip", "route", "add", "default", "via", "10.0.2.2"])
    time.sleep(1 if nics else 0)
    checks.append(("no network device besides loopback", not nics, ",".join(nics)))
    checks.append(("no default route", not routes(), ",".join(routes())))
    checks.append(("no ipv6 default route", not any(ln.split()[0] == "0" * 32 and ln.split()[9] != "lo" for ln in open("/proc/net/ipv6_route")), ""))
    for host, port in TARGETS_TCP:
        checks.append(("outbound TCP %s:%d fails" % (host, port), not try_tcp(host, port), ""))
    dns = bytes.fromhex("12340100000100000000000007657861") + b"mple\x03com\x00\x00\x01\x00\x01"
    checks.append(("outbound UDP DNS to 8.8.8.8 gets no answer", not try_udp("8.8.8.8", 53, dns), ""))
    checks.append(("name resolution of example.com fails", not try_dns("example.com"), ""))
    port = int(cfg.get("canary_port", 0))
    if port:
        for host in CANARY_HOSTS:
            try_tcp(host, port, 1)
            try_udp(host, port, b"canary", 1)
        checks.append(("canary probes sent (host must see none)", True, ""))
    ok = all(c[1] for c in checks)
    emit("isolation", ok=ok, checks=[{"name": n, "ok": o, "detail": d} for n, o, d in checks])
    return ok


def unit_state(unit):
    props = ["ActiveState", "SubState", "Result", "ExecMainStatus", "ExecMainStartTimestamp", "InvocationID", "NRestarts", "UnitFileState"]
    _, out, _ = sh(["systemctl", "show", unit, "-p", ",".join(props)])
    state = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        state[key] = value
    return state


def journal(unit, lines=80):
    text = sh(["journalctl", "-u", unit, "-b", "--no-pager", "-o", "cat", "-n", str(lines)])[1]
    return text.strip() and text or sh(["systemctl", "status", unit, "--no-pager", "-l"])[1]  # no journal text: systemd's own status lines


def wait_running(limit):
    end = time.time() + limit
    while time.time() < end:
        _, out, _ = sh(["systemctl", "is-system-running"], timeout=10)
        if out.strip() in ("running", "degraded"):
            return out.strip()
        time.sleep(3)
    return "timeout"


def psql(db, sql, user="postgres"):
    code, out, err = sh(["psql", "-At", "-d", db, "-c", sql], timeout=120, user=user)
    return out.strip() if code == 0 else None


def database_checks(cfg):
    db = cfg["database"]
    end = time.time() + cfg["verify"].get("recovery_wait_seconds", 300)
    in_recovery = None
    while time.time() < end:
        value = psql(db["name"], "select pg_is_in_recovery()")
        if value in ("t", "f"):
            in_recovery = value == "t"
            if not in_recovery:
                break
        time.sleep(3)
    result = {"in_recovery": in_recovery, "counts": {}, "newest": None}
    if in_recovery is False:
        for table in db.get("count_tables", []):
            value = psql(db["name"], 'select count(*) from "%s"' % table)
            result["counts"][table] = int(value) if value is not None and value.isdigit() else None
        if db.get("time_table"):
            result["newest"] = psql(db["name"], "select to_char(max(create_date),'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') from \"%s\"" % db["time_table"])
        result["timeline"] = psql(db["name"], "select timeline_id from pg_control_checkpoint()")
    return result


def http_checks(cfg):
    import urllib.request
    import urllib.error

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    out = []
    for check in cfg["verify"].get("http_checks", []):
        status, body = None, ""
        for _ in range(int(cfg["verify"].get("http_retries", 40))):
            try:
                req = urllib.request.Request(check["url"], headers={"Host": check.get("host", "localhost")})
                opener = urllib.request.build_opener(NoRedirect)
                try:
                    resp = opener.open(req, timeout=20)
                except urllib.error.HTTPError as exc:
                    resp = exc
                status, body = resp.status, resp.read(4096).decode("utf-8", "replace")
                break
            except OSError:
                time.sleep(3)
        out.append({"url": check["url"], "status": status, "contains": check.get("contains"), "found": (check.get("contains") in body) if check.get("contains") else None})
    return out


def odoo_login(cfg):
    login = cfg["verify"].get("login")
    if not login or login.get("kind") != "odoo":
        return {"skipped": True}
    import secrets as sec
    import urllib.request
    password = sec.token_urlsafe(18)
    script = "u = env['res.users'].browse(%d)\nu.write({'password': %r})\nenv.cr.commit()\nprint('LOGIN', u.login)\n" % (login["user_id"], password)
    argv = ["runuser", "-u", "odoo", "--", "odoo", "shell", "-c", "/etc/odoo/odoo.conf", "-d", login["db"], "--no-http"]
    proc = subprocess.run(argv, input=script, capture_output=True, text=True, timeout=600, check=False)
    user = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.startswith("LOGIN ")]
    if not user:
        return {"ok": False, "detail": "odoo shell failed: " + proc.stderr[-200:]}
    body = json.dumps({"jsonrpc": "2.0", "params": {"db": login["db"], "login": user[0], "password": password}}).encode()
    req = urllib.request.Request("http://127.0.0.1:8069/web/session/authenticate", data=body, headers={"Content-Type": "application/json"})
    try:
        reply = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except OSError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": bool(reply.get("result", {}).get("uid")), "detail": "authenticate returned uid %s" % reply.get("result", {}).get("uid")}


def collect(cfg):
    verify = cfg["verify"]
    system = wait_running(int(verify.get("journal_wait_seconds", 240)))
    for unit in cfg.get("external_fetch_services", []):
        sh(["systemctl", "start", "--no-block", unit])
    time.sleep(int(verify.get("external_settle_seconds", 20)))
    _, failed_out, _ = sh(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain", "--no-pager"])
    failed = [ln.split()[0] for ln in failed_out.splitlines() if ln.strip()]
    units = {}
    interesting = set(failed) | set(cfg.get("external_fetch_units", [])) | set(verify.get("must_be_active", [])) | set(cfg.get("expected_units", []))
    for unit in sorted(interesting):
        state = unit_state(unit)
        units[unit] = {"state": state, "journal": journal(unit) if (state.get("ActiveState") != "active" or unit in cfg.get("external_fetch_units", []) or unit in failed) else ""}
    extra = {}
    for path in cfg.get("log_files", []):
        try:
            with open(path, errors="replace") as handle:
                extra[path] = handle.read()[-3000:]
        except OSError:
            continue
    emit("units", system=system, failed=failed, units=units, logs=extra)
    emit("database", **database_checks(cfg))
    emit("http", checks=http_checks(cfg))
    emit("login", **odoo_login(cfg))
    emit("isolation_after", ok=not interfaces() and not routes() and not try_tcp("1.1.1.1", 443))


def run():
    with open(CONFIG) as handle:
        cfg = json.load(handle)
    mount_modules()
    emit("boot", kernel=os.uname().release, services_started=False)
    if not isolation(cfg):
        emit("done", services_started=False, reason="isolation could not be proven")
        subprocess.run(["systemctl", "poweroff", "--force"], check=False)
        return
    emit("starting", services_started=True)
    try:
        subprocess.run(["systemctl", "start", "multi-user.target"], check=False, timeout=int(cfg["verify"].get("start_timeout_seconds", 900)))
    except subprocess.TimeoutExpired:
        emit("start_timeout", seconds=int(cfg["verify"].get("start_timeout_seconds", 900)))
    try:
        collect(cfg)
    finally:
        emit("done", services_started=True)
        subprocess.run(["systemctl", "poweroff", "--force"], check=False)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    run() if mode == "run" else sys.exit("unknown mode")
