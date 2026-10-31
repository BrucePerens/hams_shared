#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
# This software is Proprietary, Trade-Secret.
"""Throwaway fixtures that let `provision.py --test` start, and check, every unit on a test host.

A unit used to be left out of the --test smoketest (a `smoketest_skip_in_test` flag) when a test host lacked something it
needs. Bruce, 2026-10-31: solve each technically instead. Every helper here makes the missing thing, for a test host only,
and none of it is a production secret or production data:

  * PowerDNS answers on 127.0.0.1:PDNS_TEST_PORT, so it does not fight systemd-resolved for port 53 (its API stays on 8081).
  * A tiny deterministic callbook (TEST_CALLBOOK_ROWS) fills the empty test database so the callbook exporters have a view.
  * The deployed-source layout /opt/hams/src/<repo>/<dir> the relay publishers run from is mirrored from the test sources.
  * The simulated band gets a throwaway signing key (random, written at provisioning, never in git).
  * aiortc, av, faster-whisper and piper-tts (not packaged by Debian) are installed from PyPI by hash-pinned wheels, after the hashes are checked.
  * HamCall's licensed index is copied in for the smoketest only, from a file the operator staged, and removed afterwards.

Every function takes its paths as arguments (defaulting to the real ones) so test_infrastructure.py can run them in a
temporary directory.
"""

import hashlib
import os
import re
import secrets
import shutil
import subprocess

# Drop-ins are named zz-* so they sort after the unit's own override.conf and win over it.
# The port a test host's PowerDNS answers on. Port 53 belongs to systemd-resolved's stub (127.0.0.53 and .54), and
# pdns's default wildcard bind collides with it ("Unable to bind UDP socket to '0.0.0.0:53'").
PDNS_TEST_PORT = 5300
PDNS_TEST_ADDRESS = "127.0.0.1"

# The environment variable the operator-side test script sets to the staged, ephemeral copy of HamCall's index.
HAMCALL_STAGED_ENV = "HAMS_TEST_HAMCALL_IDX"
HAMCALL_INSTALLED = "/opt/hams/hamcall/hamcall.idx"
HAMCALL_OWNER = "hamsd_hamcall_sync"

BAND_KEYS_FILE = "/etc/hams-band/band_bot_keys"
BOT_KEY_FILE = "/etc/hams-bots/band.key"
BAND_KEYS_HOST_NAME = "test-bots"

# Where the observer's PyPI-only libraries go, and the hash-pinned list they come from.
OBSERVER_PYDEPS_DIR = "/usr/local/lib/hams-test/pydeps"
OBSERVER_REQUIREMENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoketest_observer_requirements.txt")

# Fixed, obviously fake licensees (not real callsigns' holders). The same rows every run.
TEST_CALLBOOK_ROWS = (
    # callsign, first name, last name, street, city, zip, grid, class
    ("W1TST", "TESTA", "FIXTURE", "1 TEST WAY", "TESTVILLE", "02101", "FN42", "EXTRA"),
    ("K2TST", "TESTB", "FIXTURE", "2 TEST WAY", "TESTVILLE", "10001", "FN30", "GENERAL"),
    ("N3TST", "TESTC", "FIXTURE", "3 TEST WAY", "TESTVILLE", "19101", "FN20", "TECHNICIAN"),
)
TEST_CALLBOOK_PROBE = TEST_CALLBOOK_ROWS[0][0]

# One element set for the satellites publisher (it refuses to publish none): the textbook ISS example TLE, an old epoch
# (so it is published flagged stale), under a name no real catalogue row has.
TEST_TLE = (
    "TEST FIXTURE ISS",
    "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927",
    "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537",
)


def _sql_text(value):
    return "'" + value.replace("'", "''") + "'"


def callbook_fixture_sql(rows=TEST_CALLBOOK_ROWS):
    """SQL that adds the fixture licensees to ham_callbook; running it twice adds nothing the second time."""
    statements = []
    for callsign, first, last, street, city, zip_code, grid, klass in rows:
        statements.append(
            "INSERT INTO ham_callbook (callsign, first_name, last_name, street, city, zip, grid_square, "
            "license_class, create_date, write_date) "
            f"SELECT {_sql_text(callsign)}, {_sql_text(first)}, {_sql_text(last)}, {_sql_text(street)}, "
            f"{_sql_text(city)}, {_sql_text(zip_code)}, {_sql_text(grid)}, {_sql_text(klass)}, "
            "now() at time zone 'UTC', now() at time zone 'UTC' "
            f"WHERE NOT EXISTS (SELECT 1 FROM ham_callbook WHERE callsign = {_sql_text(callsign)});"
        )
    return "\n".join(statements) + "\n"


def tle_fixture_sql(tle=TEST_TLE):
    name, line1, line2 = tle
    return (
        "INSERT INTO ham_satellite_tle (name, norad_id, line1, line2, create_date, write_date) "
        f"SELECT {_sql_text(name)}, 25544, {_sql_text(line1)}, {_sql_text(line2)}, "
        "now() at time zone 'UTC', now() at time zone 'UTC' "
        f"WHERE NOT EXISTS (SELECT 1 FROM ham_satellite_tle WHERE name = {_sql_text(name)});\n"
    )


def install_callbook_fixture(run_cmd_func, db_name):
    """Adds the fixture licensees and element set to a TEST database (refuses a name that looks like production)."""
    if "prod" in db_name:
        raise RuntimeError(f"refusing to write test callbook rows into {db_name}")
    run_cmd_func(
        ["sudo", "-u", "postgres", "psql", "-d", db_name, "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=terse"],
        input=callbook_fixture_sql() + tle_fixture_sql(),
        text=True,
    )


def pdns_test_dropin(execstart):
    """A systemd drop-in that makes the (already configured) pdns ExecStart answer on the loopback test port only."""
    return (
        "# provision.py --test: PowerDNS on a high loopback port, so systemd-resolved keeps port 53.\n"
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart={execstart} --local-address={PDNS_TEST_ADDRESS} --local-port={PDNS_TEST_PORT}\n"
    )


def install_pdns_test_dropin(execstart, root="/"):
    path = os.path.join(root, "etc/systemd/system/pdns.service.d/zz-test-port.conf")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(pdns_test_dropin(execstart))
    os.chmod(path, 0o644)
    return path


def observer_test_dropin(pydeps_dir=OBSERVER_PYDEPS_DIR):
    """The observer's test-host drop-in: the PyPI libraries on its path, and its safety reports kept on this host."""
    return (
        "# provision.py --test: aiortc and av come from PyPI (hash-pinned) in " + pydeps_dir + ".\n"
        "[Service]\n"
        f'Environment="PYTHONPATH=/opt/hams/daemons:{pydeps_dir}"\n'
        # The unit names production's Odoo; a test host's observer must never reach it.
        'Environment="ODOO_URL=http://127.0.0.1:8069"\n'
    )


def install_observer_test_dropin(root="/", pydeps_dir=OBSERVER_PYDEPS_DIR):
    path = os.path.join(root, "etc/systemd/system/hams.simulated.observer.service.d/zz-test-deps.conf")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(observer_test_dropin(pydeps_dir))
    os.chmod(path, 0o644)
    return path


# Where the throwaway relay signer launcher is installed on a test host, and its unit drop-in.
TEST_SIGNER_PATH = "/usr/local/lib/hams-test/smoketest_relay_signer.py"
TEST_SIGNER_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoketest_relay_signer.py")
# Units that call out to an external service in production and have a test-host stand-in, so the test smoketest starts them.
TEST_SUBSTITUTED_UNITS = frozenset({"hams-relay-signer.service"})


def relay_signer_test_dropin(launcher=TEST_SIGNER_PATH):
    """Replaces the Cloud KMS signer's start-up with the throwaway-key launcher (same unit, same socket, same user)."""
    return (
        "# provision.py --test: sign with a throwaway key, not Google Cloud KMS.\n"
        "[Unit]\n"
        "ConditionPathExists=\n"
        "[Service]\n"
        "EnvironmentFile=\n"
        "ExecStartPre=\n"
        "ExecStart=\n"
        f"ExecStart=/usr/bin/python3 {launcher}\n"
    )


def install_relay_signer_test_dropin(root="/", launcher=TEST_SIGNER_PATH, source=TEST_SIGNER_SOURCE):
    """Installs the launcher (mode 0644, world readable) and the drop-in; returns the drop-in path."""
    dest = os.path.join(root, launcher.lstrip("/"))
    _copy_file(source, dest)
    path = os.path.join(root, "etc/systemd/system/hams-relay-signer.service.d/zz-test-signer.conf")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(relay_signer_test_dropin(launcher))
    os.chmod(path, 0o644)
    return path


def new_band_secret():
    """A fresh random 43-character secret (32 random bytes, URL-safe base64), the band's minimum."""
    return secrets.token_urlsafe(32)


def generate_band_keys(owner="odoo", band_keys_file=BAND_KEYS_FILE, bot_key_file=BOT_KEY_FILE, chown=None):
    """Writes a throwaway band key file and the matching bot key file, mode 0600, each with a fresh random secret.

    Never overwrites a key file that already exists (a test host an operator keyed by hand keeps their key).
    Returns the paths it wrote."""
    secret = new_band_secret()
    wrote = []
    for path, text in ((band_keys_file, f"{BAND_KEYS_HOST_NAME}:{secret}\n"), (bot_key_file, f"{secret}\n")):
        if os.path.exists(path):
            continue
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o600)
        if chown:
            chown(path, owner)
            chown(directory, owner)
        wrote.append(path)
    return wrote


def unit_working_directories(static_files, repos=("hams_com", "hams_open")):
    """(repo, relative dir) of every /opt/hams/src/<repo>/<dir> WorkingDirectory a test-installed unit runs from."""
    found = []
    pattern = re.compile(r"^WorkingDirectory=/opt/hams/src/(" + "|".join(repos) + r")/(\S+)$", re.MULTILINE)
    for spec in static_files:
        if "test" not in spec.get("environments", ["prod", "test"]):
            continue
        for repo, rel in pattern.findall(spec.get("content", "")):
            if (repo, rel) not in found:
                found.append((repo, rel))
    return sorted(found)


def _copy_file(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o644)


def manual_catalog_files(root):
    """Relative paths the manuals publisher reads: ham_relay_bridge's catalogue source, and every page it lists."""
    import ast
    catalog_source = os.path.join("ham_relay_bridge", "controllers", "binary_download.py")
    path = os.path.join(root, catalog_source)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    files = [catalog_source]
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MANUAL_CATALOG" for t in node.targets):
            files += [os.path.join(e["module"], e["path"]) for e in ast.literal_eval(node.value).values()]
    return files


def mirror_deployed_sources(static_files, source_roots, dest_root="/opt/hams/src", chown=None):
    """Copies each directory a unit runs from under /opt/hams/src out of the matching test source tree.

    This is the layout devbox_tools/deploy_to_production.py leaves on hams1 (a copy of the repository tree, root owned),
    made from the sources provision.py was run from. Production is not touched: it only writes under dest_root on this host.
    source_roots maps a repo name to its directory. Returns the copied destinations."""
    copied = []
    for repo, rel in unit_working_directories(static_files, tuple(source_roots)):
        src = os.path.join(source_roots[repo], rel)
        if not os.path.isdir(src):
            continue
        dest = os.path.join(dest_root, repo, rel)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        for current, dirs, files in os.walk(dest):
            for name in dirs:
                os.chmod(os.path.join(current, name), 0o755)
            for name in files:
                os.chmod(os.path.join(current, name), 0o644)
        os.chmod(dest, 0o755)
        copied.append(dest)
        # The daemons' flat helper modules (hams_config and what it imports) sit beside the daemon directories.
        if rel.startswith("daemons/"):
            for name in os.listdir(os.path.join(source_roots[repo], "daemons")):
                flat = os.path.join(source_roots[repo], "daemons", name)
                if name.endswith(".py") and os.path.isfile(flat):
                    _copy_file(flat, os.path.join(dest_root, repo, "daemons", name))
        # The satellites publisher reads its catalogue from the repository tree.
        if (repo, rel) == ("hams_com", "daemons/relay_satellites"):
            page = os.path.join("ham_satellite", "data", "satellite_catalogue.json")
            if os.path.isfile(os.path.join(source_roots[repo], page)):
                _copy_file(os.path.join(source_roots[repo], page), os.path.join(dest_root, repo, page))
        # The manuals publisher reads its catalogue and the pages from the repository tree.
        if (repo, rel) == ("hams_com", "daemons/relay_manuals"):
            for page in manual_catalog_files(source_roots[repo]):
                if os.path.isfile(os.path.join(source_roots[repo], page)):
                    _copy_file(os.path.join(source_roots[repo], page), os.path.join(dest_root, repo, page))
    if chown:
        # root owns the whole deployed tree, as the deploy tool leaves it.
        for current, dirs, files in os.walk(dest_root):
            for name in dirs + files:
                chown(os.path.join(current, name), "root")
        chown(dest_root, "root")
    return copied


def parse_requirements(text):
    """[(name, version, [hashes])] from a pip --require-hashes file; raises on an unpinned or unhashed line."""
    entries = []
    logical = re.sub(r"\\\n", " ", text)
    for line in logical.splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([A-Za-z0-9.+!_-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)$", line)
        if not m:
            raise ValueError(f"requirement is not name==version with sha256 hashes: {line[:80]}")
        entries.append((m.group(1), m.group(2), re.findall(r"sha256:([0-9a-f]{64})", m.group(3))))
    return entries


def install_observer_dependencies(run_cmd_func, pydeps_dir=OBSERVER_PYDEPS_DIR, requirements=OBSERVER_REQUIREMENTS):
    """Installs aiortc and av (and what they need) into pydeps_dir from PyPI, every file checked against its pinned hash.

    pip --require-hashes refuses any file whose hash is not listed, and --only-binary=:all: refuses a source build, so no
    downloaded code runs at install time. Skipped when the directory already holds the pinned versions."""
    with open(requirements, encoding="utf-8") as f:
        entries = parse_requirements(f.read())
    marker = os.path.join(pydeps_dir, ".installed-requirements")
    with open(requirements, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    try:
        with open(marker, encoding="utf-8") as f:
            if f.read().strip() == digest:
                return entries
    except OSError:
        pass
    os.makedirs(pydeps_dir, exist_ok=True)
    run_cmd_func([
        "python3", "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "--require-hashes",
        "--only-binary=:all:", "--no-deps", "--upgrade", "--target", pydeps_dir, "-r", requirements,
    ])
    for current, dirs, files in os.walk(pydeps_dir):  # the services that import them run as other accounts
        for name in dirs:
            os.chmod(os.path.join(current, name), 0o755)
        for name in files:
            os.chmod(os.path.join(current, name), os.stat(os.path.join(current, name)).st_mode | 0o444)
    os.chmod(pydeps_dir, 0o755)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(digest + "\n")
    return entries


def stage_hamcall_index(environ=os.environ, dest=HAMCALL_INSTALLED, owner=HAMCALL_OWNER, chown=None):
    """Copies the operator-staged HamCall index into place for this run; returns the path it installed, or None.

    None when nothing is staged or when a file already sits at dest (an operator's own, which this run must never remove).
    The copy is mode 0640 and owned by the one account that reads it."""
    staged = environ.get(HAMCALL_STAGED_ENV, "")
    if not staged or not os.path.isfile(staged) or os.path.exists(dest):
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(staged, dest)
    os.chmod(dest, 0o640)
    if chown:
        chown(dest, owner)
    return dest


def remove_hamcall_index(installed, environ=os.environ):
    """Deletes the copy stage_hamcall_index made, and the staged file it came from. Safe to call twice."""
    for path in (installed, environ.get(HAMCALL_STAGED_ENV, "")):
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def dig_txt(name, port=PDNS_TEST_PORT, address=PDNS_TEST_ADDRESS, runner=subprocess.run):
    """The TXT answer lines the test PowerDNS gives for name ('' when none)."""
    res = runner(
        ["dig", f"@{address}", "-p", str(port), "+short", "+time=3", "+tries=2", "TXT", name],
        capture_output=True, text=True, check=False,
    )
    return res.stdout.strip()
