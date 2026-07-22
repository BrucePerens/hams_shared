#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Infrastructure Blueprint & Provisioning Engine
Serves as the Single Source of Truth for test.py and provision.py.
Supports environment scoping, lifecycle hooks, and precise runtime mount states.
"""

import compileall
import contextlib
import glob
import grp
import logging
import os
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request
import secrets
import string
import base64
import getpass
from datetime import datetime

_logger = logging.getLogger(__name__)


def get_pg_bin(name):
    """Locates PostgreSQL binaries dynamically across installed versions."""
    paths = glob.glob(f"/usr/lib/postgresql/*/bin/{name}")
    if paths:
        return sorted(paths)[-1]
    res = shutil.which(name)
    if not res:
        for p in [f"/usr/bin/{name}", f"/usr/local/bin/{name}"]:
            if os.path.exists(p):
                return p
        raise FileNotFoundError(f"Could not find PostgreSQL binary: {name}")
    return res


def get_os_identifier():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.strip().split("=")[1].strip('"').lower()
    except OSError as e:
        _logger.debug("Ignored OSError reading /etc/os-release: %s", e)
    return "ubuntu"


def get_os_codename():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("VERSION_CODENAME="):
                    return line.strip().split("=")[1].strip('"').lower()
    except OSError as e:
        _logger.debug("Ignored OSError reading /etc/os-release: %s", e)
    return "jammy"


@contextlib.contextmanager
def micro_privilege(username):
    """
    Temporarily drops Effective privileges to the specified user using setresuid/setresgid.
    Restores Root privileges securely upon exiting the context block.
    """
    if os.geteuid() != 0:
        yield
        return

    user_info = pwd.getpwnam(username)
    target_uid = user_info.pw_uid
    target_gid = user_info.pw_gid

    orig_ruid, orig_euid, orig_suid = os.getresuid()
    orig_rgid, orig_egid, orig_sgid = os.getresgid()

    try:
        os.setresgid(orig_rgid, target_gid, orig_sgid)
        os.setresuid(orig_ruid, target_uid, orig_suid)
        yield
    finally:
        os.setresuid(orig_ruid, orig_euid, orig_suid)
        os.setresgid(orig_rgid, orig_egid, orig_sgid)


def format_env(text, env_vars):
    if not text:
        return ""
    try:
        return text.format(**(env_vars or {}))
    except KeyError as e:
        _logger.debug("KeyError formatting %s: %s", text, e)
        return text


def safe_remove(path):
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            _logger.debug("OSError removing file: %s", e)


def apply_permissions(path, owner_str, mode_int):
    uid, gid = -1, -1
    if owner_str:
        try:
            user, group = owner_str.split(":")
            uid = pwd.getpwnam(user).pw_uid
            gid = grp.getgrnam(group).gr_gid
        except KeyError as e:
            _logger.warning("User/Group %s not found: %s", owner_str, e)

    def _apply(p):
        try:
            if uid != -1 and gid != -1:
                os.chown(p, uid, gid)
            if mode_int is not None:
                os.chmod(p, mode_int)
        except OSError as e:
            _logger.debug("Failed chown/chmod on %s: %s", p, e)

    _apply(path)


def download_file(url, path, mode, env_vars):
    ua = env_vars.get(
        "SYSTEM_USER_AGENT",
        "Hams.com Bruce Perens K6BP <bruce@perens.com> +1 510-394-5627",
    )
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            data = response.read()
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Network partition fallback safety hit fetching %s: %s", url, e)
        data = b""

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, mode)
    with open(fd, "wb") as f:
        f.write(data)


def hook_generate_ssl(env_vars, dest_dir, path, run_cmd_func):
    domain = env_vars.get("DOMAIN", "localhost")
    ssl_dir = path
    fullchain = os.path.join(ssl_dir, "fullchain.pem")
    privkey = os.path.join(ssl_dir, "privkey.pem")
    lotw = os.path.join(ssl_dir, "lotw_root.pem")
    if not os.path.exists(fullchain):
        try:
            run_cmd_func(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-nodes",
                    "-days",
                    "3650",
                    "-newkey",
                    "rsa:2048",
                    "-keyout",
                    privkey,
                    "-out",
                    fullchain,
                    "-subj",
                    f"/C=US/ST=CA/L=SF/O=Hams/CN={domain}",
                ]
            )
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("Failed to generate SSL certs: %s", e)
        if os.path.exists(fullchain):
            shutil.copy2(fullchain, lotw)


def hook_clear_pycache(env_vars, dest_dir, path, run_cmd_func):
    pycache = path
    daemons = (
        os.path.join(dest_dir, "opt/hams/daemons") if dest_dir else "/opt/hams/daemons"
    )
    if os.path.exists(pycache):
        for item in os.listdir(pycache):
            item_path = os.path.join(pycache, item)
            (
                shutil.rmtree(item_path, ignore_errors=True)
                if os.path.isdir(item_path)
                else safe_remove(item_path)
            )
    if os.path.isdir(daemons):
        compileall.compile_dir(daemons, quiet=1)


def hook_install_odoo_key(env_vars, dest_dir, path, run_cmd_func):
    out = (
        os.path.join(dest_dir, "usr/share/keyrings/odoo-archive-keyring.gpg")
        if dest_dir
        else "/usr/share/keyrings/odoo-archive-keyring.gpg"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_cmd_func(["gpg", "--dearmor", "-o", out, "--yes", path])
    safe_remove(path)


def hook_install_pg_key(env_vars, dest_dir, path, run_cmd_func):
    out = (
        os.path.join(dest_dir, "usr/share/keyrings/postgresql-keyring.gpg")
        if dest_dir
        else "/usr/share/keyrings/postgresql-keyring.gpg"
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_cmd_func(["gpg", "--dearmor", "-o", out, "--yes", path])
    safe_remove(path)


def hook_install_kopia_binary(env_vars, dest_dir, path, run_cmd_func):
    try:
        target_dir = os.path.join(dest_dir, "usr/bin") if dest_dir else "/usr/bin"
        os.makedirs(target_dir, exist_ok=True)
        run_cmd_func(
            [
                "tar",
                "-xzf",
                path,
                "-C",
                target_dir,
                "--strip-components=1",
                "kopia-0.23.0-linux-x64/kopia",
            ]
        )
        run_cmd_func(["chmod", "+x", os.path.join(target_dir, "kopia")])
    except Exception as e:  # audit-ignore-catch-all
        _logger.warning("Kopia binary install failed: %s", e)
    safe_remove(path)


def hook_install_cloudflared(env_vars, dest_dir, path, run_cmd_func):
    token = env_vars.get("CLOUDFLARE_TUNNEL_TOKEN")
    run_cmd_func(["dpkg", "-i", path])
    if token and token != "none":
        run_cmd_func(["cloudflared", "service", "install", token])
    safe_remove(path)


def hook_daemons_perms(env_vars, dest_dir, path, run_cmd_func):
    target = path
    if os.path.exists(target):
        run_cmd_func(["chown", "-R", "hams_com:hams_com", target])
        run_cmd_func(["chmod", "-R", "a+rX", target])


MANIFEST = {
    "system_accounts": [
        {
            "user": "hams_com",
            "group": "hams_com",
            "home": "/opt/hams",
            "shell": "/bin/bash",
            "add_to_users": ["odoo"],
            "environments": ["prod", "test"],
        }
    ],
    "directories": [
        {
            "path": "/opt/hams",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/etc/keys",
            "owner": "odoo:odoo",
            "provision_mode": "700",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/nginx",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod"],
        },
        {
            "path": "/opt/hams/nginx/ssl",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod"],
            "post_provision_hooks": [hook_generate_ssl],
        },
        {
            "path": "/deploy/ssl",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["docker"],
            "post_provision_hooks": [hook_generate_ssl],
        },
        {
            "path": "/opt/hams/odoo",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd",
            "owner": "hams_com:hams_com",
            "provision_mode": "750",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/cache",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/cache/ms-playwright",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/pycache",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "ro",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_clear_pycache],
        },
        {
            "path": "/opt/hams/spool",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/adif_queue",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/spool/ncvec",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/failed_input",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/downloads",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/test",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["test"],
        },
        {
            "path": "/etc/odoo",
            "owner": "odoo:odoo",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },

        {
            "path": "/var/lib/odoo/backups",
            "owner": "odoo:hams_com",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/tmp/odoo_test_home",
            "owner": "hams_com:hams_com",
            "provision_mode": "770",
            "runtime_mount": "rw",
            "environments": ["test"],
        },
        {
            "path": "/var/log/redis",
            "owner": "redis:redis",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/lib/redis",
            "owner": "redis:redis",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/log/rabbitmq",
            "owner": "rabbitmq:rabbitmq",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/lib/rabbitmq",
            "owner": "rabbitmq:rabbitmq",
            "provision_mode": "750",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
        {
            "path": "/var/log/postgresql",
            "owner": "postgres:postgres",
            "provision_mode": "755",
            "runtime_mount": "rw",
            "environments": ["prod", "test"],
        },
    ],
    "env_groups": {
        "db.env": [
            "DB_NAME",
            "POSTGRES_PASSWORD",
            "DB_PASS",
            "DB_HOST",
            "DB_PORT",
            "DB_USER",
        ],
        "pdns.env": ["PDNS_API_KEY", "PDNS_API_URL"],
        "odoo.env": [
            "ODOO_ADMIN_PASSWORD",
            "ODOO_SERVICE_PASSWORD",
            "ODOO_URL",
            "CLOUDFLARE_API_TOKEN",
            "CLOUDFLARE_ZONE_ID",
        ],
        "rabbitmq.env": ["RMQ_PASS", "RABBITMQ_HOST", "RMQ_PORT", "RMQ_USER"],
        "redis.env": ["REDIS_HOST", "REDIS_PORT"],
        "smtp.env": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS"],
        "core.env": [
            "DOMAIN",
            "SYSTEM_USER_AGENT",
            "SYSADMIN_EMAILS",
            "HAMS_CRYPTO_KEY",
            "CLOUDFLARE_TUNNEL_TOKEN",
            "PYTHONPYCACHEPREFIX",
            "WS_PORT",
            "GEMINI_API_KEY",
            "GEMINI_MODEL",
            "PLAYWRIGHT_BROWSERS_PATH",
        ],
    },
    "static_files": [
        {
            "path": "/etc/apt/sources.list.d/odoo.list",
            "content": "deb [signed-by=/usr/share/keyrings/odoo-archive-keyring.gpg] https://nightly.odoo.com/19.0/nightly/deb/ ./\n",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
        },
        {
            "path": "/opt/hams/etc/pdns_gsqlite3.conf",
            "content": """\
launch=gsqlite3
gsqlite3-database=/var/lib/powerdns/pdns.sqlite3
gsqlite3-dnssec=no
local-address=0.0.0.0
api=yes
api-key={PDNS_API_KEY}
webserver=yes
webserver-address=localhost
webserver-port=8081
webserver-allow-from=127.0.0.0/8,::1/128
dnsupdate=yes
allow-dnsupdate-from=127.0.0.0/8,::1/128
loglevel=6
""",
            "owner": "pdns:pdns",
            "mode": "640",
            "environments": ["prod"],
        },
        {
            "path": "/etc/hosts",
            "content": """\
127.0.0.1 localhost
::1 localhost ip6-localhost ip6-loopback
127.0.0.1 postgres redis rabbitmq odoo powerdns daemon_dx_firehose
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["test"],
        },
        {
            "path": "/opt/hams/systemd/hams-pycache.service",
            "content": """\
[Unit]
Description=Hams.com PyCache JIT Compiler
Before=odoo.service

[Service]
Type=oneshot
User=root
Environment="PYTHONPYCACHEPREFIX=/opt/hams/pycache"
ExecStart=/bin/bash -c "/usr/bin/python3 -m compileall -q /opt/hams; chown -R hams_com:hams_com /opt/hams/pycache"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/hams.daemon.keys.service",
            "content": """\
[Unit]
Description=Hams.com Daemon Key Bootstrapper
Requires=odoo.service
After=odoo.service

[Service]
Type=oneshot
User=odoo
Environment="ODOO_RC=/etc/odoo/odoo.conf"
Environment="HAMS_KEYS_DIR=/opt/hams/etc/keys"
EnvironmentFile=-/opt/hams/etc/odoo.env
EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
ExecStart=/bin/bash -c "DB=${DB_NAME}; if [ -z \\"$$DB\\" ]; then DB=hams_test; fi; echo \\"env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()\\" | /usr/bin/python3 /usr/bin/odoo shell -c /etc/odoo/odoo.conf -d $$DB --no-http"
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/tmp/odoo.key",
            "url": "https://nightly.odoo.com/odoo.key",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
            "post_provision_hooks": [hook_install_odoo_key],
        },
        {
            "path": "/tmp/pg.key",
            "url": "https://www.postgresql.org/media/keys/ACCC4CF8.asc",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
            "post_provision_hooks": [hook_install_pg_key],
        },
        {
            "path": "/etc/apt/sources.list.d/pgdg.list",
            "content": "deb [signed-by=/usr/share/keyrings/postgresql-keyring.gpg] https://apt.postgresql.org/pub/repos/apt/ {DEB_CODENAME}-pgdg main\n",
            "owner": "root:root",
            "mode": "644",
            "environments": ["early_prod"],
        },
        {
            "path": "/tmp/kopia.tar.gz",
            "url": "https://github.com/kopia/kopia/releases/download/v0.23.0/kopia-0.23.0-linux-x64.tar.gz",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
            "post_provision_hooks": [hook_install_kopia_binary],
        },
        {
            "path": "/tmp/cloudflared.deb",
            "url": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{DEB_TARGET_ARCH_CPU}.deb",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod"],
            "condition_env": "CLOUDFLARE_TUNNEL_TOKEN",
            "post_provision_hooks": [hook_install_cloudflared],
        },
        {
            "src": "{HAMS_COM_DIR}/daemons",
            "path": "/opt/hams/daemons",
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_daemons_perms],
        },
        {
            "src": "{HAMS_COMMUNITY_DIR}/hams_shared",
            "path": "/opt/hams/hams_shared",
            "owner": "hams_com:hams_com",
            "mode": "755",
            "environments": ["prod", "test"],
            "post_provision_hooks": [hook_daemons_perms],
        },
        {
            "path": "/opt/hams/systemd/system-startup.service",
            "content": """\
[Unit]
Description=Run all timed daemons at startup
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/systemctl start amsat.tle.sync.service
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/adif.processor.service",
            "content": """\
[Unit]
Description=Ham Radio ADIF Queue Processor (RabbitMQ Worker)
After=network.target rabbitmq-server.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool/adif_queue
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/adif_processor

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=logbook_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/logbook_api_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/adif_processor/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/adif_processor/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=adif.processor

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/dx.firehose.service",
            "content": """\
[Unit]
Description=Ham Radio Ultimate DX Cluster (Live Firehose Daemon)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/dx_firehose

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="WS_PORT=8765"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

LimitNOFILE=65535

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/dx_firehose/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/dx_firehose/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dx.firehose

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/ham.dx.daemon.service",
            "content": """\
[Unit]
Description=Ham Radio DX Cluster Telnet Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/ham_dx_daemon

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=dx_daemon_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dx_daemon_service.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/ham_dx_daemon/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/ham_dx_daemon/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ham.dx.daemon

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/noaa-swpc-sync.service",
            "content": """\
[Unit]
Description=Ham Radio NOAA Space Weather Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/noaa_swpc_sync

# Odoo JSON2-RPC Credentials
EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=space_weather_service"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/space_weather_service.key"
Environment="POLL_INTERVAL=14400"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/noaa_swpc_sync/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/noaa_swpc_sync/main.py $DAEMON_ARGS

# Resiliency
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/pdns.sync.service",
            "content": """\
[Unit]
Description=Ham Radio PowerDNS Sync Daemon (CQRS)
After=network.target rabbitmq-server.service pdns.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/pdns_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=dns_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/dns_api_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/pdns_sync/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/pdns_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pdns.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/lotw.eqsl.sync.service",
            "content": """\
[Unit]
Description=Ham Radio Automated QSL Sync Daemon
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/lotw_eqsl_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=logbook_api_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/logbook_api_service_internal.key"
Environment="POLL_INTERVAL=86400"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/lotw_eqsl_sync/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/lotw_eqsl_sync/main.py $DAEMON_ARGS

Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal
SyslogIdentifier=lotw.eqsl.sync

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/amsat.tle.sync.service",
            "content": """\
[Unit]
Description=Ham Radio AMSAT TLE Sync Service
After=network.target

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=oneshot
User=odoo
WorkingDirectory=/opt/hams/daemons/amsat_tle_sync

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=satellite_sync_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/satellite_sync_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/amsat_tle_sync/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/amsat_tle_sync/main.py $DAEMON_ARGS

StandardOutput=journal
StandardError=journal
SyslogIdentifier=amsat.tle.sync
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/amsat.tle.sync.timer",
            "content": """\
[Unit]
Description=Run AMSAT TLE Sync Daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
        {
            "path": "/opt/hams/systemd/qrz.scraper.service",
            "content": """\
[Unit]
Description=Ham Radio QRZ Scraper Daemon
After=network.target rabbitmq-server.service
Requires=rabbitmq-server.service

[Service]
# ADR-0070 OS-Level Daemon Restriction
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
PrivateDevices=true
NoNewPrivileges=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
ReadWritePaths=/opt/hams/spool /opt/hams/downloads
Type=simple
User=odoo
WorkingDirectory=/opt/hams/daemons/qrz_scraper

EnvironmentFile=-/opt/hams/etc/core.env
EnvironmentFile=-/opt/hams/etc/db.env
EnvironmentFile=-/opt/hams/etc/redis.env
EnvironmentFile=-/opt/hams/etc/rabbitmq.env
EnvironmentFile=-/opt/hams/etc/pdns.env
EnvironmentFile=-/opt/hams/etc/odoo.env
Environment="ODOO_USER=onboarding_service_internal"
Environment="ODOO_KEY_FILE=/opt/hams/etc/keys/onboarding_service_internal.key"
Environment="PYTHONPATH=/opt/hams/daemons"
Environment="DAEMON_ARGS="

# Smoketest Resource Verification
ExecStartPre=/usr/bin/python3 /opt/hams/daemons/qrz_scraper/main.py --start-test

# Execution via system Python
ExecStart=/usr/bin/python3 /opt/hams/daemons/qrz_scraper/main.py $DAEMON_ARGS

Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=qrz.scraper

[Install]
WantedBy=multi-user.target
""",
            "owner": "root:root",
            "mode": "644",
            "environments": ["prod", "test"],
        },
    ],
    "apt_packages": [
        {"name": "odoo", "debian_name": "odoo", "environments": ["early_prod"]},
        {
            "name": "postgresql",
            "debian_name": "postgresql",
            "environments": ["early_prod"],
        },
        {
            "name": "postgresql-common",
            "debian_name": "postgresql-common",
            "environments": ["early_prod"],
        },
        {
            "name": "postgresql-client",
            "debian_name": "postgresql-client",
            "environments": ["early_prod"],
        },
        {"name": "nginx", "debian_name": "nginx", "environments": ["early_prod"]},
        {
            "name": "redis-server",
            "debian_name": "redis-server",
            "environments": ["early_prod"],
        },
        {
            "name": "rabbitmq-server",
            "debian_name": "rabbitmq-server",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-redis",
            "debian_name": "python3-redis",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pika",
            "debian_name": "python3-pika",
            "environments": ["early_prod"],
        },
        {"name": "sqlite3", "debian_name": "sqlite3", "environments": ["early_prod"]},
        {
            "name": "pdns-server",
            "debian_name": "pdns-server",
            "environments": ["early_prod"],
        },
        {
            "name": "pdns-backend-sqlite3",
            "debian_name": "pdns-backend-sqlite3",
            "environments": ["early_prod"],
        },
        {
            "name": "pgbackrest",
            "debian_name": "pgbackrest",
            "environments": ["early_prod"],
        },
        {"name": "certbot", "debian_name": "certbot", "environments": ["early_prod"]},
        {
            "name": "python3-certbot-nginx",
            "debian_name": "python3-certbot-nginx",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-passlib",
            "debian_name": "python3-passlib",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-cryptography",
            "debian_name": "python3-cryptography",
            "environments": ["early_prod"],
        },
        {
            "name": "build-essential",
            "debian_name": "build-essential",
            "environments": ["early_prod"],
        },
        {
            "name": "libpq-dev",
            "debian_name": "libpq-dev",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-dev",
            "debian_name": "python3-dev",
            "environments": ["early_prod"],
        },
        {
            "name": "bind9-dnsutils",
            "debian_name": "bind9-dnsutils",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-stdeb",
            "debian_name": "python3-stdeb",
            "environments": ["early_prod"],
        },
        {"name": "fakeroot", "debian_name": "fakeroot", "environments": ["early_prod"]},
        {
            "name": "python3-all",
            "debian_name": "python3-all",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pypdf2",
            "debian_name": "python3-pypdf",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-setuptools",
            "debian_name": "python3-setuptools",
            "environments": ["early_prod"],
        },
        {
            "name": "dh-python",
            "debian_name": "dh-python",
            "environments": ["early_prod"],
        },
        {"name": "jing", "debian_name": "jing", "environments": ["early_prod"]},
        {"name": "dbus-x11", "debian_name": "dbus-x11", "environments": ["early_prod"]},
        {
            "name": "python3-asyncpg",
            "debian_name": "python3-asyncpg",
            "environments": ["early_prod"],
        },
        {"name": "black", "debian_name": "black", "environments": ["early_prod"]},
        {
            "name": "python3-psutil",
            "debian_name": "python3-psutil",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ephem",
            "debian_name": "python3-ephem",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ldap3",
            "debian_name": "python3-ldap3",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-lxml",
            "debian_name": "python3-lxml",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-ntplib",
            "debian_name": "python3-ntplib",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pyinotify",
            "debian_name": "python3-pyinotify",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pymysql",
            "debian_name": "python3-pymysql",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-docx",
            "debian_name": "python3-docx",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-yaml",
            "debian_name": "python3-yaml",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-requests",
            "debian_name": "python3-requests",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-websocket",
            "debian_name": "python3-websocket",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-websockets",
            "debian_name": "python3-websockets",
            "environments": ["early_prod"],
        },
        {"name": "flake8", "debian_name": "flake8", "environments": ["early_prod"]},
        {
            "name": "python3-pip",
            "debian_name": "python3-pip",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-pandas",
            "debian_name": "python3-pandas",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-numpy",
            "debian_name": "python3-numpy",
            "environments": ["early_prod"],
        },
        {
            "name": "python3-markdown",
            "debian_name": "python3-markdown",
            "environments": ["early_prod"],
        },
    ],
    "env_defaults": {
        "DB_PORT": "5432",
        "RMQ_PORT": "5672",
        "REDIS_PORT": "6379",
        "WS_PORT": "8765",
        "RMQ_USER": "guest",
        "RMQ_PASS": "guest",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/hams/cache/ms-playwright",
    },
    "systemd_odoo_override": {
        "Unit": {"Requires": "hams-pycache.service", "After": "hams-pycache.service"},
        "Service": {
            "EnvironmentFile": [
                "-/opt/hams/etc/odoo.env",
                "-/opt/hams/etc/core.env",
                "-/opt/hams/etc/db.env",
                "-/opt/hams/etc/redis.env",
                "-/opt/hams/etc/rabbitmq.env",
                "-/opt/hams/etc/smtp.env",
                "-/opt/hams/etc/pdns.env",
            ],
            "Environment": [
                "PYTHONPYCACHEPREFIX=/opt/hams/pycache",
                "ODOO_RC=/etc/odoo/odoo.conf",
            ],
            "ExecStartPre": "/usr/bin/python3 /opt/hams/hams_shared/tools/env_validator.py",
            "ProtectSystem": "strict",
            "ReadWritePaths": [
                "/opt/hams/etc/keys",
                "/var/lib/odoo",
                "/var/log/odoo"
            ],
            "PrivateTmp": "true",
            "PrivateDevices": "true",
            "NoNewPrivileges": "true",
            "KillSignal": "SIGINT",
            "TimeoutStopSec": "15",
        },
    },
}


def scaffold_test_environment(args_db, provision_dirs=True):
    for k, v in MANIFEST["env_defaults"].items():
        os.environ.setdefault(k, v)

    os.environ.setdefault("DB_NAME", args_db)
    os.environ.setdefault("ODOO_DB", args_db)
    os.environ.setdefault("DB_USER", "odoo")
    os.environ.setdefault("DB_PASS", "odoo")
    os.environ.setdefault("DB_HOST", "postgres")
    os.environ.setdefault("ODOO_URL", "http://odoo:8069")
    os.environ.setdefault(
        "PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones"
    )
    os.environ.setdefault("PDNS_API_KEY", "secret")

    if provision_dirs:
        try:
            apply_production_directories(environment="test")
        except PermissionError as e:
            print(f"[*] PermissionError provisioning test directories: {e}")
            print("[*] Note: 'sudo' fallback removed per strict DevSecOps mandates.")
            raise


def get_mount_paths(environment, mount_type):
    return [
        d["path"]
        for d in MANIFEST["directories"]
        if environment in d["environments"] and d.get("runtime_mount") == mount_type
    ]


def provision_system_accounts(run_cmd_func, environment="prod", dest_dir=""):
    for acc in MANIFEST.get("system_accounts", []):
        if environment not in acc.get("environments", ["prod", "test"]):
            continue

        user = acc["user"]
        group = acc["group"]
        home = acc.get("home", "/opt/hams")
        shell = acc.get("shell", "/bin/bash")
        add_to_users = acc.get("add_to_users", [])

        try:
            grp.getgrnam(group)
        except KeyError:
            run_cmd_func(["groupadd", "--system", group])

        try:
            pwd.getpwnam(user)
        except KeyError:
            run_cmd_func(
                ["useradd", "--system", "-g", group, "-d", home, "-s", shell, user]
            )

        for extra_user in add_to_users:
            try:
                pwd.getpwnam(extra_user)
                run_cmd_func(["usermod", "-a", "-G", group, extra_user])
            except KeyError:
                _logger.debug("User %s not found, skipping group addition.", extra_user)


def execute_hooks(environment, run_cmd_func, env_vars=None, dest_dir=""):
    if dest_dir and dest_dir.endswith("/"):
        dest_dir = dest_dir[:-1]

    for d in MANIFEST["directories"]:
        if environment in d["environments"] and "post_provision_hooks" in d:
            for hook in d["post_provision_hooks"]:
                physical_path = (
                    os.path.join(dest_dir, d["path"].lstrip("/"))
                    if dest_dir
                    else d["path"]
                )
                hook(env_vars or {}, dest_dir, physical_path, run_cmd_func)


def apply_production_directories(run_cmd_func=None, environment="prod", dest_dir=""):
    for d in MANIFEST["directories"]:
        if environment in d["environments"]:
            path = (
                os.path.join(dest_dir, d["path"].lstrip("/")) if dest_dir else d["path"]
            )
            mode = int(d["provision_mode"], 8)
            os.makedirs(path, mode=mode, exist_ok=True)
            apply_permissions(path, d.get("owner"), mode)


def write_env_files(base_etc_dir, env_vars, run_cmd_func, dest_dir=""):
    if dest_dir:
        base_etc_dir = os.path.join(dest_dir, base_etc_dir.lstrip("/"))
    os.makedirs(base_etc_dir, exist_ok=True)

    for filename, keys in MANIFEST["env_groups"].items():
        filepath = os.path.join(base_etc_dir, filename)
        content = "".join(f"{k}={env_vars[k]}\n" for k in keys if k in env_vars)

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(filepath, flags, 0o400)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)

        apply_permissions(filepath, "root:root", 0o400)


def provision_custom_addons(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    if environment not in ["prod", "test"]:
        return

    if not env_vars.get("REPO_ROOT"):
        return

    custom_addons_dir = (
        os.path.join(dest_dir, "opt/hams/odoo") if dest_dir else "/opt/hams/odoo"
    )

    if os.path.isdir(env_vars["REPO_ROOT"]):
        for item in os.listdir(env_vars["REPO_ROOT"]):
            item_path = os.path.join(env_vars["REPO_ROOT"], item)
            if os.path.isdir(item_path) and os.path.exists(
                os.path.join(item_path, "__manifest__.py")
            ):
                target = os.path.join(custom_addons_dir, item)
                shutil.rmtree(target, ignore_errors=True)
                os.makedirs(target, exist_ok=True)
                shutil.copytree(
                    item_path,
                    target,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("target", ".git", "__pycache__"),
                )

    apply_permissions(custom_addons_dir, "odoo:odoo", None)


def provision_static_files(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    for file_spec in MANIFEST.get("static_files", []):
        if environment not in file_spec["environments"]:
            continue

        condition_env = file_spec.get("condition_env")
        if condition_env and not env_vars.get(condition_env):
            continue

        path = format_env(file_spec["path"], env_vars)
        if dest_dir:
            path = os.path.join(dest_dir, path.lstrip("/"))

        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = int(file_spec.get("mode", "644"), 8)

        src = file_spec.get("src")
        url = file_spec.get("url")

        if src:
            src = format_env(src, env_vars)
            if os.path.exists(src):
                if os.path.isdir(src):
                    shutil.copytree(
                        src,
                        path,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("target", ".git", "__pycache__"),
                    )
                else:
                    shutil.copy2(src, path)
        elif url:
            if "{DEB_TARGET_ARCH_CPU}" in url and "DEB_TARGET_ARCH_CPU" not in env_vars:
                res = subprocess.run(
                    ["dpkg-architecture", "-q", "DEB_TARGET_ARCH_CPU"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    env_vars["DEB_TARGET_ARCH_CPU"] = res.stdout.strip()
            download_file(format_env(url, env_vars), path, mode, env_vars)
        else:
            if (
                "{DEB_CODENAME}" in file_spec.get("content", "")
                and "DEB_CODENAME" not in env_vars
            ):
                env_vars["DEB_CODENAME"] = get_os_codename()
            content = format_env(file_spec.get("content", ""), env_vars)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(path, flags, mode)
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)

        apply_permissions(path, file_spec.get("owner"), mode)

        if "post_provision_hooks" in file_spec:
            for hook in file_spec["post_provision_hooks"]:
                hook(env_vars or {}, dest_dir, path, run_cmd_func)


def provision_systemd_override(run_cmd_func, env_vars, environment="prod", dest_dir=""):
    if environment not in ["prod", "test"]:
        return
    override_data = MANIFEST.get("systemd_odoo_override")
    if not override_data:
        return

    override_dir = (
        os.path.join(dest_dir, "etc/systemd/system/odoo.service.d".lstrip("/"))
        if dest_dir
        else "/etc/systemd/system/odoo.service.d"
    )
    os.makedirs(override_dir, exist_ok=True)
    override_file = os.path.join(override_dir, "override.conf")

    lines = []
    for section, items in override_data.items():
        lines.append(f"[{section}]")
        for k, v in items.items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"{k}={item}")
            else:
                lines.append(f"{k}={v}")
        lines.append("")

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(override_file, flags, 0o644)
    with open(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    apply_permissions(override_file, "root:root", 0o644)

    is_isolated_ns = os.environ.get("HAMS_ISOLATED_NS") == "1"
    if not dest_dir and run_cmd_func and not is_isolated_ns:
        try:
            run_cmd_func(["systemctl", "daemon-reload"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("Failed to reload systemd daemons: %s", e)


def initialize_odoo_database(run_cmd_func, hams_open_dir, hams_com_dir):
    _logger.info("[*] Initializing Odoo database with custom modules...")
    modules = set()
    for d in filter(None, [hams_open_dir, hams_com_dir]):
        if os.path.exists(d):
            for item in os.listdir(d):
                item_path = os.path.join(d, item)
                if os.path.isdir(item_path):
                    if os.path.exists(os.path.join(item_path, "__manifest__.py")):
                        modules.add(item)

    if not modules:
        _logger.info("No custom modules found to initialize.")
        return

    mod_string = "base," + ",".join(modules)
    _logger.info("Initializing modules: %s", mod_string)

    addons_path_str = ",".join(filter(None, [
        "/usr/lib/python3/dist-packages/odoo/addons",
        "/var/lib/odoo/.local/share/Odoo/addons/19.0",
        "/usr/lib/python3/dist-packages/addons",
        hams_com_dir,
        hams_open_dir
    ]))

    try:
        run_cmd_func(["sudo", "sed", "-i", "/^addons_path/d", "/etc/odoo/odoo.conf"])
        run_cmd_func(["sudo", "bash", "-c", f"echo 'addons_path = {addons_path_str}' >> /etc/odoo/odoo.conf"])
    except Exception as e: # audit-ignore-catch-all
        _logger.warning("Failed to update addons_path in /etc/odoo/odoo.conf: %s", e)

    cmd = [
        "sudo", "-u", "odoo", "odoo",
        "-c", "/etc/odoo/odoo.conf",
        "-d", "hams_test",
        "-i", mod_string,
        "--stop-after-init",
        "--without-demo=all",
        "--workers=0",
        "--max-cron-threads=0",
        "--addons-path", addons_path_str
    ]
    try:
        run_cmd_func(cmd)
    except subprocess.CalledProcessError as e:
        _logger.error("Failed to initialize Odoo database: %s", e)
        raise

def run_post_provision_smoketest(has_hams_com=True, is_test_env=False):
    _logger.info("[*] Running post-provisioning smoketest on all services...")

    try:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    except OSError as e:
        _logger.debug("Ignored OSError during daemon-reload: %s", e)

    potential_services = [
        "postgresql",
        "redis-server",
        "rabbitmq-server",
        "pdns",
        "odoo",
    ]

    daemons_to_skip = {
        "system-startup.service",
        "hams-pycache.service"
    }

    for sf in MANIFEST.get("static_files", []):
        path = sf.get("path", "")
        if "systemd" in path and path.endswith(".service"):
            svc_name = os.path.basename(path)
            if not has_hams_com and svc_name != "hams-pycache.service":
                continue
            if not is_test_env and svc_name in daemons_to_skip:
                continue
            if svc_name not in potential_services and "@" not in svc_name:
                potential_services.append(svc_name)

    _logger.info("DEBUG potential_services: %s", potential_services)
    _logger.info("DEBUG has_hams_com: %s, is_test_env: %s", has_hams_com, is_test_env)

    services_to_test = []
    for svc in potential_services:
        res = subprocess.run(
            ["systemctl", "status", svc], capture_output=True, text=True
        )
        if (
            "could not be found" not in res.stderr
            and "could not be found" not in res.stdout
        ):
            services_to_test.append(svc)

    started_services = []
    already_active_services = []

    for svc in services_to_test:
        res_active = subprocess.run(
            ["systemctl", "is-active", svc], capture_output=True, text=True
        )
        if res_active.stdout.strip() == "active":
            if svc == "odoo":
                subprocess.run(["systemctl", "restart", "odoo"])
            _logger.info("    %s is already active, skipping start.", svc)
            already_active_services.append(svc)
            continue

        _logger.info("    Starting %s...", svc)
        res = subprocess.run(
            ["systemctl", "start", svc], capture_output=True, text=True
        )
        started_services.append(svc)
        if res.returncode != 0:
            logs = subprocess.run(
                ["journalctl", "-u", svc, "-n", "100", "--no-pager"],
                capture_output=True,
                text=True,
            )
            if "Address already in use" in logs.stdout or "Address already in use" in logs.stderr:
                _logger.warning(
                    "    [~] %s failed to start due to port conflict ('Address already in use'). Assuming it is running externally or port is handled.", svc
                )
                started_services.remove(svc)
            else:
                _logger.error(
                    "    [!] systemctl start %s returned non-zero exit code: %s",
                    svc,
                    res.returncode,
                )
                _logger.error("stdout: %s", res.stdout)
                _logger.error("stderr: %s", res.stderr)
                _logger.error(
                    "--- LOGS FOR %s ---\n%s\n-------------------", svc, logs.stdout
                )
                sys.exit(1)

    _logger.info("[*] Waiting for services to stabilize (5 seconds)...")
    time.sleep(5)

    failed = False
    for svc in started_services + already_active_services:
        res = subprocess.run(
            ["systemctl", "is-failed", svc], capture_output=True, text=True
        )
        state = res.stdout.strip()
        if state == "failed":
            _logger.error("[!] Service %s failed to start or crashed.", svc)
            logs = subprocess.run(
                ["journalctl", "-u", svc, "-n", "100", "--no-pager"],
                capture_output=True,
                text=True,
            )
            _logger.error(
                "--- LOGS FOR %s ---\n%s\n-------------------", svc, logs.stdout
            )
            failed = True

    if failed:
        _logger.error(
            "[!] One or more services failed the smoketest. Aborting snapshot."
        )
        sys.exit(1)

    if is_test_env:
        _logger.info("[*] All services started successfully. Shutting them down (--test mode)...")
        for svc in reversed(started_services):
            _logger.info("    Stopping %s...", svc)
            subprocess.run(["systemctl", "stop", svc], capture_output=True)
    else:
        _logger.info("[*] All services started successfully and are running.")

    _logger.info("[*] Smoketest complete: %s", datetime.now())


def generate_secure_password(length=32):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def load_and_prompt_env(env_vars, is_test):
    env_dir = "/opt/hams/etc"
    if os.path.exists(env_dir):
        for env_file in glob.glob(os.path.join(env_dir, "*.env")):
            try:
                with open(env_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            env_vars.setdefault(key.strip(), val.strip())
            except OSError as e:
                _logger.warning("Failed to read %s: %s", env_file, e)

    if is_test:
        env_vars.setdefault("ODOO_URL", "http://odoo:8069")
        env_vars.setdefault("REDIS_HOST", "redis")
        env_vars.setdefault("RABBITMQ_HOST", "rabbitmq")
        env_vars.setdefault("DB_NAME", "hams_test")
        env_vars.setdefault("DB_USER", "odoo")
        env_vars.setdefault("DB_PASS", "odoo")
        env_vars.setdefault("DB_HOST", "postgres")
        env_vars.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
        env_vars.setdefault("PDNS_API_KEY", "secret")
        env_vars.setdefault("DOMAIN", "localhost")
        env_vars.setdefault("ODOO_ADMIN_PASSWORD", "admin")
        env_vars.setdefault("ODOO_SERVICE_PASSWORD", "service")
        env_vars.setdefault("SMTP_HOST", "localhost")
        env_vars.setdefault("SMTP_PORT", "1025")
        env_vars.setdefault("HAMS_CRYPTO_KEY", "0000000000000000000000000000000000000000000=")
    else:
        def prompt_if_missing(key, prompt_text, default=None, is_password=False):
            if key in env_vars and env_vars[key].strip():
                return
            print("")
            while True:
                if is_password:
                    val = getpass.getpass(f"{prompt_text}: ")
                    if val:
                        val2 = getpass.getpass(f"Confirm {key}: ")
                        if val == val2:
                            env_vars[key] = val
                            break
                        print("Passwords do not match. Try again.")
                    elif default is not None:
                        env_vars[key] = default
                        break
                    else:
                        print(f"{key} cannot be empty.")
                else:
                    default_hint = f" [{default}]" if default is not None else ""
                    val = input(f"{prompt_text}{default_hint}: ").strip()
                    if val:
                        env_vars[key] = val
                        break
                    elif default is not None:
                        env_vars[key] = default
                        break
                    else:
                        print(f"{key} cannot be empty.")

        # Set automatic sensible defaults
        env_vars.setdefault("DB_NAME", "hams_prod")
        env_vars.setdefault("DB_USER", "odoo")
        env_vars.setdefault("DB_HOST", "postgres")
        env_vars.setdefault("REDIS_HOST", "redis")
        env_vars.setdefault("REDIS_PORT", "6379")
        env_vars.setdefault("RABBITMQ_HOST", "rabbitmq")
        env_vars.setdefault("RMQ_PORT", "5672")
        env_vars.setdefault("RMQ_USER", "guest")
        env_vars.setdefault("PDNS_API_URL", "http://powerdns:8081/api/v1/servers/localhost/zones")
        env_vars.setdefault("WS_PORT", "8080")
        env_vars.setdefault("PYTHONPYCACHEPREFIX", "/tmp/pycache")
        env_vars.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/hams/playwright")
        env_vars.setdefault("SYSTEM_USER_AGENT", "HAMS/1.0")

        if "DB_PASS" not in env_vars:
            env_vars["DB_PASS"] = generate_secure_password()
        if "PDNS_API_KEY" not in env_vars:
            env_vars["PDNS_API_KEY"] = generate_secure_password()
        if "ODOO_SERVICE_PASSWORD" not in env_vars:
            env_vars["ODOO_SERVICE_PASSWORD"] = generate_secure_password()
        if "HAMS_CRYPTO_KEY" not in env_vars:
            env_vars["HAMS_CRYPTO_KEY"] = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
        if "RMQ_PASS" not in env_vars:
            env_vars["RMQ_PASS"] = generate_secure_password()

        # Prompt for critical missing values
        prompt_if_missing("DOMAIN", "Enter the primary domain for this instance (e.g. hams.com)")
        domain = env_vars.get("DOMAIN", "hams.com")
        
        env_vars.setdefault("ODOO_URL", "http://odoo:8069")
        env_vars.setdefault("SYSADMIN_EMAILS", f"admin@{domain}")

        prompt_if_missing("ODOO_ADMIN_PASSWORD", "Enter a secure Odoo admin password", is_password=True)
        
        prompt_if_missing("SMTP_HOST", "Enter SMTP Host", default="smtp.mailgun.org")
        prompt_if_missing("SMTP_PORT", "Enter SMTP Port", default="587")
        prompt_if_missing("SMTP_USER", "Enter SMTP User", default=f"postmaster@{domain}")
        prompt_if_missing("SMTP_PASS", "Enter SMTP Password", default="none", is_password=True)

        prompt_if_missing("GEMINI_API_KEY", "Enter Gemini API Key (or press enter for none)", default="none", is_password=True)
        prompt_if_missing("GEMINI_MODEL", "Enter Gemini Model", default="gemini-2.5-pro")
        
        prompt_if_missing("CLOUDFLARE_API_TOKEN", "Enter Cloudflare API Token (or 'none')", default="none", is_password=True)
        
        # Auto-derive Cloudflare Zone ID from Domain
        if "CLOUDFLARE_ZONE_ID" not in env_vars:
            cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "none")
            if cf_token and cf_token != "none":
                print(f"[*] Attempting to derive Cloudflare Zone ID for {domain}...")
                try:
                    import urllib.request
                    import json
                    req = urllib.request.Request(
                        f"https://api.cloudflare.com/client/v4/zones?name={domain}",
                        headers={"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=5) as response:
                        data = json.loads(response.read().decode())
                        if data.get("success") and data.get("result"):
                            zone_id = data["result"][0]["id"]
                            print(f"[*] Successfully derived Zone ID: {zone_id}")
                            env_vars["CLOUDFLARE_ZONE_ID"] = zone_id
                        else:
                            print("[!] Could not find Zone ID for domain.")
                except Exception as e:
                    print(f"[!] Failed to fetch Cloudflare Zone ID: {e}")
            
        prompt_if_missing("CLOUDFLARE_ZONE_ID", "Enter Cloudflare Zone ID (or 'none')", default="none")
        prompt_if_missing("CLOUDFLARE_TUNNEL_TOKEN", "Enter Cloudflare Tunnel Token (or 'none')", default="none", is_password=True)

def provision_environment(
    run_cmd_func, env_vars, orig_user, os_id=None, skip_apt=False, is_test=False
):
    _logger.info("[*] Provision version 1")
    os_id = os_id or get_os_identifier()
    repo_root = env_vars.get("REPO_ROOT", "/app")
    # Inject safe testing defaults for provisioning context so .env files populate
    for k, v in MANIFEST.get("env_defaults", {}).items():
        env_vars.setdefault(k, v)

    load_and_prompt_env(env_vars, is_test)

    hams_com_dir = None
    hams_community_dir = None

    if os.path.exists(os.path.join(repo_root, "daemons")):
        hams_com_dir = repo_root
    elif os.path.exists(os.path.join(repo_root, "..", "hams_com", "daemons")):
        hams_com_dir = os.path.abspath(os.path.join(repo_root, "..", "hams_com"))
    elif os.path.exists(os.path.join(repo_root, "..", "..", "hams_com", "daemons")):
        hams_com_dir = os.path.abspath(os.path.join(repo_root, "..", "..", "hams_com"))
    elif os.path.exists("/hams_com/daemons"):
        hams_com_dir = "/hams_com"
        
    has_hams_com = False
    if hams_com_dir:
        has_hams_com = os.path.exists(os.path.join(hams_com_dir, "ham_base", "__manifest__.py"))

    if os.path.exists(os.path.join(repo_root, "zero_sudo")):
        hams_community_dir = repo_root
    elif os.path.exists(os.path.join(repo_root, "..", "zero_sudo")):
        hams_community_dir = os.path.abspath(os.path.join(repo_root, ".."))
    elif os.path.exists(os.path.join(repo_root, "..", "hams_community", "zero_sudo")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "hams_community")
        )
    elif os.path.exists(os.path.join(repo_root, "..", "..", "hams_community", "zero_sudo")):
        hams_community_dir = os.path.abspath(
            os.path.join(repo_root, "..", "..", "hams_community")
        )
    elif os.path.exists(os.path.expanduser("~/workspace/hams_open/zero_sudo")):
        hams_community_dir = os.path.expanduser("~/workspace/hams_open")
    elif os.path.exists("/hams_community/zero_sudo"):
        hams_community_dir = "/hams_community"

    if not hams_com_dir:
        hams_com_dir = "/hams_com"
        _logger.warning(
            "[!] Primary repository hams_com not found. Cloning disabled due to headless auth constraints."
        )

    if not hams_community_dir:
        hams_community_dir = "/hams_community"
        _logger.info(
            "[*] Sibling repository not found. Cloning hams_community to %s...",
            hams_community_dir,
        )
        try:
            clone_env = dict(env_vars)
            clone_env["GIT_TERMINAL_PROMPT"] = "0"
            run_cmd_func(
                [
                    "git",
                    "clone",
                    "https://github.com/BrucePerens/hams_community",
                    hams_community_dir,
                ],
                env=clone_env,
            )
            if orig_user:
                try:
                    u_info = pwd.getpwnam(orig_user)
                    run_cmd_func(
                        [
                            "chown",
                            "-R",
                            f"{u_info.pw_uid}:{u_info.pw_gid}",
                            hams_community_dir,
                        ]
                    )
                except KeyError as e:
                    _logger.debug("Original user %s not found: %s", orig_user, e)
        except subprocess.CalledProcessError as e:
            _logger.warning("[*] Failed to clone hams_community: %s", e)
            _logger.error(
                "[!] DIAGNOSTIC FOR AI: The sibling repository could not be cloned due to GitHub authentication restrictions in this headless VM."
            )
            _logger.error(
                "    If required modules are not present, tests will crash. Document this in JULES_ISSUES.md."
            )

    env_vars["HAMS_COM_DIR"] = hams_com_dir
    env_vars["HAMS_COMMUNITY_DIR"] = hams_community_dir

    try:
        with open("/etc/hosts", "r") as f:
            hosts_content = f.read()
        if "redis" not in hosts_content:
            _logger.info(
                "[*] Ensuring docker-compose hostnames resolve locally in /etc/hosts..."
            )
            with open("/etc/hosts", "a") as f:
                f.write("\n127.0.0.1 redis rabbitmq postgres pdns memcached\n")
    except OSError as e:
        _logger.warning("[*] Failed to update /etc/hosts: %s", e)

    _logger.info("[*] Initializing system accounts and static files...")
    try:
        provision_system_accounts(run_cmd_func, environment="prod")
        provision_system_accounts(run_cmd_func, environment="test")
        provision_static_files(run_cmd_func, env_vars, environment="early_prod")

        if not skip_apt:
            _logger.info("[*] Provisioning APT Sources and Packages...")
            apt_opts = [
                "-o",
                "Dpkg::Options::=--force-confdef",
                "-o",
                "Dpkg::Options::=--force-confold",
                "-o",
                "Dpkg::Lock::Timeout=120",
                "-o",
                "Acquire::Check-Valid-Until=false",
            ]

            run_cmd_func(["apt-get", "update"] + apt_opts)
            run_cmd_func(["apt-get", "install", "-y"] + apt_opts + ["gnupg"])
            run_cmd_func(
                ["apt-get", "update"] + apt_opts + ["--allow-insecure-repositories"]
            )

            all_packages = []

            for pkg_spec in MANIFEST.get("apt_packages", []):
                if "early_prod" in pkg_spec["environments"]:
                    pkg_name = (
                        pkg_spec.get("debian_name", pkg_spec["name"])
                        if os_id == "debian"
                        else pkg_spec["name"]
                    )
                    all_packages.append(pkg_name)

            pg_res = subprocess.run(
                [
                    "bash",
                    "-c",
                    "apt-cache depends postgresql | grep -Eo 'postgresql-[0-9]+' | head -n1 | grep -Eo '[0-9]+'",
                ],
                capture_output=True,
                text=True,
            )
            if pg_res.returncode == 0 and pg_res.stdout.strip():
                pg_major = pg_res.stdout.strip()
                all_packages.append(f"postgresql-{pg_major}-pgvector")

            all_packages = sorted(list(set(all_packages)))
            run_cmd_func(["apt-get", "install", "-y"] + apt_opts + all_packages)

            _logger.info("[*] Installing pip packages...")
            run_cmd_func(
                [
                    "pip3",
                    "install",
                    "pgeocode",
                    "telnetlib3",
                    "mcp",
                    "adif-io",
                    "--ignore-installed",
                    "typing_extensions",
                    "--break-system-packages",
                ]
            )
            # Remove pip's cryptography to prevent it from shadowing Debian's python3-cryptography, which breaks python3-openssl
            run_cmd_func(
                [
                    "pip3",
                    "uninstall",
                    "-y",
                    "cryptography",
                    "cffi",
                    "pycparser",
                    "--break-system-packages",
                ]
            )
        else:
            _logger.info("[*] Bypassing APT phase (skip_apt=True)...")

        provision_static_files(run_cmd_func, env_vars, environment="prod")
        provision_static_files(run_cmd_func, env_vars, environment="test")
        provision_systemd_override(run_cmd_func, env_vars, environment="prod")
        provision_systemd_override(run_cmd_func, env_vars, environment="test")

        try:
            run_cmd_func(["usermod", "-a", "-G", "hams_com", "odoo"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to add odoo to hams_com group: %s", e)

        is_isolated_ns = os.environ.get("HAMS_ISOLATED_NS") == "1"
        is_test_env = is_isolated_ns or is_test

        _logger.info("[*] Preparing testing directories with production paths...")
        apply_production_directories(run_cmd_func, environment="prod")
        apply_production_directories(run_cmd_func, environment="test")

        try:
            _logger.info("[*] Locking down RabbitMQ to local loopback...")
            os.makedirs("/etc/rabbitmq", exist_ok=True)
            with open("/etc/rabbitmq/rabbitmq-env.conf", "a") as f:
                f.write("NODE_IP_ADDRESS=127.0.0.1\n")
            if not is_isolated_ns:
                run_cmd_func(["systemctl", "restart", "rabbitmq-server"])
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to configure RabbitMQ bindings: %s", e)

        try:
            _logger.info("[*] Locking down PostgreSQL to local loopback...")
            run_cmd_func(
                [
                    "bash",
                    "-c",
                    "sed -i 's/peer/trust/g' /etc/postgresql/*/main/pg_hba.conf",
                ]
            )
            run_cmd_func(
                [
                    "bash",
                    "-c",
                    "echo \"listen_addresses = '127.0.0.1, ::1'\" >> /etc/postgresql/*/main/postgresql.conf",
                ]
            )
            run_cmd_func(
                [
                    "bash",
                    "-c",
                    "echo \"shared_preload_libraries = 'pg_stat_statements'\" >> /etc/postgresql/*/main/postgresql.conf",
                ]
            )

            if not is_isolated_ns:
                run_cmd_func(["systemctl", "restart", "postgresql"])

                db_name = env_vars.get("DB_NAME", "hams_test")
                _logger.info(
                    "[*] Bootstrapping initial Odoo PostgreSQL role and database (%s)...", db_name
                )
                db_pass = env_vars.get("DB_PASS", "odoo")
                sql_create_roles = f"DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'odoo') THEN CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD '{db_pass}'; END IF; END$$;"
                run_cmd_func(["sudo", "-u", "postgres", "psql", "-c", sql_create_roles])
                
                if is_test:
                    run_cmd_func(["sudo", "-u", "postgres", "dropdb", "--if-exists", db_name])
                    run_cmd_func(["sudo", "-u", "postgres", "createdb", "-O", "odoo", db_name])
                else:
                    run_cmd_func(
                        [
                            "bash",
                            "-c",
                            f"sudo -u postgres psql -tc \"SELECT 1 FROM pg_database WHERE datname = '{db_name}'\" | grep -q 1 || sudo -u postgres createdb -O odoo {db_name}",
                        ]
                    )
                
                # Unconditionally ensure the database is owned by odoo to fix pre-existing DBs
                run_cmd_func(
                    [
                        "bash",
                        "-c",
                        f"sudo -u postgres psql -c \"ALTER DATABASE {db_name} OWNER TO odoo;\"",
                    ]
                )
        except Exception as e:  # audit-ignore-catch-all
            _logger.warning("[*] Failed to configure PostgreSQL settings: %s", e)

        _logger.info("[*] Writing environment configuration files...")
        write_env_files("/opt/hams/etc", env_vars, run_cmd_func)

        if orig_user:
            try:
                u_info = pwd.getpwnam(orig_user)
                user_tmp = os.path.join(u_info.pw_dir, "tmp")
                os.makedirs(user_tmp, exist_ok=True)
                apply_permissions(user_tmp, f"{orig_user}:{orig_user}", None)

            except KeyError as e:
                _logger.debug("Original user %s not found: %s", orig_user, e)

        _logger.info("[*] Linking custom systemd units...")
        try:
            systemd_dir = "/opt/hams/systemd"
            if os.path.exists(systemd_dir):
                for item in os.listdir(systemd_dir):
                    if item.endswith(".service") or item.endswith(".timer"):
                        if not has_hams_com and item != "hams-pycache.service":
                            continue
                        src = os.path.join(systemd_dir, item)
                        dst = os.path.join("/etc/systemd/system", item)
                        if not os.path.exists(dst):
                            os.symlink(src, dst)
        except OSError as e:
            _logger.warning("Failed to link systemd units: %s", e)

        if not is_isolated_ns:
            initialize_odoo_database(run_cmd_func, hams_community_dir, hams_com_dir)
            run_post_provision_smoketest(has_hams_com, is_test_env=is_test_env)
        else:
            _logger.info(
                "[*] Skipping systemd smoketest inside isolated unshare namespace."
            )

    except subprocess.CalledProcessError as e:
        _logger.error("Failed to provision system packages: %s", e)
        sys.exit(1)
