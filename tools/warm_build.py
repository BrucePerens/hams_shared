#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Warm build directories shared by parallel runs: an overlay filesystem over a read-only stack of layers, one per run, no lock between runs.

A slow build step (the relay's cargo tests, later OpenWrt SDK or bundler caches) used to hold ONE cache directory for its whole run, so every run waited for the one
before it, and a copy per slot cost several GB of RAM each. Here each run mounts its own overlay (`unshare -rm`, no root needed) over the warm STACK of its key, with a
fresh upper directory of its own, so the tool rebuilds only what the run's tree changed and runs never block one another. The upper is thrown away afterwards.

The caller plugs in only: the `base` directory (the lint Cargo base in RAM, or any directory on a file system with `rename`, for example /home on btrfs for a big cache), a
`key` naming the stack (project, toolchain version, target triple, profile and features), `promote` (True when the run builds exactly main) and the environment
variable that points its tool at `run.mount` (`CARGO_TARGET_DIR=<mount>/target`). Everything the tool must keep warm goes below `run.mount`; the mount point is the same
path for every run of a key (tools hash absolute paths into their fingerprints) and lies beside `base`, never in it.

    with warm_build.Run(base, key, promote) as run:
        done = subprocess.run(run.argv(command))      # once or several times; exit WARM_MOUNT_FAILED (125): the mount failed, use the plain directory
        run.passed = done.returncode == 0

Stack: layers named `warm-<key>-<id>` directly in `base` (so the RAM budget of `ram_dirs.prune_lint_cache` counts them), newest first, published as the TARGET TEXT of the
symlink `warm-<key>-current` (`layer:layer:...`). A run reads the link once and holds a SHARED flock on every layer it mounts; publishing replaces the link with
`os.replace` (atomic), so it never disturbs a mounted run. A passing run that builds exactly main keeps its upper as the new top layer (promotion: nothing is copied; a
lower layer never changes while mounted); any other run's upper is always discarded. With no stack yet the CALLER builds the base from a main checkout first (`promote=True`).

Flatten: at WARM_MAX_LAYERS a `<key>.draining` flag makes new runs wait briefly (WARM_DRAIN_WAIT seconds) so the mounts end, and whenever a run ends it tries, without
waiting, to take the EXCLUSIVE flock of every layer; when it has them all it merges the layers into the bottom one in place (renaming files over, applying whiteouts and
opaque directories, under a `.merging` mark that makes a half-merged bottom count as no stack) and publishes the bottom alone. A layer no stack names, and the directory of
a run that is gone, is deleted when its exclusive flock can be taken. Linux only; `available()` is False elsewhere or when mounts are refused.
"""

import fcntl
import os
import shutil
import stat
import subprocess
import time

WARM_MAX_LAYERS = 8
WARM_MOUNT_FAILED = 125
WARM_DRAIN_WAIT = 60.0
DRAIN_STALE_SECONDS = 600
_MOUNT_SCRIPT = 'mount -t overlay overlay -o "lowerdir=$1,upperdir=$2,workdir=$3" "$4" || exit %d; shift 4; exec "$@"' % WARM_MOUNT_FAILED


def _layer_prefix(key):
    return "warm-%s-" % key


def _current(base, key):
    return os.path.join(base, "warm-%s-current" % key)


def mount_point(base, key):
    """The one path every run of `key` sees its overlay at: `<dirname(base)>/hams-warm-<key>`."""
    return os.path.join(os.path.dirname(os.path.abspath(base)), "hams-warm-" + key)


def available(base, environ=None):
    """True when an unprivileged overlay mount works here: a real mount of an empty lower in a throwaway directory of `base`."""
    probe = os.path.join(base, "warm-run-probe-%d" % os.getpid())
    try:
        paths = [os.path.join(probe, name) for name in ("lower", "upper", "work", "mnt")]
        for path in paths:
            os.makedirs(path)
        done = subprocess.run(["unshare", "-rm", "sh", "-c", _MOUNT_SCRIPT, "sh"] + paths + ["true"], capture_output=True, env=environ)
        return done.returncode == 0
    except OSError:
        return False
    finally:
        _rmtree(probe)


def _names(base, key):
    try:
        return [n for n in os.readlink(_current(base, key)).split(":") if n]
    except OSError:
        return []


def current_stack(base, key):
    """The published layer names of `key`, newest first; [] when none, when a named layer is gone, or when the bottom one is half merged (start cold)."""
    names = _names(base, key)
    if not all(os.path.isdir(os.path.join(base, n)) for n in names):
        return []
    if names and os.path.exists(os.path.join(base, names[-1], ".merging")):
        return []
    return names


def publish(base, key, names):
    """Make `names` (newest first) the stack of `key`: a new symlink renamed over the old one, atomically."""
    temp = os.path.join(base, ".warm-%s-%d-%d" % (key, os.getpid(), time.monotonic_ns()))
    os.symlink(":".join(names), temp)
    os.replace(temp, _current(base, key))


def _try_lock(path, flag):
    handle = open(path.rstrip(os.sep) + ".lock", "a+")
    try:
        fcntl.flock(handle, flag | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def _rmtree(full):
    """Remove a run directory: the overlay's own `work/work` directory is mode 000 and has to be opened up first."""
    try:
        os.chmod(os.path.join(full, "work", "work"), 0o700)
    except OSError:
        pass
    shutil.rmtree(full, ignore_errors=True)


def _drop(full):
    _rmtree(full)
    try:
        os.unlink(full + ".lock")
    except OSError:
        pass


def collect_garbage(base, key):
    """Delete the layers of `key` that no stack names and the run directories of runs that are gone, each only while its lock can be taken exclusively. Never raises."""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return
    named = set(_names(base, key))
    for name in entries:
        full = os.path.join(base, name)
        if not os.path.isdir(full) or os.path.islink(full):
            continue
        if (name.startswith("warm-run-%s-" % key)) or (name.startswith(_layer_prefix(key)) and name not in named and not name.endswith("-current")):
            lock = _try_lock(full, fcntl.LOCK_EX)
            if lock:
                try:
                    _drop(full)
                finally:
                    lock.close()


def _is_whiteout(path):
    info = os.lstat(path)
    return stat.S_ISCHR(info.st_mode) and info.st_rdev == 0


def _is_opaque(path):
    for attr in ("user.overlay.opaque", "trusted.overlay.opaque"):
        try:
            if os.getxattr(path, attr, follow_symlinks=False) == b"y":
                return True
        except OSError:
            continue
    return False


def apply_layer(lower, upper):
    """Merge the layer directory `upper` into `lower` in place, as an overlay mount of both would show it: files are renamed over, a whiteout (character device 0/0)
    deletes the path, an opaque directory replaces the one below. `upper` is emptied."""
    for name in sorted(os.listdir(upper)):
        source, target = os.path.join(upper, name), os.path.join(lower, name)
        if _is_whiteout(source):
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            elif os.path.lexists(target):
                os.unlink(target)
            os.unlink(source)
        elif os.path.isdir(source) and not os.path.islink(source):
            if _is_opaque(source) and os.path.lexists(target):
                shutil.rmtree(target) if os.path.isdir(target) and not os.path.islink(target) else os.unlink(target)
            if os.path.isdir(target) and not os.path.islink(target):
                apply_layer(target, source)
                os.rmdir(source)
            else:
                if os.path.lexists(target):
                    os.unlink(target)
                os.rename(source, target)
        else:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            os.rename(source, target)  # replaces a file or link


def _clear_opaque_marks(path):
    for attr in ("user.overlay.opaque", "trusted.overlay.opaque"):
        try:
            os.removexattr(path, attr, follow_symlinks=False)
        except OSError:
            pass


def flatten(base, key):
    """Merge the stack of `key` into its bottom layer, when no run has a layer mounted: the exclusive flock of every layer is tried without waiting, and nothing is
    done (False) when one is held. Returns True when the stack is now the bottom layer alone. Never raises."""
    stack = current_stack(base, key)
    if len(stack) < 2:
        return False
    locks = []
    try:
        for name in stack:
            handle = _try_lock(os.path.join(base, name), fcntl.LOCK_EX)
            if not handle:
                return False
            locks.append(handle)
        if current_stack(base, key) != stack:  # someone published meanwhile
            return False
        bottom = os.path.join(base, stack[-1])
        mark = os.path.join(bottom, ".merging")
        open(mark, "w").close()
        for name in reversed(stack[:-1]):
            apply_layer(bottom, os.path.join(base, name))
        _clear_opaque_marks(bottom)
        os.unlink(mark)
        publish(base, key, [stack[-1]])
        return True
    except OSError:
        return False
    finally:
        for handle in locks:
            handle.close()
        collect_garbage(base, key)


class Run:
    """One run's overlay for `key`; see the module text. `run.mount` is the path to put the tool's directories under; `run.passed` must be set True for a promotion."""

    def __init__(self, base, key, promote):
        if not key or not all(c.isalnum() or c in "_." for c in key):
            raise ValueError("warm_build key %r: letters, digits, '_' and '.' only (a '-' would let one key's names match another's)" % (key,))
        self.base, self.key, self.promote = base, key, promote
        self.passed = False
        self.published = False

    def _wait_while_draining(self):
        flag = os.path.join(self.base, self.key + ".draining")
        deadline = time.monotonic() + WARM_DRAIN_WAIT
        while time.monotonic() < deadline:
            try:
                if time.time() - os.stat(flag).st_mtime > DRAIN_STALE_SECONDS:
                    return
            except OSError:
                return
            time.sleep(0.5)

    def __enter__(self):
        base, key = self.base, self.key
        collect_garbage(base, key)
        self._wait_while_draining()
        self.mount = mount_point(base, key)
        os.makedirs(self.mount, exist_ok=True)
        self.id = "%d-%d" % (time.time_ns(), os.getpid())
        self.dir = os.path.join(base, "warm-run-%s-%s" % (key, self.id))
        self.run_lock = open(self.dir + ".lock", "a+")  # held for the whole run: garbage collection and the budget prune leave this directory alone
        fcntl.flock(self.run_lock, fcntl.LOCK_EX)
        for name in ("upper", "work", "empty"):
            os.makedirs(os.path.join(self.dir, name))
        self.layer_locks, self.lowers = [], []
        stack = current_stack(base, key)
        for name in stack:
            handle = open(os.path.join(base, name) + ".lock", "a+")
            fcntl.flock(handle, fcntl.LOCK_SH)  # a mounted layer is never deleted or merged: both need the exclusive lock
            self.layer_locks.append(handle)
            if os.path.isdir(os.path.join(base, name)) and not os.path.exists(os.path.join(base, name, ".merging")):
                self.lowers.append(os.path.join(base, name))
            else:  # pruned or merged between reading the link and locking it: start cold rather than with a hole in the stack
                self.lowers = []
                break
        return self

    def argv(self, command):
        """`command` (an argv list) wrapped to run in a new user and mount namespace with the overlay mounted at `self.mount`. Exit 125: the mount failed."""
        lower = ":".join(self.lowers) if self.lowers else os.path.join(self.dir, "empty")
        return (["unshare", "-rm", "sh", "-c", _MOUNT_SCRIPT, "sh", lower, os.path.join(self.dir, "upper"), os.path.join(self.dir, "work"), self.mount]
                + list(command))

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self.passed and self.promote:
                self._promote()
        finally:
            for handle in self.layer_locks:
                handle.close()
            self.run_lock.close()
            _drop(self.dir)
            collect_garbage(self.base, self.key)
            self._flatten_when_full()
        return False

    def _flatten_when_full(self):
        flag = os.path.join(self.base, self.key + ".draining")
        if len(current_stack(self.base, self.key)) < WARM_MAX_LAYERS:
            try:
                os.unlink(flag)
            except OSError:
                pass
            return
        open(flag, "a").close()
        os.utime(flag)
        if flatten(self.base, self.key):
            try:
                os.unlink(flag)
            except OSError:
                pass

    def _promote(self):
        name = "%s%s" % (_layer_prefix(self.key), self.id)
        layer = os.path.join(self.base, name)
        guard = open(layer + ".lock", "a+")  # held until the stack names the layer: garbage collection must not take it in between
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            os.rename(os.path.join(self.dir, "upper"), layer)  # same file system: nothing is copied
            publish(self.base, self.key, [name] + [os.path.basename(path) for path in self.lowers])
            self.published = True
        except OSError:
            shutil.rmtree(layer, ignore_errors=True)
        finally:
            guard.close()
