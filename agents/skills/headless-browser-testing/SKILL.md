---
name: headless-browser-testing
description: Instructions on how to spin up a headless Chrome browser natively within the agent sandbox to test web rendering, avoiding network namespace or 127.0.0.1 routing issues.
---

# Headless Browser Testing within Antigravity

When you need to test the visual rendering of a web page (e.g., HTML, CSS, custom components) and observe the output via a screenshot, you might encounter issues reaching `127.0.0.1` if you rely on the `browser_subagent` or external test scripts like Odoo's `tools/test.py`.

**Why does this happen?**
`tools/test.py` and other test runners often use `unshare` to isolate processes into separate network and PID namespaces. Since the Antigravity sandbox runs as a standard user within its own container, it lacks the `CAP_SYS_ADMIN` privileges required to follow those namespace isolation commands. Furthermore, if a server is running on the physical host machine, the sandbox's `127.0.0.1` loopback interface cannot route to the host's loopback.

## The Native Solution

To successfully test web rendering and capture a screenshot, run everything manually within your own single shared sandbox namespace.

### Step 1: Start a Local Server
If you need to serve HTML files (especially if they fetch local assets via XHR/fetch), start a background Python server directly in your current working directory:

```bash
python3 -m http.server 8089
```
*(Launch this as a background task via the `run_command` tool)*

### Step 2: Create a Test HTML File
Generate a minimal `.html` file that includes the components and CSS you wish to test.

### Step 3: Screenshot via Native Chrome
Instead of relying on the `browser_subagent`, execute the native Chrome binary via the `run_headless_chrome.py` wrapper script. This script provides a lock to prevent concurrent executions and ensures robust process cleanup to prevent memory exhaustion from zombie browsers:

```bash
/home/bruce/workspace/hams_open/hams_shared/scripts/run_headless_chrome.py --headless=new --no-sandbox --disable-gpu --screenshot=/home/bruce/.gemini/antigravity/brain/<conversation-id>/screenshot.png http://127.0.0.1:8089/test_render.html
```

*(Note: Always use `--no-sandbox` and `--disable-gpu` when running Chrome inside a container shell. If you receive an error that "Sequential execution only is required," wait a few seconds and try again, or check your background tasks.)*

### Step 4: Verification
You can then embed the resulting `screenshot.png` directly into your markdown artifacts using standard image syntax: `![Screenshot](...)`. 

By ensuring both the web server and the browser run inside the exact same Bash environment, `127.0.0.1` will always resolve perfectly, completely sidestepping cross-namespace boundaries.
