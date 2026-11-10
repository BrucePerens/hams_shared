#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_trace_cache.py: the result cache of the lint's test steps, keyed by the files a step really read.

The parser and the veto rules are tested on strace text; the cache end to end with real strace, real child processes and real files
(skipped where strace cannot trace). The negative controls are the point: an injected failure is never served from the cache, a step that
touched the network or read hardware state is never stored, and --no-cache bypasses the cache.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lint_exec  # noqa: E402
import lint_trace_cache as ltc  # noqa: E402

PY = sys.executable

SAMPLE = """\
100 execve("/usr/bin/python3", ["python3", "t.py"], 0x7ffd /* 9 vars */) = 0
100 openat(AT_FDCWD</repo>, "t.py", O_RDONLY|O_CLOEXEC) = 3</repo/t.py>
100 openat(AT_FDCWD</repo>, "pkg", O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_DIRECTORY) = 3</repo/pkg>
100 openat(AT_FDCWD</repo>, "missing.py", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)
100 openat(AT_FDCWD</repo>, "out.txt", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 4</repo/out.txt>
101 openat(AT_FDCWD</repo>, "slow \\"q\\".txt", O_RDONLY <unfinished ...>
100 bind(5<socket:[1]>, {sa_family=AF_UNIX, sun_path="/tmp/own.sock"}, 110) = 0
101 <... openat resumed>) = 6</repo/slow "q".txt>
100 connect(6<socket:[2]>, {sa_family=AF_UNIX, sun_path="/tmp/own.sock"}, 110) = 0
100 connect(7<socket:[3]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("140.82.112.3")}, 16) = -1 EINPROGRESS (Operation now in progress)
100 rename("/repo/out.tmp", "/repo/out2.txt") = 0
100 --- SIGCHLD {si_signo=SIGCHLD} ---
100 +++ exited with 0 +++
"""


class ParseAndVetoTests(unittest.TestCase):
    # Tests [@ANCHOR: lint_trace_cache:parse_trace]
    def test_the_parser_sorts_reads_listings_absent_writes_and_contacts(self):
        trace = ltc.parse_trace(io.StringIO(SAMPLE))
        self.assertEqual(trace.reads, {"/repo/t.py", '/repo/slow "q".txt'})
        self.assertEqual(trace.dirs, {"/repo/pkg"})
        self.assertEqual(trace.absent, {"/repo/missing.py"})
        self.assertEqual(trace.written, {"/repo/out.txt", "/repo/out2.txt"})
        self.assertEqual(trace.execs, {"/usr/bin/python3"})
        self.assertEqual(trace.bound, {"/tmp/own.sock"})
        self.assertIn(("inet", ("140.82.112.3", 443)), trace.contacts)

    # Tests [@ANCHOR: lint_trace_cache:vetoes]
    def test_network_host_services_hardware_environment_and_randomness_are_vetoed(self):
        trace = ltc.parse_trace(io.StringIO(SAMPLE))
        self.assertEqual(ltc.vetoes(trace), ["network: contacted 140.82.112.3:443"])  # its own Unix socket is fine
        trace = ltc.Trace()
        trace.contacts = [("inet", ("127.0.0.1", 5432)), ("inet", ("127.0.0.1", 40000)), ("unix", "/var/run/postgresql/.s.PGSQL.5432"),
                          ("unix", "/run/systemd/userdb/io.systemd.DynamicUser")]
        trace.reads = {"/dev/ttyUSB0", "/proc/net/route", "/etc/resolv.conf", "/proc/self/status", "/proc/meminfo", "/proc/pressure/cpu", "/dev/shm", "/sys/fs/cgroup/cpu.max", "/dev/urandom", "/dev/pts/3",
                       "/repo/.hypothesis/examples/ab"}
        reasons = ltc.vetoes(trace, listening_before={5432})
        self.assertEqual(reasons, sorted([
            "host service: connected to loopback port 5432, which was listening before the step started",
            "host service: connected to the Unix socket /var/run/postgresql/.s.PGSQL.5432",
            "hardware: read /dev/ttyUSB0",
            "environment: read /proc/net/route",
            "randomised: read the Hypothesis example database /repo/.hypothesis/examples/ab",
        ]))

    def test_reads_between_the_pause_and_resume_markers_of_a_process_are_not_recorded(self):
        text = (
            '7 openat(AT_FDCWD</w>, "/proc/self/hams-lint-trace-pause", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)\n'
            '7 openat(AT_FDCWD</w>, "/repo/copied.rs", O_RDONLY|O_CLOEXEC) = 3</repo/copied.rs>\n'
            '8 openat(AT_FDCWD</w>, "/repo/other.rs", O_RDONLY|O_CLOEXEC) = 3</repo/other.rs>\n'
            '7 connect(4<socket:[3]>, {sa_family=AF_INET, sin_port=htons(80), sin_addr=inet_addr("10.0.0.1")}, 16) = 0\n'
            '7 openat(AT_FDCWD</w>, "/proc/self/hams-lint-trace-resume", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)\n'
            '7 openat(AT_FDCWD</w>, "/repo/built.rs", O_RDONLY|O_CLOEXEC) = 3</repo/built.rs>\n')
        trace = ltc.parse_trace(io.StringIO(text))
        self.assertEqual(trace.reads, {"/repo/other.rs", "/repo/built.rs"})
        self.assertEqual(trace.absent, set())
        self.assertEqual(len(trace.contacts), 1)  # the network is never paused

    # Tests [@ANCHOR: lint_trace_cache:step_key]
    def test_the_key_ignores_job_and_session_bookkeeping_but_not_other_variables(self):
        cache = ltc.TraceCache(tempfile.mkdtemp(), ltc.Roots({}), versions="v", environ={})
        self.addCleanup(shutil.rmtree, cache.directory, True)

        def key(**env):
            return cache.step_key(lint_exec.Step("pytest x", ["python3", "t.py"], env=dict({"PATH": "/usr/bin"}, **env)))
        self.assertEqual(key(AGENT_PROCS_JOB="lintA", BAGGAGE="a"), key(AGENT_PROCS_JOB="lintB", BAGGAGE="b"))
        self.assertNotEqual(key(HAMS_FEATURE="1"), key(HAMS_FEATURE="2"))
        self.assertNotEqual(key(DISPLAY=":0"), key())

    def test_the_maximum_age_takes_only_finite_non_negative_numbers(self):
        self.assertEqual(ltc.max_age_seconds({}), ltc.MAX_AGE_SECONDS)
        self.assertEqual(ltc.max_age_seconds({"HAMS_LINT_TRACE_CACHE_MAX_AGE": "60"}), 60.0)
        for bad in ("inf", "nan", "-5", "x"):
            self.assertEqual(ltc.max_age_seconds({"HAMS_LINT_TRACE_CACHE_MAX_AGE": bad}), ltc.MAX_AGE_SECONDS, bad)

    def test_listed_steps_are_never_traced_and_only_test_steps_are(self):
        self.assertFalse(ltc.traced("pytest daemons/relay_ca"))
        self.assertFalse(ltc.traced("check_burn_list"))
        self.assertTrue(ltc.traced("pytest tools/[1of4]"))
        self.assertTrue(ltc.traced("relay structural tests"))
        for prefix, reason in ltc.NEVER_CACHED.items():
            self.assertTrue(reason, prefix)


@unittest.skipUnless(ltc.strace_usable(), "strace cannot trace here")
class TraceCacheEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "hams_com")
        os.makedirs(os.path.join(self.repo, "data"))
        self.store = os.path.join(self.tmp, "store")
        self.counter = os.path.join(self.tmp, "runs")
        self.write("data/a.txt", "alpha\n")
        self.write("test_x.py", "import os, sys\n"
                                "open(os.environ['COUNTER'], 'a').write('x')\n"
                                "names = sorted(os.listdir(os.path.join(os.path.dirname(__file__), 'data')))\n"
                                "text = open(os.path.join(os.path.dirname(__file__), 'data', 'a.txt')).read()\n"
                                "print('ok', names, text.strip())\n"
                                "sys.exit(0 if 'FAIL' not in text else 1)\n")

    def write(self, rel, text, root=None):
        with open(os.path.join(root or self.repo, rel), "w") as handle:
            handle.write(text)

    def step(self, repo=None, name="pytest fake", script="test_x.py"):
        repo = repo or self.repo
        return lint_exec.Step(name, [PY, os.path.join(repo, script)], cwd=repo, env=dict(os.environ, COUNTER=self.counter))

    def cache(self, repo=None):
        return ltc.TraceCache(self.store, ltc.Roots({"hams_com": repo or self.repo}), versions="v1", environ={})

    def run_once(self, step=None, repo=None):
        out = io.StringIO()
        result = lint_exec.execute([step or self.step(repo)], 1, out=out, trace_cache=self.cache(repo))[0]
        return result, out.getvalue()

    def runs(self):
        try:
            with open(self.counter) as handle:
                return len(handle.read())
        except OSError:
            return 0

    # Tests [@ANCHOR: lint_exec:traced_work]
    def test_an_unchanged_step_is_a_cache_hit_with_the_original_pass(self):
        first, first_out = self.run_once()
        second, second_out = self.run_once()
        self.assertEqual((first.returncode, first.cached), (0, False))
        self.assertEqual((second.returncode, second.cached), (0, True))
        self.assertTrue(second_out.startswith(first_out), second_out)
        self.assertIn("result cache:", second_out)
        self.assertEqual(self.runs(), 1)

    # Tests [@ANCHOR: lint_trace_cache:verify]
    def test_a_changed_input_and_a_new_file_in_a_listed_directory_each_rerun_the_step(self):
        self.run_once()
        self.write("data/a.txt", "beta\n")
        self.assertFalse(self.run_once()[0].cached)
        self.write("data/b.txt", "new\n")
        self.assertFalse(self.run_once()[0].cached)
        self.assertTrue(self.run_once()[0].cached)
        self.assertEqual(self.runs(), 3)

    def test_negative_control_an_injected_failure_is_never_served_from_the_cache(self):
        self.assertTrue(self.run_once()[0].returncode == 0)
        self.write("data/a.txt", "FAIL injected\n")
        failed, out = self.run_once()
        self.assertEqual((failed.returncode, failed.cached), (1, False))
        again, _ = self.run_once()
        self.assertEqual((again.returncode, again.cached), (1, False))  # a failure is never stored
        self.write("data/a.txt", "alpha\n")  # back to the passing content: the stored pass is valid again
        self.assertTrue(self.run_once()[0].cached)
        self.assertEqual(self.runs(), 3)

    def test_negative_control_an_injected_failing_test_file_is_never_served_from_the_cache(self):
        self.run_once()
        path = os.path.join(self.repo, "test_x.py")
        with open(path) as handle:
            body = handle.read()
        self.write("test_x.py", "raise SystemExit('injected failure')\n" + body)
        failed, out = self.run_once()
        self.assertEqual((failed.returncode, failed.cached), (1, False), out)
        self.assertIn("injected failure", out)

    def test_the_same_content_in_another_checkout_is_a_hit(self):
        self.run_once()
        other = os.path.join(self.tmp, "elsewhere", "hams_com")
        shutil.copytree(self.repo, other)
        result, _ = self.run_once(repo=other)
        self.assertTrue(result.cached)
        self.assertEqual(self.runs(), 1)

    def test_a_step_that_touches_the_network_is_never_stored(self):
        for name, send in (("test_net.py", "s.connect(('192.0.2.1', 9)); s.send(b'x')\n"), ("test_net2.py", "s.sendto(b'x', ('192.0.2.2', 9))\n")):
            self.write(name, "import socket\n"
                             "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n" + send + "print('sent')\n")
            step = self.step(script=name, name="pytest " + name)
            self.assertFalse(self.run_once(step)[0].cached)
            self.assertFalse(self.run_once(step)[0].cached)
        with open(os.path.join(self.store, "not_stored.log")) as handle:
            text = handle.read()
        self.assertIn("network: contacted 192.0.2.1:9", text)  # connected UDP
        self.assertIn("network: contacted 192.0.2.2:9", text)  # unconnected UDP (dnspython does this)

    def test_reads_inside_the_work_copy_markers_are_not_inputs(self):
        tools = os.path.dirname(os.path.abspath(__file__))
        self.write("copied.txt", "copied\n")
        self.write("test_copy.py", "import sys\n"
                                   "sys.path.insert(0, %r)\n"
                                   "import lint_cache\n"
                                   "lint_cache.trace_marker('pause')\n"
                                   "open(%r).read()\n"
                                   "lint_cache.trace_marker('resume')\n"
                                   "print('done')\n" % (tools, os.path.join(self.repo, "copied.txt")))
        step = self.step(script="test_copy.py")
        self.run_once(step)
        self.write("copied.txt", "changed, but only the copy read it\n")
        self.assertTrue(self.run_once(step)[0].cached)

    # Tests [@ANCHOR: lint_trace_cache:trace_blockers]
    def test_a_step_that_runs_a_setuid_program_is_rerun_untraced_and_never_traced_again(self):
        setuid_true = os.path.join(self.tmp, "setuid_true")
        shutil.copy("/bin/true", setuid_true)
        os.chmod(setuid_true, 0o4755)
        if not os.stat(setuid_true).st_mode & 0o4000:
            self.skipTest("this file system drops the setuid bit")
        self.write("test_priv.py", "import os, subprocess\n"
                                   "open(os.environ['COUNTER'], 'a').write('x')\n"
                                   "subprocess.run([%r], check=True)\n"
                                   "print('privileged ok')\n" % setuid_true)
        step = self.step(script="test_priv.py")
        first, out = self.run_once(step)
        self.assertEqual((first.returncode, first.cached), (0, False), out)
        self.assertEqual(self.runs(), 2)  # traced, then the untraced run whose result counts
        cache = self.cache()
        self.assertIn("setuid", cache.untraceable(cache.step_key(step)))
        second, _ = self.run_once(step)
        self.assertEqual((second.returncode, second.cached), (0, False))
        self.assertEqual(self.runs(), 3)  # untraced only, never stored

    def test_a_traced_run_ends_the_moment_a_setuid_program_starts_and_takes_its_children_along(self):
        setuid_true = os.path.join(self.tmp, "setuid_true")
        shutil.copy("/bin/true", setuid_true)
        os.chmod(setuid_true, 0o4755)
        if not os.stat(setuid_true).st_mode & 0o4000:
            self.skipTest("this file system drops the setuid bit")
        late = os.path.join(self.tmp, "late")
        trace_file = os.path.join(self.tmp, "a.txt")
        started = time.monotonic()
        script = "(sleep 3; touch %s) & %s; sleep 30" % (late, setuid_true)
        code = subprocess.run([PY, ltc.__file__, "run", trace_file, "--", "bash", "-c", script]).returncode
        self.assertEqual(code, ltc.ABORTED_STATUS)
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("setuid", ltc.aborted(trace_file))
        time.sleep(4)
        self.assertFalse(os.path.exists(late))  # the background child was ended with the run

    def test_a_timeout_that_happens_only_under_the_trace_is_decided_by_an_untraced_run(self):
        self.write("test_slow.py", "import os, sys\n"
                                   "open(os.environ['COUNTER'], 'a').write('x')\n"
                                   "traced = any(l.startswith('TracerPid:') and l.split()[1] != '0' for l in open('/proc/self/status'))\n"
                                   "if traced:\n"
                                   "    sys.exit('TimeoutError: timed out')\n"
                                   "print('fast enough')\n")
        step = self.step(script="test_slow.py")
        result, out = self.run_once(step)
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(self.runs(), 2)
        cache = self.cache()
        self.assertIsNotNone(cache.untraceable(cache.step_key(step)))

    def test_a_real_failure_mentioning_a_timeout_stays_a_failure(self):
        self.write("test_bad.py", "import sys\nsys.exit('TimeoutError: timed out for real')\n")
        result, out = self.run_once(self.step(script="test_bad.py"))
        self.assertEqual((result.returncode, result.cached), (1, False))
        self.assertIn("timed out for real", out)

    def test_an_expired_pass_is_not_used(self):
        self.run_once()
        cache = self.cache()
        step = self.step()
        key = cache.step_key(step)
        self.assertIsNotNone(cache.lookup(step, key))
        self.assertIsNone(cache.lookup(step, key, now=time.time() + ltc.MAX_AGE_SECONDS + 60))

    def test_no_cache_and_the_switch_turn_it_off(self):
        tools = os.path.dirname(os.path.abspath(__file__))
        self.assertIsNone(ltc.open_cache(self.repo, tools, "v", {"HAMS_LINT_NO_CACHE": "1"}))
        self.assertIsNone(ltc.open_cache(self.repo, tools, "v", {"HAMS_LINT_TRACE_CACHE": "0"}))

    def test_a_step_not_in_the_traced_set_never_uses_it(self):
        step = self.step(name="check_something")
        self.run_once(step)
        self.assertFalse(self.run_once(step)[0].cached)
        self.assertFalse(os.path.isdir(self.store))

    # Tests [@ANCHOR: lint_trace_cache:run_traced]
    def test_a_traced_run_returns_when_the_main_process_ends_not_when_its_leftovers_do(self):
        started = time.monotonic()
        code = subprocess.run([PY, ltc.__file__, "run", os.path.join(self.tmp, "t.txt"), "--", "bash", "-c",
                               "sleep 20 >/dev/null 2>&1 & exit 3"]).returncode
        self.assertEqual(code, 3)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(subprocess.run([PY, ltc.__file__, "run", os.path.join(self.tmp, "k.txt"), "--", "bash", "-c", "kill -9 $$"]).returncode,
                         137)


if __name__ == "__main__":
    unittest.main()
