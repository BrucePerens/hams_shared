# Simulated-newcomer usability audit

Implements `docs/proposals/USABILITY_AUDIT_SIMULATED_HAM.md`: a real headless browser, driven by
a non-technical-ham persona, walking the live site and producing structured, timestamped
critique. See that proposal for the full rationale and the open questions it deliberately leaves
unsettled (persona count/cadence, where critique gets triaged, whether to allow recovery attempts
-- this implementation picks the conservative default of giving up a leg after
`CONSECUTIVE_CONFUSION_LIMIT` (3) confused steps in a row, to keep each run's signal comparable).

This is deliberately **not** a Claude Agent/fork dispatch. Per
`.claude/skills/avoiding-api-costs/SKILL.md`, it's a standalone daemon that calls Gemini directly
per decision point (Pattern A in that skill) -- which also structurally enforces the proposal's
"no implementation knowledge" constraint, since the model behind the persona is only ever shown
the current page's own visible text and interactive elements, never this repository.

## Running it

```
export GEMINI_API_KEY=<same value stored in ir.config_parameter's gemini.api_key>
python3 tools/usability_audit/usability_audit_daemon.py \
  --base-url http://127.0.0.1:8069 \
  --persona-file tools/usability_audit/personas/newcomer_technician.json \
  --out-dir /tmp/usability_audit_runs \
  --max-steps-per-leg 20
```

Needs a real running hams.com instance at `--base-url` (any environment -- point it at a local
dev server, a staging deploy, whatever you want audited) and Playwright's Chromium installed
(`playwright install chromium`, already present in this dev environment).

## Output

One `run_<timestamp>.jsonl` file per run in `--out-dir`, one JSON object per line: a `leg_start`
marker per journey leg, then one record per step with the persona's `thought`, the `action` it
took, whether it was `confused`, why, and its `suggestion`. Read it end to end for a single run's
story, or diff two runs' step counts/confusion rates for the same persona file to see whether a
round of fixes actually made things easier -- the comparison the proposal cares about, not just a
one-off report.

## Personas

Two personas exist so far. Each persona file is
`{"persona": "<description>", "legs": ["<goal 1>", "<goal 2>", ...]}`; add more files here for the
other personas `USABILITY_AUDIT_SIMULATED_HAM.md` suggests (an Extra-class ham with decades of RF
experience but low recent computer literacy; someone attempting the whole flow from a
tablet/Chromebook) rather than assuming one persona covers the range.

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

## Triage

Not yet built: turning a run's `.jsonl` into a filed backlog item per real finding. For now, read
the log and act on it directly -- the proposal leaves "where the critique record lives and how
it's triaged" as an open question this first implementation doesn't settle either.
