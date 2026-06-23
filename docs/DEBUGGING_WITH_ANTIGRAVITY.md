# Debugging Odoo UI Tours with Antigravity

Because Javascript UI Tours in Odoo are notoriously brittle and difficult to debug through static screenshots, you can leverage the **Antigravity** AI assistant to interactively debug failing tours in real-time. 

Unlike Jules (which operates autonomously and often in a CI/batch context), Antigravity is a highly interactive pair-programming partner equipped with the `chrome-devtools` MCP integration. This allows Antigravity to attach directly to a frozen, headless Chrome instance and inspect it exactly as you would with normal Chrome Developer Tools.

## Step 1: Run the Test with `--pause-on-fail`

When a tour is flaky or failing, run the test runner with the `--pause-on-fail` flag:

```bash
python3 tools/test.py -u <your_module> --pause-on-fail
```

**What this does:**
1. It exposes Chrome's remote debugging port on `9222`.
2. If the tour runner catches a Javascript exception or fails to find a DOM element, the framework will catch the exception, print a `🛑 TOUR FAILED!` warning, and freeze the browser completely (instead of killing the environment).

*(Tip: You can combine this with `--mcp` to keep the Python test server alive too).*

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
