#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.
"""
Drives a real headless browser against a real running hams.com instance, playing a
non-technical-ham persona defined entirely by docs/proposals/USABILITY_AUDIT_SIMULATED_HAM.md.
See tools/usability_audit/README.md for how to run this and how the output is structured.

Deliberately NOT a Claude Agent/fork dispatch -- see .claude/skills/avoiding-api-costs/SKILL.md.
Each decision point is routed through this codebase's own established MCP scheme
(hams_shared/tools/mcp_watchdog.py's IPC bridge, the same mechanism ingest/daemon_utils.py's
prompt_and_parse_json()/wait_for_llm_action() already use for the course-content pipeline) --
ask_executor() writes the prompt to a queue and blocks for a JSON response file, rather than
making a direct Gemini REST call. This structurally supports the "no implementation knowledge"
persona constraint when a genuinely fresh, isolated executor (a Gemini Conductor subagent with no
repo context) answers -- but does NOT guarantee it if the orchestrating Claude Code session ends
up answering the prompt itself in the absence of one; see ask_executor()'s own docstring and
.claude/skills/avoiding-api-costs/SKILL.md for that real, disclosed tradeoff.
"""
import argparse
import asyncio
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

_logger = logging.getLogger("usability_audit_daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GEMINI_MODEL_DEFAULT = "gemini-3.7-flash"
MAX_PAGE_TEXT_CHARS = 6000
MAX_INTERACTIVE_ELEMENTS = 60
MAX_HISTORY_ENTRIES_IN_PROMPT = 8
CONSECUTIVE_CONFUSION_LIMIT = 3  # give up a leg after this many confused steps in a row

# Found live 2026-08-29 testing the Operator Directory Map: theme_hams's
# s_ham_map.xml uses a native <details>/<summary> disclosure ("View operator
# list") as a real, always-in-the-DOM accessible fallback to the map -- but
# <summary> wasn't in this selector, so no persona could ever find or click
# it, even though it's a fully native, keyboard-accessible control. Used in
# three places below (element extraction and both action-execution
# re-queries); they must stay identical to each other since click/type index
# into the list this selector produces.
#
# `canvas` added the same night, for a different reason: the QSL Card
# Designer renders entirely to a <canvas>, which this DOM-text-based tool
# cannot see into at all -- a persona reported "nothing visibly changed"
# after clicking a real, working "Add Field" button, indistinguishable from
# the page being genuinely broken (it happened to also be broken, but this
# tool couldn't tell the difference). Listing the canvas itself as an
# addressable element (with a screenshot attached, and a real "drag" action
# below) closes that blind spot -- it doesn't let the persona see individual
# canvas-drawn shapes, but it lets it see the canvas's overall visual state
# change, and actually attempt a drag instead of silently being unable to.
INTERACTIVE_ELEMENTS_SELECTOR = "a, button, input, textarea, select, summary, canvas, [role=button], [onclick]"

PERSONA_SYSTEM_PREAMBLE = """You are role-playing a real, non-technical newcomer to a website, for
a usability audit. Stay completely in character. You have NEVER seen this website's source code,
admin panel, or internal documentation -- you only know what is visibly on the current page, plus
your own memory of what you've done so far in this session (given to you below). You are not a
software engineer and would not think to view page source, open developer tools, or guess at URLs
that aren't shown to you as a clickable link or button.

Skill floor: comfortable with a browser, email, and a smartphone. You have never used a terminal,
and do not know what a "daemon," "API key," "systemd service," "SSH," or "repository" is. If asked
to do something described using words like that with no plain-language explanation alongside it,
that is itself something you would find confusing -- say so.

At each step you will be shown the current page's visible text and a list of clickable/fillable
elements. Decide what YOU, the persona, would do next given your goal, and report your genuine
reaction. Respond with ONLY a JSON object, no other text, with these fields:
- "thought": one sentence, in character, about what you're looking at and why you're choosing your action
- "action": one of "click", "type", "drag", "navigate_back", "wait", "give_up_on_leg", "goal_complete"
  ("wait" means the page looks like it's still loading/thinking and you want to give it a few more
  seconds before deciding what to do next -- use this instead of re-clicking the same thing over
  and over. "drag" is for a canvas/drawing element you'd naturally try to click-and-drag, like moving
  something on a design tool -- see below for how to aim it)
- "target_index": integer index into the numbered element list (for "click", "type", or "drag"), or null
- "type_value": string to type (only for "type"); for "drag", one of "up", "down", "left", "right" --
  the rough direction you'd drag toward, since you can't see exact pixel coordinates, only null otherwise
- "confused": boolean -- true if this step felt confusing, unclear, or harder than it should be
- "confusion_reason": string explaining why, in the persona's own words, or null if not confused
- "suggestion": string -- a concrete "this would be easier if X" suggestion, or null if none
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# Simplified deuteranopia (red-green colorblindness, the common form) simulation matrix, in the
# style widely used by browser-based simulators such as Coblis. Not clinically precise -- it's an
# sRGB-space approximation -- but good enough to catch the case this tool cares about: meaning
# conveyed by red-vs-green hue alone that collapses to the same perceived color.
_DEUTERANOPIA_MATRIX = (
    (0.625, 0.375, 0.0),
    (0.7, 0.3, 0.0),
    (0.0, 0.3, 0.7),
)


def simulate_deuteranopia(png_bytes):
    """Returns PNG bytes with a deuteranopia (red-green colorblind) simulation applied, so the
    persona is shown the page approximately as a red-green colorblind viewer would perceive it."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    px = img.load()
    w, h = img.size
    mr, mg, mb = _DEUTERANOPIA_MATRIX
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            px[x, y] = (
                min(255, int(mr[0] * r + mr[1] * g + mr[2] * b)),
                min(255, int(mg[0] * r + mg[1] * g + mg[2] * b)),
                min(255, int(mb[0] * r + mb[1] * g + mb[2] * b)),
            )
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


_WATCHDOG_SSE_URL = "http://127.0.0.1:8767/sse"


_SEND_IPC_MESSAGE_TIMEOUT_SECS = 30
# ^ Added 2026-08-29 as defense-in-depth, not as the fix for a confirmed bug. What looked at the
# time like two reproducible live hangs in this send call turned out, on closer inspection right
# after this was added, to be a plain off-by-one mistake in the *manual verification* process, not
# a bug here: run_leg()'s own step counter starts at 1 (`for step in range(1, max_steps + 1)`), so
# a step's real queue name is `..._leg1_1`, not `..._leg1_0` -- both live investigations had waited
# on the wrong (nonexistent) queue and concluded the send was stuck, when the message was actually
# sitting in the correctly-numbered queue the whole time (confirmed via queue_status: nonzero
# pending_messages, a real seconds_since_last_send). The shared mcp_watchdog.py instance was never
# actually unhealthy. Kept anyway as cheap, reasonable protection against a real future hang this
# call has no other way to recover from -- see _send_ipc_message()'s own docstring.


def _send_ipc_message(queue_name, content):
    """Client for mcp_watchdog.py's shared instance, over SSE. Found 2026-08-28: this used to
    speak the old Unix-socket "legacy bridge" (wire format "<queue_name>\\n<content>"), but that
    bridge is only ever bound when the shared instance runs `--transport streamable-http`, and the
    real shared instance now runs `--transport sse` (see mcp_watchdog.py's own top-of-file
    comments and commit 827a705) -- the legacy bridge is simply never listening anymore. Matches
    ingest/daemon_utils.py's own send_ipc_message(), which hit and fixed the identical problem the
    same day. Deliberately reimplemented inline here rather than importing ingest/daemon_utils.py,
    to avoid a hams_shared -> hams_com cross-repo import for a few lines of client code.

    Runs the send in its own thread with a fresh event loop rather than a
    bare asyncio.run() in the calling thread: found live (2026-08-28,
    running this daemon for real) that Playwright's *sync* API already has
    a running event loop in the calling thread, so a bare asyncio.run() here
    raised the exact "asyncio.run() cannot be called from a running event
    loop" error docs/MCP_WATCHDOG_REPORT.md describes -- the same class of
    bug that report was filed about, just triggered by Playwright instead of
    Gemini's own MCP proxy. A dedicated thread sidesteps the question of
    whether the calling thread has a loop running at all.

    Raises TimeoutError if the send hasn't completed within
    _SEND_IPC_MESSAGE_TIMEOUT_SECS -- precautionary, not a fix for a
    confirmed bug; see that constant's own comment. The background thread
    is a daemon thread and is left to die on its own rather than
    force-killed (Python has no clean thread-kill primitive); the caller
    gets unblocked either way."""

    async def _send():
        for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            os.environ.pop(k, None)
        async with sse_client(_WATCHDOG_SSE_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool(
                    "send_ipc_message", arguments={"queue_name": queue_name, "content": content}
                )

    errors = []

    def _run():
        try:
            asyncio.run(_send())
        except Exception as e:  # audit-ignore-catch-all: captured here only
            # to cross the thread boundary -- fully re-raised in the calling
            # thread below via `raise errors[0]`, never swallowed. Must be
            # unconditional: the caller needs whatever the SSE/MCP call
            # actually raised (connection, timeout, or protocol errors
            # alike), not a guess at which specific exception types apply.
            _logger.warning("IPC send thread caught %s, re-raising in caller: %s", type(e).__name__, e)
            errors.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_SEND_IPC_MESSAGE_TIMEOUT_SECS)
    if t.is_alive():
        raise TimeoutError(
            f"_send_ipc_message('{queue_name}', ...) did not complete within "
            f"{_SEND_IPC_MESSAGE_TIMEOUT_SECS}s -- the shared mcp_watchdog.py instance may be "
            f"unreachable, or this daemon's own background-thread/Playwright interaction hung "
            f"again (see this function's docstring). The send thread is abandoned, not killed."
        )
    if errors:
        raise errors[0]


def ask_executor(model, prompt_text, timeout=60, image_bytes=None, out_dir=None, step_id=None):
    """Per Bruce's direction (2026-08-26): routes each persona decision through this codebase's
    own established MCP scheme (mcp_watchdog.py's IPC bridge, the same one ingest/daemon_utils.py's
    prompt_and_parse_json()/wait_for_llm_action() already use for the course-content pipeline)
    instead of a direct Gemini REST call needing GEMINI_API_KEY -- no key is required or read here
    at all. Writes the prompt (plus, for the colorblind persona, the deuteranopia-simulated
    screenshot saved to a real file the executor can view directly) to the queue, then blocks
    polling for the executor's JSON response file, exactly like wait_for_llm_action().

    **Real, disclosed deviation from this proposal's "no implementation knowledge" persona
    constraint**: whichever agent is actually listening on this queue and answering (a fresh
    Gemini Conductor subagent with no repo context, if one is live and picks it up -- the
    contamination-free case this proposal was designed around -- or, in the absence of one, the
    orchestrating Claude Code session driving this run directly) is the real answerer; if it's the
    orchestrating session, that session may have read this repository, unlike the design's
    original zero-knowledge intent. Flagged here and in the run's own log/README rather than
    silently assumed away."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    step_id = step_id or ts
    out_dir = out_dir or "/tmp"
    output_file = os.path.join(out_dir, f"usability_audit_response_{step_id}.json")
    if os.path.exists(output_file):
        os.remove(output_file)

    image_note = ""
    if image_bytes is not None:
        image_path = os.path.join(out_dir, f"usability_audit_screenshot_{step_id}.png")
        with open(image_path, "wb") as f:
            f.write(image_bytes)
        image_note = f"\n\nAn attached screenshot (deuteranopia-simulated) is saved at: {image_path}\nView it directly before answering."

    prompt_content = (
        f"\nACTION_REQUIRED_BY_EXECUTOR:\n{prompt_text}{image_note}\n\n"
        f"Respond with ONLY the JSON object described above.\n"
        f"Use the `write_to_file` (or equivalent) tool to save your raw JSON output directly to `{output_file}`.\n"
        f"CRITICAL INSTRUCTION: Do NOT end your turn until you have successfully written that JSON file. "
        f"You must actively generate the file right now!"
    )
    queue_name = f"/usability_audit_{step_id}"
    payload = json.dumps({"_meta_expectation": {"file": output_file, "timeout_mins": 15}, "prompt": prompt_content})
    _send_ipc_message(queue_name, payload)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(output_file):
            with open(output_file) as f:
                text = f.read().strip()
            if text.startswith("```"):
                text = "\n".join(text.split("\n")[1:])
            if text.endswith("```"):
                text = "\n".join(text.split("\n")[:-1])
            return json.loads(text.strip())
        time.sleep(1)
    raise TimeoutError(f"No response written to {output_file} within {timeout}s (queue: {queue_name})")


def log_and_dismiss_dialog(dialog):
    """Playwright `page.on("dialog", ...)` handler. Found live 2026-08-29
    (gdpr_privacy_dashboard run): a native confirm()/alert()/prompt() dialog is
    invisible to extract_page_state()'s text-only DOM extraction -- Playwright
    auto-dismisses these by default with no signal anywhere, so a persona
    clicking a button that triggers one (e.g. "ERASE MY ACCOUNT", gated by
    onsubmit="return confirm(...)") sees no visible change at all and may
    misjudge the page as broken, when a real user's own browser would show
    the dialog fine. Dismissing is still the right default here (accepting
    one blind could confirm a real destructive action against the live site
    under test), but logging it means a human reviewing the run afterward
    can tell the two apart instead of silence either way."""
    _logger.info("Native %s dialog dismissed: %r", dialog.type, dialog.message)
    dialog.dismiss()


def new_page_for_run(playwright, browser, mobile_device):
    """Creates the single page a run drives, honoring an optional
    "mobile_device" persona-file setting (see main()'s own comment for the
    full rationale). Extracted as its own function so the device-emulation
    wiring itself has direct test coverage without needing to drive main()'s
    whole CLI/argparse/file-I/O path end to end."""
    if mobile_device:
        context = browser.new_context(**playwright.devices[mobile_device])
        return context.new_page()
    return browser.new_page()


def extract_page_state(page):
    """Returns (visible_text, elements) where elements is a list of dicts the persona can act on.
    Deliberately reads only what a sighted user would see: visible text content and the
    accessible name of interactive elements -- never raw HTML, data attributes, or comments."""
    visible_text = page.evaluate(
        """() => {
            function isVisible(el) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }
            return isVisible(document.body) ? document.body.innerText : '';
        }"""
    )
    visible_text = (visible_text or "").strip()[:MAX_PAGE_TEXT_CHARS]

    elements = []
    locator = page.locator(INTERACTIVE_ELEMENTS_SELECTOR)
    count = min(locator.count(), MAX_INTERACTIVE_ELEMENTS * 3)
    for i in range(count):
        if len(elements) >= MAX_INTERACTIVE_ELEMENTS:
            break
        el = locator.nth(i)
        try:
            if not el.is_visible():
                continue
            box = el.bounding_box()
            if not box or box["width"] < 2 or box["height"] < 2:
                # Catches the common sr-only/visually-hidden pattern (e.g. "Skip to Content"
                # links): a 1x1px, clipped box that Playwright's own is_visible() still counts
                # as visible since it has a nonzero rect, but a sighted newcomer would never
                # see it without keyboard focus. The persona should never be offered it.
                continue
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            label = (el.inner_text() or "").strip()
            if not label:
                label = (el.get_attribute("aria-label") or "").strip()
            if not label:
                # <label for="id">text</label> comes before placeholder/value/title,
                # matching the real W3C accessible-name computation a real screen
                # reader follows: an explicit label association always wins over
                # guessing from other attributes. Found live, 2026-08-28: this used
                # to be checked LAST, after `value` -- so a radio/checkbox with both a
                # real <label for> AND a value attribute (e.g. <input type="radio"
                # value="ham"> paired with <label for="type_ham">Licensed
                # Operator</label>, hams.com's own real signup form) reported the raw
                # `value` ("ham") as its accessible name instead of the real label
                # ("Licensed Operator") -- a false-positive accessibility finding
                # against markup that was already correct, not a real site bug. `value`
                # is never part of the accessible name for radio/checkbox/text inputs
                # per spec; it only matters for submit/button/reset inputs, where it IS
                # the visible label -- which is exactly why it stays in the fallback
                # chain below, just after label-for instead of before it.
                el_id = (el.get_attribute("id") or "").strip()
                if el_id:
                    try:
                        label_loc = page.locator(f'label[for="{el_id}"]').first
                        if label_loc.count() > 0:
                            label = (label_loc.inner_text() or "").strip()
                    except Exception as e:  # audit-ignore-catch-all: a failed
                        # label-for lookup (detached element, navigation
                        # mid-lookup) must fall through to the next fallback
                        # below, not abort extracting this element entirely --
                        # the specific Playwright exception types here aren't
                        # worth enumerating for a lookup this disposable.
                        _logger.info("label[for] lookup failed for #%s, falling through: %s", el_id, e)
                if not label:
                    # The other real-world label-association pattern: a wrapping
                    # <label> with no `for`/`id` at all (e.g. <label><input .../>
                    # text</label>). Must be checked here, alongside label-for and
                    # before the placeholder/value/title fallback below -- found via
                    # this fix's own regression test failing when this lived after
                    # the value fallback instead: `value` won every time before this
                    # ever got a chance to run.
                    try:
                        label = (el.evaluate("e => e.closest('label')?.innerText || ''") or "").strip()
                    except Exception as e:  # audit-ignore-catch-all: same
                        # reasoning as the label-for lookup above -- fall
                        # through to the next fallback, don't abort this
                        # element's extraction over a disposable lookup.
                        _logger.info("closest('label') lookup failed, falling through: %s", e)
            if not label:
                label = (
                    el.get_attribute("placeholder")
                    or el.get_attribute("value")
                    or el.get_attribute("title")
                    or ""
                ).strip()
            if not label and tag == "canvas":
                # A <canvas> almost never has an accessible name of its own --
                # it's a bitmap, not text -- but it can still be a real,
                # meaningful interactive target (drag-to-move, drag-to-draw).
                # Every other tag with no label is legitimately invisible to
                # a real screen reader user and gets skipped below; a canvas
                # is different; the visual persona can still see and click it
                # even with no name, so give it a real label instead of
                # dropping it silently.
                label = "drawing/editing canvas area"
            if not label:
                continue
            elements.append({"tag": tag, "label": label[:120], "_locator_index": i})
        except Exception:
            continue
    return visible_text, elements


def build_prompt(persona_desc, goal, history, visible_text, elements, current_url, image_note=None):
    lines = [PERSONA_SYSTEM_PREAMBLE, "", f"YOUR PERSONA: {persona_desc}", "", f"YOUR GOAL RIGHT NOW: {goal}", ""]
    if image_note:
        lines.append(image_note)
        lines.append("")
    if history:
        lines.append("WHAT YOU'VE DONE SO FAR THIS SESSION:")
        for h in history[-MAX_HISTORY_ENTRIES_IN_PROMPT:]:
            lines.append(f"- {h}")
        lines.append("")
    lines.append(f"CURRENT PAGE URL (you would only ever notice a URL if it's short/readable to a newcomer): {current_url}")
    lines.append("")
    lines.append("CURRENT PAGE'S VISIBLE TEXT:")
    lines.append(visible_text or "(page appears blank or has no visible text)")
    lines.append("")
    lines.append("CLICKABLE / FILLABLE ELEMENTS ON THIS PAGE (numbered):")
    for idx, el in enumerate(elements):
        lines.append(f"[{idx}] <{el['tag']}> {el['label']}")
    lines.append("")
    lines.append("Respond with the JSON object described above, referring to elements by their [index].")
    return "\n".join(lines)


COLOR_VISION_NOTE = """The attached image is a screenshot of the current page with a deuteranopia
(red-green colorblindness) simulation applied, showing approximately what you actually perceive --
not what a person with typical color vision sees. If two things that should look visually
different (a status pill, a form-validation color, a chart legend, a "required" marker) look the
same or nearly the same to you in this image, that IS a real confusion/finding: say so in
"confusion_reason" and suggest a fix that doesn't rely on hue alone (an icon, a label, a pattern,
not just "make it a different color a colorblind person can also tell apart," which restates the
problem instead of solving it)."""

# Found live 2026-08-29: a persona clicking a real, working button on a page that draws to a
# <canvas> (the QSL Card Designer's "Add Field") reported "nothing visibly changed," because the
# visible-text extraction genuinely can't see into canvas-drawn pixels -- indistinguishable from
# the button being silently broken (it also, separately, happened to be). Attaching a real
# screenshot whenever the page has a canvas on it -- not just for the deuteranopia persona -- lets
# the executor actually see whether an action changed anything.
CANVAS_SCREENSHOT_NOTE = """This page has a drawing/editing canvas on it (listed above as an
element you can click or drag). The attached screenshot shows what's actually drawn there right
now -- the page's visible TEXT above cannot show canvas contents at all, so use the image, not the
text, to judge whether your last action actually changed anything on the canvas."""

# Found live 2026-08-29 running the screen_reader_user persona for the first
# time against a canvas-containing page (the Web Shack console): run_leg()
# attached a real screenshot and told the persona to "View it directly
# before answering" even though that persona is explicitly blind -- an
# instruction that breaks character and is simply impossible to follow, the
# opposite of what a canvas page actually means for this persona (a real
# blind screen-reader user cannot use an unlabeled canvas control at all).
# no_visual_access opts a persona file out of every screenshot this daemon
# would otherwise attach (colorblind simulation included -- a blind persona
# gets no use out of a deuteranopia-simulated image either), matching the
# same "colorblind_coded_daemon.log"/"colorblind" opt-in-per-persona-file
# pattern color_vision_simulation and mobile_device already establish.
CANVAS_NO_VISUAL_ACCESS_NOTE = """This page has a drawing/editing canvas on it (listed above as an
element you can click or drag). You are blind and cannot see it at all -- there is no screenshot
for you, and the page's visible TEXT cannot describe canvas contents either. If this canvas has no
real text alternative (an aria-live status region, a text description of what's drawn), treat it as
effectively unusable to you and say so as a real point of confusion, not something to guess at."""


def select_image_note(has_canvas, color_vision_simulation, no_visual_access):
    """Pure decision of whether a step should capture a screenshot at all, and
    which textual image_note (if any) accompanies the prompt. Kept
    side-effect-free (no real Playwright screenshot call here) specifically
    so this is real-unit-testable without a live page -- matches the
    "pure function specifically so this is real-unit-testable" pattern
    already established for is_local_network_ip in the Rust relay daemon.
    Returns (should_capture_screenshot: bool, image_note: str | None)."""
    if no_visual_access:
        # A blind persona gets no use out of any screenshot, deuteranopia-
        # simulated or otherwise -- checked first so it always wins over
        # color_vision_simulation regardless of persona-file ordering.
        return False, (CANVAS_NO_VISUAL_ACCESS_NOTE if has_canvas else None)
    if color_vision_simulation:
        note = COLOR_VISION_NOTE
        if has_canvas:
            # A deuteranopia-simulated screenshot is still an accurate
            # picture of what's on the canvas (only the colors are
            # altered, not the shapes/positions), so the colorblind
            # persona needs the same "the text can't show this" warning
            # a sighted persona gets on a canvas page -- without this,
            # this persona on the QSL Designer would get an image but
            # never be told the visible text can't reflect canvas state.
            note = f"{COLOR_VISION_NOTE}\n\n{CANVAS_SCREENSHOT_NOTE}"
        return True, note
    if has_canvas:
        return True, CANVAS_SCREENSHOT_NOTE
    return False, None


def run_leg(page, model, persona_desc, goal, base_url, max_steps, log_fh, color_vision_simulation=False, out_dir="/tmp", leg_id="leg", no_visual_access=False):
    history = []
    consecutive_confused = 0
    for step in range(1, max_steps + 1):
        visible_text, elements = extract_page_state(page)
        has_canvas = any(el["tag"] == "canvas" for el in elements)
        should_capture, image_note = select_image_note(has_canvas, color_vision_simulation, no_visual_access)
        image_bytes = None
        if should_capture:
            image_bytes = simulate_deuteranopia(page.screenshot()) if color_vision_simulation else page.screenshot()
        prompt = build_prompt(persona_desc, goal, history, visible_text, elements, page.url, image_note)
        try:
            # Found live 2026-08-28: a flat 600s timeout lost two consecutive legs of a
            # real colorblind-persona run to timeouts an image-free (text-only) run never
            # hit -- viewing and reasoning about an attached screenshot genuinely takes an
            # executor longer than reading a text-only prompt, and a redirect-to-the-right-
            # queue message (needed when a prior leg was lost) eats further into whatever's
            # left of the window. Give image-based steps real headroom instead of the same
            # budget as text-only ones. Applies equally to the canvas-screenshot case added
            # later the same night -- same image-reasoning cost, same fix.
            step_timeout = 900 if (color_vision_simulation or has_canvas) else 600
            decision = ask_executor(
                model, prompt, timeout=step_timeout, image_bytes=image_bytes, out_dir=out_dir, step_id=f"{leg_id}_{step}"
            )
        except (TimeoutError, OSError, json.JSONDecodeError, IndexError) as e:
            _logger.error("Executor call failed at step %d: %s", step, e)
            record = {"ts": now_iso(), "step": step, "url": page.url, "error": str(e)}
            log_fh.write(json.dumps(record) + "\n")
            log_fh.flush()
            break

        record = {
            "ts": now_iso(),
            "step": step,
            "url": page.url,
            "thought": decision.get("thought"),
            "action": decision.get("action"),
            "confused": bool(decision.get("confused")),
            "confusion_reason": decision.get("confusion_reason"),
            "suggestion": decision.get("suggestion"),
        }
        log_fh.write(json.dumps(record) + "\n")
        log_fh.flush()
        _logger.info(
            "[step %d] %s | action=%s confused=%s", step, page.url, decision.get("action"), decision.get("confused")
        )

        action = decision.get("action")
        if action == "goal_complete":
            history.append(f"Reached the goal: {decision.get('thought')}")
            break
        if action == "give_up_on_leg":
            history.append(f"Gave up: {decision.get('confusion_reason') or decision.get('thought')}")
            break

        consecutive_confused = consecutive_confused + 1 if decision.get("confused") else 0
        if consecutive_confused >= CONSECUTIVE_CONFUSION_LIMIT:
            history.append(f"Stuck {CONSECUTIVE_CONFUSION_LIMIT} steps in a row, moving on: {decision.get('confusion_reason')}")
            record = {"ts": now_iso(), "step": step, "url": page.url, "auto_stop": "consecutive_confusion_limit"}
            log_fh.write(json.dumps(record) + "\n")
            log_fh.flush()
            break

        idx = decision.get("target_index")
        try:
            if action == "click" and idx is not None:
                locator = page.locator(INTERACTIVE_ELEMENTS_SELECTOR).nth(
                    elements[idx]["_locator_index"]
                )
                locator.click(timeout=5000)
                history.append(f"Clicked '{elements[idx]['label']}': {decision.get('thought')}")
            elif action == "type" and idx is not None:
                locator = page.locator(INTERACTIVE_ELEMENTS_SELECTOR).nth(
                    elements[idx]["_locator_index"]
                )
                # Found live 2026-08-29 running the club_membership persona: a real
                # <select> dropdown (choosing a club to apply to) is a genuine,
                # fillable-in-spirit form control, but Playwright's .fill() only
                # works on <input>/<textarea>/[contenteditable] and raises for a
                # <select> ("Element is not an <input>, <textarea> or
                # [contenteditable] element") -- the daemon reported this as the
                # persona's own action failing, when it was really this dispatch
                # code never having a path for dropdowns at all.
                tag = locator.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    locator.select_option(label=decision.get("type_value") or "", timeout=5000)
                else:
                    locator.fill(decision.get("type_value") or "", timeout=5000)
                history.append(f"Typed into '{elements[idx]['label']}': {decision.get('thought')}")
            elif action == "drag" and idx is not None:
                # Coarse by design: the persona has no way to see or specify exact
                # pixel coordinates (extract_page_state() reports only text/labels,
                # never positions), so this can't reproduce precise placement --
                # but it can genuinely answer "is this thing draggable at all,"
                # which is the actual audit-relevant question for a page like the
                # QSL Card Designer that this action exists to finally test.
                locator = page.locator(INTERACTIVE_ELEMENTS_SELECTOR).nth(
                    elements[idx]["_locator_index"]
                )
                box = locator.bounding_box()
                if not box:
                    history.append(f"Tried to drag '{elements[idx]['label']}' but it has no visible position.")
                    continue
                start_x = box["x"] + box["width"] / 2
                start_y = box["y"] + box["height"] / 2
                direction = (decision.get("type_value") or "").strip().lower()
                dx, dy = {"up": (0, -80), "down": (0, 80), "left": (-80, 0), "right": (80, 0)}.get(
                    direction, (80, 80)
                )
                page.mouse.move(start_x, start_y)
                page.mouse.down()
                page.mouse.move(start_x + dx, start_y + dy, steps=10)
                page.mouse.up()
                history.append(
                    f"Dragged within '{elements[idx]['label']}' toward {direction or 'a nearby spot'}: {decision.get('thought')}"
                )
            elif action == "navigate_back":
                page.go_back(timeout=5000)
                history.append("Went back to the previous page.")
            elif action == "wait":
                # Found live 2026-08-29: an executor answering steps by hand (no live
                # Gemini Conductor) has real response latency that can land right inside
                # a page's own slow-init window, so re-clicking the same nav link out of
                # impatience restarts that page's init before it ever finishes -- turning
                # a genuine, bounded ~3.5s load into an apparent infinite hang across
                # several steps. Giving the executor an explicit way to just wait removes
                # the incentive to re-click.
                page.wait_for_timeout(3000)
                history.append("Waited a few seconds for the page to finish loading.")
            else:
                history.append(f"Unrecognized/unsupported action '{action}', stayed put.")
        except Exception as e:
            _logger.warning("Action execution failed at step %d: %s", step, e)
            history.append(f"Tried to {action} but it didn't work ({e}).")
            continue

        try:
            # Found live 2026-08-29: this settle-wait used to be inside the same
            # try/except as the click/fill/go_back call above, so a page with any
            # continuing background network traffic (a live spot feed poll, a
            # presence heartbeat, a space-weather ticker -- exactly what a real-time
            # ham console like /shack has running constantly) NEVER reaches
            # "networkidle" within 5s, and the resulting TimeoutError got
            # misattributed to the action itself. Every click/type on such a page
            # was logged as "didn't work" -- a false negative in the audit tool,
            # not a site bug -- even when the action had already succeeded a line
            # above. Confirmed directly: the exception's own attached Playwright
            # call log showed "domcontentloaded" and "load" already fired, i.e.
            # the page loaded fine; only the idle-network wait timed out. Give the
            # DOM a moment to settle, but never let this alone mark the action
            # as failed.
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass
    return history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Root URL of the running hams.com instance to audit")
    parser.add_argument("--persona-file", required=True, help="Path to a JSON file: {\"persona\": str, \"legs\": [str, ...]}")
    parser.add_argument("--out-dir", required=True, help="Directory to write the run's critique log and summary into")
    parser.add_argument("--max-steps-per-leg", type=int, default=20)
    parser.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", GEMINI_MODEL_DEFAULT))
    args = parser.parse_args()

    with open(args.persona_file) as f:
        persona_spec = json.load(f)
    persona_desc = persona_spec["persona"]
    legs = persona_spec["legs"]
    color_vision_simulation = bool(persona_spec.get("color_vision_simulation"))
    mobile_device = persona_spec.get("mobile_device")
    no_visual_access = bool(persona_spec.get("no_visual_access"))

    os.makedirs(args.out_dir, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(args.out_dir, f"run_{run_id}.jsonl")

    with sync_playwright() as p, open(log_path, "w") as log_fh:
        browser = p.chromium.launch(headless=True)
        # A persona file may set "mobile_device" to a real Playwright device
        # name (e.g. "Pixel 5" -- see playwright.devices for the full list)
        # to emulate a real phone's viewport, user agent, and touch input,
        # rather than always running at Chromium's desktop default. Same
        # opt-in-per-persona-file pattern as color_vision_simulation above.
        # new_context(**p.devices[name]) is Playwright's own documented
        # device-emulation idiom -- it works with any device descriptor
        # regardless of that device's own "default_browser_type" (this
        # daemon always launches Chromium above; a device whose real-world
        # counterpart Playwright maps to WebKit, e.g. any iPhone, still gets
        # its viewport/UA/touch metadata applied faithfully here, just
        # rendered by Chromium's engine rather than WebKit's -- pick a
        # Chromium-mapped device, e.g. an Android one, for full engine
        # fidelity too).
        page = new_page_for_run(p, browser, mobile_device)
        page.on("dialog", log_and_dismiss_dialog)

        for leg_num, goal in enumerate(legs, start=1):
            # Found live 2026-08-28/29: base_url was only navigated to once,
            # before the loop. A leg that ended via give_up_on_leg (or any
            # leg at all, really) leaves the page wherever that leg's last
            # step happened to land -- so every subsequent leg silently
            # started from a stale, unrelated page instead of a fresh
            # starting point a real user would actually have (reloading the
            # site, or at least the homepage) for a brand-new task. Confirmed
            # as the cause of a real run where legs 2-4 all "gave up" within
            # 1-2 steps, still logged against leg 1's own dead-end URL.
            page.goto(args.base_url, timeout=30000)
            _logger.info("=== Leg %d/%d: %s ===", leg_num, len(legs), goal)
            log_fh.write(json.dumps({"ts": now_iso(), "leg_start": leg_num, "goal": goal}) + "\n")
            log_fh.flush()
            run_leg(
                page,
                args.gemini_model,
                persona_desc,
                goal,
                args.base_url,
                args.max_steps_per_leg,
                log_fh,
                color_vision_simulation=color_vision_simulation,
                out_dir=args.out_dir,
                leg_id=f"run{run_id}_leg{leg_num}",
                no_visual_access=no_visual_access,
            )

        browser.close()

    _logger.info("Run complete. Critique log: %s", log_path)
    print(log_path)


if __name__ == "__main__":
    main()
