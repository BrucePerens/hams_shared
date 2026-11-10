# SPDX-License-Identifier: AGPL-3.0-or-later
"""The code of the image: /opt/hams/src checked out from git at the commits production recorded in DEPLOYED_COMMITS, then the binaries the
MANIFEST hooks build on production (Rust daemons, libcloudflared.so) built again in the image. None of this is in a backup; it comes from
the network like the packages."""
import os
import shutil
import subprocess

from .runner import RestorationError

SRC = "opt/hams/src"
DAEMONS = "opt/hams/daemons"


def parse_deployed_commits(text):
    """{name: commit} from the `name=value` lines of DEPLOYED_COMMITS."""
    return {k.strip(): v.strip() for k, _, v in (line.partition("=") for line in (text or "").splitlines()) if v.strip()}


def install_code(profile, tree, deployed_text, say):
    """Clone each profile code repository into the image at its deployed commit. Returns {"installed": {name: commit}} (empty and with a
    "skipped" reason when the backups carry no DEPLOYED_COMMITS)."""
    commits = parse_deployed_commits(deployed_text)
    repos = profile.get("code_repos", {})
    if not commits or not repos:
        say("  code: no DEPLOYED_COMMITS in the config set (or no code_repos in the profile); /opt/hams/src is not restored")
        return {"installed": {}, "skipped": "no DEPLOYED_COMMITS in the backups"}
    done = {}
    for name, url in repos.items():
        commit = commits.get(name)
        if not commit:
            raise RestorationError("DEPLOYED_COMMITS has no commit for %s" % name)
        dest = os.path.join(tree, SRC, name)
        for argv in (["git", "clone", "--quiet", url, dest], ["git", "-C", dest, "checkout", "--quiet", commit],
                     ["git", "-C", dest, "submodule", "update", "--init", "--quiet"]):
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise RestorationError("%s failed: %s" % (" ".join(argv[:4]), proc.stderr.strip()[-300:]))
        done[name] = commit
        say("  code: %s at %s" % (name, commit))
    return {"installed": done}


def build_artifacts(infra, tree, chroot, say):
    """Copy the daemons of both repositories to /opt/hams/daemons (flat, as deploy_to_production --sync-daemons does) and build the Rust
    crates and libcloudflared.so inside the image. Returns what was built; a failed build stops the image build."""
    dest = os.path.join(tree, DAEMONS)
    os.makedirs(dest, exist_ok=True)
    for repo in ("hams_open", "hams_com"):
        source = os.path.join(tree, SRC, repo, "daemons")
        if os.path.isdir(source):
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    built = []
    for crate in infra.RUST_DAEMON_CRATES:
        if os.path.exists(os.path.join(dest, crate, "Cargo.toml")):
            inside = "/%s/%s" % (DAEMONS, crate)
            chroot.run(["cargo", "build", "--release", "--manifest-path", inside + "/Cargo.toml", "--target-dir", inside + "/target"])
            built.append(crate)
    if os.path.isdir(os.path.join(dest, "cloudflared-ffi")):
        chroot.run(["python3", "-c", "import sys; sys.path.insert(0, '/%s/hams_open/hams_shared/tools'); import infrastructure as i; "
                    "i.cffi_build('/%s/cloudflared-ffi')" % (SRC, DAEMONS)])
        built.append("cloudflared-ffi")
    chroot.run(["find", "-P", "/" + DAEMONS, "-user", "root", "-exec", "chown", "-h", "hams_com:hams_com", "{}", "+"])
    say("  built: %s" % ", ".join(built))
    return built
