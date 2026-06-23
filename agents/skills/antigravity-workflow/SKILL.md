---
name: antigravity-workflow
description: Workflow and git commit mandates for the Antigravity agent working on the hams_community project.
---

# Antigravity Workflow Mandates

1. **Automatic Git Commits:** When you complete a task or a logical chunk of work that represents a stable, working state (e.g., after fixing bugs or refactoring), you MUST automatically stage and commit the changes using `git add .` and `git commit -m "..."`. Do not wait for the user to explicitly ask you to commit.
2. **Clear Commit Messages:** Write clear, descriptive commit messages summarizing the technical changes and the rationale behind them.
3. **Test Execution & Debugging:**
- Execute the test suite using `run_command` with `python3 tools/test.py`.
- **CRITICAL**: Do not wait for the tests synchronously in a loop. Use `WaitMsBeforeAsync: 500` or similar so that the test runner executes in the background. Stop calling tools and let the system notify you when the task completes.
- If the tests fail, **DO NOT** attempt to read the entire task output or the full Odoo log file, as it will exhaust your context window. Instead, read the pre-filtered summary at `/home/bruce/tmp/filtered_test.txt` using the `view_file` tool to see only the tracebacks and failures.
- If the test fails, use your planning and execution workflow to diagnose and fix the issue. Keep the user informed of your progress.
4. **Turbo Mode Compliance:** If the user is operating in "turbo mode", prioritize execution velocity. Do not block execution by asking for permission or feedback on plans unless it is an absolutely critical, dangerous, or irreversible architectural decision.
5. **Python Dependencies:** Use Debian (or Ubuntu) packages (e.g., `apt install python3-xyz`) for Python utilities preferentially over pip, to avoid externally managed environment conflicts.
6. **Ephemeral Files:** Any scratch files, test scripts, or ephemeral files you create during execution MUST be placed in a `tmp/` directory within the repository. You should create this directory if it doesn't exist. Git is configured to ignore it.
