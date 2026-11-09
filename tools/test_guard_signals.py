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
Tests [@ANCHOR: guard_signals:protected_words]
Tests [@ANCHOR: guard_signals:literal_pid_reason]
Tests [@ANCHOR: guard_signals:check_kill]
Tests [@ANCHOR: guard_signals:check_pgrep]
Tests [@ANCHOR: guard_signals:shell_script]
Tests [@ANCHOR: guard_signals:check_command]
Tests [@ANCHOR: guard_signals:expand_program]
Tests [@ANCHOR: guard_signals:piped_shell_sources]
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
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "claude_hooks"))
import guard_signals as gs  # noqa: E402

HOOK = os.path.join(HERE, "claude_hooks", "guard_signals.py")
REAL_SNAPSHOT = gs.agent_procs.snapshot  # every test case patches gs.agent_procs.snapshot to an empty table

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
    "kill -sKILL 123", "kill -09 123", "kill -n9 5", "kill -s 09 5", "kill -TERM -- -1", "kill -TERM 0", "kill 1",
    "bash <<< 'kill -9 5'", 'sh <<<"pkill foo"', "bash -s <<<'pgrep -f x'",
    "python3 -c 'import os; os.kill(5, 9)'", "python3 -c 'import os,signal; os.kill(5, signal.SIGKILL)'",
    "python3 - <<'EOF'\nimport os\nos.killpg(5, 9)\nEOF\n", "python3 -c \"import subprocess; subprocess.run(['pkill', 'x'])\"", "bash <<'EOF'\npkill foo\nEOF\n",
    "sleep 1\npkill foo\n", "echo a; kill -9 5 # note", "kill -9 5 2>/dev/null",
    # 2026-11-09: an agent killed its own parent (systemd --user): a pid from ps/ppid, then the literal pids it printed
    'pp=$(ps -o ppid= -p 2268409 | tr -d " "); echo parent $pp; kill $pp 2>/dev/null; kill 2268409 2268411 2>/dev/null',
    "pp=$(ps -o ppid= -p $$); kill $pp", "kill $PPID", "kill -TERM $PPID", "kill ${PPID}", "kill $$", "kill -TERM $$", "kill 0",
    "kill -TERM 0", "kill -- -123", "kill -TERM -- -123", "kill -- -1", "kill $(ps -o ppid= -p 5)", "kill `ps -o ppid= -p 5`",
    "ps -o ppid= -p 5 | xargs kill -TERM", "kill $(pidof sshd)", "kill -HUP $(pidof postgres)", "kill -TERM sshd", "kill postgres",
    "sudo kill -HUP systemd", "pkill systemd", "pkill -HUP sshd", "killall postgres", "sudo systemctl kill sshd",
    "bash -c 'kill $PPID'", "ssh host 'kill $PPID'",
    # find -exec
    "find . -exec true {} \\; -exec pkill x \\;", "find . -exec sh -c 'kill -9 $0' {} \\;", "find . -ok kill -9 {} +",
    # nesting
    "echo $(echo $(echo $(pkill foo)))", 'echo "a $(kill -9 5) b"',
    # Equivalent spellings (to-do signal-guard-shell-forms, 2026-11-09): a program named by a variable or ANSI-C quoting, a script piped
    # into a shell, wrappers with long or clustered options, and wrappers that were not read at all.
    "K=kill; $K -9 123", "${K:-kill} -9 1", "${K-pkill} x", "K='kill -9'; $K 123", "$'kill' -9 123", "$'p\\x6bill' x",
    "echo 'kill -9 1' | sh", "printf '%s' 'pkill foo' | bash", "echo pkill foo | bash -s", "cat <<EOF | bash\nkill -9 1\nEOF",
    "echo hi | cat | sh -c 'x' ; echo 'kill -9 1' | sh -",
    "sudo --user odoo pkill x", "sudo -nu odoo pkill x", "sudo --chdir /tmp pkill x", "time -p kill -9 1", "env -S 'kill -9 1'",
    "env -S'kill -9 1'", "env --split-string='pkill x'", "flock lock -c 'kill -9 1'", "flock -n lock -c 'pkill x'", "pkexec kill -9 1",
    "runuser -u bob kill -9 1", "script -c 'kill -9 1' /dev/null", "function f { kill -9 1; }; f", "busybox kill -9 1",
]

ALLOWED = [
    "ls -la", "grep -rn pkill docs | head", "grep -n 'pgrep -f' CLAUDE.md", "git commit -m 'never run pkill -9 or kill -9 again'",
    'git commit -m "$(cat <<\'EOF\'\nstop using pgrep -f and pkill\nEOF\n)"', "echo pkill", "echo 'kill -9 $(pgrep -f x)'",
    "cat <<'EOF'\npkill foo; kill -9 5; pgrep -f x\nEOF\n", "python3 - <<'EOF'\nprint('pkill and kill -9 are bad')\nEOF\n",
    "ps aux", "ps -eo pid,user,lstart,args | grep postgres", "ps -p 123 -o pid,user,lstart,args", "pgrep -x postgres",
    "pgrep -u bruce chrome", "pgrep -c sshd", "pidof sshd", "which pkill", "command -v pkill", "type pkill", "man kill",
    "kill -TERM 123", "kill 123", "kill -15 123", "kill -0 123", "kill -HUP 123 456", "sudo kill -TERM 123", "kill -l",
    "cat <<< 'pkill foo'", "grep -q x <<< 'kill -9 5'", "kill -sTERM 123", "kill -n15 123",
    "kill -s TERM 123", "kill --signal=TERM 123", "kill %1", "kill $!", "ps -o ppid= -p 123",
    "echo parent $(ps -o ppid= -p 123)", "ps -eo pid,ppid,args | grep systemd", "systemctl status sshd", "systemctl --user status", "sleep 100 & echo $!",
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
    "$CARGO test", "K=cargo; $K test", "${CARGO:-cargo} build", "echo hi | bash", "echo 'ls' | sh", "bash script.sh", "cat notes | sh script.sh",
    "time -p make", "sudo --user odoo ls", "sudo -nu odoo python3 x.py", "env -S 'ls -l'", "flock lock -c 'ls'", "script -c 'ls' /dev/null",
    "function f { ls; }; f", "busybox ls", "runuser -u bob ls", "$'ls' -l",
]


def agent_info(pid, ppid=1, argv=("sleep", "9"), comm="sleep", uid=None, user=None):
    """A synthetic agent_procs.ProcInfo for the table of a test."""
    uid = os.getuid() if uid is None else uid
    return gs.agent_procs.ProcInfo(pid=pid, ppid=ppid, pgid=pid, sid=pid, uid=uid, user=user or "bruce", state="S", start_ticks=1,
                                   comm=comm, argv=argv, cwd="/")


class GuardTestCase(unittest.TestCase):
    """Base: a private HAMS_JOBS_DIR so the log of a test never goes to the real ~/.cache."""

    def setUp(self):
        # The table commands name made-up pids: no real process of this machine may change a verdict.
        patcher = mock.patch.object(gs.agent_procs, "snapshot", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.assertIn("no escape", message)
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

    def test_a_here_string_is_attached_like_a_heredoc_body(self):
        commands = gs.tokenize("bash <<< 'echo one' ; cat <<<$x; ls <<<\"a b\"; cat <<<tail", [])
        self.assertEqual([c.words[0].text for c in commands], ["bash", "cat", "ls", "cat"])
        self.assertEqual([c.bodies for c in commands], [["echo one"], ["$x"], ["a b"], ["tail"]])
        self.assertEqual([len(c.words) for c in commands], [1, 1, 1, 1])

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

    def test_ansi_c_quoting_and_pipes_in_the_tokenizer(self):
        self.assertEqual(self.words("$'kill' -9 1 $'a\\x20b'"), [["kill", "-9", "1", "a b"]])
        commands = gs.tokenize("echo x | sh -s ; cat <<EOF | bash\nbody\nEOF\nls", [])
        self.assertEqual([(c.words[0].text, c.piped) for c in commands], [("echo", False), ("sh", True), ("cat", False), ("bash", True), ("ls", False)])
        self.assertEqual(commands[2].bodies, ["body"])  # the body of a heredoc whose command is piped on the same line

    def test_resolve_reads_the_wrappers_the_guard_once_missed(self):
        def name(text):
            return gs.resolve([gs.Word(t) for t in shlex.split(text)])[0]
        self.assertEqual([name(t) for t in ("sudo --user odoo pkill x", "sudo -nu odoo pkill x", "time -p kill 1", "pkexec kill 1",
                                            "runuser -u bob kill 1", "busybox kill 1", "function f { kill 1; }")], ["pkill", "pkill", "kill", "kill", "kill", "kill", "kill"])
        self.assertEqual(name("runuser -l bob -c script"), "runuser")  # the -c form stays a shell script for check_command
        self.assertEqual(name("env -S 'kill -9 1'"), "eval")
        self.assertEqual(name("flock lock -c 'kill -9 1'"), "sh")
        self.assertIsNone(name("function f"))

    # Tests [@ANCHOR: guard_signals:expand_program]
    def test_expand_program_uses_assignments_of_the_same_line_and_defaults(self):
        def program(text, assigned=None):
            return [w.text for w in gs.expand_program([gs.Word(t) for t in shlex.split(text)], assigned)]
        self.assertEqual(program("$K -9 1", {"K": "kill"}), ["kill", "-9", "1"])
        self.assertEqual(program("${K} 1", {"K": "kill -9"}), ["kill", "-9", "1"])
        self.assertEqual(program("${K:-kill} 1"), ["kill", "1"])
        self.assertEqual(program("${K-kill} 1", {"K": "echo"}), ["echo", "1"])
        self.assertEqual(program("A=1 $K 1", {"K": "pkill"}), ["A=1", "pkill", "1"])
        self.assertEqual(program("$CARGO test"), ["$CARGO", "test"])  # unknown: it may come from the environment, left alone
        self.assertEqual(program("echo $K", {"K": "kill"}), ["echo", "$K"])
        self.assertEqual(gs.expand_program([], {}), [])

    # Tests [@ANCHOR: guard_signals:piped_shell_sources]
    def test_piped_shell_sources_are_the_text_a_stdin_shell_will_run(self):
        def sources(text, index=None):
            commands = gs.tokenize(text, [])
            return gs.piped_shell_sources(commands, len(commands) - 1 if index is None else index)
        self.assertEqual(sources("echo -n 'kill -9 1' | sh"), ["kill -9 1"])
        self.assertEqual(sources("echo a | tr a b | bash"), ["a b", "a"])  # every earlier command of the pipeline, nearest first
        self.assertEqual(sources("printf '%s' 'ls -l' | sh"), ["%s ls -l", "%s", "ls -l"])
        self.assertEqual(sources("cat <<EOF | bash\nls\nEOF"), ["ls"])
        self.assertEqual(sources("echo ls | bash script.sh"), [])
        self.assertEqual(sources("echo ls | bash -c 'x'"), [])
        self.assertEqual(sources("echo ls ; sh"), [])
        self.assertEqual(sources("echo ls | cat"), [])

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
        for spec in ("9", "-9", "09", "-009", "KILL", "-KILL", "SIGKILL", "-sigkill", "kill"):
            self.assertTrue(gs.signal_is_kill(spec), spec)
        for spec in ("TERM", "-15", "HUP", "0", "SIGTERM", "-19"):
            self.assertFalse(gs.signal_is_kill(spec), spec)

    def test_check_kill_variants(self):
        def verdict_for(text):
            command = gs.tokenize(text, [])[0]
            result = gs.Verdict()
            gs.check_kill(command.words, False, result, {})
            return result
        self.assertEqual(verdict_for("kill -TERM 11 12 13").reasons, [])
        self.assertTrue(verdict_for("kill -TERM 11 12 13").notes)
        for text in ("kill -TERM 11 1", "kill -TERM 0", "kill -- -1", "kill -sKILL 5", "kill -n9 5", "kill -09 5"):
            self.assertTrue(verdict_for(text).reasons, text)
        self.assertTrue(verdict_for("kill -- -123 %1 10").reasons)  # a negative pid names a process group
        self.assertEqual(verdict_for("kill -l").notes, [])
        self.assertEqual(verdict_for("kill").reasons, [])
        self.assertTrue(verdict_for("kill -9 5").reasons)
        self.assertTrue(verdict_for("kill $x").reasons)
        self.assertEqual(verdict_for("kill $!").reasons, [])
        self.assertTrue(verdict_for("kill 12abc").reasons)
        self.assertTrue(verdict_for("kill -s KILL -- 5").reasons)

    def test_check_kill_refuses_parents_groups_and_protected_names(self):
        def reasons(text):
            result = gs.Verdict()
            gs.check_kill(gs.tokenize(text, [])[0].words, False, result, {})
            return " ".join(result.reasons)
        self.assertIn("parent", reasons("kill $PPID"))
        self.assertIn("parent", reasons("kill $$"))
        self.assertIn("parent", reasons("kill 0"))
        self.assertIn("parent", reasons("kill -- -4"))
        self.assertIn("ps`/`ppid", reasons("kill $pp"))
        self.assertIn("shared service", reasons("kill sshd"))
        self.assertEqual(reasons("kill -TERM 4"), "")

    def test_the_incident_of_2026_11_09_is_denied_for_the_variable_and_when_the_literals_are_protected(self):
        command = 'pp=$(ps -o ppid= -p 2268409 | tr -d " "); echo parent $pp; kill $pp 2>/dev/null; kill 2268409 2268411 2>/dev/null'
        verdict = gs.analyze(command)
        self.assertTrue(any("`ps`/`ppid`" in reason for reason in verdict.reasons), verdict.reasons)
        # the second kill names the pids the first run printed: here pid 2268409 is the agent's own parent
        parent = agent_info(2268409, ppid=2268400, argv=("claude",), comm="claude")
        systemd = agent_info(2268411, ppid=1, argv=("/usr/lib/systemd/systemd", "--user"), comm="systemd")
        table = {1: agent_info(1, ppid=0, argv=("/sbin/init",), comm="systemd", uid=0, user="root"), 2268409: parent,
                 2268411: systemd, 2268400: agent_info(2268400, ppid=2268411, argv=("claude",), comm="claude")}
        with mock.patch.object(gs.agent_procs, "snapshot", return_value=table), mock.patch.object(gs.os, "getpid", return_value=2268409):
            self.assertTrue(gs.analyze("kill 2268409 2268411 2>/dev/null").reasons)
            self.assertTrue(gs.analyze("kill 2268411").reasons)
            self.assertIn("ancestors", " ".join(gs.analyze("kill -TERM 2268411").reasons))
            table[5000] = agent_info(5000, ppid=1, argv=("/usr/lib/systemd/systemd", "--user"), comm="systemd")
            self.assertIn("systemd", " ".join(gs.analyze("kill -TERM 5000").reasons))

    def test_literal_pid_reason_uses_proc_ancestry_protected_words_and_ownership(self):
        me = os.getpid()
        self.assertIn("ancestors", gs.literal_pid_reason(me, {me: agent_info(me, ppid=1), 1: agent_info(1, ppid=0)}))
        self.assertIsNotNone(gs.literal_pid_reason(1, {1: agent_info(1, ppid=0), me: agent_info(me, ppid=1)}))
        postgres = agent_info(700, ppid=1, argv=("postgres: 16/main",), comm="postgres")
        self.assertIn("postgres", gs.literal_pid_reason(700, {700: postgres}))
        other = agent_info(701, ppid=1, argv=("sleep", "9"), comm="sleep", uid=os.getuid() + 1, user="other")
        self.assertIn("not by the caller", gs.literal_pid_reason(701, {701: other}))
        own = agent_info(702, ppid=me, argv=("sleep", "9"), comm="sleep")
        self.assertIsNone(gs.literal_pid_reason(702, {702: own, me: agent_info(me, ppid=1)}))
        self.assertIsNone(gs.literal_pid_reason(999999, {}))
        self.assertIsNone(gs.literal_pid_reason(0, {}))

    def test_literal_pid_reason_of_the_real_parent_and_pid_1(self):
        table = REAL_SNAPSHOT()
        self.assertIn("init", gs.literal_pid_reason(1, table))
        self.assertIn("ancestors", gs.literal_pid_reason(os.getppid(), table))
        self.assertIn("ancestors", gs.literal_pid_reason(os.getpid(), table))
        with mock.patch.object(gs, "agent_procs", None):
            self.assertIsNone(gs.literal_pid_reason(1))
            self.assertIn("postgres", gs.protected_words())

    def test_protected_words_are_the_agent_procs_list(self):
        self.assertEqual(gs.protected_words(), tuple(gs.agent_procs.PROTECTED_WORDS))
        for word in ("systemd", "postgres", "sshd"):
            self.assertIn(word, gs.protected_words())

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
        code, out, _ = self.run_hook("kill -TERM 4194300")
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

    def test_the_former_escape_variable_no_longer_bypasses_anything(self):
        commands = (
            "HAMS_ALLOW_SIGNALS=1 kill -9 123",
            "export HAMS_ALLOW_SIGNALS=1; pkill foo",
            "env HAMS_ALLOW_SIGNALS=1 pkill foo",
            "HAMS_ALLOW_SIGNALS=1 pkill foo",
            "HAMS_ALLOW_SIGNALS=1 sudo kill -9 $(pgrep -f foo)",
            "bash -c 'HAMS_ALLOW_SIGNALS=1 kill -9 123'",
            "bash -c 'export HAMS_ALLOW_SIGNALS=1; pkill foo'",
            "HAMS_ALLOW_SIGNALS=1 bash -c 'killall foo'",
            "echo HAMS_ALLOW_SIGNALS=1; kill -9 5",
            "kill -9 5 # HAMS_ALLOW_SIGNALS=1",
            "HAMS_ALLOW_SIGNALS=0 kill -9 5",
        )
        for command in commands:
            code, out, _ = self.run_hook(command)
            self.assertEqual(code, 0, command)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", command)
            self.assertIn("no escape", out["hookSpecificOutput"]["permissionDecisionReason"], command)
        with open(gs.log_path(self.environ)) as handle:
            kinds = {json.loads(line)["kind"] for line in handle}
        self.assertEqual(kinds, {"denied"})
        self.assertEqual(oct(os.stat(os.path.dirname(gs.log_path(self.environ))).st_mode & 0o777), "0o700")

    def test_the_variable_gives_no_bypass_when_the_hook_cannot_parse(self):
        too_deep = "kill " + "$(" * 12 + "x" + ")" * 12
        result = gs.decide("HAMS_ALLOW_SIGNALS=1 " + too_deep, environ=self.environ)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_escape_code_is_gone(self):
        self.assertFalse(hasattr(gs, "ESCAPE_VARIABLE"))
        self.assertFalse(hasattr(gs, "_ESCAPE_RE"))

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
        for word in ("wait --pid", "run NAME", "stop NAME", "kill-pid", "find --cmd-contains", "no escape", "ask Bruce"):
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
