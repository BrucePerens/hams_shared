# Offline Browser Namespace Testing

How to write an Odoo tour test in which **hams.com is genuinely unreachable from the browser while a
local service on the browser's own loopback keeps answering** -- the situation a real offline ham
operator is in, and the one no test in this codebase could express until now.

Implemented in `hams_shared/tools/offline_browser_ns.py`, wired into `hams_shared/tools/test.py`
behind `--offline-isolation`. First user:
`ham_shack/tests/test_shack_offline_isolation_tour.py` plus
`ham_shack/static/tests/tours/shack_offline_isolation_tour.js`.

## The problem this solves

`test.py` unshares a network namespace (`CLONE_NEWNET`, loopback only) before it starts anything.
That is real isolation *from the internet*, and it is not what an offline test needs. The werkzeug
test server and headless Chrome end up **in the same namespace**, so a tour's RPC and `fetch()` calls
to "hams.com" reach it over loopback in every test that has ever run. A real operator's outage is the
opposite asymmetry:

| | real offline operator | ordinary tour test |
|---|---|---|
| hams.com | unreachable | always reachable |
| local relay on `127.0.0.1` | reachable | reachable |

Toggling DevTools' offline switch or mocking `fetch` does not close the gap -- the first affects one
tab's own requests and the second proves the mock behaves as written. `docs/proposals/OFFLINE_HAM_OPERATION.md`
("Real gap found, 2026-09-07") names the two candidate fixes; this is the second one. The first (a
firewall rule on the werkzeug port inside the shared namespace) was rejected on the risk that
document already named: the test framework's own control channel uses that same server.

## The topology

```
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
```

The browser's only route to anything outside its namespace is the veth pair. Chrome must see the
Odoo server at exactly `127.0.0.1:8075`, because that is what `HttpCase.base_url()` says and what the
session cookie's domain is, so a **gateway** forwarder inside the child namespace listens there and
carries the traffic across.

**"Go offline" means closing that gateway's listening socket and tearing down its live connections.**
Chrome then gets a prompt `ECONNREFUSED` -- a real, fast connection failure. This is deliberate, not
incidental: an `iptables ... -j DROP` would black-hole the traffic instead, every request would hang
until its own timeout, and a tour with a 90-second budget would die of timeouts rather than assert
anything. The DevTools channel runs the other way over the same veth and is never touched, so the
test framework keeps full control of the browser the whole time.

## Privilege model, and why there is a broker

`test.py` runs the Odoo test process as the unprivileged `odoo` user. An unprivileged process cannot
`setns()` into a network namespace. Verified directly rather than assumed:

```
$ setpriv --reuid=odoo --regid=odoo --init-groups nsenter -t <pid> -n true
nsenter: reassociate to namespaces failed: Operation not permitted
```

So the test process can neither launch the browser inside the child namespace nor change the network
state itself. Instead:

* the process that **holds** the child namespace is also a **broker**: root, inside that namespace,
  listening on a unix-domain control socket;
* unix sockets are filesystem objects and cross network namespaces freely, so the unprivileged test
  process can drive the broker with no privilege at all;
* Odoo launches the browser through `ODOO_BROWSER_BIN` (Odoo's own documented override, see
  `odoo/tests/common.py:_find_executable`) pointed at a **shim**, which hands the argv to the broker
  and holds the connection open. When Odoo terminates its "Chrome" (the shim), the broker sees EOF
  and kills the real browser.

The broker never runs a binary the caller names -- it always runs the Chrome path it was configured
with -- because the control socket is reachable by the unprivileged test user.

## Two things that will bite you if you change this

1. **The DevTools port must stay ephemeral.** Chrome writes the `DevToolsActivePort` file that Odoo's
   `_spawn_chrome` polls for **only when it was asked for port 0**. Pinning an explicit
   `--remote-debugging-port` makes the browser log `DevTools listening on ws://127.0.0.1:<port>/...`
   and never write the file, and Odoo turns a missing file into `unittest.SkipTest` -- a test that
   silently does not run. So the port is discovered per launch and the forwarders are built around it.
2. **Chrome gets a `ns_profile` subdirectory of Odoo's profile directory**, so its own
   `DevToolsActivePort` lands where Odoo is not looking. The shim publishes that file into Odoo's own
   directory only *after* the parent-side forwarder is listening. Publishing it first would let Odoo
   read the port and connect to a socket nothing was serving yet.

## Running it

```bash
tools/test.py --offline-isolation -u ham_shack
```

This is **opt-in and additive**. Without the flag, nothing above runs and the test process sees
exactly the environment it always has. With it, `test.py` also narrows `--test-tags` to
`offline_isolation/<module>` -- running unrelated tours through the forwarders would put them at the
mercy of this mechanism for no gain.

Tours cannot run in parallel on this box; `--offline-isolation` uses the same systemwide `test.py`
lock as every other run, and makes no attempt to work around it.

## Writing an offline tour for another module

### 1. Tag the test class so ordinary runs never collect it

```python
@tagged("offline_isolation", "post_install", "-at_install", "-standard")
class TestMyModuleOffline(HamsHttpCase):
```

Odoo's tag selector treats a spec with no tag part (`/my_module`) as implicitly requiring `standard`
(`odoo/tests/tag_selector.py`), so `-standard` means an ordinary `test.py -u my_module` does not
select the class at all. Nothing is skipped at runtime -- which matters, because `skipTest` is banned
by this repo's linter and because a silently skipped offline test is the worst outcome available.

### 2. Talk to the broker

The environment the harness provides:

| variable | meaning |
|---|---|
| `HAMS_OFFLINE_NS_CTL` | path of the broker's control socket |
| `HAMS_OFFLINE_RELAY_PORT` | the local-relay stand-in's port (38911) |
| `ODOO_BROWSER_BIN` | the shim (set for you; you should not need it) |

The protocol is line-delimited JSON:

```json
{"cmd": "offline"}  ->  {"ok": true, "online": false}
{"cmd": "online"}   ->  {"ok": true, "online": true}
{"cmd": "status"}   ->  {"ok": true, "online": true, "relay_requests": ["/api/local_content"], "chrome_running": true}
```

`relay_requests` is every path the relay stand-in was actually asked for, which is how a test asserts
from Python that the browser really did consult the relay.

Copy `OfflineNamespaceControl` from `ham_shack/tests/test_shack_offline_isolation_tour.py` (about
twenty lines; `hams_shared/tools` is not on an Odoo test's import path, so a copy costs less than a
path hack), and **assert `HAMS_OFFLINE_NS_CTL` is set** at the top of the test. Failing there is
right; passing without isolation would test the online path under an offline name.

### 3. Drive one browser through several network states

`HttpCase.browser_js()` builds a fresh `ChromeBrowser` -- new process, new profile -- on every call,
so it can never carry a service worker, a Cache Storage entry or an IndexedDB spool from one call to
the next, which is the whole substance of an offline test. Use the `_drive()` helper in
`test_shack_offline_isolation_tour.py`: a list of `(url_path_or_callable, js_code, label)` steps
against one browser, where a callable step is a network-state change rather than a navigation, and
`url_path=None` runs code on the page that is **already open**. That ordering is the realistic one --
an operator's page is open when the connection drops, it does not get opened afterwards.

### 4. Expect `navigator.onLine` to stay `true`

The veth stays up for the DevTools channel, so Chrome still sees a link, `navigator.onLine` is
`true`, and no `online`/`offline` event fires. That is not a limitation to work around: plenty of real
outages look exactly like this (the Wi-Fi is fine, the internet is not). It means your code's
`navigator.onLine` branch is *not* what runs -- the real request is attempted, really fails, and the
failure path is what you are testing. `ham_shack`'s offline tour asserts `logContact()`'s
ORM-failure fallback for exactly this reason, and that path had no tour coverage at all before.

### 5. Set an error checker

A genuinely unreachable server makes the web client complain in the console (connection-lost
handling, bus reconnects, Chrome's own failed-request logging) and Odoo fails a `browser_js` on any
`console.error`. Pass a `browser.error_checker` that ignores that specific noise and nothing else --
see `_OFFLINE_NOISE` in the ham_shack test. Every failure the tour itself reports must still fail.

## The local-relay stand-in

`127.0.0.1:38911` inside the child namespace, mirroring the real `hams_local_relay` endpoints
`shack_sw.js` talks to (`daemons/hams_local_relay/src/local_distribution.rs`'s `DOC_MANIFEST`), with
real CORS headers because the browser really does treat it as a different origin:

* `GET /__offline_probe` -> `OFFLINE_NS_LOCAL_RELAY_REACHABLE`
* `GET /api/local_content` -> a manifest holding one item, `web_shack_manual`
* `GET /api/local_content/doc/web_shack_manual` -> `<html>OFFLINE_NS_LOCAL_RELAY_MANUAL</html>`

A stand-in rather than the real Rust binary, deliberately: what this harness has to prove is the
reachability asymmetry, and a real relay would drag a cargo build and a hamlib/audio environment into
an Odoo tour run. If a module needs different relay endpoints, extend `_RelayStubHandler` in
`offline_browser_ns.py` rather than standing up a second server -- the point of it living in the
child namespace is that it is on the *browser's* loopback, which a server started from a test's own
`setUp` is not.

## Cleanup and concurrency

Both veth ends live inside `test.py`'s own already-unshared namespace, so they are invisible to the
host and to any other test run on this box; nothing is created in `/run/netns` and no `ip netns` name
is registered. The child namespace exists only as long as the broker process, which carries
`PR_SET_PDEATHSIG(SIGKILL)`, and `test.py` tears the whole thing down in the `finally` that already
reaps Redis and RabbitMQ. A crashed run leaves nothing behind for the next one to trip over.

## Debugging a failure

* `~/tmp/offline_ns/offline_ns_chrome.log` -- the browser's own stderr. When headless Chrome refuses
  to start inside the nested namespace, this is the only place that says why; Odoo sees nothing but a
  missing `DevToolsActivePort`.
* `{"cmd": "status"}` from the test -- whether the gateway is up, what the relay was asked for, and
  whether a browser is alive.
* A tour that passes when it should not usually means the harness never went offline. The ham_shack
  tour's first step asserts the server is unreachable before anything else, and the offline
  navigation step re-checks it; copy that habit.
