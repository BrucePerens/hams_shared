# SPDX-License-Identifier: AGPL-3.0-or-later
"""`verify IMAGE`: static checks of the directory, then boot it in a virtual machine with NO network and test that it works.

Isolation is the safety mechanism: the image holds production credentials and every production unit is enabled, so a route out would let a
test boot call real services (mail, Stripe, Cloudflare, regulators). The VM is started with no network device at all (QEMU `-nic none`,
`-nodefaults`, no host directories shared) and boots to a gate service ONLY, which proves from inside that no way out exists; only then are the
production units started. Isolation that cannot be proven means the services are never started and verify FAILS. The VM runs on a COPY of the
image (an ext4 disk built from the directory); the directory is never written.
"""
import contextlib
import dataclasses
import fnmatch
import json
import os
import shutil
import socket
import subprocess
import threading
import time

from . import classify as cl
from . import units as un
from .runner import RestorationError

HERE = os.path.dirname(os.path.abspath(__file__))
SERIAL_MARK = "HAMSVERIFY "
GATE_UNIT = """\
[Unit]
Description=Restoration verify: prove isolation, then start the production units and test
DefaultDependencies=yes

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/lib/hams-verify/guest_agent.py run
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyS0
"""


@dataclasses.dataclass
class Check:
    name: str
    status: str  # ok, FAIL, warn
    detail: str = ""


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail="", warn=False):
        self.checks.append(Check(name, "ok" if ok else ("warn" if warn else "FAIL"), detail))
        return ok

    @property
    def ok(self):
        return bool(self.checks) and all(c.status != "FAIL" for c in self.checks)

    def text(self):
        lines = ["%-4s %s%s" % (c.status, c.name, (" -- " + c.detail) if c.detail else "") for c in self.checks]
        lines.append("RESULT: " + ("ALL PASS" if self.ok else "FAILED"))
        return "\n".join(lines)


def static_checks(image, profile, infra, report):
    """Checks that need no VM: description files, the unit set and its enablement against the MANIFEST, accounts, directories, packages."""
    meta_path = os.path.join(image, "RESTORATION_META.json")
    if not report.add("image has RESTORATION_META.json", os.path.isfile(meta_path)):
        return None
    with open(meta_path) as handle:
        meta = json.load(handle)
    for name in ("EXCLUDE.txt", "RESTORE.md", "usr/local/sbin/hams-restore-post"):
        report.add("image has " + name, os.path.exists(os.path.join(image, name)))
    for machine in ("etc/fstab", "etc/hostname", "etc/machine-id") + tuple(profile.get("must_not_contain", [])):
        report.add("image has no machine-specific %s" % machine, not os.path.lexists(os.path.join(image, machine)))
    if profile.get("use_manifest", True):
        classes = meta.get("host_classes", [])
        expected = un.expected_units(infra, classes)
        missing = [u for u in expected if not un.unit_file_present(image, u)]
        report.add("every production unit is in the image (%d)" % len(expected), not missing, ", ".join(missing[:10]))
        wrong = [u for u, i in sorted(expected.items()) if i["enabled"] != un.enabled_in_tree(image, u) and u not in profile.get("enablement_exceptions", [])]
        report.add("unit enablement equals production", not wrong, "differs: " + ", ".join(wrong[:10]))
        ext = {u: i for u, i in expected.items() if i["external_fetch"]}
        off = [u for u, i in ext.items() if i["enabled"] and not un.enabled_in_tree(image, u)]
        report.add("every external-fetch unit is enabled as in production (%d units)" % len(ext), not off, "not enabled: " + ", ".join(off[:10]))
        users = {ln.split(":")[0] for ln in open(os.path.join(image, "etc/passwd")) if ln.strip()}
        lost = [a["user"] for a in infra.MANIFEST["system_accounts"] if "prod" in a.get("environments", ["prod"]) and a["user"] not in users]
        report.add("every MANIFEST account is in the image", not lost, ", ".join(lost[:10]))
        dirs = [d["path"] for d in infra.MANIFEST["directories"] if "prod" in d["environments"] and infra._in_host_class(d, classes)]
        nodir = [d for d in dirs if not os.path.lexists(os.path.join(image, d.lstrip("/")))]
        report.add("every MANIFEST directory is in the image (%d)" % len(dirs), not nodir, ", ".join(nodir[:10]))
    status = open(os.path.join(image, "var/lib/dpkg/status")).read() if os.path.exists(os.path.join(image, "var/lib/dpkg/status")) else ""
    from . import assemble as asm
    installed = asm.parse_dpkg_status(status)
    lacking = [p for p in meta.get("packages", {}) if p not in installed]
    report.add("every package of the list is installed (%d)" % len(meta.get("packages", {})), not lacking, ", ".join(lacking[:10]))
    return meta


def expected_services(profile, infra, meta):
    if not profile.get("use_manifest", True):
        return [], []
    units = un.expected_units(infra, meta.get("host_classes", []))
    ext = sorted(u for u, i in units.items() if i["external_fetch"])
    return sorted(units), ext


def disk_size_mb(image):
    total = 0
    for current, dirs, files in os.walk(image):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(current, name)).st_size
    return int(total / 1048576 * 1.25) + 1024


def build_disk(image, disk, config, runner_run=subprocess.run):
    """ext4 disk image from the directory (mke2fs -d keeps owners, modes, links), then the verify layer is added to the DISK only."""
    size = disk_size_mb(image)
    with open(disk, "wb") as handle:
        handle.truncate(size * 1048576)
    runner_run(["mke2fs", "-q", "-t", "ext4", "-d", image, "-L", "hamsroot", "-E", "lazy_itable_init=1,root_owner=0:0", disk], check=True)
    stage = disk + ".stage"
    os.makedirs(stage, exist_ok=True)
    with open(os.path.join(stage, "config.json"), "w") as handle:
        json.dump(config, handle)
    shutil.copy(os.path.join(HERE, "guest_agent.py"), os.path.join(stage, "guest_agent.py"))
    with open(os.path.join(stage, "hams-verify-gate.service"), "w") as handle:
        handle.write(GATE_UNIT)
    with open(os.path.join(stage, "hosts"), "w") as handle:
        handle.write("127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n")
    script = ["rm /etc/hosts", "write %s /etc/hosts" % os.path.join(stage, "hosts"), "mkdir /usr/local/lib/hams-verify", "write %s /usr/local/lib/hams-verify/config.json" % os.path.join(stage, "config.json"),
              "write %s /usr/local/lib/hams-verify/guest_agent.py" % os.path.join(stage, "guest_agent.py"),
              "write %s /etc/systemd/system/hams-verify-gate.service" % os.path.join(stage, "hams-verify-gate.service")]
    runner_run(["debugfs", "-w", "-f", "/dev/stdin", disk], input="\n".join(script) + "\n", text=True, check=True, capture_output=True)
    shutil.rmtree(stage)
    return size


def build_modules_disk(path):
    """A second, read-only disk holding the HOST kernel's modules (the image has none; verify boots the builder's own kernel)."""
    release = os.uname().release
    source = "/lib/modules/" + release
    if not os.path.isdir(source):
        return False
    megabytes = int(subprocess.run(["du", "-sm", source], capture_output=True, text=True, check=True).stdout.split()[0])
    with open(path, "wb") as handle:
        handle.truncate((megabytes + 64) * 1048576)
    subprocess.run(["mke2fs", "-q", "-t", "ext4", "-d", source, path], check=True)
    return True


def qemu_argv(disk, modules_disk, memory_mb, smp, log_path, kvm, canary_port, network="none"):
    """The QEMU command line. Network: none. No NIC, no default devices, no shared directories, no USB, no passthrough.

    `network` other than "none" exists only so the test suite can prove verify FAILS on a VM that has a route out."""
    release = os.uname().release
    argv = ["qemu-system-x86_64", "-nodefaults", "-no-user-config", "-display", "none", "-m", str(memory_mb), "-smp", str(smp),
            "-machine", "q35" + (",accel=kvm" if kvm else ""), "-cpu", "host" if kvm else "max",
            "-kernel", "/boot/vmlinuz-" + release, "-initrd", "/boot/initrd.img-" + release,
            "-append", "root=/dev/vda rw console=ttyS0 systemd.unit=hams-verify-gate.service systemd.show_status=0 panic=-1 net.ifnames=0 "
                       "hamsverify=1",
            "-drive", "file=%s,if=virtio,format=raw,cache=unsafe" % disk,
            "-serial", "file:" + log_path, "-serial", "file:" + log_path + ".results", "-monitor", "none", "-no-reboot"]
    if modules_disk:
        argv += ["-drive", "file=%s,if=virtio,format=raw,readonly=on" % modules_disk]
    if network == "none":
        argv += ["-nic", "none"]
    else:
        argv += ["-nic", "user,model=virtio-net-pci"]
    return argv


class Canary:
    """A listener on the host (TCP and UDP, all addresses) that no packet from the isolated VM may ever reach. verify FAILS if one does."""

    def __init__(self):
        self.tcp = socket.socket()
        self.tcp.bind(("0.0.0.0", 0))
        self.port = self.tcp.getsockname()[1]
        self.tcp.listen(8)
        self.tcp.settimeout(0.5)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("0.0.0.0", self.port))
        self.udp.settimeout(0.5)
        self.hits = 0
        self._stop = False
        self._threads = [threading.Thread(target=self._loop, args=(self.tcp, True), daemon=True), threading.Thread(target=self._loop, args=(self.udp, False), daemon=True)]
        for thread in self._threads:
            thread.start()

    def _loop(self, sock, is_tcp):
        while not self._stop:
            try:
                if is_tcp:
                    conn, _ = sock.accept()
                    conn.close()
                else:
                    sock.recvfrom(512)
                self.hits += 1
            except OSError:
                continue

    def close(self):
        self._stop = True
        for thread in self._threads:
            thread.join(2)
        self.tcp.close()
        self.udp.close()


def parse_serial(text):
    """{phase: data} from the VM's result lines (`HAMSVERIFY <id> <i>/<n> <base64 chunk>`; whole-JSON lines are accepted too). The last result of
    a phase wins; "_order" lists the phases in the order they arrived."""
    import base64
    import binascii
    out, order, pending = {}, [], {}
    for line in text.splitlines():
        idx = line.find(SERIAL_MARK)
        if idx < 0:
            continue
        rest = line[idx + len(SERIAL_MARK):].strip()
        data = None
        parts = rest.split(" ")
        if len(parts) >= 2 and parts[0].isdigit() and "/" in parts[1]:
            number, total = (int(x) for x in parts[1].split("/"))
            pending.setdefault(parts[0], {})[number] = parts[2] if len(parts) > 2 else ""
            if len(pending[parts[0]]) == total:
                try:
                    blob = "".join(pending[parts[0]][i] for i in range(1, total + 1))
                    data = json.loads(base64.b64decode(blob))
                except (KeyError, ValueError, binascii.Error):
                    data = None
        else:
            try:
                data = json.loads(rest)
            except ValueError:
                data = None
        if data is not None:
            out[data.get("phase")] = data
            order.append(data.get("phase"))
    out["_order"] = order
    return out


def run_vm(image, profile, work, config, memory_mb=8192, smp=4, timeout=1800, network="none", say=print):
    """Boot the image copy; returns (serial text, canary hits, vm seconds). RestorationError when the VM cannot be built or never finishes."""
    kvm = os.access("/dev/kvm", os.W_OK)
    if not kvm:
        say("verify: /dev/kvm is not usable: using QEMU software emulation (slow); a real VM needs access to /dev/kvm")
    disk = os.path.join(work, "image.ext4")
    say("verify: building the VM disk from the directory")
    size = build_disk(image, disk, config)
    modules = os.path.join(work, "modules.ext4")
    modules_disk = modules if build_modules_disk(modules) else None
    log = os.path.join(work, "serial.log")
    open(log, "w").close()
    open(log + ".results", "w").close()
    canary = Canary()
    config["canary_port"] = canary.port
    build_disk_config_refresh(disk, config)
    argv = qemu_argv(disk, modules_disk, memory_mb, smp, log, kvm, canary.port, network)
    say("verify: booting the VM (%d MB disk, kvm=%s, network=%s)" % (size, kvm, network))
    started = time.time()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RestorationError("the VM did not finish within %d s; serial log tail:\n%s" % (timeout, open(log, errors="replace").read()[-1500:]))
    finally:
        canary.close()
    text = open(log + ".results", errors="replace").read()
    return text + "\n" + open(log, errors="replace").read(), canary.hits, time.time() - started


def build_disk_config_refresh(disk, config):
    """Rewrite config.json inside the disk copy once the canary port is known."""
    stage = disk + ".cfg"
    with open(stage, "w") as handle:
        json.dump(config, handle)
    subprocess.run(["debugfs", "-w", "-f", "/dev/stdin", disk], input="rm /usr/local/lib/hams-verify/config.json\nwrite %s /usr/local/lib/hams-verify/config.json\n" % stage,
                   text=True, check=True, capture_output=True)
    os.unlink(stage)


def evaluate(phases, hits, profile, meta, expected_counts, report, external_units, units_expected):
    """Turn the VM's phases into checks."""
    iso = phases.get("isolation")
    order = phases.get("_order", [])
    first = bool(iso and iso["ok"] and ("starting" not in order or order.index("isolation") < order.index("starting")))
    failing = "; ".join("%s %s" % (c["name"], c["detail"]) for c in (iso or {}).get("checks", []) if not c["ok"])
    report.add("isolation proven before any service started", first, failing or ("no isolation result" if not iso else ""))
    report.add("host canary saw no packet from the VM", hits == 0, "%d connections/datagrams" % hits)
    if not iso or not iso["ok"]:
        report.add("production services were NOT started (fail closed)", "starting" not in phases, "")
        return
    report.add("isolation still holds after the services ran", bool(phases.get("isolation_after", {}).get("ok")))
    units = phases.get("units", {})
    report.add("system reached a running or degraded state", units.get("system") in ("running", "degraded"), str(units.get("system")))
    states = units.get("units", {})
    real, expected_failures = [], []
    for unit in units.get("failed", []):
        info = states.get(unit, {})
        category, reason = cl.classify(dict(info.get("state", {}), ActiveState="failed"), info.get("journal", ""))
        (real if category != cl.EXPECTED else expected_failures).append("%s: %s" % (unit, reason))
    report.add("no failed unit with a real cause", not real, " | ".join(real[:6]))
    if expected_failures:
        report.add("failed units that tried the isolated network (expected, %d)" % len(expected_failures), True, " | ".join(x[:90] for x in expected_failures[:4]))
    for unit in profile["verify"].get("must_be_active", []):
        report.add("%s is active" % unit, states.get(unit, {}).get("state", {}).get("ActiveState") == "active", str(states.get(unit, {}).get("state", {}).get("ActiveState")))
    not_started, no_evidence, bad = [], [], []
    required = profile["verify"].get("network_evidence_required", [])
    for unit in external_units:
        info = states.get(unit, {"state": {}, "journal": ""})
        category, reason = cl.classify(info["state"], info["journal"])
        if category == cl.NOT_STARTED:
            not_started.append(unit)
        elif category == cl.REAL:
            bad.append("%s: %s" % (unit, reason))
        elif not cl.network_attempt_seen(info["journal"]):
            no_evidence.append(unit)
    report.add("every external-fetch unit started (%d)" % len(external_units), not not_started, ", ".join(not_started[:8]))
    report.add("no external-fetch unit failed for a real cause", not bad, " | ".join(bad[:5]))
    report.add("external-fetch units that left no network attempt in the journal (%d)" % len(no_evidence), True, ", ".join(no_evidence[:6]), warn=False)
    missing_ev = [u for u in no_evidence if any(fnmatch.fnmatch(u, pat) for pat in required)]
    report.add("units that must show a network attempt did (cloudflared, mail)", not missing_ev, ", ".join(missing_ev))
    db = phases.get("database", {})
    report.add("PostgreSQL started and finished recovery", db.get("in_recovery") is False, "in_recovery=%s" % db.get("in_recovery"))
    counts = db.get("counts", {})
    report.add("row counts are present", bool(counts) and all(isinstance(v, int) and v > 0 for v in counts.values()), json.dumps(counts))
    if expected_counts:
        diff = {k: (counts.get(k), v) for k, v in expected_counts.get("counts", {}).items() if counts.get(k) != v}
        report.add("row counts equal the backup's metadata", not diff, json.dumps(diff))
        if expected_counts.get("newest"):
            report.add("newest transaction time equals the backup's metadata", db.get("newest") == expected_counts["newest"], "%s vs %s" % (db.get("newest"), expected_counts["newest"]))
    for want, got in zip(profile["verify"].get("http_checks", []), phases.get("http", {}).get("checks", [])):
        accepted = want.get("status_in") or [want.get("status", 200)]
        ok = got["status"] in accepted and got["found"] is not False
        report.add("site answers %s" % got["url"], ok, "status %s, wanted %s" % (got["status"], accepted))
    login = phases.get("login", {})
    if not login.get("skipped"):
        report.add("a login works", bool(login.get("ok")), login.get("detail", ""))
    report.add("VM finished and powered off by itself", "done" in phases)


def verify_image(image, profile, work, expected_counts=None, network="none", say=print, infra=None, memory_mb=None, timeout=1800):
    """Static checks, then the isolated VM. Returns a Report (report.ok is the verdict). Needs root (mke2fs -d reads every file)."""
    if os.geteuid() != 0:
        raise RestorationError("verify needs root (it builds a disk from the directory, which holds root-only files): run it with sudo")
    if infra is None:
        from .build import load_infrastructure
        infra = load_infrastructure()
    report = Report()
    meta = static_checks(image, profile, infra, report)
    if meta is None:
        return report
    units_expected, external = expected_services(profile, infra, meta)
    config = {"verify": profile["verify"], "database": dict(profile["database"], datadir=meta["database"]["datadir"]), "external_fetch_units": external,
              "external_fetch_services": [u for u in external if u.endswith(".service")], "expected_units": units_expected,
              "log_files": profile["verify"].get("log_files", [])}
    text, hits, seconds = run_vm(image, profile, work, config, memory_mb or profile["verify"].get("memory_mb", 8192), profile["verify"].get("cpus", 4),
                                 timeout, network, say)
    say("verify: VM ran %.0f s" % seconds)
    evaluate(parse_serial(text), hits, profile, meta, expected_counts, report, external, units_expected)
    return report
