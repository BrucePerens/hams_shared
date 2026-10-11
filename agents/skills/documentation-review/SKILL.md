---
name: "documentation-review"
description: "How to execute a comprehensive codebase documentation review using the divide-and-conquer architecture."
---

# Documentation Review Orchestration

This skill utilizes the `divide-and-conquer` architectural pattern to systematically review and generate documentation for Odoo modules.
For a full breakdown of the agent interactions, refer to `divide-and-conquer/SKILL.md`.

## Topic Configuration

**Topic:** Documentation Verification.

For each module, the `divide-and-conquer` swarm must review all code. 
They are to ensure that the `README.md` documents all functions of the module and how developers (including AIs) are to use them.
They are to make sure that the documents under `data/` document all UI functionality, both for users and admins.
If `README.md` or `documentation.html` do not exist, they MUST generate entirely new ones.

**No placeholder screenshots (Bruce, 2026-10-11).** When generating or revising `documentation.html`,
never add an `<img>` unless it is a real screenshot of hams.com's own UI of that module, and never a
"Below are representative screenshots of the user interface" lead-in. A page without a real
screenshot has no image: do not generate a mock-up, copy `form_screenshot.png` / `list_screenshot.png`
/ `map_screenshot.png` from another module, or use a stock or AI-made picture. Page candidates that
would benefit from a real screenshot go in the report. `tools/check_doc_images.py` (a `run_linters.py`
step) fails the build on those names, on denylisted images, on missing files, and on one image shown
by three or more modules.

**CRITICAL ADDITION: Index File Synchronization**
Any new features or documentation discovered/written for a module MUST be synchronized by updating the central index files located in the `ham_base` module:
- `hams_com/ham_base/data/backend_features.html`
- `hams_com/ham_base/data/portal_features.html`
- `hams_com/ham_base/data/portal_features_order_of_interest.html`

Check-ins MUST describe modifications per-file.

## Discovery Command
`find hams_open hams_com -name "__manifest__.py" -not -path "*/.git/*"`

## Validation Commands
Since this is a documentation task, unit testing is not strictly required, but linters must still be run:
`python3 tools/run_linters.py <files>`

## Sub-agent Roles
1. **Reviewers**: Spawn Reviewers specialized in checking source code against the `README.md` and `data/` HTML requirements.
2. **Shamus**: Must act as the documentation gatekeeper, ensuring the generated text accurately reflects the source code before fixes are applied.

## Night Shift Mode
You may optionally combine this skill with `night-shift` for massive, unattended repository sweeps overnight.
