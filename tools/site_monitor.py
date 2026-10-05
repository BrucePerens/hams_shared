#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. License: AGPL-3.0.
"""Site monitor: one pass of local health checks, a page when something stays down, a dead-man ping when all is well.

Runs from `hams-site-monitor.timer` every minute on the production host. It is deliberately the smallest thing
that works and it does not depend on Odoo, so it still pages when Odoo is the thing that is down:

  * Checks: Odoo's `/web/health`, PostgreSQL (`pg_isready`), the Cloudflare tunnel's own readiness endpoint,
    the public site through the tunnel, every unit the MANIFEST says is a long-running production daemon (the
    signers included), and disk space.
  * Paging path, with no Odoo and no mail server of ours in it: an HTTPS webhook (`PAGER_WEBHOOK_URL`, the same
    variable the pager_duty monitor reads, so the same Discord/Slack/ntfy address serves both) and, if set, SMTP
    (`PAGER_FALLBACK_EMAIL`, `SMTP_HOST`, ... the same variables as pager_duty's fallback). A check must fail on
    `FAILS_BEFORE_PAGE` consecutive runs before it pages; a recovery is announced once; a check that stays down is
    repeated every `REPAGE_SECONDS`.
  * Dead-man switch: when every check passes, the monitor GETs `HAMS_MONITOR_HEARTBEAT_URL`. An off-host service
    that expects that ping every few minutes pages when it stops, which covers what no software on the host can
    report: the host, its network or this timer being dead. That service is chosen and configured by the operator
    (docs/runbooks/SITE_MONITORING.md in hams_com); nothing here signs up for anything.

Configuration is read from the environment (the unit loads `/opt/hams/etc/site_monitor.env`, root only, optional).
With no paging channel configured the monitor says so loudly on every run and exits 2, so the unit shows as failed
instead of looking like protection that is not there.

Exit status: 0 all checks pass; 1 at least one check fails; 2 no paging channel configured; 3 a page could not be
delivered on any channel.
"""
import json
import logging
import os
import shutil
import smtplib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger("site_monitor")

FAILS_BEFORE_PAGE = 2
REPAGE_SECONDS = 3600
DISK_FAIL_PERCENT_USED = 90
HTTP_TIMEOUT = 10
CORE_UNITS = ("odoo.service", "postgresql.service", "redis-server.service", "rabbitmq-server.service", "pdns.service")
SYSTEMD_DIR = "/etc/systemd/system"
DEFAULT_ODOO_URL = "http://127.0.0.1:8069/web/health"
DEFAULT_TUNNEL_READY_URL = "http://127.0.0.1:20241/ready"
DEFAULT_PUBLIC_URL = "https://hams.com/"
DEFAULT_STATE_DIR = "/var/lib/hams-monitor"
USER_AGENT = "hams-site-monitor/1.0 (self-check; +https://hams.com)"

# A check is (ok, detail). A check function never raises: a crash inside a check is a failed check.


def _http_get(url, timeout=HTTP_TIMEOUT, opener=urllib.request.urlopen):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request, timeout=timeout) as response:
        return response.status, response.read(65536).decode("utf-8", "replace")


def check_odoo(url, http_get=_http_get):
    try:
        status, body = http_get(url)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, f"{url}: {exc}"
    if status != 200:
        return False, f"{url}: HTTP {status}"
    if "pass" not in body:
        return False, f"{url}: unexpected body {body[:80]!r}"
    return True, "health endpoint passes"


def check_public_site(url, http_get=_http_get):
    try:
        status, _body = http_get(url, timeout=15)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, f"{url}: {exc}"
    return (status == 200), f"{url}: HTTP {status}"


def check_tunnel(url, http_get=_http_get, minimum=1):
    """The cloudflared metrics endpoint answers `{"status":200,"readyConnections":N,...}`; N is the edge connections."""
    try:
        status, body = http_get(url)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, f"{url}: {exc}"
    try:
        ready = int(json.loads(body).get("readyConnections", 0))
    except (ValueError, TypeError, AttributeError):
        return False, f"{url}: unreadable answer {body[:80]!r}"
    if status != 200 or ready < minimum:
        return False, f"{ready} edge connection(s) ready, HTTP {status}"
    return True, f"{ready} edge connection(s) ready"


def _find_pg_isready():
    found = shutil.which("pg_isready")
    if found:
        return found
    import glob
    candidates = sorted(glob.glob("/usr/lib/postgresql/*/bin/pg_isready"), reverse=True)
    return candidates[0] if candidates else None


def check_postgresql(run=subprocess.run, find=_find_pg_isready, connect=socket.create_connection):
    binary = find()
    if binary is None:
        try:
            connect(("127.0.0.1", 5432), timeout=5).close()
        except OSError as exc:
            return False, f"no pg_isready and TCP 127.0.0.1:5432 refused: {exc}"
        return True, "TCP 127.0.0.1:5432 accepts (pg_isready not installed)"
    try:
        result = run([binary, "-q", "-t", "5"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"pg_isready: {exc}"
    return (result.returncode == 0), f"pg_isready exit {result.returncode}"


def monitored_units(manifest_units=None, exists=os.path.exists):
    """The units that must be active: the core services plus every long-running production daemon the MANIFEST
    enables at boot that is linked on this host. A unit not linked here (a test host, a unit not yet deployed) is
    skipped, so the monitor never pages about something that was never installed."""
    if manifest_units is None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import infrastructure
            manifest_units = infrastructure.boot_service_unit_names()
        except Exception as exc:  # audit-ignore-catch-all
            logger.warning("Could not read the MANIFEST daemon list (%s); checking the core services only", exc)
            manifest_units = ()
    units = list(CORE_UNITS)
    units += sorted(u for u in manifest_units if exists(os.path.join(SYSTEMD_DIR, u)))
    return units


def check_units(units, run=subprocess.run):
    """`systemctl is-active` prints one state per unit; anything but "active" is a failure."""
    if not units:
        return True, "no units to check"
    try:
        result = run(["systemctl", "is-active", *units], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"systemctl: {exc}"
    states = result.stdout.split()
    if len(states) != len(units):
        return False, f"systemctl answered {len(states)} states for {len(units)} units"
    bad = [f"{unit}={state}" for unit, state in zip(units, states) if state != "active"]
    if bad:
        return False, "not active: " + ", ".join(bad)
    return True, f"{len(units)} units active"


def check_disk(paths, limit=DISK_FAIL_PERCENT_USED, usage=shutil.disk_usage):
    bad, seen = [], []
    for path in paths:
        try:
            total, used, _free = usage(path)
        except OSError as exc:
            bad.append(f"{path}: {exc}")
            continue
        percent = 100.0 * used / total if total else 100.0
        seen.append(f"{path} {percent:.0f}%")
        if percent >= limit:
            bad.append(f"{path} {percent:.0f}% used (limit {limit}%)")
    return (not bad), ("; ".join(bad) if bad else ", ".join(seen))


def run_checks(env, deps=None):
    """Return {check name: (ok, detail)}; never raises."""
    deps = deps or {}
    disk_paths = [p for p in env.get("HAMS_MONITOR_DISK_PATHS", "/").split(":") if p]
    plan = {
        "odoo": lambda: check_odoo(env.get("HAMS_MONITOR_ODOO_URL", DEFAULT_ODOO_URL), **deps.get("http", {})),
        "postgresql": lambda: check_postgresql(**deps.get("pg", {})),
        "tunnel": lambda: check_tunnel(env.get("HAMS_MONITOR_TUNNEL_READY_URL", DEFAULT_TUNNEL_READY_URL),
                                       **deps.get("http", {})),
        "units": lambda: check_units(monitored_units(**deps.get("units_list", {})), **deps.get("units", {})),
        "disk": lambda: check_disk(disk_paths, **deps.get("disk", {})),
    }
    public = env.get("HAMS_MONITOR_PUBLIC_URL", DEFAULT_PUBLIC_URL)
    if public:
        plan["public site"] = lambda: check_public_site(public, **deps.get("http", {}))
    results = {}
    for name, fn in plan.items():
        try:
            results[name] = fn()
        except Exception as exc:  # audit-ignore-catch-all
            results[name] = (False, f"the check itself crashed: {exc!r}")
    return results


# ---- state and the paging decision (pure) -----------------------------------------------------------------------

def decide(state, results, now, fails_before_page=FAILS_BEFORE_PAGE, repage_seconds=REPAGE_SECONDS):
    """Return (new_state, messages). Messages are (kind, check, detail) with kind page/repage/recovered."""
    new, messages = {}, []
    for name, (ok, detail) in results.items():
        old = state.get(name, {})
        entry = {"fails": 0, "paged_at": 0, "down_since": 0}
        if ok:
            if old.get("paged_at"):
                messages.append(("recovered", name, detail))
        else:
            entry["fails"] = int(old.get("fails", 0)) + 1
            entry["down_since"] = old.get("down_since") or now
            entry["paged_at"] = old.get("paged_at", 0)
            if entry["fails"] >= fails_before_page:
                if not entry["paged_at"]:
                    messages.append(("page", name, detail))
                elif now - entry["paged_at"] >= repage_seconds:
                    messages.append(("repage", name, detail))
        new[name] = entry
    return new, messages


def commit_paged(state, delivered, now):
    """After delivery: stamp paged_at for delivered pages and clear a recovered check. An undelivered page
    leaves the check unpaged so the next run tries again."""
    for kind, name, _detail in delivered:
        if kind in ("page", "repage"):
            state.setdefault(name, {})["paged_at"] = now
    return state


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


# ---- delivery ---------------------------------------------------------------------------------------------------

def format_message(kind, check, detail, host):
    label = {"page": "DOWN", "repage": "STILL DOWN", "recovered": "RECOVERED"}[kind]
    title = f"[hams.com {label}] {check}"
    return title, f"{title} on {host}: {detail}"


def send_webhook(url, title, text, kind, urlopen=urllib.request.urlopen, style="json"):
    """POST to the operator's webhook. style "json" sends {"content","text"} (Discord and Slack read one each);
    style "text" sends the plain text with Title/Priority headers (ntfy)."""
    headers = {"User-Agent": USER_AGENT}
    if style == "text":
        body = text.encode("utf-8")
        headers.update({"Title": title, "Priority": "default" if kind == "recovered" else "urgent",
                        "Tags": "white_check_mark" if kind == "recovered" else "rotating_light"})
    else:
        body = json.dumps({"content": text, "text": text}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise OSError(f"webhook answered HTTP {response.status}")


def send_email(env, title, text, smtp=smtplib.SMTP):
    port = int(env.get("SMTP_PORT") or 587)
    message = EmailMessage()
    message.set_content(text)
    message["Subject"] = title
    message["From"] = env.get("SMTP_FROM") or "site-monitor@hams.com"
    message["To"] = env["PAGER_FALLBACK_EMAIL"]
    with smtp(env["SMTP_HOST"], port, timeout=20) as server:
        if port in (587, 465):
            server.starttls()
        if env.get("SMTP_USER") and env.get("SMTP_PASS"):
            server.login(env["SMTP_USER"], env["SMTP_PASS"])
        server.send_message(message)


def channels_configured(env):
    return {"webhook": bool(env.get("PAGER_WEBHOOK_URL")),
            "email": bool(env.get("PAGER_FALLBACK_EMAIL") and env.get("SMTP_HOST"))}


def deliver(messages, env, host, webhook=send_webhook, email=send_email):
    """Send each message on every configured channel; it counts as delivered when at least one accepts it."""
    configured = channels_configured(env)
    delivered = []
    for kind, check, detail in messages:
        title, text = format_message(kind, check, detail, host)
        sent = False
        if configured["webhook"]:
            try:
                webhook(env["PAGER_WEBHOOK_URL"], title, text, kind, style=env.get("HAMS_MONITOR_WEBHOOK_STYLE", "json"))
                sent = True
            except (OSError, urllib.error.URLError, ValueError) as exc:
                logger.error("Webhook page failed: %s", exc)
        if configured["email"]:
            try:
                email(env, title, text)
                sent = True
            except (OSError, smtplib.SMTPException) as exc:
                logger.error("Email page failed: %s", exc)
        if sent:
            delivered.append((kind, check, detail))
    return delivered


def ping_heartbeat(url, urlopen=urllib.request.urlopen):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=15):
            pass
        return True
    except (OSError, urllib.error.URLError, ValueError) as exc:
        logger.error("Dead-man heartbeat ping failed: %s", exc)
        return False


def main(env=None, now=None, deps=None, host=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    host = host or socket.gethostname()
    deps = deps or {}
    state_path = os.path.join(env.get("STATE_DIRECTORY", DEFAULT_STATE_DIR).split(":")[0], "state.json")

    results = run_checks(env, deps)
    for name, (ok, detail) in results.items():
        (logger.info if ok else logger.error)("%s: %s (%s)", name, "ok" if ok else "FAIL", detail)

    state, messages = decide(load_state(state_path), results, now)
    configured = channels_configured(env)
    status = 0 if all(ok for ok, _d in results.values()) else 1
    if not any(configured.values()):
        logger.critical("No paging channel is configured (PAGER_WEBHOOK_URL, or PAGER_FALLBACK_EMAIL with SMTP_HOST): "
                        "this monitor cannot page anyone. See docs/runbooks/SITE_MONITORING.md in hams_com.")
        status = 2
    elif messages:
        delivered = deliver(messages, env, host, **deps.get("delivery", {}))
        state = commit_paged(state, delivered, now)
        if len(delivered) != len(messages):
            logger.critical("%d page(s) could not be delivered on any channel", len(messages) - len(delivered))
            status = 3
    try:
        save_state(state_path, state)
    except OSError as exc:
        logger.error("Could not save state %s: %s", state_path, exc)

    heartbeat = env.get("HAMS_MONITOR_HEARTBEAT_URL")
    if heartbeat and status == 0:
        ping_heartbeat(heartbeat, **deps.get("heartbeat", {}))
    elif not heartbeat:
        logger.warning("HAMS_MONITOR_HEARTBEAT_URL is not set: nothing outside this host notices if the host dies")
    return status


if __name__ == "__main__":
    sys.exit(main())
