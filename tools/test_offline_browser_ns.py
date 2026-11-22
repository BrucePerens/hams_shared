# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of the plain-Python parts of offline_browser_ns on the loopback: the TCP forwarder, the relay stub, the control
socket client and the Chrome command-line rewrite. Nothing here needs root, a namespace or a browser."""

import json
import os
import shutil
import socket
import tempfile
import threading
import types
import unittest
import urllib.request

import offline_browser_ns as ons


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ForwarderTests(unittest.TestCase):
    def test_forwards_bytes_and_stop_clears_running(self):
        echo = socket.socket()
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)

        def serve():
            conn, _ = echo.accept()
            conn.sendall(conn.recv(100).upper())
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        port = free_port()
        forwarder = ons.TcpForwarder("127.0.0.1", port, "127.0.0.1", echo.getsockname()[1], "test")
        forwarder.start()
        try:
            self.assertTrue(forwarder.running)
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"hello")
                self.assertEqual(client.recv(100), b"HELLO")
        finally:
            forwarder.stop()
            echo.close()
        self.assertFalse(forwarder.running)


class RelayStubTests(unittest.TestCase):
    def test_probe_and_content_list_are_served_and_requests_recorded(self):
        server = ons._RelayStubServer(("127.0.0.1", 0), ons._RelayStubHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            self.assertEqual(urllib.request.urlopen(base + "/__offline_probe", timeout=5).read(), ons.RELAY_PROBE_BODY)
            items = json.loads(urllib.request.urlopen(base + "/api/local_content", timeout=5).read())["items"]
            self.assertEqual(items[0]["id"], "web_shack_manual")
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/nothing", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(server.snapshot(), ["/__offline_probe", "/api/local_content", "/nothing"])


class ClientTests(unittest.TestCase):
    def test_client_sends_one_json_line_and_reads_the_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ctl.sock")
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen(1)
            seen = []

            def serve():
                conn, _ = listener.accept()
                seen.append(json.loads(conn.recv(1000).decode()))
                conn.sendall(b'{"ok": true}\n')
                conn.close()

            threading.Thread(target=serve, daemon=True).start()
            self.assertEqual(ons.OfflineNamespaceClient(path).go_offline(), {"ok": True})
            listener.close()
        self.assertEqual(seen, [{"cmd": "offline"}])

    def test_client_without_a_socket_path_refuses(self):
        saved = os.environ.pop("HAMS_OFFLINE_NS_CTL", None)
        try:
            with self.assertRaises(RuntimeError):
                ons.OfflineNamespaceClient()
        finally:
            if saved is not None:
                os.environ["HAMS_OFFLINE_NS_CTL"] = saved


class BrokerTests(unittest.TestCase):
    def test_chrome_command_uses_the_configured_binary_and_rewrites_profile_and_address(self):
        broker = ons._Broker({"ctl_sock": "/x/ctl", "chrome_binary": "/opt/chrome"})
        cmd, odoo_profile, ns_profile = broker._sanitize_chrome_argv(
            ["/evil/bin", "--headless", "--user-data-dir=/p/prof", "--remote-debugging-address=0.0.0.0"]
        )
        self.assertEqual(cmd[0], "/opt/chrome")
        self.assertIn("--remote-debugging-address=127.0.0.1", cmd)
        self.assertIn("--user-data-dir=" + ns_profile, cmd)
        self.assertEqual((odoo_profile, ns_profile), ("/p/prof", os.path.join("/p/prof", ons.NS_PROFILE_SUBDIR)))
        self.assertNotIn("/evil/bin", cmd)
        with self.assertRaises(ValueError):
            broker._sanitize_chrome_argv(["x", "--headless"])


class ProfilePreparationTests(unittest.TestCase):
    """The broker runs as root on a path the unprivileged test user chose."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.path.expanduser("~/.cache"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.user = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())

    def test_a_profile_base_owned_by_someone_else_is_refused(self):
        other = types.SimpleNamespace(pw_uid=os.getuid() + 1, pw_gid=os.getgid())
        with self.assertRaises(ValueError):
            ons._Broker._prepare_profile(self.tmp, os.path.join(self.tmp, ons.NS_PROFILE_SUBDIR), other)

    def test_a_symlinked_profile_base_is_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        with self.assertRaises(ValueError):
            ons._Broker._prepare_profile(link, os.path.join(link, ons.NS_PROFILE_SUBDIR), self.user)

    def test_a_symlinked_ns_profile_is_refused(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        ns = os.path.join(self.tmp, ons.NS_PROFILE_SUBDIR)
        os.symlink(target, ns)
        with self.assertRaises(ValueError):
            ons._Broker._prepare_profile(self.tmp, ns, self.user)

    def test_a_normal_profile_is_created(self):
        ns = os.path.join(self.tmp, ons.NS_PROFILE_SUBDIR)
        ons._Broker._prepare_profile(self.tmp, ns, self.user)
        self.assertTrue(os.path.isdir(ns))


# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: offline_browser_ns:offline_browser_ns_tcp_forwarder_class]
# Tests [@ANCHOR: offline_browser_ns:offline_browser_ns__relay_stub_handler_class]
# Tests [@ANCHOR: offline_browser_ns:offline_browser_ns__relay_stub_server_class]
# Tests [@ANCHOR: offline_browser_ns:offline_browser_ns__broker_class]
# Tests [@ANCHOR: offline_browser_ns:offline_browser_ns_offline_namespace_client_class]
