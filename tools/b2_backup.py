#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Encrypted, deduplicated, versioned off-site backup of hams1's files and database dumps to
Backblaze B2, using kopia over B2's S3-compatible API.

[@ANCHOR: b2_backup:tool]

What goes up (all of it encrypted on this host before it leaves, so B2 only ever holds ciphertext):
  * hams.com's Odoo filestore (the attachments pgBackRest cannot see);
  * every tenant's filestore and a logical dump of every tenant database (ADR 0105; the tenants are
    discovered from the spec directory at run time, so a new tenant is covered the night after it
    exists without a code change);
  * a logical dump of hams_prod (pgBackRest and hams.db.local.backup remain the primary database
    backups; this one is the extra copy that lives outside the PostgreSQL cluster format);
  * /opt/hams/etc (daemon keys, signer public material, host configuration), with the repository
    password and B2 key file itself excluded: backing up the key to the lock it opens protects nothing.

Subcommands (every one honours --plan, which prints what it would do and runs nothing):
  gen-password  dev box: create the repository password file (refuses to overwrite one)
  init          once: create the repository, apply retention policy, record the password fingerprint
  backup        nightly: dump the databases, snapshot every source, apply retention, record status
  restore-test  weekly: verify a sample of stored data, restore random files and one database to a
                scratch location, compare, and drop it again
  check         daily: fail when the last good backup or restore test is too old (catches a timer
                that never fired, which a failed-unit alert cannot)

Alerting is the existing path: any failure exits non-zero, the systemd unit goes `failed`, and the
pager_duty "Systemd Failed Services Tracker" check turns that into an operator alert.

KEY CUSTODY (see docs/runbooks/b2_backup.md in hams_com): kopia is symmetric, so the host that
writes backups must hold the repository password; that is unavoidable for incremental deduplicated
backup. The password is generated on the dev box, kept in ~/.secrets, printed on paper, and put on
hams1 in a root-only file. This tool never generates, rotates or changes a password on its own: init
records its SHA-256 fingerprint, and every later run refuses to start if the file no longer matches.
"""

import argparse
import dataclasses
import datetime
import fcntl
import hashlib
import json
import os
import random  # audit-ignore-weak-random: picks which files to spot-check; unpredictability is not a security property
import re
import secrets
import shutil
import subprocess
import sys

DEFAULT_CONFIG = "/opt/hams/etc/b2_backup/config.json"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SHA1_NAME_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_SECRETS = ("KOPIA_PASSWORD",)
S3_SECRETS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
# Seconds. A nightly job that has not succeeded for 36 hours, or a weekly test for 9 days, has missed a run.
BACKUP_MAX_AGE = 36 * 3600
RESTORE_MAX_AGE = 9 * 24 * 3600


class ConfigError(ValueError):
    pass


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Config:
    backend: dict
    env_file: str
    state_dir: str
    kopia: str
    max_upload_bytes_per_sec: int
    max_download_bytes_per_sec: int
    retention: dict
    paths: list            # [{"name":..., "path":..., "exclude":[...], "content_addressed": bool}]
    tenant_spec_dir: str
    tenant_data_root: str
    databases: list        # ["hams_prod"]
    pg_prefix: list        # e.g. ["runuser", "-u", "postgres", "--"]; [] when the caller is the DB role
    restore_sample_files: int
    restore_verify_percent: int
    restore_database: bool

    @property
    def lock_path(self):
        return os.path.join(self.state_dir, "lock")

    @property
    def fingerprint_path(self):
        return os.path.join(self.state_dir, "password.sha256")

    @property
    def status_path(self):
        return os.path.join(self.state_dir, "backup_status.json")

    @property
    def restore_status_path(self):
        return os.path.join(self.state_dir, "restore_test_status.json")

    @property
    def staging_dir(self):
        return os.path.join(self.state_dir, "staging")

    @property
    def scratch_dir(self):
        return os.path.join(self.state_dir, "restore-scratch")

    @property
    def kopia_config(self):
        return os.path.join(self.state_dir, "repository.config")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:  # audit-ignore-path
        raw = json.load(f)
    backend = raw.get("backend") or {}
    kind = backend.get("type")
    if kind == "s3":
        for field in ("bucket", "endpoint"):
            if not backend.get(field):
                raise ConfigError(f"backend.{field} is required for an s3 backend")
        if not backend["endpoint"].endswith("backblazeb2.com"):
            raise ConfigError("backend.endpoint must be a Backblaze B2 S3 endpoint (*.backblazeb2.com)")
    elif kind == "filesystem":
        if not backend.get("path"):
            raise ConfigError("backend.path is required for a filesystem backend")
    else:
        raise ConfigError("backend.type must be 's3' or 'filesystem'")
    retention = {"daily": 14, "weekly": 8, "monthly": 12}
    retention.update(raw.get("retention") or {})
    databases = list(raw.get("databases", ["hams_prod"]))
    for name in databases:
        if not NAME_RE.match(name):
            raise ConfigError(f"unsafe database name {name!r}")
    paths = []
    seen = set()
    for item in raw.get("paths", []):
        if not NAME_RE.match(item.get("name", "")) or not os.path.isabs(item.get("path", "")):
            raise ConfigError(f"path source needs a lower-case name and an absolute path: {item!r}")
        if item["name"] in seen:
            raise ConfigError(f"duplicate source name {item['name']}")
        seen.add(item["name"])
        paths.append({"name": item["name"], "path": item["path"],
                      "exclude": list(item.get("exclude", [])),
                      "content_addressed": bool(item.get("content_addressed", False))})
    restore = raw.get("restore_test", {})
    return Config(
        backend=backend,
        env_file=raw.get("env_file", "/opt/hams/etc/b2_backup/b2_backup.env"),
        state_dir=raw.get("state_dir", "/var/lib/hams-b2-backup"),
        kopia=raw.get("kopia", "kopia"),
        max_upload_bytes_per_sec=int(raw.get("max_upload_bytes_per_sec", 5_000_000)),
        max_download_bytes_per_sec=int(raw.get("max_download_bytes_per_sec", 10_000_000)),
        retention=retention,
        paths=paths,
        tenant_spec_dir=raw.get("tenant_spec_dir", "/opt/hams/etc/tenants.d"),
        tenant_data_root=raw.get("tenant_data_root", "/var/lib/hams-tenants"),
        databases=databases,
        pg_prefix=list(raw.get("pg_prefix", [])),
        restore_sample_files=int(restore.get("sample_files", 20)),
        restore_verify_percent=int(restore.get("verify_percent", 1)),
        restore_database=bool(restore.get("database", True)),
    )


def load_env_file(path):
    """KEY=VALUE lines (what systemd's EnvironmentFile accepts). The values are secrets and are only
    ever passed to kopia through its environment, never on a command line."""
    values = {}
    with open(path, "r", encoding="utf-8") as f:  # audit-ignore-path
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                raise ConfigError(f"{path}: line without '='")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def required_secret_names(cfg):
    return REQUIRED_SECRETS + (S3_SECRETS if cfg.backend["type"] == "s3" else ())


def fingerprint(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Source:
    name: str          # stable key; the snapshot's virtual path derives from it
    kind: str          # "path" or "db"
    path: str = ""     # for "path"
    database: str = ""  # for "db"
    exclude: tuple = ()
    content_addressed: bool = False

    @property
    def snapshot_path(self):
        """Where kopia records this source. A database dump has no real directory, so it gets a
        stable virtual one; the same name every night is what makes it a versioned series."""
        return self.path if self.kind == "path" else f"/hams-b2-virtual/db/{self.database}"


def discover_tenants(cfg):
    """Names from every *.json spec in the tenant spec directory (ADR 0105). A missing directory
    means no tenants yet; a spec that cannot be read is an error, never a silent gap."""
    try:
        entries = sorted(os.listdir(cfg.tenant_spec_dir))
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        with open(os.path.join(cfg.tenant_spec_dir, entry), "r", encoding="utf-8") as f:  # audit-ignore-path
            name = json.load(f).get("name", "")
        if not NAME_RE.match(name):
            raise ConfigError(f"tenant spec {entry} has no valid name")
        names.append(name)
    return names


def build_sources(cfg):
    sources = []
    for item in cfg.paths:
        sources.append(Source(name=item["name"], kind="path", path=item["path"],
                              exclude=tuple(item["exclude"]),
                              content_addressed=item["content_addressed"]))
    for name in discover_tenants(cfg):
        sources.append(Source(
            name=f"tenant_{name}_filestore", kind="path",
            path=os.path.join(cfg.tenant_data_root, name, "filestore", name),
            content_addressed=True))
        sources.append(Source(name=f"db_{name}", kind="db", database=name))
    for name in cfg.databases:
        sources.append(Source(name=f"db_{name}", kind="db", database=name))
    names = [s.name for s in sources]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate sources: {sorted(duplicates)}")
    return sources


# --------------------------------------------------------------------------------------------
# Running things
# --------------------------------------------------------------------------------------------

class Runner:
    """Runs commands, or in plan mode only announces them. Nothing is a stand-in: the plan prints the
    exact argv the real run would execute (no secret is ever on an argv)."""

    def __init__(self, plan=False, out=None):
        self.plan = plan
        self.out = out or sys.stdout

    def say(self, text):
        print(text, file=self.out, flush=True)

    def run(self, argv, env=None, stdout=None, stdin=None, capture=False, check=True):
        if self.plan:
            self.say("PLAN run: " + " ".join(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        proc = subprocess.run(argv, env=env, stdout=stdout if stdout is not None else (
            subprocess.PIPE if capture else None), stdin=stdin, stderr=subprocess.PIPE if capture else None,
            text=bool(capture) and stdout is None, check=False)
        if check and proc.returncode != 0:
            detail = (proc.stderr or "").strip()[-400:] if capture else ""
            raise BackupError(f"{' '.join(argv[:3])} failed with exit code {proc.returncode} {detail}".strip())
        return proc


def kopia_env(cfg, secrets_map):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "HOME", "TMPDIR")}
    env.update(secrets_map)
    env.update({
        "KOPIA_CONFIG_PATH": cfg.kopia_config,
        "KOPIA_CACHE_DIRECTORY": os.path.join(cfg.state_dir, "cache"),
        "KOPIA_LOG_DIR": os.path.join(cfg.state_dir, "logs"),
        "KOPIA_CHECK_FOR_UPDATES": "false",
        "KOPIA_PERSIST_CREDENTIALS_ON_CONNECT": "false",
        # Two cores at most for the Go runtime, on top of the unit's CPU quota.
        "GOMAXPROCS": "2",
    })
    return env


def backend_args(cfg):
    b = cfg.backend
    if b["type"] == "s3":
        args = ["s3", f"--bucket={b['bucket']}", f"--endpoint={b['endpoint']}"]
        if b.get("region"):
            args.append(f"--region={b['region']}")
        if b.get("prefix"):
            args.append(f"--prefix={b['prefix']}")
        return args
    return ["filesystem", f"--path={b['path']}"]


def repo_create_argv(cfg):
    args = [cfg.kopia, "repository", "create"] + backend_args(cfg) + ["--no-persist-credentials"]
    return args + retention_lock_args(cfg)


def retention_lock_args(cfg):
    """Optional B2 Object Lock on the repository blobs. Only for a bucket created with Object Lock
    enabled; kopia then needs the lock period at create time and extends it during maintenance."""
    lock = cfg.backend.get("object_lock")
    if not lock:
        return []
    return [f"--retention-mode={lock['mode']}", f"--retention-period={lock['period']}"]


def repo_connect_argv(cfg, readonly=False):
    args = [cfg.kopia, "repository", "connect"] + backend_args(cfg) + [
        "--no-persist-credentials",
        f"--max-upload-speed={cfg.max_upload_bytes_per_sec}",
        f"--max-download-speed={cfg.max_download_bytes_per_sec}",
    ]
    if readonly:
        args.append("--readonly")
    return args


def global_policy_argv(cfg):
    r = cfg.retention
    return [cfg.kopia, "policy", "set", "--global",
            f"--keep-daily={r['daily']}", f"--keep-weekly={r['weekly']}", f"--keep-monthly={r['monthly']}",
            "--keep-annual=0", "--keep-hourly=0", "--keep-latest=1",
            "--compression=zstd"]


def snapshot_argv(cfg, source, stdin_file=None):
    args = [cfg.kopia, "snapshot", "create", "--parallel=1", "--no-send-snapshot-report"]
    if stdin_file:
        args.append(f"--stdin-file={stdin_file}")
    return args + [source.snapshot_path]


# --------------------------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------------------------

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def ensure_dirs(cfg, runner):
    for path in (cfg.state_dir, os.path.join(cfg.state_dir, "cache"), os.path.join(cfg.state_dir, "logs")):
        if runner.plan:
            runner.say(f"PLAN mkdir -m 700 {path}")
        else:
            os.makedirs(path, mode=0o700, exist_ok=True)


class _Lock:
    def __init__(self, cfg, runner):
        self.cfg, self.runner, self.handle = cfg, runner, None

    def __enter__(self):
        if self.runner.plan:
            return self
        self.handle = open(self.cfg.lock_path, "w", encoding="utf-8")  # audit-ignore-path
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise BackupError("another b2_backup run holds the lock; a backup is still in progress") from None
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()


def load_secrets(cfg, runner):
    if runner.plan and not os.path.exists(cfg.env_file):
        runner.say(f"PLAN read secrets from {cfg.env_file} (not present on this host; "
                   f"needs {', '.join(required_secret_names(cfg))})")
        return {name: "" for name in required_secret_names(cfg)}
    values = load_env_file(cfg.env_file)
    missing = [n for n in required_secret_names(cfg) if not values.get(n)]
    if missing:
        raise ConfigError(f"{cfg.env_file} lacks {', '.join(missing)}")
    return {n: values[n] for n in required_secret_names(cfg)}


# [@ANCHOR: b2_backup:password_fingerprint]
def check_fingerprint(cfg, secrets_map, runner):
    """The repository password is never rotated silently: a file that differs from the one init
    recorded stops every run until a human has looked."""
    if runner.plan:
        runner.say(f"PLAN compare sha256(KOPIA_PASSWORD) with {cfg.fingerprint_path}")
        return
    try:
        with open(cfg.fingerprint_path, "r", encoding="utf-8") as f:  # audit-ignore-path
            recorded = f.read().strip()
    except FileNotFoundError:
        raise BackupError(f"{cfg.fingerprint_path} is missing: run `b2_backup.py init` first") from None
    if recorded != fingerprint(secrets_map["KOPIA_PASSWORD"]):
        raise BackupError(
            "the repository password in the environment file differs from the one recorded at init "
            f"({cfg.fingerprint_path}). Refusing to run: restore the original password from the "
            "printed copy; do not rotate it by editing this file")


def connect(cfg, env, runner):
    runner.run(repo_connect_argv(cfg), env=env, capture=True)


def apply_policies(cfg, env, runner):
    runner.run(global_policy_argv(cfg), env=env, capture=True)
    for item in cfg.paths:
        for pattern in item["exclude"]:
            runner.run([cfg.kopia, "policy", "set", item["path"], f"--add-ignore={pattern}"], env=env, capture=True)


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:  # audit-ignore-path
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:  # audit-ignore-path
        return json.load(f)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------

def cmd_gen_password(args, runner):
    out = os.path.expanduser(args.out)
    if runner.plan:
        runner.say(f"PLAN write a new 48-byte random password to {out} (mode 0600; refuses if it exists)")
        return 0
    if os.path.exists(out):
        raise BackupError(f"{out} exists. This tool never replaces a repository password.")
    password = secrets.token_urlsafe(48)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    print(f"wrote {out}; fingerprint {fingerprint(password)[:12]}. Print it and store the copies; "
          "the value is not shown here.")
    return 0


# [@ANCHOR: b2_backup:init]
def cmd_init(cfg, runner):
    ensure_dirs(cfg, runner)
    secrets_map = load_secrets(cfg, runner)
    env = kopia_env(cfg, secrets_map)
    with _Lock(cfg, runner):
        if not runner.plan and os.path.exists(cfg.fingerprint_path):
            raise BackupError(f"{cfg.fingerprint_path} exists: already initialised (never re-run init to rotate)")
        runner.run(repo_create_argv(cfg), env=env, capture=True)
        connect(cfg, env, runner)
        apply_policies(cfg, env, runner)
        if runner.plan:
            runner.say(f"PLAN record sha256(KOPIA_PASSWORD) in {cfg.fingerprint_path}")
        else:
            with open(cfg.fingerprint_path, "w", encoding="utf-8") as f:  # audit-ignore-path
                f.write(fingerprint(secrets_map["KOPIA_PASSWORD"]) + "\n")
            os.chmod(cfg.fingerprint_path, 0o600)
    return 0


def dump_database(cfg, source, runner):
    """pg_dump -Fc -Z0 into a root-only staging file (uncompressed custom format, so kopia can
    deduplicate and compress it), then `pg_restore --list` proves the file is readable before it is
    uploaded. Returns the staging directory, which holds exactly one file."""
    directory = os.path.join(cfg.staging_dir, source.name)
    target = os.path.join(directory, f"{source.database}.dump")
    dump_argv = cfg.pg_prefix + ["pg_dump", "-Fc", "-Z", "0", source.database]
    if runner.plan:
        runner.say(f"PLAN {' '.join(dump_argv)} > {target}; pg_restore --list {target}")
        return directory
    shutil.rmtree(directory, ignore_errors=True)
    os.makedirs(directory, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        runner.run(dump_argv, stdout=f)
    with open(target, "rb") as f:  # audit-ignore-path
        runner.run(["pg_restore", "--list"], stdin=f, stdout=subprocess.DEVNULL)
    return directory


def snapshot_source(cfg, env, source, runner):
    """One kopia snapshot of one source. Returns the snapshot id, or None in plan mode."""
    if source.kind == "path":
        if not runner.plan and not os.path.isdir(source.path):
            raise BackupError(f"source directory {source.path} does not exist")
        runner.run(snapshot_argv(cfg, source), env=env, capture=True)
    else:
        directory = dump_database(cfg, source, runner)
        try:
            dump_file = os.path.join(directory, f"{source.database}.dump")
            if runner.plan:
                runner.say(f"PLAN {' '.join(snapshot_argv(cfg, source, stdin_file=source.database + '.dump'))} "
                           f"< {dump_file}")
            else:
                with open(dump_file, "rb") as f:  # audit-ignore-path
                    runner.run(snapshot_argv(cfg, source, stdin_file=f"{source.database}.dump"),
                               env=env, stdin=f, capture=True)
        finally:
            if not runner.plan:
                shutil.rmtree(directory, ignore_errors=True)
    if runner.plan:
        return None
    return latest_snapshot(cfg, env, source, runner)


def list_snapshots(cfg, env, source, runner):
    proc = runner.run([cfg.kopia, "snapshot", "list", source.snapshot_path, "--json"], env=env, capture=True)
    return json.loads(proc.stdout or "[]")


def latest_snapshot(cfg, env, source, runner):
    snaps = list_snapshots(cfg, env, source, runner)
    if not snaps:
        raise BackupError(f"no snapshot exists for {source.name} after the backup")
    return sorted(snaps, key=lambda s: s["startTime"])[-1]


# [@ANCHOR: b2_backup:backup]
def cmd_backup(cfg, runner):
    ensure_dirs(cfg, runner)
    secrets_map = load_secrets(cfg, runner)
    env = kopia_env(cfg, secrets_map)
    started = now_utc()
    results = []
    with _Lock(cfg, runner):
        check_fingerprint(cfg, secrets_map, runner)
        connect(cfg, env, runner)
        apply_policies(cfg, env, runner)
        sources = build_sources(cfg)
        for source in sources:
            entry = {"source": source.name, "ok": False}
            try:
                snap = snapshot_source(cfg, env, source, runner)
                entry["ok"] = True
                if snap:
                    entry["snapshot_id"] = snap["id"]
                    entry["bytes"] = snap.get("stats", {}).get("totalSize", 0)
                    entry["errors"] = snap.get("stats", {}).get("errorCount", 0)
                    if entry["errors"]:
                        entry["ok"] = False
                        entry["error"] = f"{entry['errors']} file errors in the snapshot"
            except (BackupError, OSError, ConfigError) as exc:
                entry["error"] = str(exc)
            results.append(entry)
        if runner.plan:
            runner.say(f"PLAN write {cfg.status_path}; exit non-zero if any of {len(sources)} sources failed")
            return 0
    failed = [r for r in results if not r["ok"]]
    status = {"started": started.isoformat(), "finished": now_utc().isoformat(),
              "ok": not failed, "sources": results}
    write_json(cfg.status_path, status)
    for r in results:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['source']} {r.get('snapshot_id', '')} {r.get('error', '')}".rstrip())
    if failed:
        raise BackupError(f"{len(failed)} of {len(results)} sources failed: {', '.join(r['source'] for r in failed)}")
    return 0


def _relative_files(cfg, env, root_object, runner):
    proc = runner.run([cfg.kopia, "ls", "-r", root_object], env=env, capture=True)
    files = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line and not line.endswith("/") and line.startswith(root_object + "/"):
            files.append(line[len(root_object) + 1:])
    return files


def sha1_file(path):
    # burn-ignore-legacy-protocol-hash: Odoo's filestore format names every object by the SHA-1 of its
    # content; this reproduces that existing format to check a restored file, it is not our own choice.
    h = hashlib.sha1()  # burn-ignore-legacy-protocol-hash
    with open(path, "rb") as f:  # audit-ignore-path
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:  # audit-ignore-path
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def restore_sample(cfg, env, source, snap, rng, runner, scratch):
    """Restores up to restore_sample_files random files of one snapshot and checks each. A
    content-addressed store (Odoo's filestore) names every file by the SHA-1 of its content, so the
    check needs no live copy. Other sources are compared with the live file when it has not
    changed since the snapshot; files that changed legitimately are counted, not failed."""
    root = snap["rootEntry"]["obj"]
    files = _relative_files(cfg, env, root, runner)
    if not files:
        raise BackupError(f"{source.name}: the snapshot lists no files")
    picks = rng.sample(files, min(cfg.restore_sample_files, len(files)))
    checked = changed = 0
    for index, rel in enumerate(picks):
        target = os.path.join(scratch, f"{source.name}-{index}")
        runner.run([cfg.kopia, "restore", f"{root}/{rel}", target], env=env, capture=True)
        if not os.path.isfile(target):
            raise BackupError(f"{source.name}: {rel} did not restore as a file")
        if source.content_addressed:
            if SHA1_NAME_RE.match(os.path.basename(rel)):
                if sha1_file(target) != os.path.basename(rel):
                    raise BackupError(f"{source.name}: {rel} restored with the wrong content")
                checked += 1
                continue
        live = os.path.join(source.path, rel)
        if os.path.isfile(live) and os.path.getsize(live) == os.path.getsize(target):
            if sha256_file(live) != sha256_file(target):
                changed += 1
            else:
                checked += 1
        else:
            changed += 1
    if checked == 0:
        raise BackupError(f"{source.name}: none of {len(picks)} sampled files could be verified")
    return {"source": source.name, "sampled": len(picks), "verified": checked, "changed_since": changed}


def restore_database_test(cfg, env, source, snap, runner, scratch):
    """Restores the newest dump of one database into a scratch database, counts its tables, drops it."""
    root = snap["rootEntry"]["obj"]
    target = os.path.join(scratch, f"{source.database}.dump")
    runner.run([cfg.kopia, "restore", f"{root}/{source.database}.dump", target], env=env, capture=True)
    scratch_db = f"b2restoretest_{source.database}"[:60]
    pg = cfg.pg_prefix
    runner.run(pg + ["dropdb", "--if-exists", scratch_db])
    runner.run(pg + ["createdb", "-T", "template0", scratch_db])
    try:
        with open(target, "rb") as f:  # audit-ignore-path
            runner.run(pg + ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-d", scratch_db],
                       stdin=f)
        proc = runner.run(pg + ["psql", "-At", "-d", scratch_db, "-c",
                                "select count(*) from information_schema.tables where table_schema='public'"],
                          capture=True)
        tables = int(proc.stdout.strip() or 0)
        if tables == 0:
            raise BackupError(f"{source.database}: the restored dump has no tables")
        return {"database": source.database, "tables": tables}
    finally:
        runner.run(pg + ["dropdb", "--if-exists", scratch_db], check=False)


# [@ANCHOR: b2_backup:restore_test]
def cmd_restore_test(cfg, runner, seed=None):
    ensure_dirs(cfg, runner)
    secrets_map = load_secrets(cfg, runner)
    env = kopia_env(cfg, secrets_map)
    rng = random.Random(seed)  # audit-ignore-weak-random: spot-check sampling
    started = now_utc()
    problems, report = [], {"samples": [], "database": None}
    with _Lock(cfg, runner):
        check_fingerprint(cfg, secrets_map, runner)
        connect(cfg, env, runner)
        sources = build_sources(cfg)
        scratch = cfg.scratch_dir
        if runner.plan:
            runner.say(f"PLAN kopia snapshot verify --verify-files-percent={cfg.restore_verify_percent}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
            os.makedirs(scratch, mode=0o700)
        try:
            if not runner.plan:
                runner.run([cfg.kopia, "snapshot", "verify",
                            f"--verify-files-percent={cfg.restore_verify_percent}",
                            "--parallel=1", "--file-parallelism=1"], env=env, capture=True)
            snaps = {}
            for source in sources:
                if runner.plan:
                    runner.say(f"PLAN restore {cfg.restore_sample_files} random files of the latest "
                               f"snapshot of {source.name} to {scratch}, check, delete")
                    continue
                try:
                    snaps[source.name] = latest_snapshot(cfg, env, source, runner)
                except BackupError as exc:
                    problems.append(str(exc))
            for source in sources:
                if source.kind == "path" and source.name in snaps and cfg.restore_sample_files > 0:
                    try:
                        report["samples"].append(
                            restore_sample(cfg, env, source, snaps[source.name], rng, runner, scratch))
                    except BackupError as exc:
                        problems.append(str(exc))
            db_sources = [s for s in sources if s.kind == "db"]
            if cfg.restore_database and db_sources:
                # Rotate through the databases by ISO week so each is restored in turn.
                chosen = db_sources[started.isocalendar()[1] % len(db_sources)]
                if runner.plan:
                    runner.say(f"PLAN restore the latest dump of {chosen.database} into scratch database "
                               f"b2restoretest_{chosen.database}, count tables, drop it")
                elif chosen.name in snaps:
                    try:
                        report["database"] = restore_database_test(cfg, env, chosen, snaps[chosen.name],
                                                                   runner, scratch)
                    except BackupError as exc:
                        problems.append(str(exc))
        except BackupError as exc:
            problems.append(str(exc))
        finally:
            if not runner.plan:
                shutil.rmtree(scratch, ignore_errors=True)
    if runner.plan:
        return 0
    report.update({"started": started.isoformat(), "finished": now_utc().isoformat(),
                   "ok": not problems, "problems": problems})
    write_json(cfg.restore_status_path, report)
    if problems:
        raise BackupError("restore test failed: " + "; ".join(problems))
    print(f"restore test ok: {json.dumps(report['samples'])} database={json.dumps(report['database'])}")
    return 0


# [@ANCHOR: b2_backup:check]
def cmd_check(cfg, runner, now=None):
    now = now or now_utc()
    problems = []
    for label, path, limit in (("backup", cfg.status_path, BACKUP_MAX_AGE),
                               ("restore test", cfg.restore_status_path, RESTORE_MAX_AGE)):
        if runner.plan:
            runner.say(f"PLAN require a successful {label} recorded in {path} within {limit // 3600} hours")
            continue
        try:
            status = read_json(path)
        except FileNotFoundError:
            problems.append(f"no {label} has ever been recorded ({path})")
            continue
        finished = datetime.datetime.fromisoformat(status["finished"])
        age = (now - finished).total_seconds()
        if not status.get("ok"):
            problems.append(f"the last {label} failed ({finished.isoformat()})")
        elif age > limit:
            problems.append(f"the last good {label} is {age / 3600:.0f} hours old (limit {limit // 3600})")
    if problems:
        raise BackupError("; ".join(problems))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--plan", action="store_true", help="print what would be done; run nothing")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("gen-password")
    gen.add_argument("--out", required=True)
    for name in ("init", "backup", "restore-test", "check"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    runner = Runner(plan=args.plan)
    try:
        if args.command == "gen-password":
            return cmd_gen_password(args, runner)
        cfg = load_config(args.config)
        if args.command == "init":
            return cmd_init(cfg, runner)
        if args.command == "backup":
            return cmd_backup(cfg, runner)
        if args.command == "restore-test":
            return cmd_restore_test(cfg, runner)
        return cmd_check(cfg, runner)
    except (BackupError, ConfigError, OSError, ValueError) as exc:
        print(f"b2_backup: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
