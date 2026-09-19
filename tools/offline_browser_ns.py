#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. Proprietary, Trade-Secret.
# This software is proprietary and confidential.
"""Offline-isolation harness for Odoo browser-tour tests.

Why this exists
---------------
`test.py` already unshares a network namespace (`CLONE_NEWNET`, loopback only)
so tests cannot reach the real internet. That isolates the *test run* from the
network; it does not let a test express the situation a real offline ham
operator is in, which is the opposite asymmetry:

    hams.com  -> unreachable
    the local relay on 127.0.0.1 -> still perfectly reachable

Because the werkzeug/Odoo test server and headless Chrome share that one
namespace, every tour's RPC and `fetch()` to "hams.com" succeeds via loopback
in every test that exists. `docs/proposals/OFFLINE_HAM_OPERATION.md` names two
candidate fixes; this module implements the second one (a nested namespace for
the browser), because the first (a port-level firewall rule in the shared
namespace) would also cut the test framework's own control channel.

The topology
------------
::

    test.py's own network namespace ("parent")          nested namespace ("child")
    +-------------------------------------------+       +-----------------------------+
    | Odoo/werkzeug test server 127.0.0.1:8075   |       | headless Chrome             |
    |                                           |       |   devtools 127.0.0.1:<P>    |
    | forwarders:                               |       |                             |
    |   127.0.0.1:<P> -> 10.201.7.2:<P> --------+--+ +--+-> 10.201.7.2:<P>  (CDP in)  |
    |   10.201.7.1:8075 -> 127.0.0.1:8075 <-----+--+ +--+-- 127.0.0.1:8075 (GATEWAY)  |
    |                                           | veth  |                             |
    |                                           |       | local-relay stand-in        |
    |                                           |       |   127.0.0.1:38911           |
    +-------------------------------------------+       +-----------------------------+

`<P>` is the browser's own ephemeral DevTools port, discovered per launch.

The child namespace has exactly one route out: the veth pair. Chrome reaches
"hams.com" (the Odoo test server, which it must see at `127.0.0.1:8075`,
because that is what `HttpCase.base_url()` and the session cookie's domain
both say) only through the GATEWAY forwarder inside the child namespace.

"Go offline" therefore means: **close the gateway's listening socket and tear
down its live connections.** Chrome then gets a prompt `ECONNREFUSED` for
anything aimed at hams.com -- a real, fast connection failure, not a firewall
black hole that would make every request hang until it times out -- while the
local-relay stand-in on the child namespace's own loopback keeps answering.
The DevTools channel runs over the veth in the other direction and is never
touched, so the test framework keeps full control of the browser throughout.

Privilege model (this is the part that forces the broker design)
----------------------------------------------------------------
`test.py` runs the actual Odoo test process as the unprivileged `odoo` user
(`preexec_odoo`). An unprivileged process cannot `setns()` into a network
namespace -- verified directly, not assumed::

    $ setpriv --reuid=odoo --regid=odoo --init-groups nsenter -t <pid> -n true
    nsenter: reassociate to namespaces failed: Operation not permitted

So the Odoo process can neither launch Chrome inside the child namespace nor
flip the network itself. Instead:

* the process that *holds* the child namespace is also a **broker**: it stays
  root inside that namespace and listens on a unix-domain control socket;
* unix sockets are filesystem objects and cross network namespaces freely, so
  the unprivileged test process can talk to the broker with no privilege at
  all;
* Odoo launches Chrome through `ODOO_BROWSER_BIN` (Odoo's own documented
  override, see `odoo/tests/common.py:_find_executable`), pointed at this
  file's `--shim` mode, which simply hands the argv to the broker and holds
  the connection open. When the shim dies (Odoo terminating "Chrome"), the
  broker kills the real browser.

Nothing here leaks out of a test run: the veth pair lives inside `test.py`'s
own already-unshared namespace, so it is invisible to the host and to any
other concurrent test run, and the child namespace evaporates when the broker
exits (which `PR_SET_PDEATHSIG` guarantees it does).

Usage from a test
-----------------
`test.py --offline-isolation -u <module>` sets these for the Odoo process:

* ``HAMS_OFFLINE_NS_CTL``   -- path of the broker's control socket
* ``HAMS_OFFLINE_RELAY_PORT`` -- the local-relay stand-in's port
* ``ODOO_BROWSER_BIN``      -- the shim

A test then speaks the line-delimited JSON protocol below (see
`OfflineNamespaceClient`, which tests may import or re-implement in ~15 lines)::

    {"cmd": "offline"}  -> {"ok": true, "online": false}
    {"cmd": "online"}   -> {"ok": true, "online": true}
    {"cmd": "status"}   -> {"ok": true, "online": ..., "relay_requests": N, ...}

Full walk-through, including how to add an offline tour to another module:
``hams_shared/docs/OFFLINE_BROWSER_NAMESPACE_TESTING.md``.
"""

import argparse
import ctypes
import errno
import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A /30 inside 10.0.0.0/8. Only ever configured inside test.py's own
# network namespace, so it cannot collide with anything on the host or in a
# concurrent test run -- each run gets its own namespace and its own veth.
PARENT_IP = "10.201.7.1"
CHILD_IP = "10.201.7.2"
VETH_PREFIX_LEN = 30
VETH_PARENT = "hamsoff0"
VETH_CHILD = "hamsoff1"

# Chrome's DevTools port stays ephemeral, and that is not a free choice:
# Chrome writes the `DevToolsActivePort` file Odoo polls for ONLY when it was
# asked for port 0. Verified directly rather than assumed -- with an explicit
# `--remote-debugging-port=39222` the browser logs "DevTools listening on
# ws://127.0.0.1:39222/..." and never writes the file at all, and Odoo's
# `_spawn_chrome` turns a missing file into `unittest.SkipTest`, i.e. a test
# that silently does not run. So the port is discovered per launch (see
# `_Broker.launch_chrome`) and the forwarders for it are built around it.
#
# Chrome is given a `ns_profile` SUBDIRECTORY of the profile directory Odoo
# created, so its own `DevToolsActivePort` lands where Odoo is not looking;
# the shim publishes the file into Odoo's directory only once both forwarders
# are actually up. Otherwise Odoo could read the port and connect in the
# window before anything was listening on the parent side.
NS_PROFILE_SUBDIR = "ns_profile"

# Must match test.py's own `--http-port`.
ODOO_HTTP_PORT = 8075

# Matches ham_shack's existing fake-relay convention
# (tests/test_shack_sw_behavior_tour.py's FAKE_RELAY_PORT), so a tour's JS can
# keep using one literal. This one lives on the CHILD namespace's loopback, so
# it does not collide with that test's own parent-namespace server.
RELAY_PORT = 38911

CONNECT_TIMEOUT = 5


def _prctl_die_with_parent():
    """PR_SET_PDEATHSIG(SIGKILL): never outlive test.py.

    A broker that survived its parent would keep a network namespace and a
    veth end alive for nothing. Matches test.py's own `preexec_child`.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def _run_ip(*args, check=True):
    return subprocess.run(["ip", *args], check=check, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# TCP forwarding
# ---------------------------------------------------------------------------


class TcpForwarder:
    """A plain TCP relay: accept on (bind_host, bind_port), connect to (dst).

    Deliberately protocol-agnostic. The DevTools channel is an HTTP connection
    that upgrades to a WebSocket; a byte forwarder carries that unchanged,
    where an HTTP-aware proxy would have to be taught about the upgrade.

    `stop()` closes the listening socket AND every live connection, which is
    what makes "go offline" a fast, honest connection failure instead of a
    hang -- the distinction that matters for a test with a timeout.
    """

    def __init__(self, bind_host, bind_port, dst_host, dst_port, name):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.dst_host = dst_host
        self.dst_port = dst_port
        self.name = name
        self._listener = None
        self._accept_thread = None
        self._live = set()
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self):
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind_host, self.bind_port))
            listener.listen(64)
            self._listener = listener
            self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"fwd-{self.name}", daemon=True
        )
        self._accept_thread.start()

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            listener, self._listener = self._listener, None
            live, self._live = list(self._live), set()
        try:
            listener.close()
        except OSError:
            pass
        for sock in live:
            # shutdown() before close() so the peer sees the reset now rather
            # than whenever the last reference happens to be dropped.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _accept_loop(self):
        while True:
            with self._lock:
                listener = self._listener
                if not self._running or listener is None:
                    return
            try:
                client, _addr = listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(client,), name=f"fwd-{self.name}-conn", daemon=True
            ).start()

    def _handle(self, client):
        upstream = None
        try:
            upstream = socket.create_connection(
                (self.dst_host, self.dst_port), timeout=CONNECT_TIMEOUT
            )
            upstream.settimeout(None)
            client.settimeout(None)
        except OSError:
            try:
                client.close()
            except OSError:
                pass
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            return
        with self._lock:
            if not self._running:
                # Raced with stop(): do not register, just drop the pair.
                for sock in (client, upstream):
                    try:
                        sock.close()
                    except OSError:
                        pass
                return
            self._live.add(client)
            self._live.add(upstream)
        t1 = threading.Thread(target=self._pump, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=self._pump, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        with self._lock:
            self._live.discard(client)
            self._live.discard(upstream)

    @staticmethod
    def _pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# The local-relay stand-in
# ---------------------------------------------------------------------------

# Mirrors the real hams_local_relay endpoints shack_sw.js talks to
# (daemons/hams_local_relay/src/local_distribution.rs's DOC_MANIFEST). A
# stand-in rather than the real Rust binary, deliberately: what this harness
# has to prove is the *reachability asymmetry*, and a real relay would drag a
# cargo build and a hamlib/audio environment into an Odoo tour run.
RELAY_MANUAL_BODY = b"<html>OFFLINE_NS_LOCAL_RELAY_MANUAL</html>"
RELAY_PROBE_BODY = b"OFFLINE_NS_LOCAL_RELAY_REACHABLE"


class _RelayStubHandler(BaseHTTPRequestHandler):
    # Real CORS headers: the page is on the Odoo origin and the relay is a
    # different origin, exactly as in production, where the relay's own axum
    # CorsLayer allows any origin. Without this the service worker's
    # cross-origin fetch would be blocked by the browser and the test would be
    # measuring CORS, not offline behaviour.
    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler's own naming)
        self.server.record_request(self.path)
        if self.path == "/__offline_probe":
            self._send(RELAY_PROBE_BODY, "text/plain")
            return
        if self.path == "/api/local_content":
            body = json.dumps(
                {
                    "items": [
                        {
                            "id": "web_shack_manual",
                            "path": "/api/local_content/doc/web_shack_manual",
                            "filename": "Ham Radio Web Shack Manual",
                            "content_type": "text/html",
                            "size_bytes": len(RELAY_MANUAL_BODY),
                            "sha256": "offlinensfake",
                        }
                    ]
                }
            ).encode()
            self._send(body, "application/json")
            return
        if self.path == "/api/local_content/doc/web_shack_manual":
            self._send(RELAY_MANUAL_BODY, "text/html")
            return
        self.send_response(404)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def log_message(self, log_format, *args):
        pass  # real failures surface through the tour's own assertions


class _RelayStubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests_seen = []
        self._req_lock = threading.Lock()

    def record_request(self, path):
        with self._req_lock:
            self.requests_seen.append(path)

    def snapshot(self):
        with self._req_lock:
            return list(self.requests_seen)


# ---------------------------------------------------------------------------
# The broker (runs inside the nested namespace, as root)
# ---------------------------------------------------------------------------


class _Broker:
    def __init__(self, config):
        self.config = config
        self.ctl_path = config["ctl_sock"]
        self.chrome_binary = config["chrome_binary"]
        self.odoo_user = config.get("odoo_user", "odoo")
        self.gateway = TcpForwarder(
            "127.0.0.1", ODOO_HTTP_PORT, PARENT_IP, ODOO_HTTP_PORT, "gateway"
        )
        self.relay = None
        self._chromes = []
        self._chrome_lock = threading.Lock()

    # -- namespace plumbing -------------------------------------------------

    def configure_namespace(self):
        _run_ip("link", "set", "lo", "up")
        # test.py moves our end of the veth in after we exist; wait for it.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            probe = _run_ip("link", "show", VETH_CHILD, check=False)
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(f"{VETH_CHILD} never appeared in the nested namespace")
        _run_ip("addr", "add", f"{CHILD_IP}/{VETH_PREFIX_LEN}", "dev", VETH_CHILD)
        _run_ip("link", "set", VETH_CHILD, "up")
        _run_ip("route", "add", "default", "via", PARENT_IP)

    # -- browser ------------------------------------------------------------

    def _sanitize_chrome_argv(self, argv):
        """Build the real Chrome command line from what Odoo asked for.

        The binary is always this harness's own configured Chrome, never
        anything named by the caller -- the control socket is reachable by the
        unprivileged test user, and a broker that ran an arbitrary argv[0] as
        root would be a local privilege escalation rather than a test fixture.
        Odoo's own switches are passed through; only the DevTools address and
        the profile directory are rewritten (see NS_PROFILE_SUBDIR).

        Returns (cmd, odoo_profile_dir, ns_profile_dir).
        """
        out = [self.chrome_binary]
        odoo_profile = None
        for arg in argv[1:]:
            if not isinstance(arg, str):
                raise ValueError("non-string argument")
            if arg.startswith("--remote-debugging-address"):
                continue
            if arg.startswith("--user-data-dir="):
                odoo_profile = arg.split("=", 1)[1]
                continue
            out.append(arg)
        if not odoo_profile:
            raise ValueError("Odoo did not pass --user-data-dir; cannot place the browser profile")
        ns_profile = os.path.join(odoo_profile, NS_PROFILE_SUBDIR)
        out.insert(1, f"--user-data-dir={ns_profile}")
        out.insert(1, "--remote-debugging-address=127.0.0.1")
        return out, odoo_profile, ns_profile

    def launch_chrome(self, argv, env_overrides):
        cmd, _odoo_profile, ns_profile = self._sanitize_chrome_argv(argv)
        user = pwd.getpwnam(self.odoo_user)
        os.makedirs(ns_profile, exist_ok=True)
        os.chown(ns_profile, user.pw_uid, user.pw_gid)
        port_file = os.path.join(ns_profile, "DevToolsActivePort")
        if os.path.exists(port_file):
            os.unlink(port_file)
        env = dict(os.environ)
        # The broker is root, so its own HOME is root's. Chrome is about to run
        # as `odoo`, and /usr/bin/google-chrome is a wrapper script that writes
        # into $HOME before it ever execs the browser -- inheriting root's HOME
        # makes it die on a permission error with nothing but a missing
        # DevToolsActivePort to show for it. Set it from the account we are
        # actually dropping to; the shim's own forwarded HOME (below) wins if
        # the caller had a more specific one.
        env["HOME"] = user.pw_dir
        env["XDG_DATA_HOME"] = os.path.join(user.pw_dir, ".local", "share")
        env.update({k: str(v) for k, v in (env_overrides or {}).items()})

        def preexec():
            os.initgroups(self.odoo_user, user.pw_gid)
            os.setresgid(user.pw_gid, user.pw_gid, user.pw_gid)
            os.setresuid(user.pw_uid, user.pw_uid, user.pw_uid)
            try:
                libc = ctypes.CDLL("libc.so.6", use_errno=True)
                libc.prctl(1, signal.SIGKILL, 0, 0, 0)
            except OSError:
                pass

        # Keep the browser's own stderr: when a headless Chrome refuses to
        # start inside the nested namespace, this file is the only place that
        # says why -- Odoo sees nothing but a missing DevToolsActivePort and
        # turns that into a SkipTest.
        log_path = self.config.get("log_file")
        log_handle = None
        if log_path:
            log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115 - lives with the browser
        proc = subprocess.Popen(  # noqa: PLW1509 - preexec_fn is how we drop to `odoo`
            cmd,
            stdout=log_handle or subprocess.DEVNULL,
            stderr=log_handle or subprocess.DEVNULL,
            env=env,
            preexec_fn=preexec,
        )
        if log_handle is not None:
            log_handle.close()
        with self._chrome_lock:
            self._chromes.append(proc)

        # Wait for the browser to publish its own ephemeral DevTools port, then
        # make that port reachable from the parent namespace. Odoo has ten
        # seconds (BROWSER_WAIT) from the moment it spawns the shim, and it
        # only starts counting once the shim returns from its own launch call,
        # so this wait is inside that budget.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"the browser exited with code {proc.returncode} before opening its "
                    f"DevTools port (see {log_path})"
                )
            if os.path.exists(port_file) and os.path.getsize(port_file) > 5:
                break
            time.sleep(0.05)
        else:
            self.kill_chrome(proc)
            raise RuntimeError(f"the browser never wrote {port_file} (see {log_path})")
        with open(port_file, encoding="utf-8") as handle:
            port_file_body = handle.read()
        devtools_port = int(port_file_body.splitlines()[0])

        cdp_in = TcpForwarder(
            CHILD_IP, devtools_port, "127.0.0.1", devtools_port, f"cdp-in-{devtools_port}"
        )
        cdp_in.start()
        return proc, devtools_port, port_file_body, cdp_in

    def kill_chrome(self, proc):
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    # -- control socket -----------------------------------------------------

    def serve(self):
        self.configure_namespace()
        self.relay = _RelayStubServer(("127.0.0.1", RELAY_PORT), _RelayStubHandler)
        threading.Thread(target=self.relay.serve_forever, daemon=True, name="relay-stub").start()
        self.gateway.start()

        if os.path.exists(self.ctl_path):
            os.unlink(self.ctl_path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.ctl_path)
        # The test process runs as an unprivileged user in a different network
        # namespace; the socket is the only channel it has, and it lives in an
        # ephemeral, per-run test namespace.
        os.chmod(self.ctl_path, 0o666)
        srv.listen(16)

        ready = self.config.get("ready_file")
        if ready:
            with open(ready, "w", encoding="utf-8") as handle:
                handle.write("ready\n")
            os.chmod(ready, 0o666)

        while True:
            try:
                conn, _ = srv.accept()
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            threading.Thread(
                target=self._handle_control, args=(conn,), daemon=True, name="ctl"
            ).start()

    def _handle_control(self, conn):
        launched = None
        launched_forwarder = None
        try:
            reader = conn.makefile("r", encoding="utf-8")
            for line in reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    self._reply(conn, {"ok": False, "error": "bad json"})
                    continue
                cmd = request.get("cmd")
                if cmd == "offline":
                    self.gateway.stop()
                    self._reply(conn, {"ok": True, "online": False})
                elif cmd == "online":
                    self.gateway.start()
                    self._reply(conn, {"ok": True, "online": True})
                elif cmd == "status":
                    self._reply(
                        conn,
                        {
                            "ok": True,
                            "online": self.gateway.running,
                            "relay_requests": self.relay.snapshot() if self.relay else [],
                            "chrome_running": any(
                                p.poll() is None for p in list(self._chromes)
                            ),
                        },
                    )
                elif cmd == "launch":
                    try:
                        (
                            launched,
                            devtools_port,
                            port_file_body,
                            launched_forwarder,
                        ) = self.launch_chrome(request.get("argv") or [], request.get("env"))
                    except Exception as exc:  # noqa: BLE001 - reported to the caller
                        self._reply(conn, {"ok": False, "error": str(exc)})
                        continue
                    self._reply(
                        conn,
                        {
                            "ok": True,
                            "pid": launched.pid,
                            "devtools_port": devtools_port,
                            "devtools_active_port_body": port_file_body,
                        },
                    )
                    # Hold this connection for the browser's lifetime: the
                    # shim keeps it open, so EOF here means Odoo tore its
                    # "Chrome" process down and the real browser must follow.
                    code = self._wait_for_shim_eof(conn, reader, launched)
                    self._reply(conn, {"event": "exited", "code": code})
                    launched = None
                    return
                else:
                    self._reply(conn, {"ok": False, "error": f"unknown cmd {cmd!r}"})
        except OSError:
            pass
        finally:
            if launched is not None:
                self.kill_chrome(launched)
            if launched_forwarder is not None:
                launched_forwarder.stop()
            with self._chrome_lock:
                self._chromes = [p for p in self._chromes if p.poll() is None]
            try:
                conn.close()
            except OSError:
                pass

    def _wait_for_shim_eof(self, conn, reader, proc):
        """Return once either the browser exits or the shim goes away."""
        done = threading.Event()

        def watch_eof():
            try:
                reader.read()  # blocks until the shim closes the socket
            except OSError:
                pass
            done.set()

        threading.Thread(target=watch_eof, daemon=True, name="shim-eof").start()
        while not done.is_set():
            code = proc.poll()
            if code is not None:
                return code
            time.sleep(0.2)
        self.kill_chrome(proc)
        return proc.poll() if proc.poll() is not None else -1

    @staticmethod
    def _reply(conn, payload):
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Client helper (importable by tests, and used by --shim)
# ---------------------------------------------------------------------------


class OfflineNamespaceClient:
    """Minimal client for the broker's control socket.

    Deliberately dependency-free and tiny so a test in any module can either
    import it (when `hams_shared/tools` is importable) or paste the equivalent
    fifteen lines into its own file.
    """

    def __init__(self, ctl_path=None):
        self.ctl_path = ctl_path or os.environ.get("HAMS_OFFLINE_NS_CTL")
        if not self.ctl_path:
            raise RuntimeError("HAMS_OFFLINE_NS_CTL is not set -- not an offline-isolation run")

    def _request(self, payload):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        try:
            sock.connect(self.ctl_path)
            sock.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        if not data:
            raise RuntimeError("offline-namespace broker closed the connection with no reply")
        return json.loads(data.decode().splitlines()[0])

    def go_offline(self):
        return self._request({"cmd": "offline"})

    def go_online(self):
        return self._request({"cmd": "online"})

    def status(self):
        return self._request({"cmd": "status"})


# ---------------------------------------------------------------------------
# Parent-side forwarders
# ---------------------------------------------------------------------------


def _run_parent_forwarders(ready_file=None):
    _prctl_die_with_parent()
    # The Odoo test server binds 127.0.0.1 only (test.py passes
    # --http-interface 127.0.0.1); this is what makes it reachable from the
    # veth at all, and it is the parent end of the "go offline" path. The
    # DevTools direction is not here: its port is per-browser and ephemeral,
    # so the shim stands that forwarder up itself, in its own process.
    http_in = TcpForwarder(PARENT_IP, ODOO_HTTP_PORT, "127.0.0.1", ODOO_HTTP_PORT, "http-in")
    http_in.start()
    if ready_file:
        with open(ready_file, "w", encoding="utf-8") as handle:
            handle.write("ready\n")
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------------------
# The ODOO_BROWSER_BIN shim
# ---------------------------------------------------------------------------


def _run_shim(argv):
    """Stand in for the Chrome binary, from Odoo's point of view.

    Odoo `Popen`s this, then reads `<user-data-dir>/DevToolsActivePort` and
    talks to `127.0.0.1:<that port>`. The real browser is started by the
    broker inside the nested namespace and writes that file itself (the
    profile directory is on a filesystem both namespaces share), so Odoo's
    own discovery works unchanged.
    """
    ctl_path = os.environ.get("HAMS_OFFLINE_NS_CTL")
    if not ctl_path:
        print("HAMS_OFFLINE_NS_CTL is not set", file=sys.stderr)
        return 2
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(ctl_path)

    # Installed BEFORE the launch request, not after: Odoo can terminate its
    # "Chrome" at any moment, and a SIGTERM arriving in the window between
    # sending the request and installing the handler would kill this process
    # with the default disposition, leaving the real browser running until
    # PR_SET_PDEATHSIG eventually caught it. Forwarding termination to the
    # broker is just closing the socket; the broker treats EOF as "kill the
    # browser".
    def _on_signal(_sig, _frame):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    payload = {
        "cmd": "launch",
        "argv": argv,
        # Odoo points TMPDIR at the browser profile directory; Chrome writes
        # crash dumps and similar there and the test harness cleans it up.
        "env": {
            k: v
            for k, v in os.environ.items()
            if k in ("TMPDIR", "DISPLAY", "LANG", "HOME", "XDG_DATA_HOME")
        },
    }
    sock.sendall((json.dumps(payload) + "\n").encode())

    reader = sock.makefile("r", encoding="utf-8")
    first = reader.readline()
    if not first:
        return 3
    response = json.loads(first)
    if not response.get("ok"):
        print(f"offline-ns broker refused to launch the browser: {response}", file=sys.stderr)
        return 4

    # The browser is up in the other namespace on an ephemeral DevTools port.
    # Make that port answer on this namespace's loopback, because that is where
    # Odoo will look (`http://127.0.0.1:<port>/json/...`), and only THEN
    # publish the DevToolsActivePort file Odoo is polling for -- writing it
    # first would let Odoo connect to a port nothing was listening on yet.
    devtools_port = response["devtools_port"]
    cdp_out = TcpForwarder(
        "127.0.0.1", devtools_port, CHILD_IP, devtools_port, f"cdp-out-{devtools_port}"
    )
    cdp_out.start()

    odoo_profile = None
    for arg in argv[1:]:
        if arg.startswith("--user-data-dir="):
            odoo_profile = arg.split("=", 1)[1]
    if odoo_profile:
        ns_profile = os.path.join(odoo_profile, NS_PROFILE_SUBDIR)
        # Odoo's ChromeBrowser.read_log() reads <user-data-dir>/chrome_debug.log
        # and saves it as a test artefact on failure; the real browser writes
        # that into its own profile directory, so point Odoo at it.
        link = os.path.join(odoo_profile, "chrome_debug.log")
        if not os.path.exists(link):
            try:
                os.symlink(os.path.join(ns_profile, "chrome_debug.log"), link)
            except OSError:
                pass
        with open(os.path.join(odoo_profile, "DevToolsActivePort"), "w", encoding="utf-8") as fh:
            fh.write(response.get("devtools_active_port_body") or f"{devtools_port}\n")

    for line in reader:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "exited":
            code = event.get("code")
            return code if isinstance(code, int) and code >= 0 else 0
    return 0


# ---------------------------------------------------------------------------
# Setup, called from test.py
# ---------------------------------------------------------------------------


def _wait_for_file(path, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {what} ({path})")


def setup(work_dir, chrome_binary=None, odoo_user="odoo"):
    """Stand up the nested browser namespace. Returns (env_additions, cleanup).

    Must be called from a root process that is already inside test.py's own
    unshared network namespace, before the Odoo test process is started. The
    returned dict is meant to be merged into that process's environment.
    """
    os.makedirs(work_dir, exist_ok=True)
    try:
        os.chmod(work_dir, 0o777)
    except OSError:
        pass
    ctl_sock = os.path.join(work_dir, "offline_ns_ctl.sock")
    ready_file = os.path.join(work_dir, "offline_ns_ready")
    parent_ready = os.path.join(work_dir, "offline_ns_parent_ready")
    config_path = os.path.join(work_dir, "offline_ns_config.json")
    shim_path = os.path.join(work_dir, "hams_offline_chrome")
    for stale in (ctl_sock, ready_file, parent_ready):
        if os.path.exists(stale):
            os.unlink(stale)

    if chrome_binary is None:
        for candidate in (
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome-stable",
        ):
            if os.path.exists(candidate):
                chrome_binary = candidate
                break
    if not chrome_binary:
        raise RuntimeError("no Chrome/Chromium binary found for the offline browser namespace")

    log_file = os.path.join(work_dir, "offline_ns_chrome.log")
    config = {
        "ctl_sock": ctl_sock,
        "ready_file": ready_file,
        "chrome_binary": chrome_binary,
        "odoo_user": odoo_user,
        "log_file": log_file,
    }
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle)

    me = os.path.abspath(__file__)
    procs = []

    # 1. The namespace holder / broker. `unshare -n` with no -f execs straight
    #    into python, so the pid we get back IS the process holding the new
    #    namespace -- which is what `ip link set ... netns <pid>` needs.
    broker = subprocess.Popen(  # noqa: PLW1509
        ["unshare", "-n", sys.executable, me, "--broker", "--config", config_path],
        preexec_fn=_prctl_die_with_parent,
    )
    procs.append(broker)

    # 1a. Wait until the broker is really in a namespace of its own. `unshare
    #     -n` unshares and then execs python, and until that has happened the
    #     broker's pid still names THIS namespace -- moving the veth end there
    #     would put both ends in the parent, leaving the broker waiting for an
    #     interface that never arrives. Compare the namespace inode rather than
    #     sleeping and hoping.
    my_netns = os.readlink("/proc/self/ns/net")
    deadline = time.monotonic() + 30
    while True:
        if broker.poll() is not None:
            raise RuntimeError(
                f"the offline-namespace broker exited immediately (code {broker.returncode})"
            )
        try:
            if os.readlink(f"/proc/{broker.pid}/ns/net") != my_netns:
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError("the offline-namespace broker never entered its own network namespace")
        time.sleep(0.02)

    # 2. Hand it one end of a veth pair. Both ends live inside test.py's own
    #    network namespace, so no other test run on this box can see either.
    subprocess.run(["ip", "link", "del", VETH_PARENT], check=False, capture_output=True)
    subprocess.run(
        ["ip", "link", "add", VETH_PARENT, "type", "veth", "peer", "name", VETH_CHILD],
        check=True,
    )
    subprocess.run(["ip", "link", "set", VETH_CHILD, "netns", str(broker.pid)], check=True)
    subprocess.run(
        ["ip", "addr", "add", f"{PARENT_IP}/{VETH_PREFIX_LEN}", "dev", VETH_PARENT], check=True
    )
    subprocess.run(["ip", "link", "set", VETH_PARENT, "up"], check=True)

    # 3. Parent-side forwarders (CDP out, Odoo HTTP in).
    parent = subprocess.Popen(  # noqa: PLW1509
        [sys.executable, me, "--parent-forwarders", "--ready-file", parent_ready],
        preexec_fn=_prctl_die_with_parent,
    )
    procs.append(parent)

    _wait_for_file(ready_file, 60, "the offline-namespace broker")
    _wait_for_file(parent_ready, 60, "the parent-side forwarders")

    # 4. The ODOO_BROWSER_BIN shim, which Odoo will run as the `odoo` user.
    with open(shim_path, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/bin/sh\n"
            "# Generated by hams_shared/tools/offline_browser_ns.py -- stands in for the\n"
            "# Chrome binary so the browser can be launched inside the nested namespace.\n"
            f'exec {sys.executable} {me} --shim "$@"\n'
        )
    os.chmod(shim_path, 0o755)

    env_additions = {
        "ODOO_BROWSER_BIN": shim_path,
        "HAMS_OFFLINE_NS_CTL": ctl_sock,
        "HAMS_OFFLINE_RELAY_PORT": str(RELAY_PORT),
    }

    def cleanup():
        for proc in procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        proc.kill()
                    except OSError:
                        pass
        subprocess.run(["ip", "link", "del", VETH_PARENT], check=False, capture_output=True)

    return env_additions, cleanup


def main():
    # --shim is handled before argparse ever sees the command line: everything
    # after it is Chrome's own switch list, and argparse's prefix matching
    # would happily read a Chrome flag as an abbreviation of one of ours.
    if len(sys.argv) > 1 and sys.argv[1] == "--shim":
        return _run_shim([sys.argv[0], *sys.argv[2:]])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", action="store_true")
    parser.add_argument("--parent-forwarders", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--ready-file")
    args = parser.parse_args()

    if args.parent_forwarders:
        _run_parent_forwarders(args.ready_file)
        return 0
    if args.broker:
        _prctl_die_with_parent()
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        _Broker(config).serve()
        return 0
    parser.error("one of --broker, --parent-forwarders or --shim is required")
    return 1


if __name__ == "__main__":
    sys.exit(main())
