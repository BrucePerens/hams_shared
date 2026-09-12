# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.
"""
Regression coverage for usability_audit_daemon.py's own IPC client, found live
(2026-08-28) while running this daemon for real against a running hams.com.
"""
import asyncio
import io
import sys
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, __import__("os").path.dirname(__file__))
import usability_audit_daemon as uad


def _fake_sse_client(url):
    class _Ctx:
        async def __aenter__(self_inner):
            return (MagicMock(), MagicMock())

        async def __aexit__(self_inner, *exc):
            return False

    return _Ctx()


def _fake_client_session_factory(sent_calls):
    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def initialize(self_inner):
            return None

        async def call_tool(self_inner, name, arguments):
            sent_calls.append((name, arguments))
            return MagicMock()

    return _FakeSession


class _SafePatchTestCase(unittest.TestCase):
    """Matches test_mcp_watchdog.py's / test_provision.py's own convention: a
    self.safe_patch() wrapper instead of a bare `with patch(...)` context
    manager or `@patch` decorator at each call site."""

    def safe_patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj


class SendIpcMessageFromARunningEventLoopTests(_SafePatchTestCase):
    # Found live: usability_audit_daemon.py runs inside Playwright's *sync*
    # API, which already has a running asyncio event loop in the calling
    # thread -- a bare `asyncio.run()` inside _send_ipc_message crashed with
    # the exact "asyncio.run() cannot be called from a running event loop"
    # error docs/MCP_WATCHDOG_REPORT.md describes, just triggered by
    # Playwright instead of Gemini's own MCP proxy. This test reproduces
    # that exact condition (calling the sync _send_ipc_message from a thread
    # that already has a running loop) without needing a real server, by
    # calling it from inside an async function driven by asyncio.run().
    #
    # Patches uad.sse_client/uad.ClientSession (the names bound in this
    # module's own namespace by its now-module-level imports), not
    # mcp.client.sse.sse_client/mcp.client.session.ClientSession directly --
    # a `from X import Y` binds a local reference at import time, so
    # patching X.Y afterward doesn't reach a call site that already holds
    # its own reference. This only worked before because the import was
    # local to the function and re-evaluated on every call.

    def test_send_succeeds_when_called_from_a_thread_with_a_running_loop(self):
        sent_calls = []
        self.safe_patch("usability_audit_daemon.sse_client", side_effect=_fake_sse_client)
        self.safe_patch(
            "usability_audit_daemon.ClientSession",
            new=_fake_client_session_factory(sent_calls),
        )

        async def call_from_within_a_running_loop():
            # At this point asyncio.get_running_loop() succeeds in this
            # thread -- the exact condition that broke the old
            # bare-asyncio.run() implementation.
            uad._send_ipc_message("some_queue", "some content")

        # Must not raise "asyncio.run() cannot be called from a running event loop".
        asyncio.run(call_from_within_a_running_loop())

        self.assertEqual(len(sent_calls), 1)
        name, arguments = sent_calls[0]
        self.assertEqual(name, "send_ipc_message")
        self.assertEqual(arguments, {"queue_name": "some_queue", "content": "some content"})

    def test_a_real_send_failure_propagates_instead_of_being_swallowed(self):
        self.safe_patch(
            "usability_audit_daemon.sse_client", side_effect=RuntimeError("connection refused")
        )

        async def call_from_within_a_running_loop():
            uad._send_ipc_message("some_queue", "some content")

        with self.assertRaises(RuntimeError):
            asyncio.run(call_from_within_a_running_loop())

    def test_a_hung_send_raises_timeout_error_instead_of_blocking_forever(self):
        # Found live, 2026-08-29, running this daemon for real (twice,
        # reproducibly): the send thread can hang indefinitely with no
        # exception at all, even though a fresh, isolated, non-threaded call
        # against the real shared server completed in well under 50ms during
        # the same window -- see _SEND_IPC_MESSAGE_TIMEOUT_SECS's own module-
        # level comment for the full account. This reproduces a hang directly
        # (an sse_client whose __aenter__ never returns) rather than trying to
        # reproduce the exact Playwright/thread interaction, and confirms the
        # timeout actually unblocks the caller instead of hanging the test too.
        def _hanging_sse_client(url):
            class _Ctx:
                async def __aenter__(self_inner):
                    # Never resolves within the test's own real-world patience --
                    # blocks past _SEND_IPC_MESSAGE_TIMEOUT_SECS via a real
                    # threading.Event with no timeout, the same "genuinely never
                    # returns" shape the live hang had, not a long asyncio.sleep()
                    # that would just make this test itself slow.
                    threading.Event().wait()

                async def __aexit__(self_inner, *exc):
                    return False

            return _Ctx()

        self.safe_patch("usability_audit_daemon.sse_client", side_effect=_hanging_sse_client)
        self.safe_patch("usability_audit_daemon._SEND_IPC_MESSAGE_TIMEOUT_SECS", 0.2)

        async def call_from_within_a_running_loop():
            uad._send_ipc_message("some_queue", "some content")

        with self.assertRaises(TimeoutError):
            asyncio.run(call_from_within_a_running_loop())


class ExtractPageStateAccessibleNameTests(unittest.TestCase):
    # Found live, 2026-08-28, running a real screen-reader-persona audit against hams.com's
    # real signup form: extract_page_state() reported a radio input's accessible name as the
    # raw `value` attribute ("ham") instead of its real, correctly-`for`-associated <label>
    # text ("Licensed Operator") -- a false-positive accessibility finding against markup
    # that was already correct. Per the real W3C accessible-name computation a screen reader
    # follows, `value` is never part of a radio/checkbox/text input's accessible name; only an
    # explicit <label> association is. These tests exercise the real function against a real
    # (headless, local, no network) page, not a mock -- the whole point is proving the
    # extraction logic itself, not just that some function returns some string.

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def _elements_for(self, html):
        page = self._browser.new_page()
        try:
            page.set_content(html)
            _, elements = uad.extract_page_state(page)
            return elements
        finally:
            page.close()

    def test_a_label_for_association_wins_over_the_inputs_own_value_attribute(self):
        html = """
        <html><body>
          <input type="radio" name="operator_type" id="type_ham" value="ham" checked="checked"/>
          <label for="type_ham">Licensed Operator</label>
        </body></html>
        """
        elements = self._elements_for(html)
        labels = [e["label"] for e in elements]
        self.assertIn("Licensed Operator", labels)
        self.assertNotIn("ham", labels)

    def test_a_wrapping_label_also_wins_over_value(self):
        html = """
        <html><body>
          <label>
            <input type="radio" name="operator_type" value="swl"/>
            Prospective Ham / Short Wave Listener (SWL)
          </label>
        </body></html>
        """
        elements = self._elements_for(html)
        labels = [e["label"] for e in elements]
        self.assertTrue(any("Short Wave Listener" in l for l in labels))
        self.assertNotIn("swl", labels)

    def test_value_is_still_used_when_there_really_is_no_label(self):
        html = """
        <html><body>
          <input type="submit" value="Submit Order"/>
        </body></html>
        """
        elements = self._elements_for(html)
        labels = [e["label"] for e in elements]
        self.assertIn("Submit Order", labels)

    def test_aria_label_still_wins_over_a_real_label_for(self):
        html = """
        <html><body>
          <input type="radio" id="x" value="v" aria-label="Explicit ARIA Name"/>
          <label for="x">Should Not Win</label>
        </body></html>
        """
        elements = self._elements_for(html)
        labels = [e["label"] for e in elements]
        self.assertIn("Explicit ARIA Name", labels)
        self.assertNotIn("Should Not Win", labels)

    def test_an_unlabeled_canvas_gets_a_synthetic_label_instead_of_being_dropped(self):
        # Found live 2026-08-29 fixing the QSL Card Designer bug: a <canvas> almost
        # never has an accessible name (it's a bitmap, not text), so it fell through
        # every real label source and hit "if not label: continue" -- silently
        # invisible to every persona, on a page whose entire interaction surface is
        # that one element. Every other unlabeled tag is legitimately skipped (a
        # real screen-reader user can't act on it either); a canvas is the one
        # exception, since a sighted persona can still see and click/drag it.
        html = """
        <html><body>
          <canvas id="qslCanvas" width="800" height="500"></canvas>
        </body></html>
        """
        elements = self._elements_for(html)
        canvases = [e for e in elements if e["tag"] == "canvas"]
        self.assertEqual(len(canvases), 1)
        self.assertTrue(canvases[0]["label"])

    def test_a_canvas_with_a_real_aria_label_keeps_it(self):
        html = """
        <html><body>
          <canvas id="qslCanvas" width="800" height="500" aria-label="QSL card layout"></canvas>
        </body></html>
        """
        elements = self._elements_for(html)
        labels = [e["label"] for e in elements if e["tag"] == "canvas"]
        self.assertEqual(labels, ["QSL card layout"])


class ExtractPageStateStaleElementTests(unittest.TestCase):
    # Regression coverage for the `# audit-ignore-catch-all` tag + logging call added to
    # extract_page_state()'s per-element `except Exception: continue` (previously zero-trace).
    # A real, live, dynamic page can mutate its own DOM between locator.count() and a given
    # element's own turn in the loop, so one element raising mid-extraction (detached,
    # navigated away, etc.) is a genuine, expected failure mode this loop must tolerate --
    # skipping just that element, not the whole page scan -- while now also logging it instead
    # of continuing in total silence. Uses lightweight duck-typed fake page/locator/element
    # objects (extract_page_state only ever calls documented Playwright-style methods on
    # `page`, nothing type-checks it) rather than a real browser, since the fault needs to be
    # deterministic -- injected on one specific element -- not racing a real page's own DOM
    # mutation timing.

    class _ExplodingElement:
        def is_visible(self):
            raise RuntimeError("simulated: element detached from DOM")

    class _FineElement:
        def is_visible(self):
            return True

        def bounding_box(self):
            return {"x": 0, "y": 0, "width": 50, "height": 20}

        def evaluate(self, _script):
            return "button"

        def inner_text(self):
            return "Click me"

        def get_attribute(self, _name):
            return None

    class _FakeLocator:
        def __init__(self, elements):
            self._elements = elements

        def count(self):
            return len(self._elements)

        def nth(self, i):
            return self._elements[i]

    class _FakePage:
        def __init__(self, elements):
            self._locator = ExtractPageStateStaleElementTests._FakeLocator(elements)

        def evaluate(self, _script):
            return ""

        def locator(self, _selector):
            return self._locator

    def test_one_stale_element_is_skipped_and_logged_others_still_extracted(self):
        page = self._FakePage([self._ExplodingElement(), self._FineElement()])
        with self.assertLogs("usability_audit_daemon", level="INFO") as cm:
            _visible_text, elements = uad.extract_page_state(page)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["label"], "Click me")
        self.assertTrue(any("skipping element 0" in msg for msg in cm.output))


class SelectImageNoteTests(unittest.TestCase):
    # Found live 2026-08-29 running the screen_reader_user persona for real
    # against a canvas-containing page (the Web Shack console): run_leg()
    # attached a real screenshot and told the persona to "View it directly
    # before answering" even though that persona is explicitly blind --
    # impossible to follow and the opposite of what a canvas page actually
    # means for a blind user. no_visual_access is the fix; these tests pin
    # its precedence over color_vision_simulation and its exact notes.
    def test_no_visual_access_with_no_canvas_captures_nothing_and_has_no_note(self):
        should_capture, note = uad.select_image_note(
            has_canvas=False, color_vision_simulation=False, no_visual_access=True
        )
        self.assertFalse(should_capture)
        self.assertIsNone(note)

    def test_no_visual_access_with_a_canvas_gets_the_blind_specific_note_and_no_screenshot(self):
        should_capture, note = uad.select_image_note(
            has_canvas=True, color_vision_simulation=False, no_visual_access=True
        )
        self.assertFalse(should_capture)
        self.assertEqual(note, uad.CANVAS_NO_VISUAL_ACCESS_NOTE)

    def test_no_visual_access_wins_over_color_vision_simulation_regardless_of_persona_file_ordering(self):
        should_capture, note = uad.select_image_note(
            has_canvas=True, color_vision_simulation=True, no_visual_access=True
        )
        self.assertFalse(should_capture)
        self.assertEqual(note, uad.CANVAS_NO_VISUAL_ACCESS_NOTE)

    def test_color_vision_simulation_alone_captures_a_screenshot_with_the_color_note(self):
        should_capture, note = uad.select_image_note(
            has_canvas=False, color_vision_simulation=True, no_visual_access=False
        )
        self.assertTrue(should_capture)
        self.assertEqual(note, uad.COLOR_VISION_NOTE)

    def test_color_vision_simulation_with_a_canvas_concatenates_both_notes(self):
        should_capture, note = uad.select_image_note(
            has_canvas=True, color_vision_simulation=True, no_visual_access=False
        )
        self.assertTrue(should_capture)
        self.assertIn(uad.COLOR_VISION_NOTE, note)
        self.assertIn(uad.CANVAS_SCREENSHOT_NOTE, note)

    def test_plain_canvas_with_no_persona_flags_captures_a_screenshot_with_the_canvas_note(self):
        should_capture, note = uad.select_image_note(
            has_canvas=True, color_vision_simulation=False, no_visual_access=False
        )
        self.assertTrue(should_capture)
        self.assertEqual(note, uad.CANVAS_SCREENSHOT_NOTE)

    def test_no_canvas_and_no_persona_flags_captures_nothing(self):
        should_capture, note = uad.select_image_note(
            has_canvas=False, color_vision_simulation=False, no_visual_access=False
        )
        self.assertFalse(should_capture)
        self.assertIsNone(note)


class LogAndDismissDialogTests(unittest.TestCase):
    # Found live, 2026-08-29, running a real gdpr_privacy_dashboard persona audit:
    # a native confirm() dialog (gating an "ERASE MY ACCOUNT" button) is invisible
    # to extract_page_state()'s text-only DOM extraction, and Playwright silently
    # auto-dismisses dialogs by default with zero signal anywhere -- a persona
    # hitting one sees no visible change at all and may misjudge the page as
    # broken. log_and_dismiss_dialog() is the page.on("dialog", ...) handler that
    # fixes the silence (still dismissing, since accepting one blind could
    # confirm a real destructive action against the live site under test).

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def test_a_real_confirm_dialog_is_dismissed_and_logged(self):
        page = self._browser.new_page()
        try:
            page.on("dialog", uad.log_and_dismiss_dialog)
            page.set_content(
                """
                <html><body>
                  <button id="erase" onclick="window.__result = confirm('Erase everything?')">Erase</button>
                </body></html>
                """
            )
            with self.assertLogs("usability_audit_daemon", level="INFO") as captured:
                page.click("#erase")
            self.assertTrue(
                any("dismissed" in line and "Erase everything?" in line for line in captured.output),
                captured.output,
            )
            # confirm() returns false when dismissed -- proves the dialog was
            # actually resolved, not left hanging.
            self.assertEqual(page.evaluate("() => window.__result"), False)
        finally:
            page.close()


class NewPageForRunTests(unittest.TestCase):
    # Found the gap while adding the "mobile_device" persona-file setting
    # (2026-08-29): every persona run tonight had used Chromium's default
    # desktop viewport, and no persona had ever exercised the site's mobile/
    # responsive layout at all. new_page_for_run() is the extracted helper
    # main() now calls, tested directly here against a real browser rather
    # than only indirectly through main()'s own CLI/file-I/O path.

    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def test_no_mobile_device_gives_the_plain_desktop_default(self):
        page = uad.new_page_for_run(self._pw, self._browser, None)
        try:
            size = page.viewport_size
            self.assertGreater(size["width"], 1000, size)
        finally:
            page.close()

    def test_a_mobile_device_name_applies_its_real_viewport_and_user_agent(self):
        page = uad.new_page_for_run(self._pw, self._browser, "Pixel 5")
        try:
            expected = self._pw.devices["Pixel 5"]
            self.assertEqual(page.viewport_size, expected["viewport"])
            self.assertEqual(
                page.evaluate("() => navigator.userAgent"), expected["user_agent"]
            )
            self.assertGreater(page.evaluate("() => navigator.maxTouchPoints"), 0)
        finally:
            page.close()


class _FakeLocator:
    def count(self):
        return 0


class _FakePage:
    """Minimal Playwright-`page`-shaped double: just enough surface for
    run_leg() to complete one "navigate_back" step (extract_page_state()'s
    locator scan finds nothing, no screenshot is taken) and then hit its
    own post-action wait_for_load_state("networkidle") call, which this
    double always times out -- exercising the real, common "page kept
    making background network traffic" case documented at that call site.
    """

    url = "https://example.test/shack"

    def evaluate(self, script):
        return ""

    def locator(self, selector):
        return _FakeLocator()

    def go_back(self, timeout=5000):
        return None

    def wait_for_load_state(self, state, timeout=2000):
        raise uad.PlaywrightTimeoutError("networkidle wait timed out")


class _ExplodingGoBackPage(_FakePage):
    """Same minimal surface as _FakePage, but go_back() raises -- exercises run_leg()'s
    outer action-execution `except Exception` handler (the `# audit-ignore-catch-all` tag
    added alongside its pre-existing _logger.warning() call) instead of the inner
    networkidle-timeout handler _FakePage's own wait_for_load_state() exercises."""

    def go_back(self, timeout=5000):
        raise RuntimeError("simulated: navigation failed mid-action")

    def wait_for_load_state(self, state, timeout=2000):
        return None


class RunLegActionExecutionFailureTests(_SafePatchTestCase):
    # Regression test for the `# audit-ignore-catch-all` tag added to run_leg()'s outer
    # action-execution handler: it already logged via _logger.warning() (unlike the two bugs
    # documented elsewhere in this file that had zero trace at all), so this is confirming
    # that pre-existing, correct behavior still holds -- the action failure is both logged
    # and recorded into `history` for the persona/Conductor to see, and the leg continues
    # (does not raise) rather than crashing the whole audit run.
    def test_a_failed_action_is_logged_and_recorded_but_does_not_crash_the_leg(self):
        self.safe_patch(
            "usability_audit_daemon.ask_executor",
            return_value={"action": "navigate_back", "thought": "going back"},
        )
        log_fh = io.StringIO()
        with self.assertLogs("usability_audit_daemon", level="WARNING") as cm:
            history = uad.run_leg(
                _ExplodingGoBackPage(), "fake-model", "a curious ham", "go back", "https://example.test",
                max_steps=1, log_fh=log_fh,
            )
        self.assertTrue(any("Action execution failed at step 1" in msg for msg in cm.output))
        self.assertEqual(len(history), 1)
        self.assertIn("didn't work", history[0])
        self.assertIn("simulated: navigation failed mid-action", history[0])


class RunLegNetworkIdleTimeoutTests(_SafePatchTestCase):
    # Real bug found 2026-09-12: the `except PlaywrightTimeoutError: pass` around the
    # post-action wait_for_load_state("networkidle") call used to swallow this
    # expected-but-worth-knowing-about condition with zero trace anywhere -- not even
    # at debug/info level. It must now log it (matching this file's own established
    # "log an expected fallback at .info" convention used a few lines above at the
    # label[for]/closest('label') lookup fallbacks), without ever marking the action
    # itself as failed.
    def test_a_networkidle_timeout_is_logged_and_does_not_fail_the_step(self):
        self.safe_patch(
            "usability_audit_daemon.ask_executor",
            return_value={"action": "navigate_back", "thought": "going back"},
        )
        log_fh = io.StringIO()
        with self.assertLogs("usability_audit_daemon", level="INFO") as cm:
            history = uad.run_leg(
                _FakePage(), "fake-model", "a curious ham", "go back", "https://example.test",
                max_steps=1, log_fh=log_fh,
            )
        self.assertTrue(any("networkidle" in msg for msg in cm.output))
        self.assertEqual(history, ["Went back to the previous page."])


if __name__ == "__main__":
    unittest.main()
