#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. License: AGPL-3.0.
"""Tests for site_monitor.py and its MANIFEST units (hams1 readiness audit row 3)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import infrastructure as infra  # noqa: E402
import site_monitor as sm  # noqa: E402


def fake_get(table):
    def get(url, timeout=10, opener=None):
        value = table[url]
        if isinstance(value, Exception):
            raise value
        return value
    return get


class OdooCheckTests(unittest.TestCase):
    def test_pass(self):
        ok, _ = sm.check_odoo("u", http_get=fake_get({"u": (200, '{"status": "pass"}')}))
        self.assertTrue(ok)

    def test_non_200_wrong_body_and_unreachable_fail(self):
        self.assertFalse(sm.check_odoo("u", http_get=fake_get({"u": (503, "pass")}))[0])
        self.assertFalse(sm.check_odoo("u", http_get=fake_get({"u": (200, "<html>")}))[0])
        self.assertFalse(sm.check_odoo("u", http_get=fake_get({"u": ConnectionRefusedError("no")}))[0])
        self.assertFalse(sm.check_odoo("u", http_get=fake_get({"u": urllib.error.URLError("x")}))[0])
        self.assertFalse(sm.check_odoo("u", http_get=fake_get({"u": TimeoutError("slow")}))[0])


class TunnelAndPublicTests(unittest.TestCase):
    def test_tunnel_needs_a_ready_edge_connection(self):
        ready = lambda n: fake_get({"u": (200, json.dumps({"status": 200, "readyConnections": n}))})
        self.assertTrue(sm.check_tunnel("u", http_get=ready(4))[0])
        self.assertTrue(sm.check_tunnel("u", http_get=ready(1))[0])
        self.assertFalse(sm.check_tunnel("u", http_get=ready(0))[0])
        self.assertFalse(sm.check_tunnel("u", http_get=fake_get({"u": (200, "garbage")}))[0])
        self.assertFalse(sm.check_tunnel("u", http_get=fake_get({"u": (200, "[]")}))[0])
        self.assertFalse(sm.check_tunnel("u", http_get=fake_get({"u": OSError("refused")}))[0])

    def test_public_site_must_answer_200(self):
        self.assertTrue(sm.check_public_site("u", http_get=fake_get({"u": (200, "")}))[0])
        self.assertFalse(sm.check_public_site("u", http_get=fake_get({"u": (502, "")}))[0])
        self.assertFalse(sm.check_public_site("u", http_get=fake_get({"u": OSError("tls")}))[0])


class PostgresqlCheckTests(unittest.TestCase):
    def _run(self, code):
        return lambda *a, **k: SimpleNamespace(returncode=code)

    def test_pg_isready_exit_code_decides(self):
        self.assertTrue(sm.check_postgresql(run=self._run(0), find=lambda: "/x/pg_isready")[0])
        self.assertFalse(sm.check_postgresql(run=self._run(2), find=lambda: "/x/pg_isready")[0])

    def test_timeout_and_missing_binary(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("pg_isready", 15)
        self.assertFalse(sm.check_postgresql(run=slow, find=lambda: "/x")[0])

        class Sock:
            def close(self):
                pass
        self.assertTrue(sm.check_postgresql(find=lambda: None, connect=lambda *a, **k: Sock())[0])

        def refuse(*a, **k):
            raise ConnectionRefusedError()
        self.assertFalse(sm.check_postgresql(find=lambda: None, connect=refuse)[0])


class UnitsCheckTests(unittest.TestCase):
    def test_the_daemon_list_comes_from_the_manifest_and_skips_unlinked_units(self):
        manifest = infra.boot_service_unit_names()
        linked = {os.path.join(sm.SYSTEMD_DIR, "hams-relay-signer.service")}
        units = sm.monitored_units(manifest, exists=lambda p: p in linked)
        for core in sm.CORE_UNITS:
            self.assertIn(core, units)
        self.assertIn("hams-relay-signer.service", units)
        self.assertNotIn("hams-subcarrier-signer.service", units)

    def test_every_boot_daemon_and_both_signers_are_monitored_once_linked(self):
        units = sm.monitored_units(exists=lambda p: True)
        for name in infra.boot_service_unit_names():
            self.assertIn(name, units)
        self.assertIn("hams-relay-signer.service", units)
        self.assertIn("hams-subcarrier-signer.service", units)

    def test_all_active_passes_and_any_other_state_fails(self):
        run = lambda out: (lambda *a, **k: SimpleNamespace(stdout=out, returncode=0))
        self.assertTrue(sm.check_units(["a", "b"], run=run("active\nactive\n"))[0])
        ok, detail = sm.check_units(["a", "b"], run=run("active\nfailed\n"))
        self.assertFalse(ok)
        self.assertIn("b=failed", detail)
        self.assertFalse(sm.check_units(["a", "b"], run=run("active\n"))[0])

    def test_systemctl_missing_is_a_failure(self):
        def boom(*a, **k):
            raise FileNotFoundError("systemctl")
        self.assertFalse(sm.check_units(["a"], run=boom)[0])


class DiskCheckTests(unittest.TestCase):
    def test_threshold(self):
        self.assertTrue(sm.check_disk(["/"], usage=lambda p: (100, 50, 50))[0])
        self.assertFalse(sm.check_disk(["/"], usage=lambda p: (100, 90, 10))[0])
        self.assertFalse(sm.check_disk(["/"], usage=lambda p: (0, 0, 0))[0])

    def test_unreadable_path_fails(self):
        def boom(p):
            raise FileNotFoundError(p)
        self.assertFalse(sm.check_disk(["/gone"], usage=boom)[0])


class RunChecksTests(unittest.TestCase):
    def test_a_crashing_check_is_a_failed_check_not_a_crash(self):
        def boom(*a, **k):
            raise RuntimeError("bug")
        results = sm.run_checks({"HAMS_MONITOR_PUBLIC_URL": ""}, {"disk": {"usage": boom}})
        self.assertFalse(results["disk"][0])
        self.assertNotIn("public site", results)


class DecideTests(unittest.TestCase):
    def test_pages_only_after_consecutive_failures(self):
        down = {"odoo": (False, "dead")}
        state, msgs = sm.decide({}, down, 100)
        self.assertEqual(msgs, [])
        state, msgs = sm.decide(state, down, 160)
        self.assertEqual([m[:2] for m in msgs], [("page", "odoo")])

    def test_a_blip_resets_the_count(self):
        state, _ = sm.decide({}, {"odoo": (False, "x")}, 1)
        state, msgs = sm.decide(state, {"odoo": (True, "ok")}, 2)
        self.assertEqual(msgs, [])
        state, msgs = sm.decide(state, {"odoo": (False, "x")}, 3)
        self.assertEqual(msgs, [])

    def test_no_repeat_until_repage_interval_then_recovery_once(self):
        down = {"odoo": (False, "dead")}
        state = {"odoo": {"fails": 5, "paged_at": 1000, "down_since": 900}}
        state, msgs = sm.decide(state, down, 1000 + sm.REPAGE_SECONDS - 1)
        self.assertEqual(msgs, [])
        state, msgs = sm.decide(state, down, 1000 + sm.REPAGE_SECONDS)
        self.assertEqual(msgs[0][0], "repage")
        state = sm.commit_paged(state, msgs, 5000)
        self.assertEqual(state["odoo"]["paged_at"], 5000)
        state, msgs = sm.decide(state, {"odoo": (True, "back")}, 5100)
        self.assertEqual([m[:2] for m in msgs], [("recovered", "odoo")])
        state, msgs = sm.decide(state, {"odoo": (True, "back")}, 5200)
        self.assertEqual(msgs, [])

    def test_recovery_without_a_page_is_silent(self):
        state, _ = sm.decide({}, {"odoo": (False, "x")}, 1)
        _, msgs = sm.decide(state, {"odoo": (True, "ok")}, 2)
        self.assertEqual(msgs, [])


class DeliveryTests(unittest.TestCase):
    def test_webhook_json_and_text_bodies(self):
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout=0):
            seen.append(request)
            return Resp()

        sm.send_webhook("https://h/x", "T", "body", "page", urlopen=opener)
        self.assertEqual(json.loads(seen[0].data), {"content": "body", "text": "body"})
        sm.send_webhook("https://h/x", "T", "body", "page", urlopen=opener, style="text")
        self.assertEqual(seen[1].data, b"body")
        self.assertEqual(seen[1].get_header("Priority"), "urgent")

    def _channels(self, email=None, webhook=None, sms=None):
        def fail(*a, **k):
            raise OSError("down")
        chans = [("email", sm.CHANNELS[0][1], email or (lambda env, kind, title, text: None)),
                 ("webhook", sm.CHANNELS[1][1], webhook or (lambda env, kind, title, text: None))]
        if sms:
            chans.append(("sms", lambda env: bool(env.get("SMS_TO")), sms))
        return tuple(chans)

    def test_email_is_the_primary_channel_and_its_recipient_comes_from_the_environment(self):
        self.assertEqual(sm.CHANNELS[0][0], "email")
        env = {"PAGER_FALLBACK_EMAIL": "someone@example.org", "SMTP_HOST": "m", "SMTP_USER": "u", "SMTP_PASS": "p"}
        sent = []

        class Smtp:
            def __init__(self, host, port, timeout=0):
                sent.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, u, p):
                pass

            def send_message(self, message):
                sent.append(("to", message["To"], message["Subject"]))

        sm.send_email(env, "[hams.com DOWN] odoo", "text", smtp=Smtp)
        self.assertIn(("to", "someone@example.org", "[hams.com DOWN] odoo"), sent)
        source = open(sm.__file__, encoding="utf-8").read()
        self.assertNotIn("perens.com", source)
        self.assertNotIn("http://", source.replace("http://127.0.0.1", ""))

    def test_message_is_delivered_when_any_channel_accepts(self):
        env = {"PAGER_WEBHOOK_URL": "https://h", "PAGER_FALLBACK_EMAIL": "a@b", "SMTP_HOST": "m"}

        def bad(*a, **k):
            raise OSError("down")

        mails = []
        done = sm.deliver([("page", "odoo", "dead")], env, "hams1",
                          channels=self._channels(webhook=bad, email=lambda e, k, t, x: mails.append(t)))
        self.assertEqual(len(done), 1)
        self.assertEqual(mails, ["[hams.com DOWN] odoo"])

    def test_message_is_not_delivered_when_every_channel_fails(self):
        env = {"PAGER_FALLBACK_EMAIL": "a@b", "SMTP_HOST": "m", "PAGER_WEBHOOK_URL": "https://h"}

        def bad(*a, **k):
            raise OSError("down")

        self.assertEqual(sm.deliver([("page", "odoo", "dead")], env, "h", channels=self._channels(bad, bad)), [])

    def test_a_new_channel_is_one_entry_and_unconfigured_channels_are_skipped(self):
        got = []
        env = {"PAGER_FALLBACK_EMAIL": "a@b", "SMTP_HOST": "m", "SMS_TO": "+1"}
        chans = self._channels(sms=lambda e, k, t, x: got.append(t))
        self.assertEqual(len(sm.deliver([("page", "odoo", "d")], env, "h", channels=chans)), 1)
        self.assertEqual(got, ["[hams.com DOWN] odoo"])
        self.assertEqual(sm.channels_configured({}, chans), {"email": False, "webhook": False, "sms": False})

    def test_no_odoo_or_ses_anywhere_in_the_paging_path(self):
        source = open(sm.__file__, encoding="utf-8").read()
        for forbidden in ("xmlrpc", "/json/2/", "message_post", "amazonses"):
            self.assertNotIn(forbidden, source)


class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []
        self.pings = []

    @staticmethod
    def _chan(send):
        return (("email", sm.CHANNELS[0][1], send),)

    def _beat(self, req, timeout=0):
        self.pings.append(req.full_url)
        return _Ctx()

    def _deps(self, ok=True):
        good = (200, '{"status":"pass","readyConnections":4}')
        table = {
            "http://127.0.0.1:8069/web/health": good if ok else OSError("refused"),
            "http://127.0.0.1:20241/ready": good,
        }
        pg_code = 0
        return {
            "http": {"http_get": fake_get(table)},
            "pg": {"run": lambda *a, **k: SimpleNamespace(returncode=pg_code), "find": lambda: "/x"},
            "units_list": {"manifest_units": ()},
            "units": {"run": lambda cmd, **k: SimpleNamespace(stdout="active\n" * (len(cmd) - 2), returncode=0)},
            "disk": {"usage": lambda p: (100, 10, 90)},
            "delivery": {"channels": self._chan(lambda env, kind, title, text: self.sent.append(title))},
            "heartbeat": {"urlopen": self._beat},
        }

    def _env(self, **extra):
        env = {"STATE_DIRECTORY": self.tmp.name, "HAMS_MONITOR_PUBLIC_URL": "",
               "PAGER_FALLBACK_EMAIL": "ops@example.org", "SMTP_HOST": "mx", "HAMS_MONITOR_HEARTBEAT_URL": "https://beat/ping"}
        env.update(extra)
        return env

    def test_healthy_run_exits_zero_and_pings_the_dead_man_switch(self):
        deps = self._deps()
        self.assertEqual(sm.main(env=self._env(), now=1000, deps=deps, host="hams1"), 0)
        self.assertEqual(self.pings, ["https://beat/ping"])
        self.assertEqual(self.sent, [])

    def test_failure_pages_on_the_second_run_and_withholds_the_heartbeat(self):
        deps = self._deps(ok=False)
        self.assertEqual(sm.main(env=self._env(), now=1000, deps=deps, host="hams1"), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(sm.main(env=self._env(), now=1060, deps=deps, host="hams1"), 1)
        self.assertEqual(self.sent, ["[hams.com DOWN] odoo"])
        self.assertEqual(sm.main(env=self._env(), now=1120, deps=deps, host="hams1"), 1)
        self.assertEqual(self.sent, ["[hams.com DOWN] odoo"], "no repeat inside the re-page interval")
        self.assertEqual(self.pings, [])

    def test_recovery_is_announced_once(self):
        down, up = self._deps(ok=False), self._deps(ok=True)
        for now in (1000, 1060):
            sm.main(env=self._env(), now=now, deps=down, host="h")
        self.assertEqual(sm.main(env=self._env(), now=1120, deps=up, host="h"), 0)
        self.assertEqual(self.sent, ["[hams.com DOWN] odoo", "[hams.com RECOVERED] odoo"])

    def test_no_channel_configured_is_loud(self):
        env = self._env()
        del env["PAGER_FALLBACK_EMAIL"]
        self.assertEqual(sm.main(env=env, now=1, deps=self._deps(), host="h"), 2)

    def test_undelivered_page_is_retried_next_run(self):
        deps = self._deps(ok=False)
        fails = {"n": 0}

        def flaky(env, kind, title, text):
            fails["n"] += 1
            if fails["n"] == 1:
                raise OSError("down")
            self.sent.append(title)

        deps["delivery"] = {"channels": self._chan(flaky)}
        sm.main(env=self._env(), now=1000, deps=deps, host="h")
        self.assertEqual(sm.main(env=self._env(), now=1060, deps=deps, host="h"), 3)
        self.assertEqual(self.sent, [])
        self.assertEqual(sm.main(env=self._env(), now=1120, deps=deps, host="h"), 1)
        self.assertEqual(self.sent, ["[hams.com DOWN] odoo"])


class ManifestUnitTests(unittest.TestCase):
    """Tests the MANIFEST entries for hams-site-monitor.service and .timer."""

    def _entries(self):
        return {os.path.basename(s["path"]): s for s in infra.MANIFEST["static_files"]
                if os.path.basename(s["path"]).startswith("hams-site-monitor.")}

    def test_both_units_exist_for_production_only(self):
        entries = self._entries()
        self.assertEqual(set(entries), {"hams-site-monitor.service", "hams-site-monitor.timer"})
        for entry in entries.values():
            self.assertEqual(entry["environments"], ["prod"])

    def test_the_service_runs_one_pass_off_odoo_and_off_the_database(self):
        unit = self._entries()["hams-site-monitor.service"]["content"]
        self.assertIn("Type=oneshot", unit)
        self.assertIn("EnvironmentFile=-/opt/hams/etc/site_monitor.env", unit)
        self.assertIn("site_monitor.py", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("StateDirectory=hams-monitor", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertNotIn("Requires=odoo", unit)
        self.assertNotIn("Requires=postgresql", unit)

    def test_the_timer_runs_every_minute_and_is_enabled_as_a_timer(self):
        timer = self._entries()["hams-site-monitor.timer"]["content"]
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("OnBootSec=", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_the_unit_script_exists_in_this_repository(self):
        unit = self._entries()["hams-site-monitor.service"]["content"]
        line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        relative = line.split("/hams_shared/", 1)[1]
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative)))

    def test_the_monitor_is_classified_local_and_is_never_enabled_in_a_test_environment(self):
        # Its public-site and webhook calls go to our own site and to the operator's own webhook only.
        self.assertNotIn("hams-site-monitor.service", infra.external_fetch_unit_names())
        self.assertNotIn("hams-site-monitor.service", infra.boot_service_unit_names())


if __name__ == "__main__":
    unittest.main()
