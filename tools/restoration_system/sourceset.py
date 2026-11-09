# SPDX-License-Identifier: AGPL-3.0-or-later
"""A set of backups (local directory or Backblaze) opened, checked and turned into a restore plan.

A `BackupSet` knows WHAT to restore from WHERE; it holds no data. `open_local` and `open_b2` connect read-only, refuse a missing,
empty or unencrypted source, and report the metadata the verifier compares against (backup stop time, WAL range).
"""
import dataclasses
import json
import os
import re
import shutil

from .runner import RestorationError
from . import secrets_input as si
from . import sources as src

STAT_RE = re.compile(r"^(\d+) (\d+),(\d+) (\d+):(\d+)$")


@dataclasses.dataclass
class FileItem:
    """One restore action from a kopia repository: `snapshot` goes to `path` inside the image. kind: tree, file or config_tar."""
    label: str
    snapshot: dict
    path: str
    kind: str


@dataclasses.dataclass
class DbPlan:
    """How the database is restored: kind "pgbackrest" (restore + WAL fetch) or "streams" (sealed base backup and WAL in kopia)."""
    kind: str
    backup_label: str
    stop_time: str
    wal_first: str
    wal_last: str
    details: dict


class BackupSet:
    def __init__(self, kind, runner, secrets, profile, work):
        self.kind, self.runner, self.secrets, self.profile, self.work = kind, runner, secrets, profile, work
        self.kopia = None
        self.pg = None
        self.pg_info = None
        self.pg_backup = None
        self.kopia_snapshots = []
        self.stream_snapshots = {}

    def close(self):
        if self.kopia:
            self.kopia.close()

    def describe(self):
        """Metadata for the image's RESTORATION_META.json and the coverage report. Names and times only, no secret."""
        out = {"kind": self.kind, "kopia_snapshots": len(self.kopia_snapshots)}
        if self.pg_backup:
            out["pgbackrest"] = {"label": self.pg_backup["label"], "stop": self.pg_backup["timestamp"]["stop"],
                                 "wal_start": self.pg_backup["archive"]["start"], "wal_max": self.pg_info["archive"][-1]["max"]}
        return out

    def file_items(self):
        """The kopia restore actions, in the order they should run."""
        items = []
        if self.kind == "b2":
            by_path = src.newest_by_path(self.kopia_snapshots)
            for path in self.profile["b2_kopia_paths"]:
                if path in by_path:
                    items.append(FileItem("kopia " + path, by_path[path], path, "tree"))
            for name, path in self.profile["b2_kopia_files"].items():
                snap = by_path.get("/hams-b2-virtual/file/" + name)
                if snap:
                    items.append(FileItem("kopia file " + name, snap, path, "file"))
        else:
            config = src.newest_by_tag(self.kopia_snapshots, "config")
            for name, snap in sorted(config.items()):
                items.append(FileItem("config archives " + name, snap, "/", "config_tar"))
            files = src.newest_by_tag(self.kopia_snapshots, "files")
            for name, path in self.profile["local_files_sources"].items():
                if name in files:
                    items.append(FileItem("files " + name, files[name], path, "tree"))
        return items

    def db_plan(self):
        """DbPlan from the pgBackRest repository when there is one, else from the sealed streams. RestorationError when neither."""
        if self.pg_backup:
            b = self.pg_backup
            return DbPlan("pgbackrest", b["label"], str(b["timestamp"]["stop"]), b["archive"]["start"], self.pg_info["archive"][-1]["max"], {})
        base = src.newest_by_tag(self.kopia_snapshots, "base")
        if not base:
            raise RestorationError("the backup set holds no PostgreSQL backup: no pgBackRest repository and no sealed base backup")
        name, snap = sorted(base.items())[-1]
        wal = src.newest_by_tag(self.kopia_snapshots, "wal")
        return DbPlan("streams", name, str(snap["startTime"]), "", "", {"base": snap, "wal": [wal[k] for k in sorted(wal)]})


def _kopia_checks(kopia, label):
    src.SourceCheck.kopia_encrypted(kopia)
    snaps = kopia.snapshots()
    src.SourceCheck.kopia_not_empty(snaps, label)
    return snaps


def open_local(root, runner, secrets, profile, work):
    """Open a local set: <root>/<kopia_repo> and optionally <root>/<pgbackrest_repo>. RestorationError for any refusal.

    A directory that holds only plaintext staging (db/, wal/, config/ without a repository) is refused: it is not encrypted."""
    if not os.path.isdir(root) or not os.listdir(root):
        raise RestorationError("local source %s is missing or empty" % root)
    cfg = profile["local"]
    kopia_dir = os.path.join(root, cfg["kopia_repo"])
    pg_dir = os.path.join(root, cfg["pgbackrest_repo"])
    have_kopia = os.path.isdir(kopia_dir)
    have_pg = os.path.isdir(pg_dir)
    if not have_kopia and not have_pg:
        raise RestorationError("%s holds neither a kopia repository (%s) nor a pgBackRest repository (%s): no encrypted source found; "
                               "plaintext staging directories are refused" % (root, cfg["kopia_repo"], cfg["pgbackrest_repo"]))
    bs = BackupSet("local", runner, secrets, profile, work)
    try:
        if have_pg:
            db = profile["database"]
            src.SourceCheck.pgbackrest_marker(pg_dir, db["stanza"])
            secrets.need([si.PGBACKREST])
            bs.pg = src.PgBackRestRepo(runner, secrets, os.path.join(work, "pgbackrest-state"), db["stanza"], db["cipher_type"],
                                       {"type": "posix", "path": pg_dir}, os.path.join(work, "pg-probe"))
            bs.pg.write_config()
            bs.pg_info = bs.pg.info()
            bs.pg_backup = bs.pg.check_info(bs.pg_info)
        if have_kopia:
            src.SourceCheck.kopia_marker(kopia_dir)
            secrets.need([si.KOPIA])
            bs.kopia = src.KopiaRepo(runner, secrets, os.path.join(work, "kopia-state"), ["filesystem", "--path=%s" % kopia_dir], "local").connect()
            bs.kopia_snapshots = _kopia_checks(bs.kopia, "local")
    except Exception:
        bs.close()
        raise
    return bs


def open_b2(runner, secrets, profile, work):
    """Open the Backblaze set: the kopia repository under the bucket's files/ prefix and the pgBackRest repository."""
    secrets.need([si.B2_KEY_ID, si.B2_KEY, si.KOPIA, si.PGBACKREST])
    b2, db = profile["b2"], profile["database"]
    bs = BackupSet("b2", runner, secrets, profile, work)
    try:
        bs.pg = src.PgBackRestRepo(runner, secrets, os.path.join(work, "pgbackrest-state"), db["stanza"], db["cipher_type"],
                                   {"type": "s3", "path": db["pgbackrest_repo_path_b2"], "bucket": b2["bucket"], "endpoint": b2["endpoint"],
                                    "region": b2["region"]}, os.path.join(work, "pg-probe"))
        bs.pg.write_config()
        bs.pg_info = bs.pg.info()
        bs.pg_backup = bs.pg.check_info(bs.pg_info)
        backend = ["s3", "--bucket=%s" % b2["bucket"], "--endpoint=%s" % b2["endpoint"], "--region=%s" % b2["region"], "--prefix=%s" % b2["kopia_prefix"]]
        bs.kopia = src.KopiaRepo(runner, secrets, os.path.join(work, "kopia-state"), backend, "backblaze").connect()
        bs.kopia_snapshots = _kopia_checks(bs.kopia, "backblaze")
    except Exception:
        bs.close()
        raise
    return bs


def apply_rsync_stat(tree, stat_file=".hams-rsync-stat.json"):
    """Apply the owners and modes the second backup kept in `<tree>/.hams-rsync-stat.json` (rsync --fake-super values `mode rdev uid:gid`)
    and delete that file. Returns the number of entries applied. Root only."""
    path = os.path.join(tree, stat_file)
    if not os.path.isfile(path):
        return 0
    with open(path) as handle:
        values = json.load(handle)
    count = 0
    for rel, text in values.items():
        m = STAT_RE.match(text)
        target = os.path.join(tree, rel)
        if not m or not os.path.lexists(target):
            continue
        os.lchown(target, int(m.group(4)), int(m.group(5)))
        if not os.path.islink(target):
            os.chmod(target, int(m.group(1), 8) & 0o7777)
        count += 1
    os.unlink(path)
    return count


def extract_config_tar(archive, tree, runner):
    """Extract one config-<stamp>.tar.zst (made with --numeric-owner, paths relative to /) over the image, keeping numeric owners."""
    runner.run(["tar", "--zstd", "-xpf", archive, "--numeric-owner", "--xattrs", "--same-owner", "-C", tree])


def restore_item(item, bs, tree, scratch, runner):
    """Perform one FileItem into `tree`. Returns a short text for the log."""
    dest_root = os.path.join(scratch, "item")
    shutil.rmtree(dest_root, ignore_errors=True)
    if item.kind == "tree":
        target = os.path.join(tree, item.path.lstrip("/"))
        bs.kopia.restore(item.snapshot["id"], target)
        return "%s -> %s (%d entries fixed from rsync stat)" % (item.label, item.path, apply_rsync_stat(target))
    if item.kind == "file":
        bs.kopia.restore(item.snapshot["id"], dest_root)
        found = [os.path.join(d, f) for d, _, fs in os.walk(dest_root) for f in fs] if os.path.isdir(dest_root) else [dest_root]
        if len(found) != 1:
            raise RestorationError("%s: expected one file in the snapshot, found %d" % (item.label, len(found)))
        target = os.path.join(tree, item.path.lstrip("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(found[0], target)
        shutil.rmtree(dest_root, ignore_errors=True)
        return "%s -> %s" % (item.label, item.path)
    bs.kopia.restore(item.snapshot["id"], dest_root)
    archives = sorted(f for f in os.listdir(dest_root) if re.match(r"^config-\d{8}T\d{6}Z\.tar\.zst$", f))
    if not archives:
        raise RestorationError("%s: no config archive in the snapshot" % item.label)
    extract_config_tar(os.path.join(dest_root, archives[-1]), tree, runner)
    shutil.rmtree(dest_root, ignore_errors=True)
    return "%s: extracted %s" % (item.label, archives[-1])
