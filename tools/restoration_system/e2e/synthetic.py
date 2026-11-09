# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small synthetic backup set for the end-to-end test: NEVER real backups, keys or Backblaze.

Makes, in a directory the caller owns: a PostgreSQL 18 cluster with a few rows, a pgBackRest repository (posix) encrypted with a throwaway
passphrase holding a full backup and archived WAL after it, and a kopia repository (filesystem) with a throwaway password holding the local
set's `config` archive and `files` tree. Returns the throwaway passphrases and the expected row counts and newest time.
"""
import os
import secrets
import subprocess
import time

PG_BIN = "/usr/lib/postgresql/18/bin"


def sh(argv, env=None, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, env=env, **kw)


def make(root, newest_pad=0):
    """Create the set under `root`. Returns {"kopia_password", "pgbackrest_passphrase", "expected"}."""
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    kopia_pw, pg_pass = "kopia-" + secrets.token_hex(8), "pgbr-" + secrets.token_hex(8)
    base_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + PG_BIN, "HOME": root, "LC_ALL": "C.UTF-8"}
    work = os.path.join(root, ".synthetic-work")
    os.makedirs(work, exist_ok=True)
    repo = os.path.join(root, "synth-pgbackrest")
    conf = os.path.join(work, "pgbackrest.conf")
    data = os.path.join(work, "pgdata")
    sock = os.path.join(work, "sock")
    os.makedirs(repo, mode=0o700, exist_ok=True)
    os.makedirs(sock, exist_ok=True)
    with open(conf, "w") as handle:
        handle.write("[global]\nrepo1-path=%s\nrepo1-cipher-type=aes-256-cbc\nlog-path=%s\nlock-path=%s\nspool-path=%s\n\n[synth]\npg1-path=%s\npg1-socket-path=%s\npg1-port=55987\npg1-user=postgres\n"
                     % (repo, work, work, work, data, sock))
    env = dict(base_env, PGBACKREST_REPO1_CIPHER_PASS=pg_pass)
    sh([PG_BIN + "/initdb", "-D", data, "--no-sync", "-A", "trust", "-U", "postgres"], env=env)
    opts = ["-c", "port=55987", "-c", "listen_addresses=''", "-c", "unix_socket_directories=" + sock, "-c", "wal_level=replica", "-c", "archive_mode=on",
            "-c", "archive_command='pgbackrest --config=%s --stanza=synth archive-push %%p'" % conf, "-c", "archive_timeout=10"]
    sh([PG_BIN + "/pg_ctl", "-D", data, "-w", "-l", os.path.join(work, "pg.log"), "-o", " ".join(opts), "start"], env=env)
    psql = [PG_BIN + "/psql", "-h", sock, "-p", "55987", "-U", "postgres", "-At"]
    sh(psql + ["-c", "create database synth"], env=env)
    sh(psql + ["-d", "synth", "-c", "create table items(id serial primary key, name text, create_date timestamp default now()); "
                                    "insert into items(name) select 'row' || g from generate_series(1,100) g"], env=env)
    sh(["pgbackrest", "--config=" + conf, "--stanza=synth", "stanza-create"], env=env)
    sh(["pgbackrest", "--config=" + conf, "--stanza=synth", "--type=full", "--start-fast", "backup"], env=env)
    time.sleep(1.2)
    sh(psql + ["-d", "synth", "-c", "insert into items(name) select 'late' || g from generate_series(1,25) g"], env=env)
    sh(psql + ["-d", "synth", "-c", "select pg_switch_wal()"], env=env)
    time.sleep(2)
    counts = sh(psql + ["-d", "synth", "-c", "select count(*), to_char(max(create_date),'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') from items"], env=env).stdout.strip().split("|")
    sh(psql + ["-d", "synth", "-c", "select pg_switch_wal()"], env=env)
    time.sleep(2)
    sh([PG_BIN + "/pg_ctl", "-D", data, "-m", "fast", "-w", "stop"], env=env)
    expected = {"counts": {"items": int(counts[0])}, "newest": counts[1]}
    kopia_repo = os.path.join(root, "synth-kopia")
    kenv = dict(base_env, KOPIA_PASSWORD=kopia_pw, KOPIA_CHECK_FOR_UPDATES="false")
    kcfg = ["--config-file=" + os.path.join(work, "kopia.config"), "--log-dir=" + os.path.join(work, "klogs"), "--no-use-keyring"]
    sh(["kopia", "repository", "create", "filesystem", "--path=" + kopia_repo, "--no-persist-credentials", "--cache-directory=" + os.path.join(work, "kcache")] + kcfg, env=kenv)
    stage = os.path.join(work, "stage")
    os.makedirs(os.path.join(stage, "etc/nginx/conf.d"))
    os.makedirs(os.path.join(stage, "opt/hams/etc"))
    with open(os.path.join(stage, "etc/nginx/conf.d/synth.conf"), "w") as handle:
        handle.write("server { listen 80 default_server; root /var/www/synth; index index.html; }\n")
    with open(os.path.join(stage, "opt/hams/etc/synth.env"), "w") as handle:
        handle.write("SYNTH=1\n")
    cfgdir = os.path.join(work, "config")
    os.makedirs(cfgdir)
    sh(["tar", "--zstd", "-cf", os.path.join(cfgdir, "config-20261109T000000Z.tar.zst"), "--numeric-owner", "-C", stage, "."])
    site = os.path.join(work, "site")
    os.makedirs(site)
    with open(os.path.join(site, "index.html"), "w") as handle:
        handle.write("<html><body>synthetic site</body></html>\n")
    sh(["kopia", "snapshot", "create", cfgdir, "--tags=kind:config", "--tags=name:config", "--json"] + kcfg, env=kenv)
    sh(["kopia", "snapshot", "create", site, "--tags=kind:files", "--tags=name:site", "--json"] + kcfg, env=kenv)
    sh(["kopia", "repository", "disconnect"] + kcfg, env=kenv)
    return {"kopia_password": kopia_pw, "pgbackrest_passphrase": pg_pass, "expected": expected, "work": work}
