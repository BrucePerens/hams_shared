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
import io
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone

from PIL import Image
from playwright.sync_api import sync_playwright

_logger = logging.getLogger("usability_audit_daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GEMINI_MODEL_DEFAULT = "gemini-3.7-flash"
MAX_PAGE_TEXT_CHARS = 6000
MAX_INTERACTIVE_ELEMENTS = 60
MAX_HISTORY_ENTRIES_IN_PROMPT = 8
CONSECUTIVE_CONFUSION_LIMIT = 3  # give up a leg after this many confused steps in a row

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
- "action": one of "click", "type", "navigate_back", "give_up_on_leg", "goal_complete"
- "target_index": integer index into the numbered element list (for "click" or "type"), or null
- "type_value": string to type (only for "type"), or null
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


_WATCHDOG_BRIDGE_SOCK = os.path.expanduser("~/workspace/tmp/mcp_watchdog_ipc.sock")


def _send_ipc_message(queue_name, content):
    """Dependency-free client for mcp_watchdog.py's own "legacy bridge" -- the same mechanism
    ingest/daemon_utils.py's send_ipc_message() uses for plain (non-MCP-agent) Python processes.
    Wire format: connect, write "<queue_name>\\n<content>", close. Deliberately reimplemented
    inline here rather than importing ingest/daemon_utils.py, to avoid a hams_shared -> hams_com
    cross-repo import for three lines of socket code."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(_WATCHDOG_BRIDGE_SOCK)
    try:
        sock.sendall(f"{queue_name}\n{content}".encode("utf-8"))
    finally:
        sock.close()


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
    locator = page.locator("a, button, input, textarea, select, [role=button], [onclick]")
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
                label = (
                    el.get_attribute("aria-label")
                    or el.get_attribute("placeholder")
                    or el.get_attribute("value")
                    or el.get_attribute("title")
                    or ""
                ).strip()
            if not label:
                # A real, previously-undetected gap: a properly-accessible input with a
                # real <label for="id">text</label> (exactly the pattern a real sighted
                # user actually reads) but no aria-label/placeholder/value/title of its
                # own -- e.g. hams.com's own real signup form's password fields -- fell
                # through every fallback above and was silently dropped, making the
                # persona unable to see a field a real user can see fine. Found live,
                # 2026-08-26: a real audit run reported "no password box" against a page
                # that genuinely has one, traced to exactly this gap. Mirrors how a
                # sighted user actually resolves an unlabeled input's name.
                el_id = (el.get_attribute("id") or "").strip()
                if el_id:
                    try:
                        label_loc = page.locator(f'label[for="{el_id}"]').first
                        if label_loc.count() > 0:
                            label = (label_loc.inner_text() or "").strip()
                    except Exception:
                        pass
            if not label:
                continue
            elements.append({"tag": tag, "label": label[:120], "_locator_index": i})
        except Exception:
            continue
    return visible_text, elements


def build_prompt(persona_desc, goal, history, visible_text, elements, current_url, color_vision_note=None):
    lines = [PERSONA_SYSTEM_PREAMBLE, "", f"YOUR PERSONA: {persona_desc}", "", f"YOUR GOAL RIGHT NOW: {goal}", ""]
    if color_vision_note:
        lines.append(color_vision_note)
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


def run_leg(page, model, persona_desc, goal, base_url, max_steps, log_fh, color_vision_simulation=False, out_dir="/tmp", leg_id="leg"):
    history = []
    consecutive_confused = 0
    for step in range(1, max_steps + 1):
        visible_text, elements = extract_page_state(page)
        image_bytes = None
        color_vision_note = None
        if color_vision_simulation:
            image_bytes = simulate_deuteranopia(page.screenshot())
            color_vision_note = COLOR_VISION_NOTE
        prompt = build_prompt(persona_desc, goal, history, visible_text, elements, page.url, color_vision_note)
        try:
            decision = ask_executor(
                model, prompt, timeout=600, image_bytes=image_bytes, out_dir=out_dir, step_id=f"{leg_id}_{step}"
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
                locator = page.locator("a, button, input, textarea, select, [role=button], [onclick]").nth(
                    elements[idx]["_locator_index"]
                )
                locator.click(timeout=5000)
                history.append(f"Clicked '{elements[idx]['label']}': {decision.get('thought')}")
            elif action == "type" and idx is not None:
                locator = page.locator("a, button, input, textarea, select, [role=button], [onclick]").nth(
                    elements[idx]["_locator_index"]
                )
                locator.fill(decision.get("type_value") or "", timeout=5000)
                history.append(f"Typed into '{elements[idx]['label']}': {decision.get('thought')}")
            elif action == "navigate_back":
                page.go_back(timeout=5000)
                history.append("Went back to the previous page.")
            else:
                history.append(f"Unrecognized/unsupported action '{action}', stayed put.")
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception as e:
            _logger.warning("Action execution failed at step %d: %s", step, e)
            history.append(f"Tried to {action} but it didn't work ({e}).")
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

    os.makedirs(args.out_dir, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(args.out_dir, f"run_{run_id}.jsonl")

    with sync_playwright() as p, open(log_path, "w") as log_fh:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(args.base_url, timeout=30000)

        for leg_num, goal in enumerate(legs, start=1):
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
            )

        browser.close()

    _logger.info("Run complete. Critique log: %s", log_path)
    print(log_path)


if __name__ == "__main__":
    main()
