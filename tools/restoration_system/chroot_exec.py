# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs one command inside the image directory, in a private mount namespace (so /proc and /dev are gone when it ends).

Started as `unshare --mount --propagation private python3 chroot_exec.py TREE COMMAND...` by assemble.Chroot. Needs root.
"""
import os
import subprocess
import sys


def main(argv):
    tree, command = argv[1], argv[2:]
    if not command:
        sys.exit("chroot_exec: no command")
    for source, target, extra in (("/dev", "dev", ["--rbind"]), ("proc", "proc", ["-t", "proc"]), ("/sys", "sys", ["--rbind"])):
        os.makedirs(os.path.join(tree, target), exist_ok=True)
        subprocess.run(["mount", *extra, source, os.path.join(tree, target)], check=True)
    os.chroot(tree)
    os.chdir("/")
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8", "HOME": "/root", "TERM": "dumb"}
    env.update({k: v for k, v in os.environ.items() if k.startswith("HAMS_CHROOT_")})
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main(sys.argv)
