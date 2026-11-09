#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for claude_hooks/guard_signals.py, the PreToolUse hook that refuses SIGKILL, pkill, killall, `pgrep -f` and pid lists built
by command substitution (Bruce, 2026-11-08). A table of real command texts (the exact command of the 12:18:33 incident
included) is judged by `analyze`, and the real script is run with the hook JSON on standard input, as Claude Code runs it.

Tests [@ANCHOR: guard_signals:Word]
Tests [@ANCHOR: guard_signals:Command]
Tests [@ANCHOR: guard_signals:Verdict]
Tests [@ANCHOR: guard_signals:find_paren_end]
Tests [@ANCHOR: guard_signals:find_backtick_end]
Tests [@ANCHOR: guard_signals:read_heredoc]
Tests [@ANCHOR: guard_signals:tokenize]
Tests [@ANCHOR: guard_signals:skip_options]
Tests [@ANCHOR: guard_signals:resolve]
Tests [@ANCHOR: guard_signals:signal_is_kill]
Tests [@ANCHOR: guard_signals:check_kill]
Tests [@ANCHOR: guard_signals:check_pgrep]
Tests [@ANCHOR: guard_signals:shell_script]
Tests [@ANCHOR: guard_signals:check_command]
Tests [@ANCHOR: guard_signals:analyze]
Tests [@ANCHOR: guard_signals:log_path]
Tests [@ANCHOR: guard_signals:log_event]
Tests [@ANCHOR: guard_signals:deny_message]
Tests [@ANCHOR: guard_signals:decide]
Tests [@ANCHOR: guard_signals:main]
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "claude_hooks"))
import guard_signals as gs  # noqa: E402

HOOK = os.path.join(HERE, "claude_hooks", "guard_signals.py")

DENIED = [
    # the incident of 2026-11-08 12:18:33, exactly as typed
    'sudo kill -9 $(pgrep -f "[h]ams_pf_probe")',
    "kill -9 123", "kill -KILL 123", "kill -SIGKILL 123", "kill -s KILL 123", "kill -s 9 123", "kill --signal=KILL 123",
    "kill --signal=9 123", "kill -n 9 123", "sudo -u odoo kill -9 5", "kill -9 -- -123", "/bin/kill -9 5",
    "pkill hams_pf_probe", "pkill -f hams_pf_probe", "sudo pkill -9 -u odoo chrome", "killall python3", "skill -9 bob",
    "fuser -k 8069/tcp", "fuser -km /mnt", "systemctl kill -s KILL postgresql", "sudo systemctl kill foo",
    'until ! pgrep -f "tools/test.py" >/dev/null; do sleep 3; done',
    "while pgrep -f 'tools/test.py'; do sleep 3; done; python3 x.py",
    "bash -c 'until ! pgrep -f \"tools/test.py\"; do sleep 3; done'",
    "pgrep -af tools/test.py", "pgrep -fl x", "pgrep --full x", "pgrep -u odoo -f '^python3 hams_shared/tools/test.py'",
    "echo $(pgrep -f foo)", "x=`pgrep -f foo`; echo $x", 'echo "$(pgrep -af foo)"',
    "kill $(pgrep chrome)", "kill $(pidof foo)", "kill `pgrep foo`", "kill $pid", "kill ${PIDS}", 'kill "$pid"',
    "kill 123 $(cat pids)", "kill -TERM $(ps -o pid= -C foo)",
    "ps aux | grep foo | awk '{print $2}' | xargs kill", "pgrep foo | xargs kill -TERM", "echo 1 2 | xargs -r kill",
    "ps -eo pid,args | awk '/x/ {print $1}' | xargs -n1 sudo kill", "pgrep foo | xargs -I{} kill {}",
    "for p in $(pgrep foo); do kill $p; done", "for p in 1 2; do kill -9 $p; done",
    "find /proc -maxdepth 1 -exec kill {} \\;", "find . -exec pkill x \\;", "ssh host 'sudo pkill -f foo'",
    "ssh -p 22 host sudo kill -9 5", "watch -n 5 pkill foo", "eval 'kill -9 5'", 'sh -c "kill -9 5"', "bash -lc 'pkill foo'",
    "nohup pkill foo &", "timeout 5 pkill foo", "nice -n 5 killall x", "env FOO=1 kill -9 5", "sudo -n -u odoo env X=1 pkill x",
    "cd /tmp && pkill foo", "true || pkill foo", "(pkill foo)", "{ pkill foo; }", "! pgrep -f foo",
    "python3 -c 'import os; os.kill(5, 9)'", "python3 -c 'import os,signal; os.kill(5, signal.SIGKILL)'",
    "python3 - <<'EOF'\nimport os\nos.killpg(5, 9)\nEOF\n", "python3 -c \"import subprocess; subprocess.run(['pkill', 'x'])\"", "bash <<'EOF'\npkill foo\nEOF\n",
    "sleep 1\npkill foo\n", "echo a; kill -9 5 # note", "kill -9 5 2>/dev/null",
    # find -exec
    "find . -exec true {} \\; -exec pkill x \\;", "find . -exec sh -c 'kill -9 $0' {} \\;", "find . -ok kill -9 {} +",
    # nesting
    "echo $(echo $(echo $(pkill foo)))", 'echo "a $(kill -9 5) b"',
]

ALLOWED = [
    "ls -la", "grep -rn pkill docs | head", "grep -n 'pgrep -f' CLAUDE.md", "git commit -m 'never run pkill -9 or kill -9 again'",
    'git commit -m "$(cat <<\'EOF\'\nstop using pgrep -f and pkill\nEOF\n)"', "echo pkill", "echo 'kill -9 $(pgrep -f x)'",
    "cat <<'EOF'\npkill foo; kill -9 5; pgrep -f x\nEOF\n", "python3 - <<'EOF'\nprint('pkill and kill -9 are bad')\nEOF\n",
    "ps aux", "ps -eo pid,user,lstart,args | grep postgres", "ps -p 123 -o pid,user,lstart,args", "pgrep -x postgres",
    "pgrep -u bruce chrome", "pgrep -c sshd", "pidof sshd", "which pkill", "command -v pkill", "type pkill", "man kill",
    "kill -TERM 123", "kill 123", "kill -15 123", "kill -0 123", "kill -HUP 123 456", "sudo kill -TERM 123", "kill -l",
    "kill -s TERM 123", "kill --signal=TERM 123", "kill %1", "kill -- -123", "kill $!", "sleep 100 & echo $!",
    "cargo test --all", "cargo clippy --all-targets -- -D warnings", "git status && git log --oneline | head",
    "sleep 5", "until [ -e /tmp/x ]; do sleep 1; done", "while sleep 1; do :; done & echo started",
    "python3 hams_shared/tools/agent_procs.py wait --pid 123 --start-time 456 --timeout 600",
    "python3 hams_shared/tools/agent_procs.py wait --job build",
    "python3 hams_shared/tools/agent_procs.py stop build --force",
    "python3 hams_shared/tools/agent_procs.py kill-pid 123 --start-time 456 --force",
    "python3 hams_shared/tools/agent_procs.py find --cmd-contains hams_pf_probe",
    "until ! python3 hams_shared/tools/agent_procs.py find --quiet --cmd-contains tools/test.py; do sleep 3; done",
    "bash -c 'until [ -e done.marker ]; do sleep 1; done'", "timeout 60 sleep 100", "nice -n 10 python3 x.py 2>&1 | tail -5",
    "sudo -n -u odoo env HAMS_ISOLATED_NS=1 python3 hams_shared/tools/test.py --wait -u hams_x",
    "x=$(date +%s); echo $x", "echo $((1+2))", "find . -name '*.py' -exec grep -l kill {} \\;", "xargs -n1 echo",
    "ssh host 'systemctl status foo'", "python3 -c 'print(1)'", "python3 -c 'import os; os.kill(5, 0)'",
    "docker ps", "tail -f log | grep -m1 DONE", "pgrep", "echo kill -9 > notes.txt", "sed -i 's/pkill/stop/' file",
    "# pkill foo in a comment", "echo done # kill -9 5", "",
    "find . -exec echo {} \\; -exec ls {} +", "find . -exec sh -c 'echo $0' {} \\;",
]


class GuardTestCase(unittest.TestCase):
    """Base: a private HAMS_JOBS_DIR so the log of a test never goes to the real ~/.cache."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard-test-")
        self.environ = {"HAMS_JOBS_DIR": os.path.join(self.tmp, "jobs")}
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_hook(self, command, tool="Bash", extra_env=None):
        """Run the real script the way Claude Code does; returns (exit code, parsed stdout JSON or None, stderr)."""
        payload = json.dumps({"session_id": "s1", "tool_name": tool, "tool_input": {"command": command}})
        done = subprocess.run([sys.executable, HOOK], input=payload, capture_output=True, text=True,
                              env=dict(os.environ, **self.environ, **(extra_env or {})))
        return done.returncode, (json.loads(done.stdout) if done.stdout.strip() else None), done.stderr


class TableTests(GuardTestCase):
    """analyze and decide on the table of commands."""

    def test_every_forbidden_command_is_denied(self):
        self.assertGreater(len(DENIED), 50)
        for command in DENIED:
            verdict = gs.analyze(command)
            self.assertTrue(verdict.reasons, f"not denied: {command!r}")
            result = gs.decide(command, environ=self.environ)
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny", command)

    def test_every_ordinary_command_is_allowed(self):
        self.assertGreater(len(ALLOWED), 60)
        for command in ALLOWED:
            verdict = gs.analyze(command)
            self.assertEqual(verdict.reasons, [], f"wrongly denied: {command!r}")
            result = gs.decide(command, environ=self.environ)
            self.assertTrue(result is None or "permissionDecision" not in result["hookSpecificOutput"], command)

    def test_the_incident_command_is_denied_with_both_reasons_and_the_tool_named(self):
        command = 'sudo kill -9 $(pgrep -f "[h]ams_pf_probe")'
        verdict = gs.analyze(command)
        text = " ".join(verdict.reasons)
        self.assertIn("SIGKILL", text)
        self.assertIn("pgrep -f", text)
        self.assertIn("kill needs literal pid", text)
        message = gs.decide(command, environ=self.environ)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("agent_procs.py", message)
        self.assertIn("HAMS_ALLOW_SIGNALS=1", message)
        self.assertIn("Never find processes by a command-line pattern", message)

    def test_literal_kill_is_allowed_with_a_reminder_not_a_permission_decision(self):
        result = gs.decide("kill -TERM 123", environ=self.environ)
        self.assertNotIn("permissionDecision", result["hookSpecificOutput"])
        self.assertIn("ps line", result["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(gs.decide("ls", environ=self.environ))
        self.assertIsNone(gs.decide("kill -l", environ=self.environ))


class ParserTests(GuardTestCase):
    """tokenize, find_paren_end, find_backtick_end, read_heredoc, skip_options, resolve, shell_script, Word, Command,
    Verdict."""

    def words(self, text):
        return [[w.text for w in c.words] for c in gs.tokenize(text, [])]

    def test_quotes_escapes_comments_and_operators(self):
        self.assertEqual(self.words("echo 'a b' \"c d\" e\\ f # no"), [["echo", "a b", "c d", "e f"]])
        self.assertEqual(self.words("a && b || c; d | e & f\ng"), [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"]])
        self.assertEqual(self.words("cmd 2>/dev/null >out arg"), [["cmd", "arg"]])
        self.assertEqual(self.words("cmd >&2 x"), [["cmd", "x"]])
        self.assertEqual(self.words("echo a#b"), [["echo", "a#b"]])
        self.assertEqual(self.words(""), [])

    def test_substitutions_become_commands_and_mark_words(self):
        commands = gs.tokenize("kill $(pgrep -x foo) `date` <(ls) \"$(id)\"", [])
        names = [c.words[0].text for c in commands]
        self.assertEqual(sorted(names), sorted(["pgrep", "date", "ls", "id", "kill"]))
        kill = next(c for c in commands if c.words[0].text == "kill")
        self.assertTrue(all(w.expands for w in kill.words[1:]))
        self.assertFalse(kill.words[0].expands)
        plain = gs.tokenize("kill 123 '$x'", [])[0]
        self.assertEqual([w.expands for w in plain.words], [False, False, False])

    def test_heredoc_bodies_attach_to_the_command(self):
        commands = gs.tokenize("cat <<EOF\nline one\nEOF\necho after\n", [])
        self.assertEqual([c.words[0].text for c in commands], ["cat", "echo"])
        self.assertEqual(commands[0].bodies, ["line one"])
        tabbed = gs.tokenize("cat <<-X\n\tbody\n\tX\nls", [])
        self.assertEqual(tabbed[0].bodies, ["\tbody"])
        self.assertEqual([c.words[0].text for c in tabbed], ["cat", "ls"])

    def test_nesting_too_deep_raises(self):
        text = "echo " + "$(" * 12 + "x" + ")" * 12
        with self.assertRaises(ValueError):
            gs.tokenize(text, [])
        with self.assertRaises(ValueError):
            gs.analyze(text)

    def test_find_paren_end_and_backtick_end_and_read_heredoc(self):
        self.assertEqual(gs.find_paren_end("a (b) c) d", 0), 7)
        self.assertEqual(gs.find_paren_end("x ')' \")\" y) z", 0), 11)
        self.assertEqual(gs.find_paren_end("never closed", 0), len("never closed"))
        self.assertEqual(gs.find_backtick_end("ab`cd", 0), 2)
        self.assertEqual(gs.find_backtick_end("a\\`b`c", 0), 4)
        self.assertEqual(gs.find_backtick_end("none", 0), 4)
        self.assertEqual(gs.read_heredoc("one\ntwo\nEND\nrest", 0, "END", False), ("one\ntwo", 12))
        self.assertEqual(gs.read_heredoc("one\n", 0, "END", False), ("one", 4))

    def test_skip_options_resolve_and_wrappers(self):
        words = [gs.Word(t) for t in "sudo -n -u odoo env A=1 nice -n 5 timeout 10 pkill x".split()]
        name, rest, via = gs.resolve(words)
        self.assertEqual((name, rest[0].text), ("pkill", "pkill"))
        self.assertFalse(via)
        self.assertEqual(gs.skip_options([gs.Word(t) for t in ["-a", "-u", "x", "cmd"]], 0, {"-u"}), 3)
        self.assertEqual(gs.skip_options([gs.Word(t) for t in ["--", "-a"]], 0, set()), 1)
        name, rest, via = gs.resolve([gs.Word(t) for t in "xargs -r -n1 kill".split()])
        self.assertEqual((name, via), ("kill", True))
        self.assertEqual(gs.resolve([gs.Word(t) for t in "until ! A=1 pgrep x".split()])[0], "pgrep")
        self.assertIsNone(gs.resolve([gs.Word("A=1")])[0])
        self.assertIsNone(gs.resolve([gs.Word(t) for t in "command -v pkill".split()])[0])

    def test_shell_script_extraction(self):
        self.assertEqual(gs.shell_script([gs.Word(t) for t in ["bash", "-c", "ls"]]), "ls")
        self.assertEqual(gs.shell_script([gs.Word(t) for t in ["bash", "-lc", "ls"]]), "ls")
        self.assertIsNone(gs.shell_script([gs.Word(t) for t in ["bash", "script.sh"]]))

    def test_data_classes(self):
        word = gs.Word("x")
        self.assertFalse(word.expands)
        self.assertEqual(gs.Command([word]).bodies, [])
        self.assertEqual((gs.Verdict().reasons, gs.Verdict().notes), ([], []))


class RuleTests(GuardTestCase):
    """signal_is_kill, check_kill, check_pgrep, check_command."""

    def test_signal_is_kill_spellings(self):
        for spec in ("9", "-9", "KILL", "-KILL", "SIGKILL", "-sigkill", "kill"):
            self.assertTrue(gs.signal_is_kill(spec), spec)
        for spec in ("TERM", "-15", "HUP", "0", "SIGTERM", "-19"):
            self.assertFalse(gs.signal_is_kill(spec), spec)

    def test_check_kill_variants(self):
        def verdict_for(text):
            command = gs.tokenize(text, [])[0]
            result = gs.Verdict()
            gs.check_kill(command.words, False, result)
            return result
        self.assertEqual(verdict_for("kill -TERM 1 2 3").reasons, [])
        self.assertTrue(verdict_for("kill -TERM 1 2 3").notes)
        self.assertEqual(verdict_for("kill -l").notes, [])
        self.assertEqual(verdict_for("kill").reasons, [])
        self.assertTrue(verdict_for("kill -9 5").reasons)
        self.assertTrue(verdict_for("kill $x").reasons)
        self.assertEqual(verdict_for("kill $!").reasons, [])
        self.assertTrue(verdict_for("kill 12abc").reasons)
        self.assertTrue(verdict_for("kill -s KILL -- 5").reasons)

    def test_check_pgrep_variants(self):
        def reasons(text):
            result = gs.Verdict()
            gs.check_pgrep(gs.tokenize(text, [])[0].words, result)
            return result.reasons
        for text in ("pgrep -f x", "pgrep -af x", "pgrep -lf x", "pgrep --full x", "pgrep -u odoo -f x"):
            self.assertTrue(reasons(text), text)
        for text in ("pgrep x", "pgrep -x x", "pgrep -u bruce x", "pgrep -c x", "pgrep --count x"):
            self.assertEqual(reasons(text), [], text)

    def test_check_command_recursion_and_unrelated_commands(self):
        result = gs.Verdict()
        for text in ("ls -la", "echo hi", "pidof x", "systemctl status x", "fuser 80/tcp", "find . -name x"):
            gs.check_command(gs.tokenize(text, [])[0], result, 0)
        self.assertEqual(result.reasons, [])
        deep = gs.Verdict()
        gs.check_command(gs.tokenize("bash -c 'sh -c \"eval \\\"pkill x\\\"\"'", [])[0], deep, 0)
        self.assertTrue(deep.reasons)


class HookProcessTests(GuardTestCase):
    """main, decide, log_path, log_event, deny_message through the real script."""

    def test_the_incident_command_is_denied_through_the_script(self):
        code, out, _ = self.run_hook('sudo kill -9 $(pgrep -f "[h]ams_pf_probe")')
        self.assertEqual(code, 0)
        decision = out["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("agent_procs.py", decision["permissionDecisionReason"])

    def test_ordinary_commands_print_nothing_and_exit_zero(self):
        for command in ("grep -rn foo .", "ps aux", "cargo test", "git status", "sleep 3", "ls"):
            code, out, err = self.run_hook(command)
            self.assertEqual((code, out), (0, None), command)

    def test_a_literal_kill_gets_the_reminder_and_no_decision(self):
        code, out, _ = self.run_hook("kill -TERM 123")
        self.assertEqual(code, 0)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_other_tools_bad_json_and_missing_command_are_allowed(self):
        self.assertEqual(self.run_hook("pkill foo", tool="Read")[1], None)
        for raw in ("not json", "[]", "{}", '{"tool_name": "Bash", "tool_input": {"command": 5}}', ""):
            out = io.StringIO()
            self.assertEqual(gs.main(io.StringIO(raw), out, self.environ), 0)
            self.assertEqual(out.getvalue(), "")

    def test_main_prints_the_decision_json(self):
        out = io.StringIO()
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "pkill x"}})
        self.assertEqual(gs.main(io.StringIO(payload), out, self.environ), 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_human_escape_allows_and_logs(self):
        command = "HAMS_ALLOW_SIGNALS=1 kill -9 4242"
        code, out, _ = self.run_hook(command)
        self.assertEqual((code, out), (0, None))
        with open(gs.log_path(self.environ)) as handle:
            lines = [json.loads(line) for line in handle]
        self.assertEqual([line["kind"] for line in lines], ["override"])
        self.assertEqual(lines[0]["command"], command)
        self.assertEqual(lines[0]["session"], "s1")
        self.assertEqual(oct(os.stat(os.path.dirname(gs.log_path(self.environ))).st_mode & 0o777), "0o700")

    def test_the_escape_must_be_exactly_one_and_a_word_of_its_own(self):
        for command in ("HAMS_ALLOW_SIGNALS=0 kill -9 5", "XHAMS_ALLOW_SIGNALS=1 kill -9 5", "HAMS_ALLOW_SIGNALS=10 kill -9 5",
                        "echo HAMS_ALLOW_SIGNALS=1x; kill -9 5"):
            self.assertEqual(self.run_hook(command)[1]["hookSpecificOutput"]["permissionDecision"], "deny", command)
        self.assertIsNone(self.run_hook("export HAMS_ALLOW_SIGNALS=1; pkill foo")[1])

    def test_denials_are_logged_too(self):
        self.run_hook("pkill foo")
        with open(gs.log_path(self.environ)) as handle:
            self.assertEqual(json.loads(handle.readline())["kind"], "denied")

    def test_a_crash_in_the_hook_fails_open_for_ordinary_commands_only(self):
        too_deep_plain = "echo " + "$(" * 12 + "x" + ")" * 12
        self.assertIsNone(gs.decide(too_deep_plain, environ=self.environ))
        self.assertEqual(self.run_hook(too_deep_plain), (0, None, ""))
        too_deep_signal = "kill " + "$(" * 12 + "x" + ")" * 12
        result = gs.decide(too_deep_signal, environ=self.environ)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(gs.decide("HAMS_ALLOW_SIGNALS=1 " + too_deep_signal, environ=self.environ))
        with open(gs.log_path(self.environ)) as handle:
            self.assertIn("error", [json.loads(line)["kind"] for line in handle])

    def test_log_path_and_an_unwritable_log_never_raise(self):
        self.assertTrue(gs.log_path({}).endswith(os.path.join(".cache", "hams-jobs", "guard.log")))
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        gs.log_event("denied", "pkill x", ["r"], environ={"HAMS_JOBS_DIR": os.path.join(blocker, "sub")})  # no exception

    def test_deny_message_names_the_replacements(self):
        message = gs.deny_message(["a", "a", "b"])
        self.assertEqual(message.count("a;") + message.count("a."), 1)
        for word in ("wait --pid", "run NAME", "stop NAME", "kill-pid", "find --cmd-contains", "HAMS_ALLOW_SIGNALS=1"):
            self.assertIn(word, message)

    def test_a_missing_script_in_the_settings_wrapper_command_allows_everything(self):
        """The command used in .claude/settings.json: when the script is absent the wrapper must exit 0, silently."""
        wrapper = 'f="$HAMS_TEST_HOOK"; [ -f "$f" ] || exit 0; exec python3 "$f"'
        absent = subprocess.run(["bash", "-c", wrapper], input='{"tool_name":"Bash","tool_input":{"command":"pkill x"}}',
                                capture_output=True, text=True, env=dict(os.environ, HAMS_TEST_HOOK=self.tmp + "/none.py"))
        self.assertEqual((absent.returncode, absent.stdout), (0, ""))
        present = subprocess.run(["bash", "-c", wrapper], input='{"tool_name":"Bash","tool_input":{"command":"pkill x"}}',
                                 capture_output=True, text=True, env=dict(os.environ, HAMS_TEST_HOOK=HOOK, **self.environ))
        self.assertEqual(json.loads(present.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
