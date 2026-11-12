#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`provision.py --test-environment`: turn any test host into one where test.py and run_linters.py work.

A LAN machine, a fresh Debian 13 VM, the lima VM and a Claude Code cloud container all get the same command. The
steps, each of which checks before it acts so a second run changes nothing (a container restart only restarts
PostgreSQL):

1. refuse a production host (the markers test.py refuses on);
2. find the sibling layout (<workspace>/hams_com, <workspace>/hams_open with its hams_shared submodule) and clone
   whatever is missing, with the first push credential the host already has (cloud git proxy, an authenticated gh,
   an SSH key named with --git-key); existing checkouts are never checked out, reset or pulled (ADR 0102);
3. the cloud container only: point python3/pip3 at the distro's interpreter, which apt's python3-* modules need;
4. apt: the MANIFEST "apt_packages" (minus odoo) plus MANIFEST "test_environment_apt_packages", pgvector;
   pip: RUNTIME_PIP_PACKAGES and TEST_PIP_PACKAGES, each only when missing;
5. Odoo 19 as a Debian package: nightly.odoo.com where it is reachable, otherwise built from github.com/odoo/odoo;
6. kopia (the MANIFEST's pinned release), cargo-deny, the Playwright headless Chromium, `npm ci` in hams_shared, and
   the browser ML assets under /var/lib/odoo/hams_ml_assets, made by production's own provisioning script
   (hams_com ham_shack/tools/provision_ml_assets.py) so ham_shack's real-route tests find what production serves;
7. PostgreSQL running (systemctl, or pg_ctlcluster without systemd) with the odoo and runner roles and an empty
   hams_test database; Redis and RabbitMQ enabled where systemd runs them (test.py starts its own otherwise);
8. what `--test` provides and a HAMS_ISOLATED_NS=1 run does not (host_requirements.py): the hamsd_* accounts, the
   MANIFEST directories, the cache_manager_ro login file, git safe.directory = *;
9. /etc/profile.d/hams_test_environment.sh: a random per-host HAMS_CRYPTO_KEY (never a real secret) and the
   variables test.py and run_linters.py need on this host;
10. a self-check: run_linters.py --changed on a clean hams_com tree and test.py -u zero_sudo.

Gate machines: set PostgreSQL's max_locks_per_transaction to 256 (default 64, needs a server restart, so it is a manual
operator step and this script does not change it). Parallel test shards on one server share a lock table of
max_locks_per_transaction x (max_connections + prepared transactions) slots, and a heavy test transaction can hold hundreds
of relation locks; with the default, shards failed with "out of shared memory ... increase max_locks_per_transaction".

No unit that fetches from third-party servers (MANIFEST external_fetch) is ever enabled or started: this path
enables only postgresql, redis-server and rabbitmq-server. Full entries for every anchor: tools/README.md.
"""
import base64
import fcntl
import glob
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

import host_requirements
import infrastructure
import provision_cache


GITHUB_OWNER = "BrucePerens"

# [@ANCHOR: provision_test_environment:repositories]
# The checkouts a test host needs, cloned beside each other in the workspace. hams_shared is not listed: it is
# hams_open's submodule, and hams_com's hams_shared is the symlink ../hams_open/hams_shared (tracked in hams_com).
REPOSITORIES = ("hams_open", "hams_com")

# [@ANCHOR: provision_test_environment:production_markers]
# The same two signals test.py's refuse_to_run_on_production_host() uses (a parity test pins them).
PRODUCTION_MARKER_FILES = ("/opt/hams/src/DEPLOY_LOG", "/opt/hams/src/DEPLOYED_COMMITS")
PRODUCTION_DATABASE_NAME = "hams_prod"

ODOO_BRANCH = "19.0"
ODOO_DIR = "/opt/odoo19"
ODOO_KEY_URL = "https://nightly.odoo.com/odoo.key"
# The host `playwright install` downloads its browsers from (refused by a cloud session's proxy).
PLAYWRIGHT_CDN_URL = "https://cdn.playwright.dev/"
ML_ASSET_ROOT = "/var/lib/odoo/hams_ml_assets"  # the root ham_shack/controllers/offline.py serves from
ML_ASSET_SCRIPT = os.path.join("ham_shack", "tools", "provision_ml_assets.py")
ML_ASSET_HOSTS = ("https://registry.npmjs.org/", "https://huggingface.co/")  # what that script downloads from
LOCK_PATH = "/var/lib/hams-test-environment.lock"
ODOO_SOURCE_LIST = "/etc/apt/sources.list.d/odoo.list"
# Building the Odoo package from its source tree (debian/ ships in it).
ODOO_BUILD_APT_PACKAGES = ("debhelper", "dh-python", "rsync", "python3-setuptools", "git")
PROFILE_PATH = "/etc/profile.d/hams_test_environment.sh"
DEBIAN_PYTHON_DEFAULTS = "/usr/share/python3/debian_defaults"
TEST_DATABASE = "hams_test"
APT_UPDATE_STAMP = "/var/lib/hams-test-environment.apt-updated"
APT_UPDATE_MAX_AGE = 12 * 3600
_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_-]*\Z")
_PROFILE_LINE_RE = re.compile(r"^export ([A-Z_][A-Z0-9_]*)=(.*)$")


# [@ANCHOR: provision_test_environment:report]
class Report:
    """What a run did. `changes` are the steps that changed the host (empty on a second run), `skipped` the steps
    left out with the reason, `notes` everything else worth printing (the push credential in use). Under --plan
    (`plan`) a change is printed as WOULD CHANGE."""

    def __init__(self, plan=False):
        self.plan = plan
        self.changes = []
        self.skipped = []
        self.notes = []

    def changed(self, text):
        self.changes.append(text)
        print(f"[test-environment] {'WOULD CHANGE' if self.plan else 'CHANGE'}: {text}", flush=True)

    def skip(self, text):
        self.skipped.append(text)
        print(f"[test-environment] SKIPPED: {text}", flush=True)

    def note(self, text):
        self.notes.append(text)
        print(f"[test-environment] {text}", flush=True)


# [@ANCHOR: provision_test_environment:production_refusal_reasons]
def production_refusal_reasons(marker_files=None, database_names=()):
    """Why this host looks like production; an empty list means it does not. `marker_files` None reads
    PRODUCTION_MARKER_FILES at call time. Pure apart from os.path.exists."""
    if marker_files is None:
        marker_files = PRODUCTION_MARKER_FILES
    reasons = [f"{path} exists (deploy_to_production.py writes it on the production host only)"
               for path in marker_files if os.path.exists(path)]
    if PRODUCTION_DATABASE_NAME in database_names:
        reasons.append(f"a database named {PRODUCTION_DATABASE_NAME!r} exists on this host's PostgreSQL")
    return reasons


# [@ANCHOR: provision_test_environment:list_database_names]
def list_database_names(socket_dir="/var/run/postgresql", runner=subprocess.run):
    """Database names on the local PostgreSQL, or () when no server is running (no socket in `socket_dir`).
    A running server that cannot be listed exits 2: the production check must never be skipped."""
    if not glob.glob(os.path.join(socket_dir, ".s.PGSQL.*")):
        return ()
    cmd = ["runuser", "-u", "postgres", "--", "psql", "-h", socket_dir, "-d", "postgres", "-At",
           "-c", "SELECT datname FROM pg_database"]
    res = runner(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[test-environment] ERROR: PostgreSQL is running but its databases could not be listed for the "
              f"production check: {res.stderr.strip()}", flush=True)
        sys.exit(2)
    return tuple(line.strip() for line in res.stdout.splitlines() if line.strip())


# [@ANCHOR: provision_test_environment:refuse_production]
def refuse_production(marker_files=None, database_names=()):
    """Exits with status 2, before anything is changed, when this host looks like production. No override."""
    reasons = production_refusal_reasons(marker_files, database_names)
    if not reasons:
        return
    print("[test-environment] REFUSING: this looks like the PRODUCTION host.\n"
          + "".join(f"   - {reason}\n" for reason in reasons)
          + "   provision.py --test-environment never runs on hams1 (the same rule as test.py).", flush=True)
    sys.exit(2)


# [@ANCHOR: provision_test_environment:discover_layout]
def discover_layout(tools_dir, repo_root, workspace=None):
    """The checkout paths as a dict {workspace, hams_open, hams_com, hams_shared}. `workspace` (--workspace) wins;
    otherwise, when `repo_root` (provision._find_repo_root) is a git checkout (hams_open or hams_com), its parent;
    when it is not (the directory holding a standalone hams_shared clone, which _find_repo_root also accepts),
    repo_root itself; with no repo_root, the parent of the hams_shared clone holding `tools_dir`. hams_shared is
    always hams_open's submodule path."""
    if workspace:
        base = os.path.abspath(workspace)
    elif repo_root and os.path.exists(os.path.join(repo_root, ".git")):
        base = os.path.dirname(os.path.abspath(repo_root))
    elif repo_root:
        # The directory that holds a standalone hams_shared clone (no hams_open around it yet).
        base = os.path.abspath(repo_root)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(tools_dir)))
    hams_open = os.path.join(base, "hams_open")
    return {
        "workspace": base,
        "hams_open": hams_open,
        "hams_com": os.path.join(base, "hams_com"),
        "hams_shared": os.path.join(hams_open, "hams_shared"),
    }


# [@ANCHOR: provision_test_environment:check_git_key]
def check_git_key(path):
    """The absolute path of the SSH key named with --git-key, after checking it is a regular file with mode 600.
    Raises ValueError otherwise. Only stat() is used: the key is never opened, read, copied or printed."""
    absolute = os.path.abspath(path)
    try:
        info = os.stat(absolute)
    except OSError as e:
        raise ValueError(f"--git-key {absolute}: {e.strerror}") from e
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"--git-key {absolute} is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise ValueError(f"--git-key {absolute} has mode {mode:03o}; it must be 600 (chmod 600 {absolute})")
    return absolute


# [@ANCHOR: provision_test_environment:choose_credentials]
def choose_credentials(environ, gh_authenticated, git_key=None):
    """The push credential to use, as a dict {method, url_format, ssh_command, note}. The first that applies wins:
    the cloud session's git proxy (CLAUDE_CODE_REMOTE=true), an authenticated gh, the --git-key SSH key (checked by
    check_git_key, which may raise ValueError), else read-only HTTPS. url_format takes {owner} and {repo}."""
    https = "https://github.com/{owner}/{repo}"
    if environ.get("CLAUDE_CODE_REMOTE") == "true":
        return {"method": "cloud_proxy", "url_format": https, "ssh_command": None,
                "note": "push credentials: the cloud session's git proxy (nothing to set up)"}
    if gh_authenticated:
        return {"method": "gh", "url_format": https, "ssh_command": None,
                "note": "push credentials: the authenticated gh CLI (gh auth setup-git)"}
    if git_key:
        key = check_git_key(git_key)
        ssh_command = f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes"
        return {"method": "ssh_key", "url_format": "git@github.com:{owner}/{repo}.git", "ssh_command": ssh_command,
                "note": f"push credentials: the SSH key {key}, set as core.sshCommand on these clones only"}
    return {"method": "read_only", "url_format": https, "ssh_command": None,
            "note": "push credentials: NONE. Cloned read-only over HTTPS; pushing is not set up (authenticate gh, "
                    "or pass --git-key <path to a mode-600 deploy key>, and run again)"}


# [@ANCHOR: provision_test_environment:gh_authenticated]
def gh_authenticated(as_user, runner=subprocess.run):
    """True when the gh CLI is installed and `gh auth status` succeeds for the runner user. Read-only."""
    if not shutil.which("gh"):
        return False
    res = runner(as_user(["gh", "auth", "status"]), capture_output=True, text=True)
    return res.returncode == 0


# [@ANCHOR: provision_test_environment:missing_repositories]
def missing_repositories(layout):
    """[(name, path)] of REPOSITORIES with no checkout. A path that exists, is not a git checkout and is not empty
    raises ValueError: it is somebody's directory, never overwritten."""
    missing = []
    for name in REPOSITORIES:
        path = layout[name]
        if os.path.exists(os.path.join(path, ".git")):
            continue
        if os.path.isdir(path) and os.listdir(path):
            raise ValueError(f"{path} exists and is not a git checkout; move it away or pass --workspace")
        missing.append((name, path))
    return missing


# [@ANCHOR: provision_test_environment:retry]
def retry(action, tries=3, delay=30, sleep=time.sleep):
    """Calls action() until it returns without CalledProcessError, at most `tries` times, `delay` seconds apart.
    Re-raises the last error. For network steps (git clone) only."""
    for attempt in range(1, tries + 1):
        try:
            return action()
        except subprocess.CalledProcessError:
            if attempt == tries:
                raise
            print(f"[test-environment] attempt {attempt} failed; retrying in {delay} s", flush=True)
            sleep(delay)
    return None


# [@ANCHOR: provision_test_environment:provision_repositories]
def provision_repositories(layout, credentials, run, as_user, report, plan=False):
    """Clones the missing REPOSITORIES (shallow, into <path>.partial and then renamed, so an interrupted clone never
    looks complete), initialises hams_open's hams_shared submodule only when it has no tools/test.py (and then checks
    out hams_shared's main, which run_linters.py requires), and applies the
    credential: `gh auth setup-git` for gh, core.sshCommand on each checkout for an SSH key. Existing checkouts'
    working trees are never touched."""
    if credentials["method"] == "gh":
        run(as_user(["gh", "auth", "setup-git"]))
    for name, path in missing_repositories(layout):
        url = credentials["url_format"].format(owner=GITHUB_OWNER, repo=name)
        partial = path + ".partial"
        clone = ["git"]
        if credentials["ssh_command"]:
            clone += ["-c", f"core.sshCommand={credentials['ssh_command']}"]
        clone += ["clone", "--depth", "1", url, partial]
        if plan:
            print(f"PLAN run: {' '.join(clone)} && mv {partial} {path}", flush=True)
            continue
        if os.path.exists(partial):
            shutil.rmtree(partial)
        retry(lambda: run(as_user(clone)))
        os.rename(partial, path)
        report.changed(f"cloned {name} into {path}")
    if credentials["ssh_command"]:
        for name in REPOSITORIES:
            _set_git_config(layout[name], "core.sshCommand", credentials["ssh_command"], run, as_user, report, plan)
    shared_marker = os.path.join(layout["hams_shared"], "tools", "test.py")
    if os.path.exists(shared_marker):
        return
    if os.path.isfile(os.path.join(layout["hams_open"], ".git")):
        # A worktree: its submodule directory is empty by design; link the main checkout's (fix_worktree_symlinks).
        run(as_user(["python3", "fix_worktree_symlinks.py"]), cwd=layout["hams_open"])
        report.changed(f"ran fix_worktree_symlinks.py in {layout['hams_open']}")
        return
    retry(lambda: run(as_user(["git", "-C", layout["hams_open"], "submodule", "update", "--init", "--depth", "1"])))
    # hams_open's pin can lag hams_shared's main, and run_linters.py refuses a hams_shared behind origin/main. This
    # submodule checkout was created just now, so moving it to main changes nobody's work.
    shared = layout["hams_shared"]
    retry(lambda: run(as_user(["git", "-C", shared, "fetch", "-q", "--depth", "1", "origin", "main"])))
    run(as_user(["git", "-C", shared, "checkout", "-q", "--detach", "FETCH_HEAD"]))
    report.changed("initialised hams_open's hams_shared submodule at hams_shared's main")


# [@ANCHOR: provision_test_environment:_set_git_config]
def _set_git_config(repo, key, value, run, as_user, report, plan):
    if not os.path.exists(os.path.join(repo, ".git")):
        if plan:
            print(f"PLAN run: git -C {repo} config --local {key} <value>", flush=True)
        return
    current = subprocess.run(["git", "-C", repo, "config", "--local", "--get", key], capture_output=True, text=True)
    if current.returncode == 0 and current.stdout.strip() == value:
        return
    run(as_user(["git", "-C", repo, "config", "--local", key, value]))
    report.changed(f"set {key} in {repo}")


# [@ANCHOR: provision_test_environment:select_apt_packages]
def select_apt_packages(manifest, os_id, have_command):
    """(wanted, skipped) for this host: wanted is the sorted package names from manifest["apt_packages"] for
    early_prod (minus odoo, which install_odoo handles) and manifest["test_environment_apt_packages"]; skipped maps a
    test-environment package to why it is left out (its skip_if_command is on PATH). `have_command(name) -> bool`.
    Debian uses debian_name where an entry has one. Pure."""
    def apt_name(spec):
        return spec.get("debian_name", spec["name"]) if os_id == "debian" else spec["name"]

    wanted = {apt_name(spec) for spec in manifest.get("apt_packages", [])
              if "early_prod" in spec["environments"] and spec["name"] != "odoo"}
    skipped = {}
    for spec in manifest.get("test_environment_apt_packages", []):
        command = spec.get("skip_if_command")
        if command and have_command(command):
            skipped[apt_name(spec)] = f"`{command}` is already on PATH"
            continue
        wanted.add(apt_name(spec))
    return sorted(wanted), skipped


# [@ANCHOR: provision_test_environment:_apt_candidate]
def _apt_candidate(pkg):
    res = subprocess.run(["apt-cache", "policy", pkg], capture_output=True, text=True)
    match = re.search(r"Candidate:\s*(\S+)", res.stdout)
    return bool(match) and match.group(1) != "(none)"


# [@ANCHOR: provision_test_environment:_apt_update_if_stale]
def _apt_update_if_stale(run, report, plan, now=None):
    now = time.time() if now is None else now
    try:
        age = now - os.path.getmtime(APT_UPDATE_STAMP)
    except OSError:
        age = None
    if age is not None and age < APT_UPDATE_MAX_AGE:
        return
    retry(lambda: run(["apt-get", "update", "-q"]))
    if not plan:
        with open(APT_UPDATE_STAMP, "w", encoding="utf-8"):
            pass
    report.note("apt-get update")


# [@ANCHOR: provision_test_environment:_install_apt]
def _install_apt(run, packages, label, report):
    """Installs the missing ones through infrastructure._apt_install_missing (simulation first, nothing removed or
    upgraded); a refusal there is fatal here."""
    before = len(infrastructure.get_hook_failures())
    installed = infrastructure._apt_install_missing(run, list(packages), ["-q", "--no-install-recommends"], label)
    failures = infrastructure.get_hook_failures()[before:]
    if failures:
        raise RuntimeError(f"apt ({label}) refused: {failures}")
    if installed:
        report.changed(f"apt ({label}) installed {' '.join(installed)}")


# [@ANCHOR: provision_test_environment:provision_apt_packages]
def provision_apt_packages(run, os_id, report, plan=False):
    """apt-get update (at most every 12 hours), then the select_apt_packages list that has an apt candidate on this
    release (a name without one is reported: awscli on Ubuntu 24.04 goes to pip, as provisioning does), then the
    pgvector package for the newest installed PostgreSQL."""
    _apt_update_if_stale(run, report, plan)
    wanted, skipped = select_apt_packages(infrastructure.MANIFEST, os_id, lambda c: shutil.which(c) is not None)
    for pkg, why in sorted(skipped.items()):
        report.note(f"apt: not installing {pkg}: {why}")
    installed = infrastructure._installed_apt_packages()
    no_candidate = [pkg for pkg in wanted if pkg not in installed and not _apt_candidate(pkg)]
    for pkg in no_candidate:
        if pkg in infrastructure.PIP_FALLBACK_APT_PACKAGES:
            report.note(f"apt: {pkg} has no candidate on this release; installed with pip instead")
        else:
            report.skip(f"apt: {pkg} has no candidate on this release")
    _install_apt(run, [p for p in wanted if p not in no_candidate], "test-environment", report)
    majors = sorted((d for d in os.listdir("/usr/lib/postgresql") if d.isdigit()), key=int) \
        if os.path.isdir("/usr/lib/postgresql") else []
    if majors:
        _install_apt(run, [f"postgresql-{majors[-1]}-pgvector"], "pgvector", report)
    return no_candidate


# [@ANCHOR: provision_test_environment:missing_pip_distributions]
def missing_pip_distributions(names, installed):
    """The names in `names` whose distribution is not installed; `installed(name) -> bool`. Pure."""
    return [name for name in names if not installed(name)]


# [@ANCHOR: provision_test_environment:_pip_installed]
def _pip_installed(python):
    def probe(name):
        res = subprocess.run([python, "-c", "import importlib.metadata as m, sys; m.version(sys.argv[1])", name],
                             capture_output=True, text=True)
        return res.returncode == 0
    return probe


# [@ANCHOR: provision_test_environment:provision_pip_packages]
def provision_pip_packages(run, no_apt_candidate, report, python="/usr/bin/python3"):
    """pip for the system interpreter, each group only when something in it is missing: the PIP_FALLBACK_APT_PACKAGES
    of the apt names with no candidate, RUNTIME_PIP_PACKAGES (then pip's cryptography is removed, as provisioning
    does, so Debian's python3-cryptography wins), TEST_PIP_PACKAGES; PyMuPDF 1.25 when `import pymupdf` fails (Ubuntu
    24.04's python3-fitz has no such module) and mypy when `python3 -m mypy` fails."""
    probe = _pip_installed(python)
    pip = [python, "-m", "pip", "install", "-q", "--break-system-packages"]
    fallback = [infrastructure.PIP_FALLBACK_APT_PACKAGES[p] for p in no_apt_candidate
                if p in infrastructure.PIP_FALLBACK_APT_PACKAGES]
    missing = missing_pip_distributions(fallback, probe)
    if missing:
        run(pip + missing)
        report.changed(f"pip installed {' '.join(missing)}")
    if missing_pip_distributions(infrastructure.RUNTIME_PIP_PACKAGES, probe):
        run(pip + list(infrastructure.RUNTIME_PIP_PACKAGES) + ["--ignore-installed", "typing_extensions"])
        run([python, "-m", "pip", "uninstall", "-y", "-q", "cryptography", "cffi", "pycparser",
             "--break-system-packages"])
        report.changed("pip installed RUNTIME_PIP_PACKAGES")
    if missing_pip_distributions(infrastructure.TEST_PIP_PACKAGES, probe):
        run(pip + list(infrastructure.TEST_PIP_PACKAGES) + ["--ignore-installed"])
        report.changed("pip installed TEST_PIP_PACKAGES")
    if subprocess.run([python, "-c", "import pymupdf"], capture_output=True).returncode != 0:
        run(pip + ["--ignore-installed", "--no-deps", "pymupdf>=1.25,<1.26"])
        report.changed("pip installed PyMuPDF 1.25 (no pymupdf module from apt)")
    if subprocess.run([python, "-m", "mypy", "--version"], capture_output=True).returncode != 0:
        run(pip + ["mypy", "--ignore-installed", "pathspec"])
        report.changed("pip installed mypy")


# [@ANCHOR: provision_test_environment:cloud_python_links]
def cloud_python_links(distro_python):
    """[(link, target)] that point python3, pip3 and pip at the distro's interpreter in a cloud container, whose
    image points them at another Python (apt's compiled python3-* modules are built for the distro's). Pure."""
    version = os.path.basename(distro_python)
    return [
        ("/usr/bin/python3", version),
        ("/usr/local/bin/python3", f"/usr/bin/{version}"),
        ("/usr/local/bin/pip3", "/usr/bin/pip3"),
        ("/usr/local/bin/pip", "/usr/bin/pip3"),
    ]


# [@ANCHOR: provision_test_environment:distro_default_python]
def distro_default_python(debian_defaults_text):
    """The interpreter name (e.g. "python3.12") that the `default-version` line of Debian's
    /usr/share/python3/debian_defaults names. Raises ValueError when there is none. Pure. (py3versions reads the same
    file but is itself a python3 script, so it fails while python3 points at another interpreter.)"""
    match = re.search(r"^default-version\s*=\s*(python3\.\d+)\s*$", debian_defaults_text, re.MULTILINE)
    if not match:
        raise ValueError("debian_defaults has no default-version = python3.N line")
    return match.group(1)


# [@ANCHOR: provision_test_environment:provision_cloud_python]
def provision_cloud_python(environ, run, report, plan=False):
    """In a cloud session container only (CLAUDE_CODE_REMOTE=true): repoints the links of cloud_python_links() that
    point elsewhere. /usr/local/bin entries that do not exist are left alone. Never runs on any other host: the dev
    box, a LAN host or production keep their python3."""
    if environ.get("CLAUDE_CODE_REMOTE") != "true":
        return
    with open(DEBIAN_PYTHON_DEFAULTS, encoding="utf-8") as defaults:
        distro = distro_default_python(defaults.read())
    if not shutil.which(distro):
        raise RuntimeError(f"{DEBIAN_PYTHON_DEFAULTS} names {distro}, which is not installed")
    for link, target in cloud_python_links(distro):
        if link.startswith("/usr/local/") and not os.path.lexists(link):
            continue
        if os.path.islink(link) and os.readlink(link) == target:
            continue
        managed = os.path.islink(link) and os.readlink(link).startswith("/etc/alternatives/")
        if link == "/usr/bin/python3" and managed:
            # The image manages python3 through update-alternatives; drop that so it cannot point back.
            run(["update-alternatives", "--remove-all", "python3"])
        run(["ln", "-sfn", target, link])
        report.changed(f"pointed {link} at {target}")


# [@ANCHOR: provision_test_environment:odoo_changelog_entry]
def odoo_changelog_entry(sha, day, date_rfc2822, branch=ODOO_BRANCH):
    """The debian/changelog stanza for an Odoo package built from github.com/odoo/odoo `branch` at commit `sha`
    (12 hex digits) committed on `day` (YYYYMMDD). Version 19.0.0~git<day>.<sha>-1 sorts below any nightly 19.0
    release, so a later apt upgrade replaces it. Pure."""
    return (f"odoo (19.0.0~git{day}.{sha}-1) unstable; urgency=medium\n\n"
            f"  * Built from github.com/odoo/odoo {branch} at {sha} for a hams.com test environment.\n\n"
            f" -- hams.com test environment <noreply@hams.com>  {date_rfc2822}\n\n")


# [@ANCHOR: provision_test_environment:url_reachable]
def url_reachable(url, opener=urllib.request.urlopen, timeout=15):
    """True when `url` answers a HEAD request with 2xx or 3xx. A cloud session's proxy refuses some hosts
    (nightly.odoo.com, cdn.playwright.dev) with 403, which is False here."""
    try:
        with opener(urllib.request.Request(url, method="HEAD"), timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError):
        return False


# [@ANCHOR: provision_test_environment:provision_odoo]
def provision_odoo(run, report, systemd, plan=False, reachable=lambda: url_reachable(ODOO_KEY_URL)):
    """Installs the odoo package when dpkg lacks it: from nightly.odoo.com (the MANIFEST's apt source and key) when
    reachable, otherwise built with dpkg-buildpackage from a shallow clone of github.com/odoo/odoo ODOO_BRANCH in
    ODOO_DIR. Under systemd the odoo service the package starts is stopped and disabled: a test host never runs it."""
    if "odoo" in infrastructure._installed_apt_packages():
        return
    if reachable():
        source = next(f for f in infrastructure.MANIFEST["static_files"] if f["path"] == ODOO_SOURCE_LIST)
        if not plan:
            fetched = provision_cache.fetch(ODOO_KEY_URL)
            provision_cache.install(fetched, "/tmp/hams-odoo.key", 0o644)
            infrastructure.hook_install_odoo_key({}, "", "/tmp/hams-odoo.key", run)
            with open(ODOO_SOURCE_LIST, "w", encoding="utf-8") as out:
                out.write(source["content"])
        run(["apt-get", "update", "-q"])
        run(["apt-get", "install", "-y", "-q", "odoo"])
        report.changed("installed odoo from nightly.odoo.com")
    else:
        _build_odoo_from_github(run, report, plan)
    if systemd:
        run(["systemctl", "disable", "--now", "odoo"])


# [@ANCHOR: provision_test_environment:_build_odoo_from_github]
def _build_odoo_from_github(run, report, plan):
    _install_apt(run, ODOO_BUILD_APT_PACKAGES, "odoo-build", report)
    src = os.path.join(ODOO_DIR, "src")
    if not os.path.isdir(os.path.join(src, ".git")):
        if plan:
            print(f"PLAN run: git clone --depth 1 --branch {ODOO_BRANCH} https://github.com/odoo/odoo {src}")
        else:
            os.makedirs(ODOO_DIR, exist_ok=True)
            if os.path.exists(src):
                shutil.rmtree(src)
            retry(lambda: run(["git", "clone", "-q", "--depth", "1", "--branch", ODOO_BRANCH, "--single-branch",
                               "https://github.com/odoo/odoo", src]))
    debs = sorted(glob.glob(os.path.join(ODOO_DIR, "odoo_*_all.deb")))
    if not debs and not plan:
        sha = subprocess.run(["git", "-C", src, "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        day = subprocess.run(["git", "-C", src, "log", "-1", "--format=%cd", "--date=format:%Y%m%d"],
                             capture_output=True, text=True, check=True).stdout.strip()
        now = subprocess.run(["date", "-R"], capture_output=True, text=True, check=True).stdout.strip()
        run(["git", "-C", src, "checkout", "-q", "--", "debian/changelog"])
        changelog = os.path.join(src, "debian", "changelog")
        with open(changelog, encoding="utf-8") as current:
            text = odoo_changelog_entry(sha, day, now) + current.read()
        with open(changelog, "w", encoding="utf-8") as out:
            out.write(text)
        log_path = os.path.join(ODOO_DIR, "build.log")
        print(f"[test-environment] building the odoo package (several minutes); log {log_path}", flush=True)
        with open(log_path, "w", encoding="utf-8") as log:
            run(["dpkg-buildpackage", "-b", "-us", "-uc", "-d"], cwd=src, stdout=log, stderr=subprocess.STDOUT)
        debs = sorted(glob.glob(os.path.join(ODOO_DIR, "odoo_*_all.deb")))
        report.changed(f"built {debs[-1]}")
    deb = debs[-1] if debs else os.path.join(ODOO_DIR, "odoo_<version>_all.deb")
    run(["apt-get", "install", "-y", "-q", "--no-install-recommends", deb])
    report.changed(f"installed {deb}")


# [@ANCHOR: provision_test_environment:provision_kopia]
def provision_kopia(run, report, plan=False):
    """Installs /usr/bin/kopia from the MANIFEST's pinned release (sha256 checked by provision_cache) when no kopia is
    on PATH. The b2_backup tests need it; provisioning installs it for production only."""
    if shutil.which("kopia"):
        return
    spec = next(f for f in infrastructure.MANIFEST["static_files"] if f["path"] == "/tmp/kopia.tar.gz")
    arch = infrastructure._kopia_release_arch({})
    url = spec["url"].replace("{KOPIA_ARCH}", arch)
    if plan:
        print(f"PLAN fetch {url} and install /usr/bin/kopia", flush=True)
        return
    fetched = provision_cache.fetch(url, expected_sha256=spec["sha256_by_arch"][arch], timeout=300)
    provision_cache.install(fetched, "/tmp/hams-kopia.tar.gz", 0o644)
    infrastructure.hook_install_kopia_binary({"KOPIA_ARCH": arch}, "", "/tmp/hams-kopia.tar.gz", run)
    report.changed("installed /usr/bin/kopia")


# [@ANCHOR: provision_test_environment:provision_user_tools]
def provision_user_tools(layout, run, as_user, home, report, reachable=url_reachable):
    """As the runner user: `cargo install --locked cargo-deny` when cargo exists and `cargo deny` does not (the
    supply-chain lint step), the Playwright headless Chromium when the user's ~/.cache/ms-playwright has none (reported
    as skipped when its download host is unreachable), and `npm ci` in hams_shared when its node_modules has no
    ESLint."""
    if shutil.which("cargo") and subprocess.run(as_user(["cargo", "deny", "--version"]),
                                                capture_output=True).returncode != 0:
        run(as_user(["cargo", "install", "--locked", "-q", "cargo-deny"]))
        report.changed("installed cargo-deny")
    elif not shutil.which("cargo"):
        report.skip("cargo-deny: no cargo on PATH (install rustup to run the Rust lint steps)")
    if glob.glob(os.path.join(home, ".cache", "ms-playwright", "chromium_headless_shell-*")):
        pass
    elif not reachable(PLAYWRIGHT_CDN_URL):
        report.skip(f"Playwright headless Chromium: {PLAYWRIGHT_CDN_URL} is unreachable from this host; the tests "
                    "that launch Playwright's own Chromium (uk_ofcom_sync's browser tests) fail here")
    else:
        commands = infrastructure.playwright_test_browser_commands(None)
        run(commands[0])
        run(as_user(["env", "-u", "PLAYWRIGHT_BROWSERS_PATH"] + commands[1]))
        report.changed("installed the Playwright headless Chromium")
    if not os.path.exists(os.path.join(layout["hams_shared"], "node_modules", ".bin", "eslint")):
        run(as_user(["npm", "ci", "--no-audit", "--no-fund", "-q"]), cwd=layout["hams_shared"])
        report.changed("npm ci in hams_shared")


# [@ANCHOR: provision_test_environment:provision_ml_assets]
def provision_ml_assets(layout, run, report, plan=False, reachable=url_reachable, manifest_exists=os.path.isfile,
                        is_link=os.path.islink):
    """The browser ML assets a test host serves, made the way production makes them: the root /var/lib/odoo/hams_ml_assets
    owned by `odoo`, then hams_com's own `ham_shack/tools/provision_ml_assets.py` run as `odoo` (nice 10) when the root has
    no `manifest.json` (the script writes it last, so a half-finished download is repeated), then `--verify-relay-pins`. The pin
    verification runs on EVERY call that finds the script, also when the manifest exists: the script writes the manifest before
    the verification, so a first run whose verification failed leaves a manifest, and skipping the check on the next run would
    let a host serve assets that match no pin (local hashing, no network; a mismatch raises). No
    test-only file or route: ham_shack's TestMlAssetsRoute serves this real root. Unreachable download hosts or a hams_com
    checkout without the script are a reported skip (the two route tests then fail on that host). Network: npm and
    HuggingFace, once, about 1.8 GB. A symlink at the root is refused (RuntimeError): /var/lib/odoo belongs to `odoo`, so `install -d
    -o odoo` run as root on a link the odoo account planted would chown and chmod whatever it points at. Raises CalledProcessError."""
    script = os.path.join(layout["hams_com"], ML_ASSET_SCRIPT)
    manifest = os.path.join(ML_ASSET_ROOT, "manifest.json")
    if manifest_exists(manifest):
        if plan:
            print(f"PLAN {script} --verify-relay-pins as odoo (the assets exist)", flush=True)
        elif os.path.isfile(script):
            run(as_user_command("odoo", "/var/lib/odoo")(["python3", script, "--verify-relay-pins"]))
        return
    if plan:
        print(f"PLAN {ML_ASSET_ROOT} owned by odoo, then {script} as odoo and --verify-relay-pins", flush=True)
        return
    if not os.path.isfile(script):
        report.skip(f"browser ML assets: {script} does not exist; ham_shack's TestMlAssetsRoute fails on this host")
        return
    down = [host for host in ML_ASSET_HOSTS if not reachable(host)]
    if down:
        report.skip(f"browser ML assets: {', '.join(down)} unreachable from this host; ham_shack's TestMlAssetsRoute "
                    "fails here")
        return
    if is_link(ML_ASSET_ROOT):
        raise RuntimeError(f"browser ML assets: {ML_ASSET_ROOT} is a symbolic link; refusing to chown through it as root. "
                           "Inspect where it points, remove the link, and run the provisioning again.")
    as_odoo = as_user_command("odoo", "/var/lib/odoo")
    run(["install", "-d", "-o", "odoo", "-g", "odoo", ML_ASSET_ROOT])
    run(as_odoo(["nice", "-n", "10", "python3", script]))
    run(as_odoo(["python3", script, "--verify-relay-pins"]))
    report.changed(f"provisioned the browser ML assets under {ML_ASSET_ROOT}")


# [@ANCHOR: provision_test_environment:host_alias_line]
def host_alias_line(manifest):
    """The `127.0.0.1 postgres redis ...` line of the MANIFEST's test-environment /etc/hosts template: the service
    names test code connects to. Raises StopIteration/ValueError when the template or the line is missing. Pure."""
    template = next(f for f in manifest["static_files"]
                    if f["path"] == "/etc/hosts" and "test" in f.get("environments", []))
    for line in template["content"].splitlines():
        fields = line.split()
        if fields and fields[0] == "127.0.0.1" and "redis" in fields:
            return line
    raise ValueError("the test /etc/hosts template has no 127.0.0.1 service alias line")


# [@ANCHOR: provision_test_environment:missing_host_aliases]
def missing_host_aliases(hosts_text, alias_line):
    """The names of `alias_line` that no 127.0.0.1 line of `hosts_text` lists, in order. Pure."""
    present = set()
    for line in hosts_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if fields and fields[0] == "127.0.0.1":
            present.update(fields[1:])
    return [name for name in alias_line.split()[1:] if name not in present]


# [@ANCHOR: provision_test_environment:provision_host_aliases]
def provision_host_aliases(report, plan=False, path="/etc/hosts"):
    """Appends `127.0.0.1 <missing names>` to /etc/hosts when any service alias of host_alias_line() is missing
    (never rewrites the file: a cloud container's /etc/hosts carries its own entries and is reset on restart)."""
    alias_line = host_alias_line(infrastructure.MANIFEST)
    with open(path, encoding="utf-8") as hosts:
        text = hosts.read()
    missing = missing_host_aliases(text, alias_line)
    if not missing:
        return
    line = "127.0.0.1 " + " ".join(missing)
    if plan:
        print(f"PLAN append {path}: {line}", flush=True)
        return
    with open(path, "a", encoding="utf-8") as hosts:
        hosts.write(("" if text.endswith("\n") or not text else "\n") + line + "\n")
    report.changed(f"added {line} to {path}")


# [@ANCHOR: provision_test_environment:wait_until]
def wait_until(probe, timeout, interval=2, sleep=time.sleep, clock=time.monotonic):
    """Calls probe() (-> bool) every `interval` seconds until it returns True (then True) or `timeout` seconds have
    passed (then False). Blocks the caller."""
    deadline = clock() + timeout
    while True:
        if probe():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


# [@ANCHOR: provision_test_environment:offline_clusters]
def offline_clusters(pg_lsclusters_output):
    """[(version, name)] of the clusters `pg_lsclusters -h` lists as not online. Pure."""
    clusters = []
    for line in pg_lsclusters_output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] != "online":
            clusters.append((parts[0], parts[1]))
    return clusters


# [@ANCHOR: provision_test_environment:provision_services]
def provision_services(run, report, systemd, plan=False):
    """Starts what test.py needs. Under systemd: postgresql, redis-server and rabbitmq-server enabled and started
    (and nothing else). Without systemd (a cloud container): the offline PostgreSQL clusters with pg_ctlcluster, and
    redis-server and rabbitmq-server started as their own users when they do not answer (the daemon test suites
    need them on the host; test.py starts its own in its namespace)."""
    if systemd:
        for unit in ("postgresql", "redis-server", "rabbitmq-server"):
            active = subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
            enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", unit]).returncode == 0
            if not (active and enabled):
                run(["systemctl", "enable", "--now", unit])
                report.changed(f"enabled and started {unit}")
        return
    res = subprocess.run(["pg_lsclusters", "-h"], capture_output=True, text=True)
    for version, name in offline_clusters(res.stdout):
        run(["pg_ctlcluster", version, name, "start"])
        report.changed(f"started PostgreSQL {version}/{name}")
    # The daemon test suites run_linters.py runs talk to a host Redis and RabbitMQ, as on a systemd test host.
    if subprocess.run(["redis-cli", "ping"], capture_output=True, text=True).stdout.strip() != "PONG":
        run(["runuser", "-u", "redis", "--", "redis-server", "/etc/redis/redis.conf", "--daemonize", "yes"])
        report.changed("started redis-server (no systemd)")
    if subprocess.run(["rabbitmqctl", "-q", "status"], capture_output=True).returncode != 0:
        run(["runuser", "-u", "rabbitmq", "--", "rabbitmq-server", "-detached"])
        # `-detached` returns before the node registers with epmd, when await_startup still fails at once.
        if not plan and not wait_until(lambda: subprocess.run(["rabbitmqctl", "-q", "await_startup", "--timeout",
                                                               "10"], capture_output=True).returncode == 0, 120):
            raise RuntimeError("rabbitmq-server did not start within 120 s (see /var/log/rabbitmq)")
        report.changed("started rabbitmq-server (no systemd)")


# [@ANCHOR: provision_test_environment:role_sql]
def role_sql(runner_user):
    """The idempotent SQL that creates the odoo role and the runner's own role (both SUPERUSER LOGIN, as test.py's
    namespace provisioning does) when missing. Raises ValueError for a user name that is not a plain identifier."""
    if not _ROLE_RE.match(runner_user):
        raise ValueError(f"{runner_user!r} is not a plain PostgreSQL role name")
    sql = ("DO $$ BEGIN\n"
           "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'odoo') THEN "
           "CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD 'odoo'; END IF;\n")
    if runner_user not in ("odoo", "postgres"):
        sql += (f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{runner_user}') THEN "
                f"CREATE ROLE \"{runner_user}\" WITH SUPERUSER LOGIN; END IF;\n")
    return sql + "END $$;\n"


# [@ANCHOR: provision_test_environment:_psql]
def _psql(sql):
    return ["runuser", "-u", "postgres", "--", "psql", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql]


# [@ANCHOR: provision_test_environment:provision_database]
def provision_database(run, runner_user, report):
    """The odoo and runner roles (role_sql) and an empty TEST_DATABASE owned by odoo, each only when missing."""
    roles = subprocess.run(_psql("SELECT rolname FROM pg_roles"), capture_output=True, text=True, check=True)
    have = set(roles.stdout.split())
    wanted = {"odoo"} | ({runner_user} if runner_user not in ("postgres",) else set())
    if not wanted <= have:
        run(_psql(role_sql(runner_user)))
        report.changed(f"created PostgreSQL role(s) {' '.join(sorted(wanted - have))}")
    dbs = subprocess.run(_psql("SELECT datname FROM pg_database"), capture_output=True, text=True, check=True)
    if TEST_DATABASE not in dbs.stdout.split():
        run(["runuser", "-u", "postgres", "--", "createdb", "-O", "odoo", TEST_DATABASE])
        report.changed(f"created the empty {TEST_DATABASE} database")


# [@ANCHOR: provision_test_environment:provision_test_host_requirements]
def provision_test_host_requirements(run, report, plan=False):
    """What `provision.py --test` provides and a test run inside `sudo -u odoo env HAMS_ISOLATED_NS=1` does not
    (host_requirements.missing_requirements lists it): the MANIFEST test system accounts (the hamsd_* ones) and
    every MANIFEST directory (/var/lib/odoo/backups, the key directories) through the same infrastructure calls
    --test makes; the cache_manager_ro PostgreSQL role and its login file (the `DB_USER and DB_PASS must both be
    set` failure), with the pg_hba.conf rule that lets the daemon log in over the socket; and a system-wide git
    `safe.directory = *`, so the odoo user can read checkouts it does not own. Needs the database
    (provision_database first). The profile's HAMS_CRYPTO_KEY is not checked here: it reaches test.py through
    `sudo -u odoo env`. Idempotent. Raises RuntimeError when a requirement is still missing afterwards. Production
    code in infrastructure.py is called, not changed; production never reaches this."""
    before = host_requirements.missing_requirements(check_crypto_key=False)
    infrastructure.provision_system_accounts(run, environment="prod")
    infrastructure.provision_system_accounts(run, environment="test")
    infrastructure.apply_production_directories(run, environment="prod")
    infrastructure.apply_production_directories(run, environment="test")
    if plan:
        print("PLAN cache_manager_ro role and login file, its pg_hba.conf rule and git safe.directory = *", flush=True)
        return
    infrastructure._provision_cache_manager_role(run, TEST_DATABASE)
    if infrastructure._allow_cache_manager_socket_password_for_test():
        run(_psql("SELECT pg_reload_conf()"))
    if not host_requirements.git_trusts_every_directory():
        run(["git", "config", "--system", "--add", "safe.directory", "*"])
    still = host_requirements.missing_requirements(check_crypto_key=False)
    if still:
        raise RuntimeError("the test host still lacks: " + "; ".join(text for _code, text in still))
    if before:
        report.changed("provisioned " + ", ".join(code for code, _text in before))


# [@ANCHOR: provision_test_environment:parse_profile]
def parse_profile(text):
    """{name: value} of the `export NAME=value` lines in a profile written by profile_text. Pure."""
    values = {}
    for line in text.splitlines():
        match = _PROFILE_LINE_RE.match(line.strip())
        if match:
            values[match.group(1)] = shlex.split(match.group(2))[0] if match.group(2) else ""
    return values


# [@ANCHOR: provision_test_environment:profile_text]
def profile_text(values):
    """The /etc/profile.d file for `values` ({name: value}, written sorted, each shell-quoted). Pure."""
    lines = ["# Written by hams_shared/tools/provision.py --test-environment. Re-running it keeps HAMS_CRYPTO_KEY.",
             "# HAMS_CRYPTO_KEY is a random per-host key for test databases: never a real secret, never copy it."]
    lines += [f"export {name}={shlex.quote(value)}" for name, value in sorted(values.items())]
    # The user-level toolchains (rustup's cargo, rustfmt and clippy at the pinned version; pip --user scripts) come before the distribution's older
    # /usr/bin/cargo, which has neither rustfmt nor clippy (agents' shells lacked them until 2026-11-08). Not an `export NAME=value` line: parse_profile
    # ignores it, and it holds no per-host value.
    lines += ['for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do',
              '  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH" ;; esac',
              "done",
              "export PATH"]
    return "\n".join(lines) + "\n"


# [@ANCHOR: provision_test_environment:profile_values]
def profile_values(existing, environ, layout, home_mode):
    """The variables for the profile: HAMS_CRYPTO_KEY (the existing one, else 32 random bytes in base64, the format
    provisioning generates); HAMS_BATCH_TREE_DIR=<workspace>/workspace/tmp when the runner's home is not traversable
    by others (mode lacks o+x: the odoo user could not read a batch tree under it); HAMS_LINT_REMOTE=local in a
    cloud session (the LAN lint machines are unreachable). `existing` is parse_profile of the current file."""
    values = {"HAMS_CRYPTO_KEY": existing.get("HAMS_CRYPTO_KEY")
              or base64.b64encode(secrets.token_bytes(32)).decode("ascii")}
    if not home_mode & stat.S_IXOTH:
        values["HAMS_BATCH_TREE_DIR"] = os.path.join(layout["workspace"], "workspace", "tmp")
    if environ.get("CLAUDE_CODE_REMOTE") == "true":
        values["HAMS_LINT_REMOTE"] = "local"
    return values


# [@ANCHOR: provision_test_environment:provision_profile]
def provision_profile(layout, environ, home, runner_user, report, plan=False, path=PROFILE_PATH):
    """Writes PROFILE_PATH (mode 644) when its content would change, creates HAMS_BATCH_TREE_DIR (mode 755, owned by
    the runner) when set, and adds one line sourcing the profile to the runner's ~/.bashrc (non-login shells skip
    /etc/profile.d). Returns the values. The key is never printed."""
    try:
        with open(path, encoding="utf-8") as current:
            old_text = current.read()
    except FileNotFoundError:
        old_text = ""
    values = profile_values(parse_profile(old_text), environ, layout, stat.S_IMODE(os.stat(home).st_mode))
    text = profile_text(values)
    if plan:
        print(f"PLAN write {path} ({', '.join(sorted(values))})", flush=True)
        return values
    if text != old_text:
        with open(path, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(path, 0o644)
        report.changed(f"wrote {path} ({', '.join(sorted(values))})")
    batch = values.get("HAMS_BATCH_TREE_DIR")
    if batch and not os.path.isdir(batch):
        os.makedirs(batch, mode=0o755)
        for directory in (os.path.dirname(batch), batch):
            os.chmod(directory, 0o755)
            shutil.chown(directory, runner_user)
        report.changed(f"created {batch}")
    bashrc = os.path.join(home, ".bashrc")
    line = f"[ -f {path} ] && . {path}"
    try:
        with open(bashrc, encoding="utf-8") as current:
            has_line = line in current.read()
    except FileNotFoundError:
        has_line = False
    if not has_line:
        with open(bashrc, "a", encoding="utf-8") as out:
            out.write(line + "\n")
        shutil.chown(bashrc, runner_user)
        report.changed(f"added the profile to {bashrc}")
    return values


# [@ANCHOR: provision_test_environment:self_check_commands]
def self_check_commands(layout, hams_com_clean):
    """[(label, argv, cwd)] of the self-check, and [why] for what is left out: run_linters.py --changed in hams_com
    (only on a clean tree: a dirty one would lint someone's work in progress) and test.py -u zero_sudo. Pure."""
    commands, skipped = [], []
    if hams_com_clean:
        commands.append(("run_linters.py --changed", ["python3", "tools/run_linters.py", "--changed"],
                         layout["hams_com"]))
    else:
        skipped.append("run_linters.py --changed: hams_com has uncommitted changes")
    commands.append(("test.py -u zero_sudo", ["python3", "hams_shared/tools/test.py", "-u", "zero_sudo", "-d",
                                              TEST_DATABASE], layout["hams_com"]))
    return commands, skipped


# [@ANCHOR: provision_test_environment:run_self_check]
def run_self_check(layout, env, as_user, report, runner=subprocess.run):
    """Runs self_check_commands as the runner user with `env` (the profile values applied). Returns the labels that
    failed; the caller exits 1 when any did."""
    status = runner(["git", "-C", layout["hams_com"], "status", "--porcelain"], capture_output=True, text=True)
    commands, skipped = self_check_commands(layout, status.returncode == 0 and not status.stdout.strip())
    for why in skipped:
        report.skip(f"self-check {why}")
    failed = []
    for label, argv, cwd in commands:
        print(f"[test-environment] self-check: {label} (in {cwd})", flush=True)
        if runner(as_user(argv), cwd=cwd, env=env).returncode != 0:
            failed.append(label)
            print(f"[test-environment] self-check FAILED: {label}", flush=True)
    return failed


# [@ANCHOR: provision_test_environment:as_user_command]
def as_user_command(user, home):
    """A function argv -> argv that runs a command as `user` (runuser, HOME set to `home`) when this process is root
    and the user is not root, and returns argv unchanged otherwise. Pure."""
    def wrap(argv):
        if os.geteuid() != 0 or not user or user == "root":
            return list(argv)
        return ["runuser", "-u", user, "--", "env", f"HOME={home}"] + list(argv)
    return wrap


# [@ANCHOR: provision_test_environment:acquire_lock]
def acquire_lock(path=LOCK_PATH, wait=True):
    """An exclusive flock on `path` (created mode 600) held for the rest of the run: two provisioning runs at once
    raced each other's Odoo package build. Returns the open file, which must stay referenced; the
    lock is released when the process exits. When another run holds it: waits (printing that it does) when `wait`,
    else raises RuntimeError."""
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not wait:
            handle.close()
            raise RuntimeError(f"another provision.py --test-environment run holds {path}")
        print(f"[test-environment] waiting for another provision.py --test-environment run ({path})", flush=True)
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


# [@ANCHOR: provision_test_environment:start]
def start(run, env_vars, orig_user, tools_dir, repo_root, workspace=None, git_key=None, plan=False):
    """Steps 1 and 2 of the module docstring: refuses production (marker files and the hams_prod database), finds the
    layout, picks the push credential and clones what is missing. `run` is provision.py's run_sys (check=True; prints
    instead under --plan). Returns the state dict finish() takes. Exits 2 on a production host; raises ValueError for a
    bad --git-key or an occupied checkout path."""
    report = Report(plan)
    refuse_production(database_names=list_database_names())
    lock = None if plan else acquire_lock()
    runner_user = orig_user or "root"
    home = os.path.expanduser(f"~{runner_user}")
    as_user = as_user_command(runner_user, home)
    systemd = os.path.isdir("/run/systemd/system")
    layout = discover_layout(tools_dir, repo_root, workspace)
    report.note(f"workspace {layout['workspace']}, runner {runner_user}, systemd {'yes' if systemd else 'no'}")
    credentials = choose_credentials(env_vars, gh_authenticated(as_user), git_key)
    report.note(credentials["note"])
    provision_repositories(layout, credentials, run, as_user, report, plan)
    return {"report": report, "layout": layout, "runner_user": runner_user, "home": home, "as_user": as_user,
            "systemd": systemd, "lock": lock}


# [@ANCHOR: provision_test_environment:finish]
def finish(state, run, env_vars, os_id, self_check=True, plan=False):
    """Steps 3 to 10 of the module docstring on the state start() returned. Returns the Report; exits 1 when the
    self-check fails. Under --plan nothing is changed and the self-check does not run."""
    report, layout, as_user = state["report"], state["layout"], state["as_user"]
    runner_user, home, systemd = state["runner_user"], state["home"], state["systemd"]
    provision_cloud_python(env_vars, run, report, plan)
    no_candidate = provision_apt_packages(run, os_id, report, plan)
    provision_pip_packages(run, no_candidate, report)
    provision_odoo(run, report, systemd, plan)
    provision_kopia(run, report, plan)
    provision_host_aliases(report, plan)
    provision_services(run, report, systemd, plan)
    if not plan:
        provision_database(run, runner_user, report)
    provision_test_host_requirements(run, report, plan)
    provision_user_tools(layout, run, as_user, home, report)
    provision_ml_assets(layout, run, report, plan)
    values = provision_profile(layout, env_vars, home, runner_user, report, plan)
    report.note(f"{len(report.changes)} change(s) to this host; {len(report.skipped)} step(s) skipped")
    for text in report.skipped:
        report.note(f"  skipped: {text}")
    if plan:
        return report
    if not self_check:
        report.note("self-check not run (--no-self-check)")
        return report
    env = dict(env_vars)
    env.update(values)
    env["HOME"] = home
    failed = run_self_check(layout, env, as_user, report)
    if failed:
        print(f"[test-environment] FAILED self-check: {', '.join(failed)}", flush=True)
        sys.exit(1)
    report.note("self-check passed: run_linters.py --changed and test.py -u zero_sudo")
    return report
