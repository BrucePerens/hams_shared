# Debugging Odoo UI Tours with Antigravity

A UI tour is an Odoo browser test that replays a user's clicks and checks what the page shows. Tours are brittle and hard to debug from static screenshots, so you can use the **Antigravity** AI assistant (Google's AI coding assistant) to debug a failing tour interactively, while the browser is still open.

Unlike Jules (Google's coding agent, which works autonomously in the background), Antigravity is an interactive pair-programming partner. With the `chrome-devtools` MCP (Model Context Protocol) integration it can attach to a frozen Chrome instance and inspect it as you would with Chrome Developer Tools.

## Step 1: Run the Test with `--pause-on-fail`

When a tour is flaky or failing, run the test runner with the `--pause-on-fail` flag:

```bash
python3 hams_shared/tools/test.py -u <your_module> --pause-on-fail
```

**What this does:**
1. It starts Chrome with a window instead of headless (so the machine needs a display) and exposes Chrome's remote debugging port on `9222`.
2. If the tour runner catches a Javascript exception or fails to find a DOM element, the framework will catch the exception, print a `🛑 TOUR FAILED!` warning, and freeze the browser completely (instead of killing the environment).

*(Tip: `--mcp` launches the MCP test server instead of running the tests and shutting down; see `agents/skills/mcp-test-runner/SKILL.md`.)*

## Step 2: Ask Antigravity to Investigate

Once the terminal prints `🛑 TOUR FAILED! Pausing indefinitely (--pause-on-fail active)`, switch over to your chat with Antigravity and ask for help. 

**Example Prompts:**
* *"Hey Antigravity, my Odoo tour just failed and the browser is paused on port 9222. Can you connect to the DevTools MCP, inspect the DOM, and tell me why the 'Save' button wasn't clicked?"*
* *"Antigravity, the tour failed. Please connect to Chrome DevTools, check the browser console for Javascript errors, and evaluate the current DOM state to see if a modal is blocking the screen."*

## Step 3: Interactive Debugging

Once Antigravity connects to port 9222, it can:
* **Read the DOM:** Search for the specific nodes your tour was trying to click.
* **Diagnose Z-Index / Overlays:** Tell you if a `.modal-backdrop` or loading spinner is intercepting the click.
* **Evaluate JS Console:** Run native Javascript to test alternative selectors (e.g. `document.querySelector('button[name="action_save"]')`) without having to restart the 2-minute test suite.

## Step 4: Iterative Fixes

Once Antigravity identifies the issue (for instance, a race condition where the tour didn't wait for a Notification to fade):
1. Ask Antigravity to write the correct patch in your `.js` tour file (e.g., adding `TourUtils.waitForAbsence('.o_notification')`).
2. Kill the paused test runner (Ctrl+C).
3. Re-run the test to verify the fix.

By utilizing `--pause-on-fail` and Antigravity's interactive DevTools skill, you can turn hours of frustrating "guess-and-check" headless test debugging into a fast, interactive session.
