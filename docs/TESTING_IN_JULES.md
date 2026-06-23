# Testing in the Jules VM Environment

## Prerequisites

IN_JULES_VM=1 is set in the environment to tell programs they are in the Jules VM,
you need not provide it on the command-line.

## Running the Test Program

```bash
tools/test.py
```

## Targeting Specific Modules (-u flag)

By default, the test runner executes against all local modules. To restrict the testing scope to a single module (which saves significant time), use the `-u <module_name>` flag.

This flag works globally across **all** execution modes (`standard`, `xml`, `downloads`).

**Examples:**

Run the test suite but ONLY for the `user_websites` module:
```bash
tools/test.py -u user_websites
```

## Handling UI Tour Failures & Headless Chrome Watchdogs

> **NOTICE:** For the exhaustive, centralized guide on constructing resilient UI tours, you MUST consult [docs/LLM_WRITING_TOURS.md](LLM_WRITING_TOURS.md).

**CRITICAL:** The platform natively patches `HttpCase.browser_js` to suppress fatal headless Chrome teardown crashes (e.g., `Fetch API Error`, "Owl un-mounted component", and "element below a modal"). **You MUST NOT use `@unittest.skip` to evade UI tour failures.** If a tour fails, you must fix the underlying JavaScript logic or race condition.

Due to the asynchronous nature of Odoo 19's Owl UI framework, tours can still suffer from race conditions where the tour executor attempts to click elements before they are fully rendered (especially modals and wizards).

To guarantee architectural compliance and stabilize the build, you MUST utilize the centralized DOM wait macros provided by `zero_sudo`.

**Import the Utilities:**
```javascript
import { TourUtils } from "@zero_sudo/js/tour_utils";
```

**Available Wait Macros:**
* `TourUtils.waitForAbsence(selector, description)`: Pauses the tour until the element is entirely removed from the DOM (e.g., waiting for an RPC loading overlay to vanish).

**Usage Example:**
```javascript
steps: () => [
    {
        content: "Click open wizard",
        trigger: 'button[name="action_confirm"]',
        run: 'click',
    },
    { trigger: '.modal-dialog', run: function() {} }, // Native DOM polling
    {
        content: "Interact with wizard",
        trigger: 'button[name="action_confirm"]',
        run: 'click',
    }
]
```

## Linter and Anchor Verification (AI Guidance)

When operating in this environment, any code generated is strictly audited by two custom DevSecOps tools. You should actively test your output against these tools:

* **`tools/check_burn_list.py`**: A strict AST (Abstract Syntax Tree) linter that enforces architectural mandates (e.g., Zero-Sudo, safe dynamic SQL, UI tour stability).
* **`tools/verify_anchors.py`**: A script that enforces bidirectional traceability between source code, tests, and documentation using `[@ANCHOR: ...]` tags.

**CRITICAL GUIDANCE FOR AI ASSISTANTS:** If you encounter a confusing error or failure from either of these scripts in your test logs, **DO NOT GUESS**. You possess the ability to read files. You MUST autonomously fetch and read the source code of `tools/check_burn_list.py` and `tools/verify_anchors.py`.

Their internal source code contains explicit `[!] DIAGNOSTIC FOR AI` messages, regex patterns, and detailed AST matching logic. Reading their source code will instantly explain exactly what formatting or architectural rule you violated and how to correct it.
