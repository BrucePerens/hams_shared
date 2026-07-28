#!/usr/bin/env python3
import os
import sys
import fcntl
import psutil
import time
import signal
import subprocess

LOCK_FILE = "/tmp/run_headless_chrome.lock"

def reap_headless_chromes():
    my_uid = os.getuid()
    for p in psutil.process_iter(["pid", "name", "uids", "cmdline"]):
        try:
            cmdline = p.info.get("cmdline") or []
            is_headless = any("--headless" in str(arg) for arg in cmdline)
            if (
                p.info.get("name") in ("chrome", "chromium", "chromium-browser", "google-chrome")
                and p.info.get("uids")
                and p.info["uids"].real == my_uid
                and is_headless
            ):
                print(f"[*] Pre-killing stray headless chrome process {p.info['pid']}")
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, KeyError):
            pass

    time.sleep(0.5)
    for p in psutil.process_iter(["pid", "name", "uids", "cmdline"]):
        try:
            cmdline = p.info.get("cmdline") or []
            is_headless = any("--headless" in str(arg) for arg in cmdline)
            if (
                p.info.get("name") in ("chrome", "chromium", "chromium-browser", "google-chrome")
                and p.info.get("uids")
                and p.info["uids"].real == my_uid
                and is_headless
            ):
                p.kill()
                p.wait(timeout=1.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, psutil.TimeoutExpired, KeyError):
            pass

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run_headless_chrome.py [google-chrome args...]")
        sys.exit(1)

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Sequential execution only is required. Another headless browser is currently running.", file=sys.stderr)
        sys.exit(1)

    # 1. Kill any existing headless chromes before starting
    reap_headless_chromes()

    # 2. Launch google-chrome in a new process group
    cmd = ["google-chrome"] + sys.argv[1:]
    
    print(f"[*] Launching headless chrome: {' '.join(cmd)}")
    
    process = subprocess.Popen(cmd, preexec_fn=os.setsid)
    
    def terminate_process(signum, frame):
        print(f"\n[!] Received signal {signum}. Forcefully terminating headless chrome...")
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        reap_headless_chromes()
        sys.exit(1)
        
    signal.signal(signal.SIGTERM, terminate_process)
    signal.signal(signal.SIGINT, terminate_process)
    
    try:
        process.wait()
    finally:
        print(f"[*] Ensuring process group for PID {process.pid} is terminated...")
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        
        # Fallback to reap directly matching chromes
        reap_headless_chromes()
        
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

if __name__ == "__main__":
    main()
