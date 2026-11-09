# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source adapters: read-only access to a set of backups, local or on Backblaze.

Two repository kinds are read:
  * a kopia repository (files, configuration, keys, the Odoo filestore; on the local set also the sealed PostgreSQL base
    backups and WAL streams) through the `kopia` program;
  * a pgBackRest repository (the PostgreSQL cluster) through the `pgbackrest` program.
Nothing is ever created, changed or deleted in a repository: kopia is connected with --readonly and pgBackRest is only asked for
`info`, `restore` and `archive-get`. A source that is missing, empty or not encrypted is refused (`SourceCheck`).
"""
import datetime
import json
import os
import re

from .runner import RestorationError

WAL_RE = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$")
SALTED = b"Salted__"


def tag(snapshot, key):
    """Value of kopia tag `key` on a snapshot (kopia stores tags as "tag:<key>")."""
    return (snapshot.get("tags") or {}).get("tag:" + key)


def newest_by_path(snapshots):
    """{source path: newest snapshot} of a `kopia snapshot list --all --json` result."""
    out = {}
    for snap in sorted(snapshots, key=lambda s: s["startTime"]):
        out[snap["source"]["path"]] = snap
    return out


def newest_by_tag(snapshots, kind):
    """{name tag: newest snapshot} among snapshots whose `kind` tag equals `kind` (the local set's sealed streams)."""
    out = {}
    for snap in sorted(snapshots, key=lambda s: s["startTime"]):
        if tag(snap, "kind") == kind:
            out[tag(snap, "name")] = snap
    return out


class KopiaRepo:
    """A read-only connection to one kopia repository, living in `state_dir` (0700, removed by close())."""

    def __init__(self, runner, secrets, state_dir, backend_args, label):
        self.runner, self.secrets, self.state_dir, self.backend_args, self.label = runner, secrets, state_dir, backend_args, label
        self.connected = False

    def _argv(self, *args):
        return ["kopia", *args, "--config-file=%s" % os.path.join(self.state_dir, "repository.config"),
                "--log-dir=%s" % os.path.join(self.state_dir, "logs"), "--no-use-keyring"]

    def _call(self, *args, capture=True):
        return self.runner.run(self._argv(*args), secret_env=self.secrets.kopia_env(), capture=capture).stdout

    def connect(self):
        """Connect read-only. RestorationError with a clear text for a wrong password or an absent repository."""
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        try:
            self._call("repository", "connect", *self.backend_args, "--no-persist-credentials", "--readonly",
                       "--cache-directory=%s" % os.path.join(self.state_dir, "cache"))
        except RestorationError as exc:
            raise RestorationError("cannot open the %s kopia repository (wrong password, wrong location or unreachable): %s"
                                   % (self.label, exc))
        self.connected = True
        return self

    def encryption(self):
        """The repository's encryption algorithm name, from `kopia repository status --json`."""
        return json.loads(self._call("repository", "status", "--json")).get("contentFormat", {}).get("encryption", "")

    def snapshots(self):
        return json.loads(self._call("snapshot", "list", "--all", "--json"))

    def restore(self, snapshot_id, destination):
        """Restore a snapshot into `destination` (created), keeping owners, modes and symlinks; any error fails."""
        os.makedirs(destination, exist_ok=True)
        self._call("snapshot", "restore", snapshot_id, destination, "--no-ignore-permission-errors", capture=False)

    def close(self):
        if self.connected:
            self.runner.run(self._argv("repository", "disconnect"), secret_env=self.secrets.kopia_env(), check=False, capture=True)
            self.connected = False


class SourceCheck:
    """The refusals that apply to every source: missing, empty, not encrypted."""

    @staticmethod
    def kopia_marker(repo_dir):
        """A local kopia repository directory must hold the repository format blob; else it is missing or not a repository."""
        if not os.path.isdir(repo_dir):
            raise RestorationError("kopia repository %s does not exist" % repo_dir)
        if not any(name.startswith("kopia.repository") for name in os.listdir(repo_dir)):
            raise RestorationError("%s is not a kopia repository (no kopia.repository file): refusing" % repo_dir)

    @staticmethod
    def kopia_encrypted(repo):
        algorithm = repo.encryption()
        if not algorithm or algorithm.upper() == "NONE":
            raise RestorationError("kopia repository %s is not encrypted (encryption %r): refusing an unencrypted source" % (repo.label, algorithm))

    @staticmethod
    def kopia_not_empty(snapshots, label):
        if not snapshots:
            raise RestorationError("kopia repository %s holds no snapshots: refusing an empty source" % label)

    @staticmethod
    def pgbackrest_marker(repo_dir, stanza):
        """A local pgBackRest repository must hold backup.info for the stanza and it must carry the OpenSSL salted header (encrypted)."""
        info = os.path.join(repo_dir, "backup", stanza, "backup.info")
        if not os.path.isfile(info):
            raise RestorationError("pgBackRest repository %s has no backup.info for stanza %s: missing or empty source" % (repo_dir, stanza))
        with open(info, "rb") as handle:
            head = handle.read(len(SALTED))
        if head != SALTED:
            raise RestorationError("pgBackRest repository %s is not encrypted (backup.info has no encryption header): refusing" % repo_dir)


def next_segment(name):
    """Next 16 MiB WAL segment name on the same timeline."""
    m = WAL_RE.match(name)
    tl, log, seg = (int(g, 16) for g in m.groups())
    seg += 1
    if seg == 0x100:
        log, seg = log + 1, 0
    return "%08X%08X%08X" % (tl, log, seg)


def segment_number(name):
    m = WAL_RE.match(name)
    _, log, seg = (int(g, 16) for g in m.groups())
    return log * 0x100 + seg


def segment_name(timeline, number):
    return "%08X%08X%08X" % (timeline, number // 0x100, number % 0x100)


class PgBackRestRepo:
    """A pgBackRest repository, local (posix) or S3 (Backblaze), opened through a private config file in `state_dir` (0700).

    The config file holds only locations and the cipher TYPE; passphrase and S3 keys come from the secret store in the environment
    of each command."""

    def __init__(self, runner, secrets, state_dir, stanza, cipher_type, repo, pg_path):
        self.runner, self.secrets, self.state_dir, self.stanza = runner, secrets, state_dir, stanza
        self.cipher_type, self.repo, self.pg_path = cipher_type, repo, pg_path
        self.conf = os.path.join(state_dir, "pgbackrest.conf")

    def write_config(self):
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        lines = ["[global]", "repo1-cipher-type=%s" % self.cipher_type, "log-path=%s" % os.path.join(self.state_dir, "log"),
                 "lock-path=%s" % os.path.join(self.state_dir, "lock"), "spool-path=%s" % os.path.join(self.state_dir, "spool"),
                 "log-level-console=warn", "log-level-file=info"]
        if self.repo["type"] == "posix":
            lines += ["repo1-type=posix", "repo1-path=%s" % self.repo["path"]]
        else:
            lines += ["repo1-type=s3", "repo1-path=%s" % self.repo["path"], "repo1-s3-bucket=%s" % self.repo["bucket"],
                      "repo1-s3-endpoint=%s" % self.repo["endpoint"], "repo1-s3-region=%s" % self.repo["region"]]
        lines += ["", "[%s]" % self.stanza, "pg1-path=%s" % self.pg_path, ""]
        for sub in ("log", "lock", "spool"):
            os.makedirs(os.path.join(self.state_dir, sub), mode=0o700, exist_ok=True)
        fd = os.open(self.conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines))

    def _argv(self, *args, with_stanza=True):
        argv = ["pgbackrest", "--config=%s" % self.conf, "--allow-root"]
        if with_stanza:
            argv.append("--stanza=%s" % self.stanza)
        return argv + list(args)

    def call(self, *args, check=True, capture=True):
        return self.runner.run(self._argv(*args), secret_env=self.secrets.pgbackrest_env(), check=check, capture=capture)

    def info(self):
        """The stanza's entry of `pgbackrest info --output=json`. RestorationError: wrong passphrase, unreachable, or no such stanza."""
        try:
            data = json.loads(self.call("info", "--output=json").stdout)
        except RestorationError as exc:
            raise RestorationError("cannot read the pgBackRest repository (wrong passphrase, wrong location or unreachable): %s" % exc)
        for stanza in data:
            if stanza["name"] == self.stanza:
                return stanza
        raise RestorationError("pgBackRest repository has no stanza %s" % self.stanza)

    @staticmethod
    def check_info(stanza):
        """Refusals on an info entry: not encrypted, no backup, status not ok. Returns the newest backup."""
        if stanza.get("cipher", "none") == "none":
            raise RestorationError("pgBackRest repository is not encrypted (cipher none): refusing an unencrypted source")
        if not stanza.get("backup"):
            raise RestorationError("pgBackRest repository holds no backup: refusing an empty source")
        if stanza.get("status", {}).get("code", 0) not in (0,):
            raise RestorationError("pgBackRest status: %s" % stanza["status"].get("message"))
        return stanza["backup"][-1]

    def restore_argv(self, datadir, target_time, wal_dir, backup_label=None):
        """argv of `pgbackrest restore` for point-in-time recovery from WAL pre-fetched into wal_dir (the image never reads the backups)."""
        argv = self._argv("--pg1-path=%s" % datadir, "restore", "--archive-mode=off",
                          "--recovery-option=restore_command=cp %s/%%f %%p" % wal_dir)
        if backup_label:
            argv.append("--set=%s" % backup_label)
        if target_time:
            argv += ["--type=time", "--target=%s" % target_time, "--target-action=promote"]
        return argv

    def restore(self, datadir, target_time, wal_dir, backup_label=None):
        self.runner.run(self.restore_argv(datadir, target_time, wal_dir, backup_label), secret_env=self.secrets.pgbackrest_env(), capture=True)

    def fetch_wal(self, backup, stanza_info, wal_dir):
        """Pre-fetch every WAL segment and timeline history file from the backup's first segment to the newest archived one.

        Returns the list of file names fetched. RestorationError when the segment the backup starts from is missing: the point-in-time
        restore would not reach consistency."""
        os.makedirs(wal_dir, exist_ok=True)
        first = backup["archive"]["start"]
        last = stanza_info["archive"][-1]["max"]
        start_tl, last_tl = int(first[:8], 16), int(last[:8], 16)
        fetched = []
        for tl in range(start_tl, last_tl + 1):
            name = "%08X.history" % tl
            if tl > 1 and self.call("archive-get", name, os.path.join(wal_dir, name), check=False).returncode == 0:
                fetched.append(name)
        for number in range(segment_number(first), segment_number(last) + 1):
            for tl in range(start_tl, last_tl + 1):
                name = segment_name(tl, number)
                if self.call("archive-get", name, os.path.join(wal_dir, name), check=False).returncode == 0:
                    fetched.append(name)
        if first not in fetched:
            raise RestorationError("WAL segment %s, where the base backup starts, is not in the archive: point-in-time recovery cannot work" % first)
        return fetched


def parse_timestamp(value):
    """UTC datetime from a pgBackRest epoch or a kopia RFC 3339 time."""
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
