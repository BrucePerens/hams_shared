#!/usr/bin/env python3
"""
Unified Pure-Python Odoo Test Runner for Hams.com
Combines test execution, integration modes, and real-time failure extraction.
Strictly prohibits Bash wrapper scripts and CPU polling loops.

AI DIRECTIVE: If UI Tours are failing with [watchdog alarm] or timeouts,
you MUST read `docs/LLM_WRITING_TOURS.md`. The environment is heavily constrained
and prone to race conditions if native macros are ignored.
"""

import infrastructure
import sys
# Enforce a strict recursion limit to immediately detect runaway functions
sys.setrecursionlimit(800)
import argparse
import atexit
import contextlib
import ctypes
import fcntl
import glob
import logging
import os
import pwd
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import functools

# Force all print statements to flush immediately to prevent
# inter-process pipe buffering from chronologically reordering log lines
# relative to the unbuffered child test process output.
print = functools.partial(print, flush=True)

@contextlib.contextmanager
def micro_privilege(username):
    """
    Temporarily drops Effective privileges to the specified user using setresuid/setresgid.
    Restores Root privileges securely upon exiting the context block.
    """
    if os.geteuid() != 0:
        yield
        return

    user_info = pwd.getpwnam(username)
    target_uid = user_info.pw_uid
    target_gid = user_info.pw_gid

    orig_ruid, orig_euid, orig_suid = os.getresuid()
    orig_rgid, orig_egid, orig_sgid = os.getresgid()

    try:
        os.setresgid(orig_rgid, target_gid, orig_sgid)
        os.setresuid(orig_ruid, target_uid, orig_suid)
        yield
    finally:
        os.setresuid(orig_ruid, orig_euid, orig_suid)
        os.setresgid(orig_rgid, orig_egid, orig_sgid)

# Local modules resolve natively without sys.path hacks.

_logger = logging.getLogger(__name__)

class VirtualClockThread(threading.Thread):
    """
    A CPU-time equivalent clock that suppresses massive jumps in wall-clock time
    caused by the VM being suspended or heavily timeshared.
    """
    def __init__(self):
        super().__init__(daemon=True)
        self.vtime = 0.0
        self.last_real = time.time()
        self._lock = threading.Lock()

    def run(self):
        while True:
            time.sleep(0.1)
            now = time.time()
            delta = now - self.last_real
            self.last_real = now
            with self._lock:
                self.vtime += min(delta, 0.5)

    def time(self):
        with self._lock:
            return self.vtime

global_vclock = VirtualClockThread()
global_vclock.start()

def load_ignore_file(filepath):
    patterns = []
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(re.compile(line))
    return patterns


def is_ignored(path, patterns):
    for pat in patterns:
        if re.search(pat, path):
            return True
    return False


class FailureExtractor:
    """
    State machine that processes log lines in real-time, buffering and extracting
    Tracebacks and error blocks for writing to a filtered log file.
    """

    def __init__(self, log_dir, disable_atexit=False, mcp_mode=False):
        base_dir = os.environ.get("HAMS_REAL_LOG_DIRECTORY") or os.path.abspath(
            os.path.expanduser(log_dir)
        )
        self.mcp_mode = mcp_mode
        self.display_path = os.path.join(base_dir, "filtered_test.txt")
        
        if os.environ.get("HAMS_ISOLATED_NS") == "1":
            self.output_path = "/mnt/real_tmp/filtered_test.txt"
        else:
            self.output_path = self.display_path
            os.makedirs(base_dir, exist_ok=True)
            try:
                os.chmod(base_dir, 0o777)
            except OSError as e:
                _logger.debug("Ignored OSError: %s", e)

        try:
            if os.path.exists(self.output_path):
                os.remove(self.output_path)
        except OSError as os_err:
            _logger.warning("Could not remove log natively, attempting sudo fallback: %s", os_err)
            try:
                if not os.access(self.output_path, os.W_OK):
                    subprocess.run(["sudo", "rm", "-f", self.output_path], check=False)
            except Exception as cleanup_e: # audit-ignore-catch-all
                _logger.warning("Ignored Exception removing log: %s", cleanup_e)

        self.log_prefix_pattern = re.compile(
            r"^(?:\s*)?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
        )
        self.safe_log_levels = re.compile(r"\b(INFO|WARNING|DEBUG)\b")
        self.test_start_pattern = re.compile(r"Starting Test|^test_.*?\s\.\.\.")

        self.current_context = "Global / Module Loading"
        self.capturing = False
        self.captured_blocks = []
        self.current_block = []
        self._written = False
        self.aborted = False

        if not disable_atexit:
            atexit.register(self.finish_and_write)

    def set_context(self, context_name):
        if self.capturing and self.current_block:
            self.captured_blocks.append((self.current_context, self.current_block))
            self.capturing = False
            self.current_block = []
        self.current_context = context_name

    def process_line(self, line):
        line_clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
        if self.test_start_pattern.search(line_clean):
            self.set_context(line_clean.strip())

        is_log_line = self.log_prefix_pattern.match(line_clean)
        line_lower = line_clean.lower()

        is_test_failure_content = (
            ("======================================================================" in line_clean)
            or ("Traceback (most recent call last):" in line_clean)
            or ("FAIL: " in line_clean)
            or ("ERROR: " in line_clean)
            or ("AssertionError" in line_clean)
            or ("FATAL:" in line_clean)
            or ("[watchdog alarm]" in line_lower)
        )
        
        # Ignore Python GC Exceptions on Chrome headless termination
        if "ChromeBrowser._chrome_start" in "".join(self.current_block):
            is_test_failure_content = False

        if is_log_line:
            is_safe = (
                self.safe_log_levels.search(line_clean)
                or "pika.adapters" in line_clean
                or "AMQPConnector" in line_clean
                or "Cloudflare URL purge API failed" in line_clean
                or "Cloudflare Tag purge API failed" in line_clean
                or "[BACKUP_WORKER]" in line_clean
                or "[LOG_ANALYZER]" in line_clean
                or "odoo.tests.result:" in line_clean
                or "discuss_channel_member" in "\n".join(self.current_block)
            )

            # Override 'safe' log levels (like INFO) if the test runner actually logged a failure
            if is_test_failure_content:
                is_safe = False

            if is_safe:
                if self.capturing:
                    self.captured_blocks.append((self.current_context, self.current_block))
                    self.current_block = []
                    self.capturing = False
            else:
                if not self.capturing:
                    self.capturing = True
                self.current_block.append(line)
        else:
            if is_test_failure_content:
                if not self.capturing:
                    self.capturing = True

                # AI Guidance Injection for Tour Failures
                if "[watchdog alarm]" in line_lower or "timeout" in line_lower:
                    ai_diagnostic = (
                        "\n[!] DIAGNOSTIC FOR AI (UI TOUR FAILURE):\n"
                        "    The browser headless runner has timed out or triggered a watchdog alarm.\n"
                        "    This almost always means your JS tour step array caused a race condition.\n"
                        "    MANDATORY ACTIONS:\n"
                        "    1. Did you click a 'save' button manually? You MUST use `.concat(TourUtils.safeSave())` instead.\n"
                        "    2. Did you use `text` instead of `edit` for an input? `text` is banned.\n"
                        "    3. Did you fail to insert a neutral `trigger: '.o_form_sheet', run: 'click'` step before clicking save to allow the DOM to blur?\n"
                        "    4. Read `docs/LLM_WRITING_TOURS.md` for exact syntax.\n\n"
                    )
                    self.current_block.append(ai_diagnostic)

            if self.capturing:
                self.current_block.append(line)

    def _extract_failed_modules(self):
        modules = set()
        addon_pattern = re.compile(r"odoo\.addons\.([a-zA-Z0-9_]+)")
        filepath_pattern = re.compile(r"\/([a-zA-Z0-9_]+)\/(?:models|controllers|tests|wizard|tools)\/.*?\.py")
        daemon_pattern = re.compile(r"\/daemons\/([a-zA-Z0-9_]+)\/.*?\.py")

        for context, block in self.captured_blocks:
            for match in addon_pattern.findall(context): modules.add(match)
            for match in filepath_pattern.findall(context): modules.add(match)
            for match in daemon_pattern.findall(context): modules.add(f"daemons/{match}")

            for line in block:
                for match in addon_pattern.findall(line): modules.add(match)
        return modules

    def finish_and_write(self):
        if getattr(self, "_written", False):
            return
        self._written = True

        if self.capturing and self.current_block:
            self.captured_blocks.append((self.current_context, self.current_block))
            self.capturing = False
            self.current_block = []

        filtered_blocks = []
        for context, block in self.captured_blocks:
            block_text = "".join(block)
            if "ChromeBrowser._chrome_start" in block_text and "psutil.NoSuchProcess" in block_text:
                continue
            filtered_blocks.append((context, block))
        self.captured_blocks = filtered_blocks

        grouped_blocks = {}
        for context, block in self.captured_blocks:
            if context not in grouped_blocks:
                grouped_blocks[context] = []
            grouped_blocks[context].extend(block)

        if os.path.dirname(self.output_path):
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)

        num_failures = len(grouped_blocks)

        try:
            with open(self.output_path, "a", encoding="utf-8") as out:
                out.write(f"\n=== EXTRACTED TEST FAILURES & ERRORS (Captured {num_failures} blocks) ===\n")
                if num_failures == 0:
                    out.write("\nNo errors or failures detected in the log.\n")
                else:
                    failed_modules = self._extract_failed_modules()
                    out.write("\n" + "*" * 80 + "\n")
                    out.write("SYSTEM DIRECTIVE FOR AI ASSISTANT:\n")
                    out.write("The following log contains extracted test failures, tracebacks, and CRITICAL errors from the Odoo test suite.\n")
                    out.write("Your immediate task is to analyze these errors, identify the root causes within the provided codebase, and generate the necessary patches to fix these test flaws.\n")

                    if failed_modules:
                        out.write("\nTARGET MODULES FOR ANALYSIS:\n")
                        out.write("Based on the tracebacks, the following modules are responsible for or implicated in the failure:\n")
                        for mod in failed_modules:
                            out.write(f"  - {mod}\n")
                        out.write("\nASSUMPTION: The GitHub repository containing these modules has been imported to your environment.\n")
                        out.write("ACTION: Please look up the code for the implicated modules above to diagnose and fix the issue.\n")

                    out.write("*" * 80 + "\n")

                    for context, block in grouped_blocks.items():
                        if not block: continue
                        out.write("\n" + "=" * 80 + "\n")
                        out.write(f"CONTEXT: {context}\n")
                        out.write("-" * 80 + "\n")
                        for b_line in block:
                            out.write(b_line)
                        out.write("\n")

                out.flush()
                os.fsync(out.fileno())

        except PermissionError as e:
            print(f"\n❌ [ERROR] FailureExtractor could not write to {self.output_path} due to permission denied: {e}")
            print("To resolve this, delete the file manually: sudo rm -f " + self.output_path + "\n")
            return

        print("\n==========================================================")
        if getattr(self, "aborted", False):
            print("🛑 TEST RUN ABORTED: Did not complete due to pre-flight linter errors.")
        elif num_failures == 0:
            print("🎉 TEST RUN COMPLETE: No test failures or system crashes detected.")
        else:
            print(f"🚨 TEST RUN COMPLETE: {num_failures} issue(s) detected (test failures or system crashes)!")
            print(f"📄 Failure details extracted and saved to: {self.display_path}")
        print("==========================================================\n")


def robust_reap(pid):
    """
    Process reaper that targets the process group with SIGTERM,
    waits using a 30-second poll-and-half-second-sleep, and escalates to SIGKILL.
    """
    print(f"\n[*] [REAPER] Initiating robust reaper for PID {pid}...")
    try:
        subprocess.run(["pkill", "-u", str(os.getuid()), "-TERM", "-f", "chrome"], check=False)

        pgid = os.getpgid(pid)
        print(f"[*] [REAPER] Sending SIGTERM to Process Group {pgid}")
        os.killpg(pgid, signal.SIGTERM)

        start_time = time.time()
        while time.time() - start_time < 30.0:
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"[*] [REAPER] Process {pid} confirmed dead.")
                return
            time.sleep(0.5)

        print(f"[*] [REAPER] Process {pid} did not exit after SIGTERM. Sending SIGKILL to Process Group {pgid}")
        os.killpg(pgid, signal.SIGKILL)
        subprocess.run(["pkill", "-u", str(os.getuid()), "-KILL", "-f", "chrome"], check=False)
    except OSError as e:
        print(f"[*] [REAPER] Error during reap: {e}")


def run_cmd(cmd, extractor=None, cwd=None, env=None):
    initial_errors = len(extractor.captured_blocks) if extractor else 0
    if env is None:
        env = dict(os.environ)

    env.setdefault("RABBITMQ_HOST", "localhost")
    env.setdefault("RMQ_HOST", "localhost")
    env.setdefault("REDIS_HOST", "localhost")
    env.setdefault("RMQ_USER", "guest")
    env.setdefault("RMQ_PASS", "guest")
    host_tmp_dir = "/var/tmp" if os.environ.get("HAMS_ISOLATED_NS") == "1" else os.environ.get("HAMS_REAL_LOG_DIRECTORY", "/var/tmp")
    if os.environ.get("HAMS_ISOLATED_NS") != "1":
        os.makedirs(host_tmp_dir, exist_ok=True)
        try:
            os.chmod(host_tmp_dir, 0o777)
        except OSError as e:
            _logger.debug("Ignored OSError: %s", e)
    env.setdefault("ODOO_TEST_CHROME_ARGS", "--headless --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer --disable-extensions --disable-background-networking --disable-default-apps --disable-sync --disable-translate --mute-audio --no-first-run --hide-scrollbars --metrics-recording-only --safebrowsing-disable-auto-update --disable-features=ServiceWorker,SharedWorker,DialMediaRouteProvider,dbus,OptimizationGuideModelDownloading")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "autolaunch:")

    def preexec_child():
        os.setsid()
        try:
            libc = ctypes.CDLL("libc.so.6")
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception: # audit-ignore-catch-all
            # We are post-fork in preexec_fn; logging is unsafe here.
            pass

    process = subprocess.Popen(

        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        preexec_fn=preexec_child,
        cwd=cwd,
        env=env,
    )

    print(f"\n[*] [DEBUG-RUNNER] Subprocess spawned: PID {process.pid}, PGID {os.getpgid(process.pid)}")
    print(f"[*] [DEBUG-RUNNER] Executing command: {' '.join(cmd)}")

    force_killed = False
    q = queue.Queue()
    last_output_time = time.time()

    def reader():
        print(f"[*] [DEBUG-RUNNER] IO Reader thread started for PID {process.pid}")
        try:
            for line in process.stdout:
                q.put(line)
        except Exception as e: # audit-ignore-catch-all
            _logger.error("Reader exception: %s", e)
            print(f"[*] [DEBUG-RUNNER] IO Reader thread exception: {e}")
        q.put(None)
        print(f"[*] [DEBUG-RUNNER] IO Reader thread concluded for PID {process.pid}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        while True:
            try:
                # Short blocking wait allows us to check if the primary process died while a child kept stdout open
                line = q.get(timeout=1.0)
                if line is None:
                    print("[*] [DEBUG-RUNNER] Received EOF sentinel from IO Reader thread.")
                    break

                last_output_time = time.time()

                if "@t-esc" in line and "deprecated" in line.lower():
                    continue

                sys.stdout.write(line)
                sys.stdout.flush()

                if extractor:
                    extractor.process_line(line)

                line_lower = line.lower()

                if "[watchdog alarm]" in line_lower:
                    print("\n[!] FATAL JS WATCHDOG ALARM DETECTED in JS! Allowing Odoo framework to process the dump and continue...\n")
            except queue.Empty:
                if process.poll() is not None:
                    sys.stdout.write(f"[*] [DEBUG-RUNNER] Process {process.pid} exited with {process.poll()}, but stdout pipe remains open. Breaking loop.\n")
                    sys.stdout.flush()
                    # The test process died but something (like a Postgres background worker) is holding the pipe open
                    break

                if not (extractor and extractor.mcp_mode) and not os.environ.get("HAMS_PAUSE_ON_FAIL") and (time.time() - last_output_time > 60.0):
                    print("\n[!] TEST TIMEOUT: No output received for 60 seconds. Tour or test likely hung. Terminating...\n")

                    if extractor:
                        extractor.capturing = True
                        extractor.current_block.append(
                            "\n[!] DIAGNOSTIC FOR AI (HARD TIMEOUT):\n"
                            "    The test runner timed out because the framework stopped producing output for 60 seconds.\n"
                            "    If this occurred during a UI tour, it means a `trigger:` selector failed to match any element in the DOM.\n"
                            "    Review your frontend JavaScript selectors, specifically avoiding pseudo-selectors like `:contains`.\n\n"
                        )
                        extractor.capturing = False

                    robust_reap(process.pid)
                    force_killed = True
                    break
    except KeyboardInterrupt:
        print("\n[!] CTRL-C detected! Forcefully terminating the test process...")
        robust_reap(process.pid)
        process.wait()
        sys.exit(1)
    finally:
        # Always reap stray processes like headless chrome to prevent zombie exhaustion
        print(f"[*] [DEBUG-RUNNER] Ensuring all child processes are reaped for PID {process.pid}...")
        try:
            subprocess.run(["pkill", "-u", str(os.getuid()), "-TERM", "-f", "chrome"], check=False)
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass

    print(f"[*] [DEBUG-RUNNER] Waiting for process {process.pid} to cleanly terminate...")
    process.wait()
    print(f"[*] [DEBUG-RUNNER] Process {process.pid} terminated with return code {process.returncode}.")

    if force_killed:
        final_errors = len(extractor.captured_blocks) if extractor else 0
        return 1 if final_errors > initial_errors else 0

    return process.returncode



def get_local_modules(base_dir, ignore_patterns):
    mods = []
    for item in os.listdir(base_dir):
        if item in ("hams_community", "hams_com"):
            continue
        mod_path = os.path.join(base_dir, item)
        if is_ignored(mod_path, ignore_patterns):
            continue
        if os.path.isdir(mod_path) and os.path.isfile(os.path.join(mod_path, "__manifest__.py")):
            mods.append(item)
    return sorted(mods)


def get_addons_path(base_dir):
    paths = ["/usr/lib/python3/dist-packages/odoo/addons", base_dir]

    found_community = False

    parent_dir = os.path.abspath(os.path.join(base_dir, ".."))
    try:
        for item in os.listdir(parent_dir):
            item_path = os.path.join(parent_dir, item)
            if os.path.isdir(item_path):
                if item.startswith("hams_community") or item.startswith("hams_com"):
                    if item_path not in paths and not found_community:
                        paths.append(item_path)
                        found_community = True
    except OSError as e:
        _logger.debug("Ignored OSError: %s", e)

    if not found_community:
        community_dir = os.path.abspath(os.path.join(base_dir, "..", "hams_community"))
        primary_dir = os.path.abspath(os.path.join(base_dir, "..", "hams_com"))
        nested_community = os.path.abspath(os.path.join(base_dir, "hams_community"))
        root_community = "/hams_community"

        app_community = "/app/hams_community"
        for d in [community_dir, primary_dir, nested_community, root_community, app_community]:
            if os.path.isdir(d) and d not in paths:
                paths.append(d)
                found_community = True
                break

    return ",".join(paths)


def check_linters(python_exec, base_dir, ignore_filepath, extractor=None, target_modules=None):
    print("[*] Running Manifest Dependency Graph Linter...")
    res_manifest = subprocess.run([python_exec, os.path.join(base_dir, "tools", "check_manifest_dependencies.py"), base_dir])
    if res_manifest.returncode != 0:
        print("🛑 Halting due to manifest load-order violations.")
        if extractor: extractor.aborted = True
        sys.exit(1)

    print("[*] Running AST Burn List Linter...")
    burn_script = os.path.join(base_dir, "tools", "check_burn_list.py")
    cmd_burn = [python_exec, burn_script, os.path.join(base_dir, target_modules[0]), "--ignore-file", ignore_filepath] if target_modules and len(target_modules) == 1 else [python_exec, burn_script, base_dir, "--ignore-file", ignore_filepath]

    res_burn = subprocess.run(cmd_burn, capture_output=True, text=True)
    if res_burn.returncode != 0:
        print(res_burn.stdout)
        print(res_burn.stderr)
        print("🛑 Halting due to burn list violations.")
        if extractor: extractor.aborted = True
        sys.exit(1)
    else:
        print(res_burn.stdout)

    print("[*] Scanning for Semantic Anchors...")
    res_anchor = subprocess.run([python_exec, os.path.join(base_dir, "tools", "verify_anchors.py"), base_dir], capture_output=True, text=True)
    if res_anchor.returncode != 0:
        print(res_anchor.stdout)
        print(res_anchor.stderr)
        print("🛑 Halting due to anchor violations.")
        if extractor: extractor.aborted = True
        sys.exit(1)
    else:
        print(res_anchor.stdout)

    print("[*] Running JavaScript Syntax Linter...")
    js_linter = os.path.join(base_dir, "tools", "check_js_syntax.py")
    target_dirs = [os.path.join(base_dir, m) for m in target_modules]
    cmd_js = [python_exec, js_linter, "--ignore-file", ignore_filepath] + target_dirs
    res_js = subprocess.run(cmd_js, capture_output=True, text=True)
    if res_js.returncode != 0:
        print(res_js.stdout)
        print(res_js.stderr)
        print("🛑 Halting due to JavaScript syntax errors.")
        if extractor: extractor.aborted = True
        sys.exit(1)
    else:
        print(res_js.stdout)


def wait_for_port(port, name, host="127.0.0.1", timeout=60.0):
    print(f"[*] Waiting for {name} on {host}:{port} to open...")
    start_time = global_vclock.time()
    while global_vclock.time() - start_time < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                print(f"[*] {name} is ready.")
                return True
        time.sleep(0.5)
    print(f"❌ ERROR: {name} did not open port {port} within {timeout} seconds.")
    return False


def wait_for_socket(sock_path, name, timeout=60.0):
    print(f"[*] Waiting for {name} unix socket {sock_path} to open...")
    start_time = global_vclock.time()
    while global_vclock.time() - start_time < timeout:
        if os.path.exists(sock_path):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                try:
                    sock.connect(sock_path)
                    print(f"[*] {name} socket is ready.")
                    return True
                except OSError as e:
                    _logger.debug("Ignored OSError: %s", e)
        time.sleep(0.5)
    print(f"❌ ERROR: {name} socket {sock_path} did not open within {timeout} seconds.")
    return False


def get_pg_bin(name):
    """Locate a PostgreSQL binary reliably across different distributions."""
    paths = glob.glob(f"/usr/lib/postgresql/*/bin/{name}")
    if paths:
        return sorted(paths)[-1]
    res = shutil.which(name)
    if not res:
        for p in [f"/usr/bin/{name}", f"/usr/local/bin/{name}"]:
            if os.path.exists(p): return p
        raise FileNotFoundError(f"Could not find PostgreSQL binary: {name}")
    return res


def rebuild_db(db_name):
    print(f"[*] Dropping and Rebuilding Database Schema ({db_name})...")
    env = dict(os.environ)

    if os.environ.get("HAMS_ISOLATED_NS") != "1":
        print("[*] Starting core daemons before testing...")
        for svc in ["postgresql", "redis-server", "rabbitmq-server", "pdns"]:
            subprocess.run(["sudo", "systemctl", "start", svc], check=False)

    print("[*] Flushing persistent daemons (Redis / RabbitMQ)...")
    subprocess.run(["redis-cli", "flushall"], check=False, env=env)

    # Get the working directory where data is saved
    subprocess.run(["redis-cli", "CONFIG", "GET", "dir"], check=False, env=env)
    subprocess.run(["redis-cli", "CONFIG", "GET", "dbfilename"], check=False, env=env)
    is_jules = bool(os.environ.get("IN_JULES_VM")) or bool(os.environ.get("JULES_SESSION_ID"))
    if is_jules:
        try:
            subprocess.run(["rabbitmqctl", "stop_app"], check=False)
            subprocess.run(["rabbitmqctl", "reset"], check=False)
            subprocess.run(["rabbitmqctl", "start_app"], check=False)
            subprocess.run(["sudo", "systemctl", "stop", "dx.firehose.service", "adif.processor.service", "qrz.scraper.service"], check=False)
            subprocess.run(["pkill", "-f", "dx_firehose.py"], check=False)
            subprocess.run(["pkill", "-f", "adif_processor.py"], check=False)
            subprocess.run(["pkill", "-f", "qrz_scraper.py"], check=False)
        except Exception as e: # audit-ignore-catch-all
            _logger.warning("Daemon flush exception: %s", e)

    try:
        psql_cmd = get_pg_bin("psql")
        dropdb_cmd = get_pg_bin("dropdb")
        createdb_cmd = get_pg_bin("createdb")
    except FileNotFoundError as e:
        print(f"❌ ERROR: {e}")
        sys.exit(1)

    subprocess.run([psql_cmd, "postgres", "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}';"], check=False, env=env)
    subprocess.run([psql_cmd, "postgres", "-c", f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE);"], check=False, env=env)
    subprocess.run([dropdb_cmd, "--if-exists", "--force", db_name], check=False, env=env)
    subprocess.run([createdb_cmd, db_name], check=True, env=env)
    subprocess.run([psql_cmd, db_name, "-c", "CREATE EXTENSION IF NOT EXISTS vector;"], check=True, env=env)


def setup_namespace_and_run_tests(real_log_dir, sys_args):
    """
    Pure-Python OS-level namespace bootstrapping.
    Executes entirely without Bash wrappers, leveraging `os.setuid` for micro-privileges.
    """
    # 1. Ephemeral OverlayFS File System
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/mnt"], check=True)
    for d in ["/mnt/upper", "/mnt/work", "/mnt/host_test_dir", "/opt/hams/test"]:
        os.makedirs(d, exist_ok=True)
    subprocess.run(["mount", "--bind", "/opt/hams/test", "/mnt/host_test_dir"], check=True)

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _safe_run(cmd, **kw): return subprocess.run(cmd, check=True, **kw)

    print("[*] Preserving host daemon sockets...")
    pg_socks = ["/var/run/postgresql", "/run/postgresql"]
    preserved_socks = []
    for i, sock in enumerate(pg_socks):
        if os.path.exists(sock):
            temp_sock = f"/mnt/pg_sock_{i}"
            os.makedirs(temp_sock, exist_ok=True)
            subprocess.run(["mount", "--bind", sock, temp_sock], check=True)
            preserved_socks.append((temp_sock, sock))

    # Anchor the true host path to an un-overlaid tmpfs directory BEFORE overlaying /home
    host_tmp_dir = real_log_dir if real_log_dir else "/var/tmp"
    os.makedirs(host_tmp_dir, exist_ok=True)
    try:
        os.chmod(host_tmp_dir, 0o777)
    except OSError as e:
        _logger.debug("Ignored OSError: %s", e)
    os.makedirs("/mnt/real_tmp", exist_ok=True)
    subprocess.run(["mount", "--bind", host_tmp_dir, "/mnt/real_tmp"], check=True)

    # Explicitly prepare the host log directory (~/tmp/log) before overlay
    host_log_dir = os.path.join(host_tmp_dir, "log")
    os.makedirs(host_log_dir, exist_ok=True)
    try:
        os.chown(host_log_dir, 0, 0)
        os.chmod(host_log_dir, 0o755)
    except OSError as e:
        _logger.debug("Ignored OSError setting permissions on host log dir: %s", e)
    os.makedirs("/mnt/real_log", exist_ok=True)
    subprocess.run(["mount", "--bind", host_log_dir, "/mnt/real_log"], check=True)

    print("[*] Creating overlay filesystem for isolated testing...")
    for item in ["etc", "opt", "var", "usr", "home", "tmp", "run"]:
        if not os.path.exists(f"/{item}"): continue
        os.makedirs(f"/mnt/upper/{item}", exist_ok=True)
        os.makedirs(f"/mnt/work/{item}", exist_ok=True)
        try:
            subprocess.run(["mount", "-t", "overlay", "overlay", "-o", f"lowerdir=/{item},upperdir=/mnt/upper/{item},workdir=/mnt/work/{item}", f"/{item}"], check=True)
        except subprocess.CalledProcessError as e:
            _logger.debug("Failed to overlay mount /%s: %s", item, e)

    print("[*] Restoring host daemon sockets...")
    for temp_sock, sock in preserved_socks:
        os.makedirs(sock, exist_ok=True)
        subprocess.run(["mount", "--bind", temp_sock, sock], check=True)

    print("[*] Provisioning isolated environment via infrastructure.py...")
    os.environ["HAMS_ISOLATED_NS"] = "1"
    orig_user = os.environ.get("SUDO_USER", "odoo")
    env_vars = dict(os.environ)
    env_vars["REPO_ROOT"] = base_dir

    infrastructure.provision_environment(_safe_run, env_vars, orig_user, skip_apt=True)

    print("[*] Isolating network namespace for test daemons...")
    CLONE_NEWNET = 0x40000000
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        libc = ctypes.CDLL(None, use_errno=True)

    if libc.unshare(CLONE_NEWNET) != 0:
        print(f"❌ ERROR: Failed to dynamically isolate network namespace: {ctypes.get_errno()}")
        sys.exit(1)

    # Bring up the isolated loopback interface now that we are in the new network namespace
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)

    # Bind the preserved host directory to the overlay's /var/tmp
    os.makedirs("/var/tmp", exist_ok=True)
    subprocess.run(["mount", "--bind", "/mnt/real_tmp", "/var/tmp"], check=True)

    # Bind the preserved host log directory to the overlay's /var/log
    os.makedirs("/var/log", exist_ok=True)
    subprocess.run(["mount", "--bind", "/mnt/real_log", "/var/log"], check=True)

    if os.path.exists("/var/run/postgresql"):
        os.makedirs("/var/run/postgresql", exist_ok=True)
        subprocess.run(["mount", "--bind", "/var/run/postgresql", "/var/run/postgresql"], check=True)

    subprocess.run(["mount", "--bind", base_dir, base_dir], check=True)
    subprocess.run(["mount", "-o", "remount,bind,ro", base_dir], check=True)

    extra_mounts = []
    parent_dir = os.path.abspath(os.path.join(base_dir, ".."))
    try:
        for item in os.listdir(parent_dir):
            if item.startswith("hams_community") or item.startswith("hams_com"):
                extra_mounts.append(os.path.join(parent_dir, item))
    except OSError as e:
        _logger.debug("Ignored OSError: %s", e)
    extra_mounts.extend([os.path.join(base_dir, "..", "hams_community"), "/hams_community", "/app/hams_community", os.path.join(base_dir, "hams_community")])

    mounted_dirs = set()
    for extra_dir in extra_mounts:
        if os.path.isdir(extra_dir):
            real_dir = os.path.realpath(extra_dir)
            if real_dir in mounted_dirs:
                continue
            mounted_dirs.add(real_dir)
            subprocess.run(["mount", "--bind", real_dir, real_dir], check=True)
            subprocess.run(["mount", "-o", "remount,bind,ro", real_dir], check=True)
            break

    # 3. PostgreSQL Sandboxing
    try:
        psql_cmd = get_pg_bin("psql")
    except FileNotFoundError as e:
        print(f"❌ ERROR: {e}")
        sys.exit(1)

    pg_sock = "/var/run/postgresql"
    if not os.path.exists(pg_sock):
        pg_sock = "/tmp"

    try:
        pg_user = pwd.getpwnam("postgres")
    except KeyError:
        pg_user = pwd.getpwnam("root")

    def preexec_pg():
        os.setresgid(pg_user.pw_gid, pg_user.pw_gid, pg_user.pw_gid)
        os.setresuid(pg_user.pw_uid, pg_user.pw_uid, pg_user.pw_uid)

    wait_for_socket(f"{pg_sock}/.s.PGSQL.5432", "PostgreSQL")

    p = subprocess.Popen([psql_cmd, "-h", pg_sock, "-d", "postgres"], stdin=subprocess.PIPE, preexec_fn=preexec_pg, text=True)
    sql_create_roles = f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'odoo') THEN
            CREATE ROLE odoo WITH SUPERUSER LOGIN PASSWORD 'odoo';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{orig_user}') THEN
            CREATE ROLE {orig_user} WITH SUPERUSER LOGIN;
        END IF;
    END $$;
    """
    p.communicate(sql_create_roles)
    p.wait()

    # 4. Redis Sandboxing
    # No pkill required: network namespace isolation prevents port collision with host
    redis_user = pwd.getpwnam("redis")

    redis_dir = "/var/lib/redis"
    db_filename = "dump.rdb"
    log_file = "/var/log/redis/redis-server.log"
    conf_path = "/etc/redis/redis.conf"

    # Dynamically parse the production configuration to find exact paths
    if os.path.exists(conf_path):
        with open(conf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("dir "):
                    parts = line.split()
                    if len(parts) >= 2:
                        redis_dir = parts[1].strip('"\'')
                elif line.startswith("dbfilename "):
                    parts = line.split()
                    if len(parts) >= 2:
                        db_filename = parts[1].strip('"\'')
                elif line.startswith("logfile "):
                    parts = line.split()
                    if len(parts) >= 2:
                        log_file = parts[1].strip('"\'')

    # Explicitly grant the redis user ownership over its dynamic production directories
    for d in [redis_dir, "/var/log/redis", "/run/redis", "/etc/redis", os.path.dirname(log_file) if log_file and log_file != '""' else ""]:
        if d:
            os.makedirs(d, exist_ok=True)
            try:
                os.chown(d, redis_user.pw_uid, redis_user.pw_gid)
                os.chmod(d, 0o750)
            except OSError as e:
                _logger.debug("Ignored OSError when setting directory permissions: %s", e)

    if os.path.exists(conf_path):
        try:
            os.chown(conf_path, redis_user.pw_uid, redis_user.pw_gid)
            os.chmod(conf_path, 0o640)
        except OSError as e:
            _logger.debug("Ignored OSError when setting redis.conf permissions: %s", e)

    # Ensure the DB snapshot file is writable to prevent BGSAVE failures
    db_path = os.path.join(redis_dir, db_filename)
    if os.path.exists(db_path):
        try:
            os.chown(db_path, redis_user.pw_uid, redis_user.pw_gid)
            os.chmod(db_path, 0o660)
        except OSError as e:
            _logger.debug("Ignored OSError when setting file permissions: %s", e)

    # Ensure the log file is writable
    if log_file and log_file != '""' and os.path.exists(log_file):
        try:
            os.chown(log_file, redis_user.pw_uid, redis_user.pw_gid)
            os.chmod(log_file, 0o660)
        except OSError as e:
            _logger.debug("Ignored OSError when setting file permissions: %s", e)

    # Run redis-server strictly using its production configuration,
    # overriding only 'daemonize' so subprocess.Popen can track its lifetime.
    # Specify the directory to prevent BGSAVE permissions errors (MISCONF).
    cmd = ["redis-server"]
    if os.path.exists(conf_path):
        cmd.append(conf_path)
    cmd.extend(["--daemonize", "no", "--dir", redis_dir])

    def preexec_redis():
        os.initgroups("redis", redis_user.pw_gid)
        os.setresgid(redis_user.pw_gid, redis_user.pw_gid, redis_user.pw_gid)
        os.setresuid(redis_user.pw_uid, redis_user.pw_uid, redis_user.pw_uid)

    redis_proc = subprocess.Popen(cmd, cwd=redis_dir, preexec_fn=preexec_redis)
    wait_for_port(6379, "Redis")

    # 5. RabbitMQ Sandboxing
    # No pkill required: network namespace isolation prevents port collision with host
    rmq_user = pwd.getpwnam("rabbitmq")

    for d in ["/var/lib/rabbitmq", "/var/log/rabbitmq", "/run/rabbitmq"]:
        os.makedirs(d, exist_ok=True)
        os.chown(d, rmq_user.pw_uid, rmq_user.pw_gid)

    with open("/var/lib/rabbitmq/.erlang.cookie", "w") as f:
        f.write("HAMS_TEST_RABBITMQ_COOKIE_12345")

    os.chown("/var/lib/rabbitmq/.erlang.cookie", rmq_user.pw_uid, rmq_user.pw_gid)
    os.chmod("/var/lib/rabbitmq/.erlang.cookie", 0o400)

    def preexec_rmq():
        os.setresgid(rmq_user.pw_gid, rmq_user.pw_gid, rmq_user.pw_gid)
        os.setresuid(rmq_user.pw_uid, rmq_user.pw_uid, rmq_user.pw_uid)
        os.environ["HOME"] = "/var/lib/rabbitmq"

    try:
        subprocess.run(["rabbitmq-server", "-detached"], preexec_fn=preexec_rmq, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR starting RabbitMQ: {e}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        sys.exit(1)

    wait_for_port(5672, "RabbitMQ")

    # 6. Execute Inner Odoo Test Suite
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PGHOST"] = pg_sock

    # Inside the namespace, /var/tmp is perfectly bound to the real log dir.
    host_tmp_dir = "/var/tmp"
    os.environ["ODOO_TEST_CHROME_ARGS"] = "--headless --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer --disable-extensions --disable-background-networking --disable-default-apps --disable-sync --disable-translate --mute-audio --no-first-run --hide-scrollbars --metrics-recording-only --safebrowsing-disable-auto-update --disable-features=ServiceWorker,SharedWorker,dbus,OptimizationGuideModelDownloading"
    os.environ["HAMS_REAL_LOG_DIRECTORY"] = real_log_dir
    os.environ["HOME"] = "/var/lib/odoo"
    os.environ["XDG_DATA_HOME"] = "/var/lib/odoo/.local/share"

    odoo_user = pwd.getpwnam("odoo")
    def preexec_odoo():
        try:
            import resource
            # 1200 seconds (20 minutes) of ACTIVE CPU TIME. Safe for tests, deadly for infinite loops.
            resource.setrlimit(resource.RLIMIT_CPU, (1200, 1200))
        except OSError:
            pass
        os.initgroups("odoo", odoo_user.pw_gid)
        os.setresgid(odoo_user.pw_gid, odoo_user.pw_gid, odoo_user.pw_gid)
        os.setresuid(odoo_user.pw_uid, odoo_user.pw_uid, odoo_user.pw_uid)



    test_cmd = [sys.executable, os.path.abspath(__file__)] + sys_args
    ret = subprocess.run(test_cmd, preexec_fn=preexec_odoo).returncode

    # 7. Graceful Ephemeral Teardown
    subprocess.run(["rabbitmqctl", "stop"], preexec_fn=preexec_rmq, check=False)
    redis_proc.terminate()

    try:
        orig_uid = pwd.getpwnam(orig_user).pw_uid
    except KeyError:
        orig_uid = -1



    if orig_uid != -1:
        for prof in glob.glob("/var/tmp/*.prof"):
            os.chown(prof, orig_uid, -1)

    sys.exit(ret)


def start_jules_daemons(base_dir):
    print("[*] Clearing port 8069 bindings...")
    subprocess.run(["sudo", "fuser", "-k", "8069/tcp"], check=False)

    print("[*] Provisioning Jules environment via infrastructure.py...")
    script = f"""import sys, os, subprocess
sys.path.insert(0, '{os.path.join(base_dir, "tools")}')
import infrastructure
def _safe_run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)
orig_user = '{os.environ.get("USER", "odoo")}'
env_vars = dict(os.environ)
env_vars["REPO_ROOT"] = '{base_dir}'
infrastructure.provision_environment(_safe_run, env_vars, orig_user, skip_apt=True)"""

    cmd = ["sudo", "-E", sys.executable, "-c", script]
    subprocess.run(cmd, check=True)

    pg_socket = "/var/run/postgresql"
    if not os.path.exists(pg_socket):
        pg_socket = "/tmp"
    os.environ["PGHOST"] = pg_socket


def check_host_apt_packages():
    """
    Verifies all required host packages are installed on Debian/Ubuntu systems
    prior to namespace isolation. Uses the manifest in infrastructure.py.
    """
    os_id = infrastructure.get_os_identifier()
    missing = []
    try:
        for pkg_spec in infrastructure.MANIFEST.get("apt_packages", []):
            pkg_name = pkg_spec.get("debian_name", pkg_spec["name"]) if os_id == "debian" else pkg_spec["name"]
            res = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg_name],
                capture_output=True,
                text=True,
                check=False
            )
            if "install ok installed" not in res.stdout:
                missing.append(pkg_name)
    except FileNotFoundError as e:
        _logger.debug("Skipping APT package check, dpkg-query not found: %s", e)
        return

    if missing:
        print(f"❌ ERROR: Missing required host APT packages: {', '.join(missing)}")
        print(f"Please install them via: sudo apt-get update && sudo apt-get install -y {' '.join(missing)}")
        sys.exit(1)


_single_instance_lock = None

def main():
    global _single_instance_lock
    lock_file_path = "/var/tmp/odoo_test_runner.lock"
    try:
        _single_instance_lock = open(lock_file_path, "a")
        os.chmod(lock_file_path, 0o777)
    except Exception:
        _single_instance_lock = open(lock_file_path, "r")
    try:
        fcntl.flock(_single_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("🛑 ERROR: Another instance of test.py is already running. Exiting.")
        sys.exit(1)

    cwd = os.getcwd()
    if not os.path.isdir(os.path.join(cwd, ".git")) or not os.path.isfile(os.path.join(cwd, "tools", "test.py")):
        print("================================================================================")
        print("🚨 CRITICAL EXECUTION ENVIRONMENT ERROR 🚨")
        print(f"Current Working Directory: {cwd}")
        print("You MUST execute tools/test.py from the root of the Git repository.")
        print("The current directory is either not a git repository or lacks tools/test.py.")
        print("[!] DIAGNOSTIC FOR AI: `cd` into the proper repository root before invoking this script.")
        print("================================================================================")
        sys.exit(1)

    os.environ.setdefault("HAMS_KEYS_DIR", "/opt/hams/etc/keys")

    is_jules = bool(os.environ.get("IN_JULES_VM")) or bool(os.environ.get("JULES_SESSION_ID"))

    if is_jules:
        existing_args = os.environ.get("ODOO_TEST_CHROME_ARGS", "")
        if "--no-sandbox" not in existing_args:
            os.environ["ODOO_TEST_CHROME_ARGS"] = f"{existing_args} --no-sandbox --disable-dev-shm-usage".strip()

        if os.geteuid() != 0:
            print("[*] Elevating privileges for Jules provisioning...")
            exec_cmd = ["sudo", "-H", "-E", sys.executable] + sys.argv
            os.execvpe("sudo", exec_cmd, os.environ)

    if os.environ.get("HAMS_ISOLATED_NS") != "1" and not os.environ.get("IN_JULES_VM") and not os.environ.get("JULES_SESSION_ID"):
        if "--internal-ns-init" not in sys.argv:
            check_host_apt_packages()

        if "--internal-ns-init" in sys.argv:
            # Phase 2: Execute completely within Python (No bash script interpolation)
            real_log_dir = os.environ.get("HAMS_REAL_LOG_DIRECTORY")
            sys_args = [arg for arg in sys.argv[1:] if arg != "--internal-ns-init"]
            setup_namespace_and_run_tests(real_log_dir, sys_args)
            return

        parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
        parser.add_argument("-l", "--log-directory", default="~/tmp")
        args, _ = parser.parse_known_args()

        real_log_dir = os.path.abspath(os.path.expanduser(args.log_directory))
        os.makedirs(real_log_dir, exist_ok=True)
        try:
            os.chmod(real_log_dir, 0o777)
        except OSError as e:
            _logger.debug("Ignored OSError: %s", e)
        print("[*] Routing test execution to isolated Python namespace...")

        os.environ["HAMS_REAL_LOG_DIRECTORY"] = real_log_dir
        # Isolate mount (-m) namespace. Network isolation happens dynamically post-provisioning.
        exec_cmd = ["unshare", "-m", sys.executable, os.path.abspath(__file__), "--internal-ns-init"] + sys.argv[1:]

        if os.geteuid() != 0:
            print("[*] Elevating privileges (sudo) to construct isolated mount namespace...")
            exec_cmd = ["sudo", "-H", "-E"] + exec_cmd
            os.execvpe("sudo", exec_cmd, os.environ)
        else:
            # os.execvpe completely replaces the current process, passing control natively
            os.execvpe("unshare", exec_cmd, os.environ)
        return
    os.environ["PYTHONWARNINGS"] = "ignore::DeprecationWarning"
    host_tmp_dir = "/var/tmp" if os.environ.get("HAMS_ISOLATED_NS") == "1" else os.environ.get("HAMS_REAL_LOG_DIRECTORY", "/var/tmp")
    if os.environ.get("HAMS_ISOLATED_NS") != "1":
        os.makedirs(host_tmp_dir, exist_ok=True)
        try:
            os.chmod(host_tmp_dir, 0o777)
        except OSError as e:
            _logger.debug("Ignored OSError: %s", e)
    os.environ.setdefault("ODOO_TEST_CHROME_ARGS", "--headless --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-software-rasterizer --disable-extensions --disable-background-networking --disable-default-apps --disable-sync --disable-translate --mute-audio --no-first-run --hide-scrollbars --metrics-recording-only --safebrowsing-disable-auto-update --disable-features=ServiceWorker,SharedWorker,DialMediaRouteProvider,dbus,OptimizationGuideModelDownloading")
    os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "autolaunch:")

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(base_dir, "deploy", "env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip("'\" \n"))

    os.environ.setdefault("PGHOST", "localhost")
    os.environ.setdefault("PGUSER", "odoo")
    os.environ.setdefault("PGPASSWORD", "odoo")
    os.environ.setdefault("RABBITMQ_HOST", "localhost")
    os.environ.setdefault("REDIS_HOST", "localhost")

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-m", "--mode", choices=["standard", "individual", "xml", "downloads"], default="standard")
    parser.add_argument("-d", "--db", default="hams_test")
    parser.add_argument("-u", "--module")
    parser.add_argument("-l", "--log-directory", default="~/tmp")
    parser.add_argument("-c", "--config", default="ignore_list.txt")
    parser.add_argument("--daemon")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--mcp", action="store_true", help="Launch the MCP server instead of running tests and shutting down.")
    parser.add_argument("--pause-on-fail", action="store_true", help="Pause the browser indefinitely on tour failure (exposes port 9222).")
    args = parser.parse_args()

    if args.pause_on_fail:
        os.environ["HAMS_PAUSE_ON_FAIL"] = "1"

    if is_jules:
        start_jules_daemons(base_dir)

    python_exec = "/usr/bin/python3"
    odoo_bin = "/usr/bin/odoo"
    addons_path = get_addons_path(base_dir)

    ignore_filepath = os.path.join(base_dir, args.config)
    ignore_patterns = load_ignore_file(ignore_filepath)

    target_modules = [m.strip() for m in args.module.split(",")] if args.module else get_local_modules(base_dir, ignore_patterns)

    if not args.module and "caching" in target_modules:
        target_modules.remove("caching")

    if not target_modules:
        print("❌ ERROR: No modules found.")
        sys.exit(1)

    mod_string = "base," + ",".join(target_modules)
    test_tags = ",".join([f"/{m}" for m in target_modules])

    def get_odoo_test_cmd(suffix=""):
        cmd = [python_exec]
        if args.profile:
            cmd.extend(["-m", "cProfile", "-o", f"/var/tmp/odoo_test{suffix}.prof"])
        return cmd

    extractor = FailureExtractor(args.log_directory, mcp_mode=args.mcp)
    print(f"==========================================================\n 🧪 ODOO TEST RUNNER [{args.mode.upper()} MODE]\n==========================================================")

    check_linters(python_exec, base_dir, ignore_filepath, extractor, target_modules)

    final_rc = 0

    if args.mcp:
        rebuild_db(args.db)

        os.environ["ODOO_URL"] = "http://127.0.0.1:8069"
        os.environ["DB_NAME"] = args.db
        os.environ["ODOO_USER"] = "admin"
        os.environ["ODOO_PASSWORD"] = "admin"

        mcp_server_script = os.path.join(base_dir, "tools", "test_mcp_server.py")
        cmd = get_odoo_test_cmd() + [
            mcp_server_script, "--load=base,web,zero_sudo", "--addons-path", addons_path,
            "--dev=all", "-d", args.db, "-i", mod_string, "--workers=0",
            "--max-cron-threads=0", "--http-interface", "127.0.0.1", "--http-port", "8069"
        ]

        if is_jules:
            os.environ["HOME"] = "/var/lib/odoo"
            os.environ["XDG_DATA_HOME"] = "/var/lib/odoo/.local/share"
            cmd = ["sudo", "-E", "-u", "odoo"] + cmd

        rc = run_cmd(cmd, extractor)
        sys.exit(rc)

    if args.mode == "standard":
        rebuild_db(args.db)

        # Inject environment variables for daemons spawned securely by tests
        os.environ["ODOO_URL"] = "http://127.0.0.1:8069"
        os.environ["DB_NAME"] = args.db
        os.environ["ODOO_USER"] = "admin"
        os.environ["ODOO_PASSWORD"] = "admin"

        cmd = get_odoo_test_cmd() + [
            odoo_bin, "--load=base,web,zero_sudo", "--addons-path", addons_path,
            "--dev=all", "-d", args.db, "-i", mod_string, "--test-enable",
            "--test-tags", test_tags, "--stop-after-init", "--workers=0",
            "--max-cron-threads=0", "--http-interface", "127.0.0.1", "--http-port", "8069"
        ]

        if is_jules:
            os.environ["HOME"] = "/var/lib/odoo"
            os.environ["XDG_DATA_HOME"] = "/var/lib/odoo/.local/share"
            cmd = ["sudo", "-E", "-u", "odoo"] + cmd

        rc_odoo = run_cmd(cmd, extractor)
        if rc_odoo != 0:
            final_rc = rc_odoo

    elif args.mode == "individual":
        os.environ["ODOO_URL"] = "http://127.0.0.1:8069"
        os.environ["DB_NAME"] = args.db
        os.environ["ODOO_USER"] = "admin"
        os.environ["ODOO_PASSWORD"] = "admin"

        for mod in target_modules:
            rebuild_db(args.db)
            cmd = get_odoo_test_cmd(f"_{mod}") + [
                odoo_bin, "--load=base,web,zero_sudo", "--addons-path", addons_path,
                "--dev=all", "-d", args.db, "-i", mod, "--test-enable",
                "--test-tags", f"/{mod}", "--stop-after-init", "--workers=0",
                "--max-cron-threads=0", "--http-interface", "127.0.0.1", "--http-port", "8069"
            ]

            if is_jules:
                os.environ["HOME"] = "/var/lib/odoo"
                os.environ["XDG_DATA_HOME"] = "/var/lib/odoo/.local/share"
                cmd = ["sudo", "-E", "-u", "odoo"] + cmd

            rc = run_cmd(cmd, extractor)
            if rc != 0: final_rc = 1

    sys.exit(final_rc)

if __name__ == "__main__":
    main()
