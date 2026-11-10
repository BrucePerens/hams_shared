# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of verify: the failure classifier, the QEMU command line (no network), the host canary, the evaluation of VM results, and the static
checks with negative controls (an image with an external-fetch unit disabled fails)."""
import json
import os
import socket

import infrastructure as infra
from restoration_system import accounts
from restoration_system import classify as cl
from restoration_system import code_layer
from restoration_system import coverage as cov
from restoration_system import profile as prof
from restoration_system import units as un
from restoration_system import verify as ver


def state(**kw):
    base = {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "ExecMainStatus": "1", "ExecMainStartTimestamp": "Mon 2026-11-09 10:00:00 UTC"}
    base.update(kw)
    return base


def test_network_failure_is_expected():
    journal = "requests.exceptions.ConnectionError: HTTPSConnectionPool(host='x'): Max retries exceeded: [Errno 101] Network is unreachable"
    assert cl.classify(state(), journal)[0] == cl.EXPECTED
    assert cl.classify(state(), "socket.gaierror: [Errno -3] Temporary failure in name resolution")[0] == cl.EXPECTED
    assert cl.classify(state(), "dial tcp 198.41.0.1:7844: connect: network is unreachable")[0] == cl.EXPECTED


def test_real_failures_are_real():
    assert cl.classify(state(), "FileNotFoundError: [Errno 2] No such file or directory: '/opt/hams/etc/x.env'")[0] == cl.REAL
    assert cl.classify(state(), "PermissionError: [Errno 13] Permission denied: '/opt/hams/etc/keys/a'")[0] == cl.REAL
    assert cl.classify(state(), "ModuleNotFoundError: No module named 'redis'")[0] == cl.REAL
    assert cl.classify(state(ExecMainStatus="203"), "daemon.service: Main process exited, code=exited, status=203/EXEC")[0] == cl.REAL
    assert cl.classify(state(), "Traceback (most recent call last):\n  File \"a.py\", line 1\nKeyError: 'x'")[0] == cl.REAL
    assert cl.classify(state(), "")[0] == cl.REAL


def test_refused_connection_to_this_machine_is_real_not_network():
    journal = "psycopg2.OperationalError: connection to server at \"127.0.0.1\", port 5432 failed: Connection refused"
    assert cl.classify(state(), journal)[0] == cl.REAL
    assert cl.classify(state(ActiveState="active", Result="success"), journal)[0] == cl.REAL


def test_traceback_ending_in_a_non_network_error_is_real_even_with_network_words():
    journal = "connecting to https://x ... Network is unreachable\nTraceback (most recent call last):\n  File \"a\"\nValueError: bad config"
    assert cl.classify(state(), journal)[0] == cl.REAL


def test_clean_and_unstarted_units():
    assert cl.classify({"ActiveState": "active", "SubState": "running", "Result": "success", "ExecMainStartTimestamp": "x"}, "")[0] == cl.OK
    assert cl.classify({"ActiveState": "inactive", "SubState": "dead", "Result": "success"}, "")[0] == cl.NOT_STARTED
    assert cl.network_attempt_seen("Unable to establish connection to 198.41.200.1")
    assert not cl.network_attempt_seen("connect to 127.0.0.1 refused")


def test_qemu_has_no_network_device_or_shared_directory():
    argv = ver.qemu_argv("/d.img", "/m.img", 2048, 2, "/log", False, 1234)
    joined = " ".join(argv)
    assert "-nic none" in joined and "-nodefaults" in argv
    for forbidden in ("netdev", "-net ", "virtfs", "usb", "hostfwd", "vfio", "-device virtio-net"):
        assert forbidden not in joined
    assert "systemd.unit=hams-verify-gate.service" in joined
    assert "user" in " ".join(ver.qemu_argv("/d", None, 1, 1, "/l", False, 1, network="user")), "only the test-suite control gets a NIC"


def test_canary_counts_a_connection():
    canary = ver.Canary()
    try:
        socket.create_connection(("127.0.0.1", canary.port), timeout=2).close()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.sendto(b"x", ("127.0.0.1", canary.port))
        udp.close()
        for _ in range(20):
            if canary.hits >= 2:
                break
            import time
            time.sleep(0.2)
    finally:
        canary.close()
    assert canary.hits >= 2


def phases(**over):
    base = {"_order": ["boot", "isolation", "starting", "units", "database", "http", "login", "isolation_after", "done"],
            "isolation": {"ok": True, "checks": [{"name": "n", "ok": True, "detail": ""}]},
            "starting": {"services_started": True},
            "isolation_after": {"ok": True},
            "units": {"system": "running", "failed": [], "units": {"pg.service": {"state": {"ActiveState": "active"}, "journal": ""}}},
            "database": {"in_recovery": False, "counts": {"items": 125}, "newest": "2026-11-09T09:47:38Z"},
            "http": {"checks": [{"url": "http://127.0.0.1/", "status": 200, "found": True}]},
            "login": {"skipped": True}, "done": {"services_started": True}}
    base.update(over)
    return base


PROFILE = {"verify": {"http_checks": [{"url": "http://127.0.0.1/", "status": 200}], "must_be_active": ["pg.service"], "network_evidence_required": ["cloudflared*"]}}
META = {"database": {"target_time": None}}


def judge(ph, hits=0, external=(), expected=None):
    report = ver.Report()
    ver.evaluate(ph, hits, PROFILE, META, expected, report, list(external), [])
    return report


def test_healthy_vm_passes():
    report = judge(phases(), expected={"counts": {"items": 125}, "newest": "2026-11-09T09:47:38Z"})
    assert report.ok, report.text()


def test_failed_isolation_fails_closed_and_services_must_not_have_started():
    bad = phases(isolation={"ok": False, "checks": [{"name": "outbound TCP 1.1.1.1:443 fails", "ok": False, "detail": ""}]})
    report = judge(bad)
    assert not report.ok and "isolation" in report.text()
    started = phases(isolation=bad["isolation"])
    report = judge({"_order": ["boot", "isolation", "starting"], "isolation": bad["isolation"], "starting": {"services_started": True}})
    assert not report.ok and any(c.name.startswith("production services were NOT started") and c.status == "FAIL" for c in report.checks)
    assert started


def test_canary_hit_fails():
    assert not judge(phases(), hits=1).ok


def test_wrong_counts_and_stuck_recovery_fail():
    assert not judge(phases(), expected={"counts": {"items": 126}}).ok
    assert not judge(phases(database={"in_recovery": True, "counts": {}, "newest": None})).ok


def test_real_unit_failure_fails_but_network_failure_does_not():
    net = {"failed": ["sync.service"], "units": {"sync.service": {"state": state(), "journal": "socket.gaierror: Temporary failure in name resolution"}}, "system": "degraded"}
    assert judge(phases(units=dict(net, units=dict(net["units"], **{"pg.service": {"state": {"ActiveState": "active"}, "journal": ""}})))).ok
    real = {"failed": ["sync.service"], "units": {"sync.service": {"state": state(), "journal": "PermissionError: [Errno 13] Permission denied"},
                                                  "pg.service": {"state": {"ActiveState": "active"}, "journal": ""}}, "system": "degraded"}
    assert not judge(phases(units=real)).ok


def test_external_fetch_unit_must_have_started_and_cloudflared_must_show_a_network_attempt():
    units = {"system": "running", "failed": [], "units": {
        "pg.service": {"state": {"ActiveState": "active"}, "journal": ""},
        "fcc.sync.service": {"state": {"ActiveState": "inactive", "SubState": "dead", "Result": "success"}, "journal": ""}}}
    assert not judge(phases(units=units), external=["fcc.sync.service"]).ok
    quiet = {"system": "running", "failed": [], "units": {
        "pg.service": {"state": {"ActiveState": "active"}, "journal": ""},
        "cloudflared-1.service": {"state": {"ActiveState": "active", "ExecMainStartTimestamp": "x"}, "journal": "started"}}}
    assert not judge(phases(units=quiet), external=["cloudflared-1.service"]).ok
    quiet["units"]["cloudflared-1.service"]["journal"] = "ERR Unable to establish connection with Cloudflare edge: dial tcp 198.41.192.7:7844: network is unreachable"
    assert judge(phases(units=quiet), external=["cloudflared-1.service"]).ok


def test_parse_serial_reassembles_chunked_base64_results():
    import base64
    blob = base64.b64encode(json.dumps({"phase": "units", "system": "degraded", "x": "y" * 400}).encode()).decode()
    pieces = [blob[i:i + 120] for i in range(0, len(blob), 120)]
    text = "\n".join("HAMSVERIFY 7 %d/%d %s" % (i + 1, len(pieces), piece) for i, piece in enumerate(pieces))
    text = "audit: noise\n" + text + "\nHAMSVERIFY 8 1/2 AAAA\n"
    parsed = ver.parse_serial(text)
    assert parsed["units"]["system"] == "degraded" and parsed["_order"] == ["units"], "an incomplete result is ignored"


def test_parse_serial_keeps_order():
    text = 'noise\nHAMSVERIFY {"phase": "boot"}\n[ 1.0] x HAMSVERIFY {"phase": "isolation", "ok": true}\nHAMSVERIFY not json\n'
    parsed = ver.parse_serial(text)
    assert parsed["_order"] == ["boot", "isolation"] and parsed["isolation"]["ok"]


def fake_image(tmp_path, classes):
    """An image directory that satisfies static_checks for the real MANIFEST, built the way the builder builds it."""
    image = tmp_path / "image"
    expected = un.expected_units(infra, classes)
    for unit, info in expected.items():
        path = image / info["path"].lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Unit]\n")
    un.link_units(str(image), expected)
    for unit in un.enablement_plan(str(image), expected)[0]:
        wants = image / "etc/systemd/system/multi-user.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        os.symlink("/opt/hams/systemd/" + unit, wants / unit)
    for spec in infra.MANIFEST["directories"]:
        if "prod" in spec["environments"] and infra._in_host_class(spec, classes):
            (image / spec["path"].lstrip("/")).mkdir(parents=True, exist_ok=True)
    (image / "etc").mkdir(exist_ok=True)
    (image / "etc/passwd").write_text("".join("%s:x:%d:%d::/:/bin/false\n" % (a["user"], 900 + i, 900 + i) for i, a in enumerate(infra.MANIFEST["system_accounts"])))
    (image / "var/lib/dpkg").mkdir(parents=True, exist_ok=True)
    (image / "var/lib/dpkg/status").write_text("Package: nginx\nStatus: install ok installed\n")
    for name in ("EXCLUDE.txt", "RESTORE.md"):
        (image / name).write_text("x")
    (image / "usr/local/sbin").mkdir(parents=True)
    (image / "usr/local/sbin/hams-restore-post").write_text("x")
    (image / "RESTORATION_META.json").write_text(json.dumps({"host_classes": classes, "packages": {"nginx": ["profile"]}, "database": {"datadir": "/x"}}))
    return image, expected


def test_static_checks_pass_on_a_complete_image_and_fail_when_an_external_unit_is_disabled(tmp_path):
    classes = ["b2_backup"]
    image, expected = fake_image(tmp_path, classes)
    profile = prof.load()
    report = ver.Report()
    ver.static_checks(str(image), profile, infra, report)
    assert report.ok, report.text()
    external_timer = sorted(u for u, i in expected.items() if i["external_fetch"] and u.endswith(".timer") and i["enabled"])[0]
    for wants in (image / "etc/systemd/system").glob("*.wants"):
        link = wants / external_timer
        if link.is_symlink():
            link.unlink()
    broken = ver.Report()
    ver.static_checks(str(image), profile, infra, broken)
    assert not broken.ok
    assert any("external-fetch" in c.name and c.status == "FAIL" and external_timer in c.detail for c in broken.checks), broken.text()


def test_static_checks_fail_when_a_unit_file_is_missing(tmp_path):
    image, expected = fake_image(tmp_path, ["b2_backup"])
    victim = sorted(expected)[0]
    os.unlink(image / "etc/systemd/system" / victim)
    os.unlink(image / expected[victim]["path"].lstrip("/"))
    report = ver.Report()
    ver.static_checks(str(image), prof.load(), infra, report)
    assert not report.ok and victim in report.text()


def test_coverage_reports_gaps_and_distinguishes_sources():
    profile = prof.load()
    local = cov.build_coverage(infra, profile, "local")
    b2 = cov.build_coverage(infra, profile, "b2")
    structural = {"accounts_not_in_backups", "package_list_not_in_backups", "code_not_in_backups", "built_artifacts_not_in_backups"}
    assert not structural & {g[0] for g in local["gaps"]}  # the config set carries passwd/group, dpkg status and DEPLOYED_COMMITS
    assert structural <= {g[0] for g in b2["gaps"]}
    assert "b2_no_system_config" in {g[0] for g in b2["gaps"]} and "b2_no_system_config" not in {g[0] for g in local["gaps"]}
    assert "/opt/hams/etc" in " ".join(local["from_backups"]) or local["from_backups"]
    assert len(local["from_backups"]) > len(b2["from_backups"])
    assert "COVERAGE" in cov.render(local, "local")


def test_captured_accounts_and_deployed_commits():
    users, groups = accounts.captured_ids("root:x:0:0::/root:/bin/bash\nhams_com:x:998:997::/opt/hams:/bin/bash\n", "root:x:0:\nhams_com:x:997:odoo\n")
    assert users == {"root": 0, "hams_com": 998} and groups == {"root": 0, "hams_com": 997}
    assert code_layer.parse_deployed_commits("hams_com=9976e1bd\nhams_open=6925c203\nhams_shared=fa775f3\n")["hams_open"] == "6925c203"
