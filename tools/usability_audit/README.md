# Simulated-newcomer usability audit

Implements `docs/proposals/USABILITY_AUDIT_SIMULATED_HAM.md`: a real headless browser, driven by
a non-technical-ham persona, walking the live site and producing structured, timestamped
critique. See that proposal for the full rationale and the open questions it deliberately leaves
unsettled (persona count/cadence, where critique gets triaged, whether to allow recovery attempts
-- this implementation picks the conservative default of giving up a leg after
`CONSECUTIVE_CONFUSION_LIMIT` (3) confused steps in a row, to keep each run's signal comparable).

This is deliberately **not** a Claude Agent/fork dispatch. Per
`.claude/skills/avoiding-api-costs/SKILL.md`, it's a standalone daemon that routes each decision
point through this codebase's own established MCP scheme (`hams_shared/tools/mcp_watchdog.py`'s
IPC bridge, the same one `ingest/daemon_utils.py` uses for the course-content pipeline) rather
than a direct API call -- no `GEMINI_API_KEY` needed. `ask_executor()` writes the prompt to a
queue and blocks for a JSON response file; whichever agent answers (a fresh, isolated Gemini
Conductor subagent if one is live and listening, or the orchestrating Claude Code session itself
if not) is the real answerer. **Real, disclosed tradeoff**: the "no implementation knowledge"
persona constraint only structurally holds when a genuinely fresh, isolated executor answers --
if the orchestrating session ends up answering its own prompts, that session may have read this
repository. See `ask_executor()`'s own docstring and the skill file for the full nuance.

## Running it

```
python3 tools/usability_audit/usability_audit_daemon.py \
  --base-url http://127.0.0.1:8069 \
  --persona-file tools/usability_audit/personas/newcomer_technician.json \
  --out-dir /tmp/usability_audit_runs \
  --max-steps-per-leg 20
```

Needs a real running hams.com instance at `--base-url` (any environment -- point it at a local
dev server, a staging deploy, whatever you want audited), Playwright's Chromium installed
(`playwright install chromium`, already present in this dev environment), and something actually
listening on the `mcp_watchdog` IPC bridge to answer prompts -- either a live Gemini Conductor
session, or the orchestrating Claude Code session itself calling the `wait_for_inbox`/
`send_ipc_message` MCP tools directly and answering each `usability_audit_<step_id>` queue by
hand (reasoning in persona, writing the JSON response file the prompt names).

**Verified 2026-08-26 against a real local dev server (leg 1 of a `newcomer_technician` run
completed end to end, log content confirmed correct).** One real trap hit during that run, worth
knowing before debugging this again: there are two MCP servers exposing near-identical
`queue_status`/`wait_for_inbox`/`send_ipc_message` tools, `mcp__watchdog__*` and
`mcp__watchdog_shared__*`. The legacy bridge socket `_send_ipc_message()` writes to is served by
the **shared** one -- `queue_status` on the plain `mcp__watchdog__*` server will report
`"exists": false` for a queue the daemon genuinely wrote to, because that server holds its own
private, empty queue dict. Always use the `_shared` variants to answer this daemon's prompts.

## Output

One `run_<timestamp>.jsonl` file per run in `--out-dir`, one JSON object per line: a `leg_start`
marker per journey leg, then one record per step with the persona's `thought`, the `action` it
took, whether it was `confused`, why, and its `suggestion`. Read it end to end for a single run's
story, or diff two runs' step counts/confusion rates for the same persona file to see whether a
round of fixes actually made things easier -- the comparison the proposal cares about, not just a
one-off report.

## Personas

This README originally said "three personas exist so far" -- stale; there are now 18 covering a
wide range of member-side journeys plus accessibility variants (screen reader, colorblindness,
mobile). Each persona file is
`{"persona": "<description>", "legs": ["<goal 1>", "<goal 2>", ...]}` (optionally
`"color_vision_simulation": true`, `"no_visual_access": true`, or `"mobile_device": "<Playwright
device name>"`, see below). Notably, every one of those 18 personas is either already an existing
member or creates an account as its own first leg -- none of them represent a purely anonymous
visitor who never authenticates at all, which is its own real, distinct persona (see
`anonymous_external_visitor.json` below, added specifically to close that gap).

- `personas/anonymous_external_visitor.json` -- a first-time visitor who deliberately never signs
  up or logs in for the whole run, evaluating what's actually visible/usable to the public: whether
  the homepage explains the site's value with no account, what's genuinely browsable without
  authentication (callsign lookup, directory/map, repeater directory, events, forum, classifieds),
  what happens when they try account-specific pages anyway (a clean login prompt vs. a confusing
  wall vs. accidentally-exposed real data), and whether anything during the anonymous visit would
  make them hesitate before ever reaching sign-up. The daemon needs no special code for this --
  Playwright's `browser.new_context()` is already a fresh, cookie-free context per run by default,
  and no persona (including this one) has ever needed a daemon-level login step; "never sign up"
  is enforced purely through this persona's own natural-language instructions, the same way every
  other persona's goals (including "sign up for an account") already are. The `anonymous_visitor`
  key in its JSON file is documentary only, matching this codebase's existing incremental pattern
  for persona-specific flags -- it isn't read by any code path yet.

- `personas/newcomer_technician.json` -- a brand-new Technician-class licensee with heavy
  smartphone/social-media fluency but zero radio or networking background.
- `personas/screen_reader_user.json` -- a licensed ham who is blind and navigates entirely by
  screen reader and keyboard, per Bruce's explicit direction that serving hams with disabilities
  is a standing alternate path this audit pursues, not an afterthought. This persona's own prompt
  states it would never notice anything that exists only as a visual cue (color alone, an
  icon-only control with no accessible name, a hover-only tooltip) -- which is a real, separate
  finding channel from the newcomer persona: a page can be perfectly clear to a sighted newcomer
  and simultaneously unusable here. Worth noting: `extract_page_state()`'s element listing already
  falls back to `aria-label`/`title` when there's no visible text, which happens to double as a
  decent (if partial) approximation of what a screen reader exposes -- it does not yet reconstruct
  heading/landmark structure or true reading order, which a deeper accessibility pass would want.
- `personas/red_green_colorblind.json` -- a licensed ham with deuteranopia (red-green
  colorblindness), added per Bruce's explicit direction (he has red-green colorblindness himself).
  Setting `"color_vision_simulation": true` in a persona file makes `run_leg` capture a real
  screenshot at each step, apply a deuteranopia simulation matrix
  (`usability_audit_daemon.simulate_deuteranopia`, an sRGB-space approximation in the style of
  browser-based simulators like Coblis -- not clinically precise, but enough to catch red/green
  hue collapsing to the same perceived color), and send it to Gemini as inline image data
  alongside the usual text state, with an added instruction to flag anywhere meaning depends on
  red-vs-green hue alone. This is the one persona that needs the page's actual rendered colors,
  which the text-only extraction otherwise never captures at all.

## Triage

Not yet built: turning a run's `.jsonl` into a filed backlog item per real finding. For now, read
the log and act on it directly -- the proposal leaves "where the critique record lives and how
it's triaged" as an open question this first implementation doesn't settle either.
