#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Backup self-test: prove on the machine itself that the newest backup can really be restored, and tell the monitor when it cannot.

    backup_selftest.py [--config FILE] run [--only STAGE ...] [--json]   run the stages; one PASS/FAIL/SKIP line each; --json: a JSON report
    backup_selftest.py [--config FILE] report                    print the last run's JSON report (schema 1)
    backup_selftest.py [--config FILE] record-fingerprint --confirm-record   (a person, once) record sha256 of each key
    backup_selftest.py [--config FILE] check [--max-age-days 9]  for the pager's "synthetic" check: fails when no good run is recent
    backup_selftest.py [--config FILE] stages                    list the stages and their exit codes

Stages, in order (a stage is named in its line, in the alert and in the exit code; docs/runbooks/BACKUP_SELFTEST.md says what each proves):
  preflight          refuses (exit 75, no alert) when the load is high or the scratch filesystem has under 2x the database size free
  key_fingerprint    sha256 of each backup key's material against the recorded fingerprint file: prints only "fingerprint OK/MISMATCH"
  pgbackrest_check   `pgbackrest check` (WAL archiving works end to end)
  backup_age         newest full backup under 36 h old (pgbackrest info); an empty repository fails
  wal_age            newest archived WAL object in the repository under 15 min old, and the one pg_stat_archiver reports is in it
  wal_chain          no missing WAL segment between the newest backup's first segment and the newest archived segment
  pgbackrest_verify  `pgbackrest verify`: every file of the repository against its checksum, and the WAL chain
  restore .. cleanup OPTIONAL, not in the hams1 default (the restoration system's VM verification is that restore proof; the storage
                     profile keeps it): restore the newest backup (and WAL) into a scratch directory, start a throwaway PostgreSQL on a free port bound
                     to a unix socket in it (nice 19, ionice idle, systemd-run MemoryMax/CPUQuota), sanity SQL (row counts against the
                     live database, newest replayed transaction within the WAL lag), read every relation (pg_checksums, or a dump to
                     /dev/null), then remove the scratch directory
  kopia_restore      the existing b2_backup.py restore-test (kopia files repository + database dump); an empty repository fails
  storage_restore    the dev server's /storage backup: devbox_tools/hams1_storage_restore_test.py (kopia snapshots of the second backup)
The same file tests the dev server's /storage backup with a different config (profile "storage"; restore source "basebackup_dir").

Exit code: 0 all passed; 75 refused (preflight); otherwise the code of the first failed stage (see `stages`); 2 for a configuration error.
No key, passphrase or encryption setting is ever created, changed, printed or logged here: keys are read at run time from the files the config
names, only to hash them (and to scrub them from any tool output). Nothing here writes to a backup repository.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hmac
import io
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import b2_backup

DEFAULT_CONFIG = "/opt/hams/etc/backup_selftest/config.json"
EXIT_CONFIG = 2
EXIT_REFUSED = 75
# Order is the order a full run goes in; the code is the process exit status when that stage is the first to fail.
STAGE_CODES = {
    "preflight": 10, "key_fingerprint": 11, "pgbackrest_check": 12, "backup_age": 13, "wal_age": 14, "wal_chain": 15,
    "pgbackrest_verify": 16, "restore": 17, "start": 18, "sanity_sql": 19, "checksum_read": 20, "cleanup": 21, "kopia_restore": 22,
    "storage_restore": 23,
}
RESTORE_GROUP = ("restore", "start", "sanity_sql", "checksum_read", "cleanup")
DEFAULT_STAGES = {
    "hams1": ["preflight", "key_fingerprint", "pgbackrest_check", "backup_age", "wal_age", "wal_chain", "pgbackrest_verify",
              "kopia_restore"],
    "storage": ["preflight", "key_fingerprint", "storage_restore", "restore"],
}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?\Z")
WAL_NAME_RE = re.compile(r"^[0-9A-F]{24}\Z")
STAMP_RE = re.compile(r"^\d{8}T\d{6}Z\Z")
CONTROL_SETTINGS = {  # pg_controldata label -> parameter that a hot-standby style recovery requires to be >= the original's
    "max_connections setting": "max_connections", "max_worker_processes setting": "max_worker_processes",
    "max_wal_senders setting": "max_wal_senders", "max_prepared_xacts setting": "max_prepared_transactions",
    "max_locks_per_xact setting": "max_locks_per_transaction",
}
MAX_DETAIL = 200
SOCKET_PATH_LIMIT = 90


# [@ANCHOR: backup_selftest:backup_selftest_configuration_error_class]
class ConfigurationError(ValueError):
    pass


# [@ANCHOR: backup_selftest:backup_selftest_stage_failed_class]
class StageFailed(RuntimeError):
    """A stage found a problem; the message is the (secret-free) detail on its FAIL line."""


# [@ANCHOR: backup_selftest:backup_selftest_refused_class]
class Refused(RuntimeError):
    """The test must not run now (load, disk): not a backup problem, so no alert; the freshness check catches a test that never runs."""


# ---------------------------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """The JSON config with defaults filled in; raises ConfigurationError for anything unusable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read config {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigurationError("config must be a JSON object")
    cfg = dict(raw)
    cfg.setdefault("profile", "hams1")
    if cfg["profile"] not in DEFAULT_STAGES:
        raise ConfigurationError("profile must be one of " + ", ".join(sorted(DEFAULT_STAGES)))
    cfg.setdefault("state_dir", "/var/lib/hams-backup-selftest")
    cfg.setdefault("scratch_root", os.path.join(cfg["state_dir"], "scratch"))
    cfg.setdefault("fingerprint_file", "/opt/hams/etc/backup_fingerprints.json")
    cfg.setdefault("keys", [])
    cfg.setdefault("forbidden_paths", ["/var/lib/postgresql", "/var/lib/pgbackrest", "/var/lib/odoo", "/opt/hams/etc"])
    cfg.setdefault("run_as", [])
    cfg.setdefault("scratch_owner", None)
    cfg["limits"] = {"systemd_run": True, "memory_max": "2G", "cpu_quota": "100%", "max_load_per_cpu": 0.5,
                     "min_free_ratio": 2.0, **(raw.get("limits") or {})}
    cfg["pgbackrest"] = {"bin": "pgbackrest", "stanza": "hams_prod", "options": [], "verify": True, "full_max_age_hours": 36,
                         "wal_max_age_minutes": 15, "backup_types": ["full"], "repo": None, **(raw.get("pgbackrest") or {})}
    cfg["live"] = {"psql": ["psql"], "run_as": cfg["run_as"], **(raw.get("live") or {})}
    cfg["restore"] = {"source": "pgbackrest", "pg_user": "postgres", "recovery": "latest", "tables": [], "databases": [],
                      "max_lag_seconds": 1200, "recovery_timeout": 3 * 3600, "start_timeout": 600, "pg_bin": None,
                      "shared_buffers": "256MB", **(raw.get("restore") or {})}
    cfg["basebackup_dir"] = dict(raw.get("basebackup_dir") or {})
    cfg["kopia"] = dict(raw.get("kopia") or {})
    cfg["storage_restore"] = dict(raw.get("storage_restore") or {})
    cfg.setdefault("stages", DEFAULT_STAGES[cfg["profile"]])
    unknown = [s for s in cfg["stages"] if s not in STAGE_CODES]
    if unknown:
        raise ConfigurationError("unknown stage(s): " + ", ".join(unknown))
    for key in cfg["keys"]:
        if not NAME_RE.match(str(key.get("name", ""))) or key.get("kind") not in ("env_file", "ini_file", "raw_file") or not key.get("path"):
            raise ConfigurationError(f"a key entry needs name, kind (env_file, ini_file or raw_file) and path: {key.get('name')!r}")
        if key["kind"] != "raw_file" and not key.get("var"):
            raise ConfigurationError(f"key {key['name']}: kinds env_file and ini_file need var")
    if "key_fingerprint" in cfg["stages"] and not cfg["keys"]:
        raise ConfigurationError("stage key_fingerprint is configured but keys is empty")
    if "restore" in cfg["stages"]:
        rest = cfg["restore"]
        if rest["source"] not in ("pgbackrest", "basebackup_dir"):
            raise ConfigurationError("restore.source must be pgbackrest or basebackup_dir")
        if not rest["tables"]:
            raise ConfigurationError("restore.tables must list at least one table to count (an empty restore must not pass)")
        for item in rest["tables"]:
            if not NAME_RE.match(str(item.get("db", ""))) or not TABLE_RE.match(str(item.get("table", ""))):
                raise ConfigurationError(f"restore.tables entry needs a plain db and table name: {item!r}")
        if rest["source"] == "basebackup_dir" and not (cfg["basebackup_dir"].get("db_dir") and cfg["basebackup_dir"].get("wal_dir")):
            raise ConfigurationError("restore.source basebackup_dir needs basebackup_dir.db_dir and basebackup_dir.wal_dir")
    return cfg


# ---------------------------------------------------------------------------------------------------------------------------
# Keys and fingerprints
# ---------------------------------------------------------------------------------------------------------------------------

def read_key_value(spec: dict) -> str:
    """The key's text, read at run time from the file the config names. Raises StageFailed (naming the key, never its value)."""
    name, kind, path = spec["name"], spec["kind"], spec["path"]
    try:
        with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        raise StageFailed(f"key {name}: its file is missing or unreadable") from None
    if kind == "raw_file":
        value = text.strip()
    else:
        value = ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            left, sep, right = line.partition("=")
            if sep and left.strip() == spec["var"]:
                value = right.strip().strip('"').strip("'")
    if not value:
        raise StageFailed(f"key {name}: no value found in its file")
    return value


def key_fingerprints(cfg: dict) -> tuple[dict, list]:
    """({name: sha256 hex}, [the secret texts, for scrubbing tool output]). The texts never leave this process."""
    prints, secret_texts = {}, []
    for spec in cfg["keys"]:
        value = read_key_value(spec)
        prints[spec["name"]] = b2_backup.fingerprint(value)  # the same hash b2_backup.py records for its own password
        secret_texts.append(value)
    return prints, secret_texts


def stage_key_fingerprint(ctx) -> str:
    try:
        with open(ctx.cfg["fingerprint_file"], "r", encoding="utf-8") as handle:  # audit-ignore-path
            recorded = json.load(handle).get("fingerprints") or {}
    except (OSError, ValueError, AttributeError):
        raise StageFailed("no usable fingerprint record: a person must run record-fingerprint once") from None
    if not recorded:
        raise StageFailed("the fingerprint record is empty")
    current, _ = key_fingerprints(ctx.cfg)
    bad = [name for name in current if not hmac.compare_digest(current[name], str(recorded.get(name, "")))]
    if bad:
        raise StageFailed("fingerprint MISMATCH: " + ", ".join(bad) + " (key changed, or not recorded)")
    return f"fingerprint OK for {len(current)} key(s): " + ", ".join(sorted(current))


def cmd_record_fingerprint(cfg: dict, out) -> int:
    """Written once by a person; never overwrites. Prints each name and its sha256 (a hash, not the key) for the custody sheets."""
    path = cfg["fingerprint_file"]
    prints, _ = key_fingerprints(cfg)
    record = {"version": 1, "recorded": datetime.datetime.now(datetime.timezone.utc).isoformat(), "fingerprints": prints}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigurationError(f"{path} exists: refusing to overwrite a recorded fingerprint (a person removes it deliberately)") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=1, sort_keys=True)
        handle.write("\n")
    for name in sorted(prints):
        print(f"{name}: sha256 {prints[name]}", file=out)
    print(f"recorded {len(prints)} fingerprint(s) in {path}; copy each onto the custody sheet beside its key", file=out)
    return 0


# ---------------------------------------------------------------------------------------------------------------------------
# Context, process running, reporting
# ---------------------------------------------------------------------------------------------------------------------------

# [@ANCHOR: backup_selftest:backup_selftest_context_class]
class Context:
    """Shared state of one run: the config, scrubbing of secrets from tool output, and cached pgbackrest info."""

    def __init__(self, cfg: dict, secret_texts: list | None = None, now=time.time):
        self.cfg = cfg
        self.now = now
        self.secret_texts = [s for s in (secret_texts or []) if len(s) >= 6]
        self._info = None
        self.scratch_dir = None

    def scrub(self, text: str) -> str:
        for secret in self.secret_texts:
            text = text.replace(secret, "<redacted>")
        return text

    def limits_prefix(self) -> list:
        lim = self.cfg["limits"]
        prefix = ["nice", "-n", "19", "ionice", "-c", "3"]
        if lim.get("systemd_run"):
            prefix = ["systemd-run", "--scope", "--quiet", "-p", f"MemoryMax={lim['memory_max']}", "-p", f"CPUQuota={lim['cpu_quota']}"] + prefix
        return prefix

    def wrap(self, argv: list, limited: bool = False) -> list:
        """argv as the run-as user (when configured), under the limits for the heavy work."""
        return (self.limits_prefix() if limited else []) + list(self.cfg["run_as"]) + list(argv)

    def run(self, argv: list, timeout: float, ok_codes=(0,), label: str = "") -> subprocess.CompletedProcess:
        label = label or os.path.basename(argv[0])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "HOME": os.environ.get("HOME", "/")})
        except FileNotFoundError:
            raise StageFailed(f"{label} not found") from None
        except subprocess.TimeoutExpired:
            raise StageFailed(f"{label} timed out after {int(timeout)} s") from None
        proc.stdout, proc.stderr = self.scrub(proc.stdout or ""), self.scrub(proc.stderr or "")
        if proc.returncode not in ok_codes:
            raise StageFailed(f"{label} exit {proc.returncode}: {last_line(proc.stderr or proc.stdout)}")
        return proc

    # -- pgbackrest -------------------------------------------------------------------------------------------------------
    def pgbackrest_argv(self, *args: str) -> list:
        pg = self.cfg["pgbackrest"]
        argv = [pg["bin"]] + list(pg["options"]) + [f"--stanza={pg['stanza']}"]
        if pg.get("repo"):
            argv.append(f"--repo={pg['repo']}")
        return argv + list(args)

    def info(self) -> dict:
        """The stanza's entry of `pgbackrest info --output=json`, restricted to the configured repo; cached."""
        if self._info is None:
            argv = self.wrap(self.pgbackrest_argv("--output=json", "info"))
            proc = self.run(argv, 300, label="pgbackrest info")
            try:
                stanzas = json.loads(proc.stdout)
            except ValueError:
                raise StageFailed("pgbackrest info did not return JSON") from None
            wanted = [s for s in stanzas if s.get("name") == self.cfg["pgbackrest"]["stanza"]]
            if not wanted:
                raise StageFailed("pgbackrest info lists no such stanza")
            self._info = wanted[0]
        return self._info

    def backups(self, types=None) -> list:
        repo = self.cfg["pgbackrest"].get("repo")
        found = []
        for item in self.info().get("backup", []):
            if repo and item.get("database", {}).get("repo-key") not in (None, repo):
                continue
            if types and item.get("type") not in types:
                continue
            found.append(item)
        return sorted(found, key=lambda b: b["timestamp"]["stop"])


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "no output")[:MAX_DETAIL]


# [@ANCHOR: backup_selftest:backup_selftest_report_class]
class Report:
    """The result lines of a run; also what is written to the status file."""

    def __init__(self, out):
        self.out = out
        self.results: list[dict] = []

    def add(self, stage: str, status: str, detail: str = "") -> None:
        detail = detail[:MAX_DETAIL]
        self.results.append({"stage": stage, "status": status, "detail": detail})
        print(f"{status} {stage}: {detail}".rstrip(": "), file=self.out, flush=True)

    @property
    def first_failure(self):
        return next((r["stage"] for r in self.results if r["status"] == "FAIL"), None)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r["status"] == "PASS" for r in self.results)


# ---------------------------------------------------------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------------------------------------------------------

def inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def free_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def database_size_bytes(ctx: Context) -> int:
    """Size of the restored database: pgbackrest's record of the newest backup, or the base backup's manifest."""
    if ctx.cfg["restore"]["source"] == "pgbackrest":
        newest = ctx.backups()
        if not newest:
            raise StageFailed("the repository holds no backup")
        return int(newest[-1].get("info", {}).get("size", 0))
    return basebackup_manifest_size(newest_basebackup(ctx.cfg)[1])


def stage_preflight(ctx: Context) -> str:
    lim = ctx.cfg["limits"]
    load = os.getloadavg()[0]
    ceiling = lim["max_load_per_cpu"] * (os.cpu_count() or 1)
    if load > ceiling:
        raise Refused(f"load average {load:.1f} is above {ceiling:.1f} ({lim['max_load_per_cpu']} per CPU); try again later")
    root = ctx.cfg["scratch_root"]
    for forbidden in ctx.cfg["forbidden_paths"]:
        if inside(root, forbidden) or inside(forbidden, root):
            raise StageFailed(f"scratch root {root} overlaps {forbidden}")
    if "restore" not in ctx.cfg["stages"]:
        return f"load {load:.1f} under {ceiling:.1f}"
    os.makedirs(root, mode=0o700, exist_ok=True)
    chown_to_owner(ctx, root)
    size = database_size_bytes(ctx)
    free = free_bytes(root)
    if size <= 0:
        raise StageFailed("the database size of the newest backup is unknown")
    need = int(size * lim["min_free_ratio"])
    if free < need:
        raise Refused(f"{free // 2**20} MiB free under {root}, need {need // 2**20} MiB ({lim['min_free_ratio']}x the database)")
    return f"load {load:.1f} under {ceiling:.1f}; {free // 2**30} GiB free for a {size // 2**20} MiB database"


def stage_pgbackrest_check(ctx: Context) -> str:
    ctx.run(ctx.wrap(ctx.pgbackrest_argv("check")), 900, label="pgbackrest check")
    return "WAL archiving works (pgbackrest check)"


def stage_backup_age(ctx: Context) -> str:
    pg = ctx.cfg["pgbackrest"]
    newest = ctx.backups(pg["backup_types"])
    if not newest:
        raise StageFailed("no " + "/".join(pg["backup_types"]) + " backup in the repository")
    age_h = (ctx.now() - newest[-1]["timestamp"]["stop"]) / 3600
    if age_h > pg["full_max_age_hours"]:
        raise StageFailed(f"newest {newest[-1]['type']} backup {newest[-1]['label']} is {age_h:.1f} h old (limit {pg['full_max_age_hours']} h)")
    return f"newest {newest[-1]['type']} backup {newest[-1]['label']} is {age_h:.1f} h old"


def psql_scalar(ctx: Context, base: list, sql: str, run_as: list, db: str = "postgres") -> str:
    argv = list(run_as) + list(base) + ["-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", sql]
    return ctx.run(argv, 300, label="psql (live)").stdout.strip()


WAL_FILE_RE = re.compile(r"(?:^|/)([0-9A-F]{24})-[0-9a-f]{40}\.[a-z0-9]+\Z")


def next_segment(name: str) -> str:
    """The WAL segment that follows `name` on the same timeline (16 MiB segments, 256 per log file)."""
    tli, log, seg = int(name[:8], 16), int(name[8:16], 16), int(name[16:], 16) + 1
    if seg == 256:
        log, seg = log + 1, 0
    return "%08X%08X%08X" % (tli, log, seg)


def wal_files(ctx: Context) -> dict:
    """{segment name: upload time (epoch)} of the archived WAL in the repository (`pgbackrest repo-ls`, one listing of the archive)."""
    archives = ctx.info().get("archive", [])
    if not archives:
        raise StageFailed("the repository holds no archived WAL")
    argv = ctx.wrap(ctx.pgbackrest_argv("--output=json", "--recurse", "repo-ls", f"archive/{ctx.cfg['pgbackrest']['stanza']}/{archives[-1]['id']}"))
    try:
        listing = json.loads(ctx.run(argv, 900, label="pgbackrest repo-ls").stdout)
    except ValueError:
        raise StageFailed("pgbackrest repo-ls did not return JSON") from None
    found = {}
    for path, entry in listing.items():
        match = WAL_FILE_RE.search(path)
        if match and entry.get("type") == "file":
            found[match.group(1)] = float(entry.get("time", 0))
    if not found:
        raise StageFailed("the repository holds no archived WAL")
    return found


def stage_wal_age(ctx: Context) -> str:
    pg, live = ctx.cfg["pgbackrest"], ctx.cfg["live"]
    files = wal_files(ctx)
    newest = max(files)
    age = ctx.now() - files[newest]
    if age > pg["wal_max_age_minutes"] * 60:
        raise StageFailed(f"newest archived WAL {newest} is {age / 60:.1f} min old (limit {pg['wal_max_age_minutes']} min)")
    row = psql_scalar(ctx, live["psql"], "select coalesce(last_archived_wal,''), coalesce(last_failed_time > last_archived_time, false) "
                      "from pg_stat_archiver", live["run_as"])
    wal, failing = (row.split("|") + ["", ""])[:2]
    if failing == "t":
        raise StageFailed("the live cluster's newest archive attempt failed")
    if WAL_NAME_RE.match(wal) and wal[:8] == newest[:8] and wal not in files:
        raise StageFailed("the repository lacks the WAL segment PostgreSQL reports as archived")
    return f"newest archived WAL {newest} is {age / 60:.1f} min old"


# [@ANCHOR: backup_selftest:wal_chain]
def stage_wal_chain(ctx: Context) -> str:
    """From the first WAL segment the newest backup needs to the newest archived segment, every segment must exist; `pgbackrest verify`
    only checks the WAL a backup needs to become consistent, so a hole later in the archive (which a restore to the latest point
    would stop at) is found here."""
    newest = ctx.backups()
    if not newest:
        raise StageFailed("the repository holds no backup")
    start = newest[-1].get("archive", {}).get("start")
    if not start or not WAL_NAME_RE.match(start):
        raise StageFailed("the newest backup records no first WAL segment")
    files = wal_files(ctx)
    tip = max((n for n in files if n[:8] == start[:8]), default=None)
    if tip is None or start not in files:
        raise StageFailed(f"WAL segment {start} (the first the backup needs) is not in the archive")
    missing, cur, count = [], start, 0
    while cur < tip:
        cur = next_segment(cur)
        count += 1
        if cur not in files:
            missing.append(cur)
    if missing:
        raise StageFailed(f"{len(missing)} WAL segment(s) missing between {start} and {tip}, first {missing[0]}")
    return f"WAL chain {start} .. {tip} is complete ({count + 1} segments)"


def stage_pgbackrest_verify(ctx: Context) -> str:
    if not ctx.cfg["pgbackrest"]["verify"]:
        return "disabled in the config"
    if not ctx.backups():
        raise StageFailed("empty repository: there is nothing to verify, and an empty repository is not a pass")
    ctx.run(ctx.wrap(ctx.pgbackrest_argv("verify")), 3 * 3600, label="pgbackrest verify")
    return "every repository file and the WAL chain verify"


def stage_kopia_restore(ctx: Context) -> str:
    """The existing weekly restore test of b2_backup.py, called as a library, plus a guard against an empty repository."""
    path = ctx.cfg["kopia"].get("b2_config")
    if not path:
        raise StageFailed("kopia.b2_config is not set")
    sink = io.StringIO()
    try:
        cfg = b2_backup.load_config(path)
        with contextlib.redirect_stdout(sink):
            b2_backup.cmd_restore_test(cfg, b2_backup.Runner(out=sink))
        with open(cfg.restore_status_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            report = json.load(handle)
    except (b2_backup.BackupError, b2_backup.ConfigError, OSError, ValueError) as exc:
        raise StageFailed("b2_backup restore-test: " + last_line(ctx.scrub(str(exc)))) from None
    compared = (len(report.get("samples") or []) + len(report.get("files") or []) + sum(
        1 for k in ("database", "recordings", "tenant_archives") if report.get(k)))
    if not report.get("ok") or compared == 0:
        raise StageFailed("the restore test compared nothing (empty repository or no sources): not a pass")
    return f"restored and compared {compared} item(s) from the kopia repository"


def stage_storage_restore(ctx: Context) -> str:
    command = ctx.cfg["storage_restore"].get("command")
    if not command:
        raise StageFailed("storage_restore.command is not set")
    scratch = os.path.join(ctx.cfg["scratch_root"], "storage-restore")
    if os.path.exists(scratch):
        raise StageFailed(f"{scratch} exists: a leftover from an interrupted run; remove it")
    try:
        proc = ctx.run(ctx.wrap(list(command) + ["--scratch", scratch], limited=True), ctx.cfg["storage_restore"].get("timeout", 6 * 3600),
                        label="the /storage restore test")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    passes = sum(1 for ln in proc.stdout.splitlines() if ln.startswith("PASS "))
    if "RESULT: ALL PASS" not in proc.stdout or passes == 0:
        raise StageFailed("the /storage restore test did not report ALL PASS")
    return f"/storage restore test: {passes} check(s) passed"


# -- restore group --------------------------------------------------------------------------------------------------------

def chown_to_owner(ctx: Context, path: str) -> None:
    owner = ctx.cfg.get("scratch_owner")
    if owner:
        entry = pwd.getpwnam(owner)
        os.chown(path, entry.pw_uid, entry.pw_gid)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def newest_basebackup(cfg: dict) -> tuple[str, str]:
    """(stamp, directory) of the newest base backup that has a verified.json sidecar (as hams1_storage_backup.py writes)."""
    root = cfg["basebackup_dir"]["db_dir"]
    try:
        names = sorted(n for n in os.listdir(root) if STAMP_RE.match(n) and os.path.isdir(os.path.join(root, n))
                       and os.path.exists(os.path.join(root, n + ".verified.json")))
    except OSError:
        names = []
    if not names:
        raise StageFailed("no verified base backup under " + root)
    return names[-1], os.path.join(root, names[-1])


def basebackup_manifest_size(directory: str) -> int:
    try:
        with open(os.path.join(directory, "backup_manifest"), "r", encoding="utf-8") as handle:  # audit-ignore-path
            return sum(int(f.get("Size", 0)) for f in json.load(handle).get("Files", []))
    except (OSError, ValueError):
        raise StageFailed("the base backup's backup_manifest is missing or unreadable") from None


# [@ANCHOR: backup_selftest:backup_selftest_restore_state_class]
class RestoreState:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.root = None      # per-run scratch directory
        self.data = None      # restored PGDATA
        self.sock = None
        self.port = None
        self.pg_bin = None
        self.started = False
        self.t0 = ctx.now()
        self.checksums = False

    def pg(self, tool: str) -> str:
        return os.path.join(self.pg_bin, tool) if self.pg_bin else tool

    def psql(self, sql: str, db: str = "postgres") -> str:
        rest = self.ctx.cfg["restore"]
        argv = self.ctx.wrap([self.pg("psql"), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", self.sock, "-p", str(self.port),
                              "-U", rest["pg_user"], "-d", db, "-c", sql])
        return self.ctx.run(argv, 600, label="psql (scratch)").stdout.strip()


def append_text(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:  # audit-ignore-path
        handle.write(text)


def restore_into(state: RestoreState) -> str:
    ctx = state.ctx
    cfg, rest = ctx.cfg, ctx.cfg["restore"]
    os.makedirs(cfg["scratch_root"], mode=0o700, exist_ok=True)
    chown_to_owner(ctx, cfg["scratch_root"])
    state.root = tempfile.mkdtemp(prefix="st-", dir=cfg["scratch_root"])
    chown_to_owner(ctx, state.root)
    ctx.scratch_dir = state.root
    state.data = os.path.join(state.root, "data")
    state.sock = os.path.join(state.root, "sock")
    if len(state.sock) > SOCKET_PATH_LIMIT:
        raise StageFailed("the scratch path is too long for a unix socket; use a shorter scratch_root")
    for sub in (state.data, state.sock):
        os.mkdir(sub, 0o700)
        chown_to_owner(ctx, sub)
    if rest["source"] == "pgbackrest":
        extra = ["--type=immediate", "--target-action=promote"] if rest["recovery"] == "immediate" else []
        argv = ctx.wrap(ctx.pgbackrest_argv(f"--pg1-path={state.data}", f"--tablespace-map-all={os.path.join(state.root, 'tblspc')}",
                                            "--archive-mode=off", *extra, "restore"), limited=True)
        ctx.run(argv, rest["recovery_timeout"], label="pgbackrest restore")
        label = ctx.backups()[-1]["label"]
    else:
        label = restore_basebackup_dir(state)
    try:
        with open(os.path.join(state.data, "PG_VERSION"), "r", encoding="utf-8") as handle:  # audit-ignore-path
            major = handle.read().strip().split(".")[0]
    except OSError:
        raise StageFailed("the restore left no PG_VERSION") from None
    state.pg_bin = rest["pg_bin"] or next((d for d in (f"/usr/lib/postgresql/{major}/bin",) if os.path.isdir(d)), None)
    if not state.pg_bin:
        raise StageFailed(f"no PostgreSQL {major} binaries on this host")
    return f"restored {label} into {state.data}"


def restore_basebackup_dir(state: RestoreState) -> str:
    """pg_basebackup -Ft zstd (base.tar.zst + pg_wal.tar) and pg_receivewal .zst segments, as hams1_storage_backup.py keeps them."""
    ctx, cfg = state.ctx, state.ctx.cfg
    stamp, directory = newest_basebackup(cfg)
    base, wal = os.path.join(directory, "base.tar.zst"), os.path.join(directory, "pg_wal.tar")
    if not os.path.exists(base):
        raise StageFailed(f"{base} is missing")
    unpack = ["sh", "-c", 'zstd -dc "$1" | tar -x -C "$2"', "sh", base, state.data]
    ctx.run(ctx.wrap(unpack, limited=True), cfg["restore"]["recovery_timeout"], label="unpacking base.tar.zst")
    if os.path.exists(wal):
        ctx.run(ctx.wrap(["tar", "-x", "-f", wal, "-C", os.path.join(state.data, "pg_wal")], limited=True), 3600,
                label="unpacking pg_wal.tar")
    os.chmod(state.data, 0o700)
    wal_dir = cfg["basebackup_dir"]["wal_dir"]
    command = f"zstd -dc -q {wal_dir}/%f.zst > \"%p\""
    append_text(os.path.join(state.data, "postgresql.auto.conf"), f"restore_command = '{command}'\n")
    open(os.path.join(state.data, "recovery.signal"), "w").close()
    return stamp


def control_values(state: RestoreState) -> dict:
    proc = state.ctx.run(state.ctx.wrap([state.pg("pg_controldata"), state.data]), 60, label="pg_controldata")
    values = {}
    for line in proc.stdout.splitlines():
        label, _, value = line.partition(":")
        if label.strip() in CONTROL_SETTINGS:
            values[CONTROL_SETTINGS[label.strip()]] = value.strip()
        if label.strip() == "Data page checksum version":
            state.checksums = value.strip() not in ("", "0")
    return values


def configure_scratch(state: RestoreState) -> None:
    """Make the restored directory a harmless throwaway: no network, no archiving, its own socket, no trust beyond its 0700 directory."""
    ctx, rest = state.ctx, state.ctx.cfg["restore"]
    state.port = free_port()
    values = control_values(state)
    hba = os.path.join(state.root, "pg_hba.scratch.conf")
    with open(hba, "w", encoding="utf-8") as handle:
        handle.write("local all all trust\n")
    lines = ["# written by backup_selftest.py", f"port = {state.port}", f"unix_socket_directories = '{state.sock}'", "listen_addresses = ''",
             "archive_mode = off", "archive_command = ''", "primary_conninfo = ''", f"hba_file = '{hba}'", "fsync = off",
             "synchronous_commit = off", f"shared_buffers = {rest['shared_buffers']}", "logging_collector = off", "ssl = off",
             "shared_preload_libraries = ''", "wal_receiver_status_interval = 0"]
    lines += [f"{key} = {value}" for key, value in sorted(values.items())]
    conf = os.path.join(state.data, "postgresql.conf")
    if not os.path.exists(conf):  # a Debian-layout cluster keeps its configuration outside the data directory
        append_text(conf, "# minimal, written by backup_selftest.py\n")
    append_text(os.path.join(state.data, "postgresql.auto.conf"), "\n".join(lines) + "\n")
    for name in (conf, os.path.join(state.data, "postgresql.auto.conf"), hba):
        chown_to_owner(ctx, name)


def start_scratch(state: RestoreState) -> str:
    ctx, rest = state.ctx, state.ctx.cfg["restore"]
    configure_scratch(state)
    log = os.path.join(state.root, "postgres.log")
    argv = ctx.wrap([state.pg("pg_ctl"), "-D", state.data, "-l", log, "-w", "-t", str(rest["start_timeout"]), "start"], limited=True)
    try:
        ctx.run(argv, rest["start_timeout"] + 60, label="pg_ctl start")
    except StageFailed as exc:
        tail = ""
        with contextlib.suppress(OSError), open(log, "r", encoding="utf-8", errors="replace") as handle:  # audit-ignore-path
            tail = last_line(ctx.scrub(handle.read()[-2000:]))
        raise StageFailed(f"{exc}; log: {tail}") from None
    state.started = True
    deadline = time.time() + rest["recovery_timeout"]
    while True:
        try:
            if state.psql("select not pg_is_in_recovery()") == "t":
                break
        except StageFailed:
            pass
        if time.time() > deadline:
            raise StageFailed("recovery did not finish in time")
        if subprocess.run(ctx.wrap([state.pg("pg_ctl"), "-D", state.data, "status"]), capture_output=True).returncode != 0:
            raise StageFailed("the scratch server stopped during recovery")
        time.sleep(2)
    return f"scratch PostgreSQL up on a unix socket (port {state.port}), recovery finished"


def sanity_sql(state: RestoreState) -> str:
    ctx, cfg = state.ctx, state.ctx.cfg
    rest, live = cfg["restore"], cfg["live"]
    notes = []
    names = state.psql("select datname from pg_database where datallowconn and not datistemplate").split()
    for needed in set(item["db"] for item in rest["tables"]) | set(rest["databases"]):
        if needed not in names:
            raise StageFailed(f"database {needed} is missing from the restored cluster")
    if rest["recovery"] == "latest":
        newest = state.psql("select extract(epoch from pg_last_xact_replay_timestamp())")
        if not newest:
            raise StageFailed("no transaction was replayed: the restore holds nothing newer than the backup")
        lag = state.t0 - float(newest)
        if lag > rest["max_lag_seconds"]:
            raise StageFailed(f"newest restored transaction is {lag:.0f} s older than the restore start (limit {rest['max_lag_seconds']} s): WAL gap or stale archive")
        notes.append(f"newest transaction {max(lag, 0):.0f} s behind")
    for item in rest["tables"]:
        sql = f'select count(*) from {item["table"]}'
        got = int(state.psql(sql, item["db"]) or -1)
        want = int(psql_scalar(ctx, live["psql"], sql, live["run_as"], db=item["db"]) or -1)
        tolerance = item.get("tolerance_pct", 10) / 100.0
        if got < 0 or want < 0 or (want > 0 and got == 0) or abs(got - want) > tolerance * max(want, 1):
            raise StageFailed(f"{item['db']}.{item['table']}: {got} rows restored, {want} live (tolerance {item.get('tolerance_pct', 10)} %)")
        notes.append(f"{item['table']} {got}/{want}")
    return "; ".join(notes)


def checksum_read(state: RestoreState) -> str:
    """Read every relation: pg_checksums when the cluster has checksums (server stopped first), otherwise a dump of each database."""
    ctx, rest = state.ctx, state.ctx.cfg["restore"]
    if not state.checksums:
        names = state.psql("select datname from pg_database where datallowconn and not datistemplate").split()
        for name in names:
            argv = ctx.wrap([state.pg("pg_dump"), "-h", state.sock, "-p", str(state.port), "-U", rest["pg_user"], "-Z", "0", "-f", "/dev/null", name],
                            limited=True)
            ctx.run(argv, 6 * 3600, label="pg_dump")
        return f"no data checksums: dumped {len(names)} database(s) to /dev/null without error"
    stop_scratch(state, "fast")  # a clean shutdown: pg_checksums refuses a cluster that was not shut down cleanly
    proc = ctx.run(ctx.wrap([state.pg("pg_checksums"), "--check", "-D", state.data], limited=True), 6 * 3600, label="pg_checksums")
    summary = [ln for ln in proc.stdout.splitlines() if "Data checksum" in ln or "Files scanned" in ln or "Blocks scanned" in ln]
    return "pg_checksums: " + "; ".join(s.strip() for s in summary[:3])


def stop_scratch(state: RestoreState, mode: str = "immediate") -> None:
    if state.started:
        state.ctx.run(state.ctx.wrap([state.pg("pg_ctl"), "-D", state.data, "-m", mode, "-w", "stop"]), 300, label="pg_ctl stop")
        state.started = False


def remove_scratch(state: RestoreState) -> str:
    stop_scratch(state)
    if state.root:
        if not inside(state.root, state.ctx.cfg["scratch_root"]) or os.path.realpath(state.root) == os.path.realpath(state.ctx.cfg["scratch_root"]):
            raise StageFailed("refusing to remove a directory outside the scratch root")
        shutil.rmtree(state.root)
    return "scratch directory removed"


def run_restore_group(ctx: Context, report: Report) -> None:
    state = RestoreState(ctx)
    steps = [("restore", restore_into), ("start", start_scratch), ("sanity_sql", sanity_sql), ("checksum_read", checksum_read)]
    broken = False
    try:
        for name, step in steps:
            if broken:
                report.add(name, "SKIP", "an earlier restore stage failed")
                continue
            try:
                report.add(name, "PASS", step(state))
            except StageFailed as exc:
                report.add(name, "FAIL", str(exc))
                broken = True
    finally:
        try:
            report.add("cleanup", "PASS", remove_scratch(state))
        except (StageFailed, OSError) as exc:
            report.add("cleanup", "FAIL", f"the scratch directory may remain: {last_line(str(exc))}")


SIMPLE_STAGES = {
    "preflight": stage_preflight, "key_fingerprint": stage_key_fingerprint, "pgbackrest_check": stage_pgbackrest_check,
    "backup_age": stage_backup_age, "wal_age": stage_wal_age, "wal_chain": stage_wal_chain, "pgbackrest_verify": stage_pgbackrest_verify,
    "kopia_restore": stage_kopia_restore, "storage_restore": stage_storage_restore,
}


# [@ANCHOR: backup_selftest:run_stages]
def run_stages(ctx: Context, report: Report, only=None) -> int:
    """Run the configured stages (or `only`); returns the exit status. A refusal in preflight stops the run before anything is started."""
    wanted = [s for s in ctx.cfg["stages"] if not only or s in only or (s in RESTORE_GROUP and "restore" in only)]
    for name in wanted:
        if name in RESTORE_GROUP and name != "restore":
            continue
        if name == "restore":
            if any(r["stage"] == "preflight" and r["status"] == "FAIL" for r in report.results):
                for member in RESTORE_GROUP:
                    report.add(member, "SKIP", "preflight failed")
            else:
                run_restore_group(ctx, report)
            continue
        try:
            report.add(name, "PASS", SIMPLE_STAGES[name](ctx))
        except Refused as exc:
            report.add(name, "REFUSED", str(exc))
            return EXIT_REFUSED
        except StageFailed as exc:
            report.add(name, "FAIL", str(exc))
    failed = report.first_failure
    return STAGE_CODES[failed] if failed else 0


# ---------------------------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------------------------

def status_path(cfg: dict) -> str:
    return os.path.join(cfg["state_dir"], "selftest_status.json")


# [@ANCHOR: backup_selftest:report_document]
def report_document(cfg: dict, report: Report, code: int) -> dict:
    """The machine-readable result (schema 1): what `run --json` prints and the status file holds, so another tool (the restoration
    system) can reuse the checks. Every stage has its name, exit code, status (PASS, FAIL, SKIP or REFUSED) and a secret-free detail."""
    return {"schema": 1, "profile": cfg["profile"], "finished": datetime.datetime.now(datetime.timezone.utc).isoformat(), "exit": code,
            "ok": code == 0, "failed_stage": report.first_failure, "refused": code == EXIT_REFUSED,
            "stages": [{**r, "code": STAGE_CODES[r["stage"]]} for r in report.results]}


def write_status(cfg: dict, report: Report, code: int) -> None:
    path = status_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as handle:  # audit-ignore-path
            previous = json.load(handle)
    except (OSError, ValueError):
        previous = {}
    data = report_document(cfg, report, code)
    data["last_success"] = data["finished"] if code == 0 else previous.get("last_success")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:  # audit-ignore-path
        json.dump(data, handle, indent=1)
    os.replace(tmp, path)


def cmd_run(cfg: dict, only, out, err, as_json: bool = False) -> int:
    """With as_json the stage lines go to `err` and the one JSON document (report_document) to `out`."""
    document_out, out = out, (err if as_json else out)
    os.makedirs(cfg["state_dir"], mode=0o700, exist_ok=True)
    with open(os.path.join(cfg["state_dir"], "lock"), "w", encoding="utf-8") as lock:  # audit-ignore-path
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("REFUSED another backup self-test is running", file=out)
            return EXIT_REFUSED
        report = Report(out)
        try:
            _, secret_texts = key_fingerprints(cfg) if cfg["keys"] else ({}, [])
        except StageFailed:
            secret_texts = []  # key_fingerprint reports it; the other stages need no key
        ctx = Context(cfg, secret_texts)
        code = run_stages(ctx, report, only)
        with contextlib.suppress(OSError):
            write_status(cfg, report, code)
        if as_json:
            print(json.dumps(report_document(cfg, report, code), indent=1), file=document_out)
        if code == EXIT_REFUSED:
            print("backup selftest refused to run (not a backup failure)", file=err)
        elif code:
            print(f"backup selftest FAILED at stage {report.first_failure}", file=err)
        else:
            print("RESULT: ALL PASS", file=out)
        return code


def cmd_check(cfg: dict, max_age_days: float, err) -> int:
    """For a pager "synthetic" check: exit 1 with `SEVERITY:high` and a message (stage name only) when the last run failed or is too old."""
    try:
        with open(status_path(cfg), "r", encoding="utf-8") as handle:  # audit-ignore-path
            data = json.load(handle)
    except (OSError, ValueError):
        print("SEVERITY:high\nbackup selftest has never run", file=err)
        return 1
    if data.get("failed_stage"):
        print(f"SEVERITY:high\nbackup selftest failed at stage {data['failed_stage']}", file=err)
        return 1
    last = data.get("last_success")
    try:
        age_days = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(last)).total_seconds() / 86400
    except (TypeError, ValueError):
        print("SEVERITY:high\nbackup selftest has never passed", file=err)
        return 1
    if age_days > max_age_days:
        print(f"SEVERITY:high\nbackup selftest last passed {age_days:.0f} days ago", file=err)
        return 1
    print(f"backup selftest passed {age_days:.1f} days ago")
    return 0


def main(argv=None, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--only", action="append", choices=sorted(STAGE_CODES), help="run just this stage (repeatable)")
    run.add_argument("--json", action="store_true", help="print one JSON document on standard output (stage lines go to standard error)")
    sub.add_parser("report", help="print the last run's JSON result from the status file")
    rec = sub.add_parser("record-fingerprint")
    rec.add_argument("--confirm-record", action="store_true", required=True,
                     help="required: a person is deliberately recording the fingerprints of the current keys")
    chk = sub.add_parser("check")
    chk.add_argument("--max-age-days", type=float, default=9.0)
    sub.add_parser("stages")
    args = parser.parse_args(argv)
    if args.command == "stages":
        for name, code in STAGE_CODES.items():
            print(f"{code} {name}", file=out)
        return 0
    try:
        cfg = load_config(args.config)
        if args.command == "record-fingerprint":
            return cmd_record_fingerprint(cfg, out)
        if args.command == "check":
            return cmd_check(cfg, args.max_age_days, err)
        if args.command == "report":
            with open(status_path(cfg), "r", encoding="utf-8") as handle:  # audit-ignore-path
                print(handle.read(), file=out)
            return 0
        return cmd_run(cfg, args.only, out, err, args.json)
    except (ConfigurationError, StageFailed, OSError) as exc:
        print(f"backup_selftest: {exc}", file=err)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
