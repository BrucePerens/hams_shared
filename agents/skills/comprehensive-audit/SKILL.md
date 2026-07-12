---
name: comprehensive-audit
description: "Systematically audits an Odoo 19 codebase for exceptions, security, and compliance by orchestrating specialized subagents."
---

# Comprehensive Codebase Audit

This skill provides an automated, long-running workflow to perform a deep codebase audit. 

## Workflow
When invoked, you MUST execute the following steps completely non-interactively:

1. **Discover Target Directories**:
   Use `list_dir` or a terminal command to find all directories containing Python code, Rust code, tests, daemons, tools, or UI assets inside the three core repositories:
   - `/home/bruce/workspace/hams_com` (Make sure to include `daemons/` and other non-module folders!)
   - `/home/bruce/workspace/hams_open`
   - `/home/bruce/workspace/hams_open/hams_shared`
   DO NOT discover only Odoo modules! There is lots of code that isn't part of
   an Odoo module in this code-base.

2. **Spawn Specialized Subagents**:
   For each discovered code directory, use the `invoke_subagent` tool to concurrently spawn twelve specialized subagents. You can invoke them in batches (e.g., 5-10 directories at a time) to avoid exceeding system concurrency limits. Keep track of the `conversationId` for each spawned subagent.

   **Subagent 1: The Exception Hunter**
   - **Role**: `Exception Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look strictly for test obfuscation, swallowed exceptions (bare excepts, except Exception without the # audit-ignore-catch-all bypass), and improper HTTPError handling. Tests should NOT swallow exceptions. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 2: The Security Hunter**
   - **Role**: `Security Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look strictly for compliance with access control best practices: ensure multi-tenant isolation is respected, flag any `sudo()` usage that lacks a documented architectural justification, verify access rules, and ensure database queries use parameterized ORM methods rather than raw string concatenation. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 3: The Compliance Hunter**
   - **Role**: `Compliance Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look strictly for Odoo 19 compliance and project-specific rules: missing 'name' fields (models must not use _rec_name) and soft-dependency 'try...except ImportError' evasions (modules must fast-fail). Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 4: The Licensing Hunter**
   - **Role**: `Licensing Hunter`
   - **Prompt**: "Audit the directory {module_path}. Code in `hams_com` must be proprietary (unless it's an external module like `radae`). Code in `hams_shared` and `hams_open` should be AGPL-3. Check file headers for the correct licensing string. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 5: The AI Laziness Hunter**
   - **Role**: `AI Laziness Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look strictly for AI laziness issues: incomplete code, placeholder `TODO`s indicating skipped logic, native `print()` calls (use logging instead), and empty `pass` blocks within exception handlers. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 6: The Modernization Hunter**
   - **Role**: `Modernization Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look for legacy patterns that must be modernized: replace legacy `_sql_constraints` with the new Odoo 19 `models.Constraint` API, replace legacy `_check_recursion()` with `self._has_cycle()`, ensure `@api.constrains` are robust, and find local/function-level imports (like `werkzeug`) that should be moved to the module level. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 7: The UI Appropriateness Hunter**
   - **Role**: `UI Appropriateness Hunter`
   - **Prompt**: "Audit the directory {module_path}. Check if UI code looks modern and user-friendly. For JS tours, look for missing DOM blur events after 'edit' steps (triggering action buttons immediately), and missing `expectUnloadPage: true` flags on form submissions. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 8: The Code Quality Hunter**
   - **Role**: `Code Quality Hunter`
   - **Prompt**: "Audit the directory {module_path}. Look for poor code quality and violations of best practices. Check for unreadable/unmaintainable logic, violation of DRY (Don't Repeat Yourself) principles, inefficient loops/queries affecting performance, and high-latency anti-patterns. Report any findings with precise file paths, line numbers, and snippets."

   **Subagent 9: The Target Community Hunter**
   - **Role**: `Target Community Hunter`
   - **Prompt**: "Audit the directory {module_path}. Evaluate the features, terminology, and workflows for appropriateness to the target user community. CRITICAL RULES: 1. Keep ham jargon OUT of the `hams_open` repository, as it is an Open Source repository for general use. 2. For proprietary modules in `hams_com` that are primarily for system admins, allow standard system admin jargon without worry about hams. 3. Only enforce ham jargon on user-facing modules in `hams_com`. Report any areas that violate these rules."

   **Subagent 10: The UI Quality Hunter**
   - **Role**: `UI Quality Hunter`
   - **Prompt**: "Audit the directory {module_path}. Review XML/QWeb templates and JS components for premium modern design aesthetics. Check for responsiveness, user-friendly layouts, intuitive workflows, and polish. Report any subpar UI elements or areas needing visual improvement."

   **Subagent 11: The Missing Features Hunter**
   - **Role**: `Missing Features Hunter`
   - **Prompt**: "Audit the directory {module_path}. Analyze the module's domain and purpose. Identify obvious missing features, incomplete integrations, or functionality gaps that could be easily filled to provide a more comprehensive and robust product. Report your feature suggestions clearly."

   **Subagent 12: The Rust Quality Hunter**
   - **Role**: `Rust Quality Hunter`
   - **Prompt**: "Audit the directory {module_path} for any Rust code (`.rs`, `Cargo.toml`). Look for unidiomatic Rust code, non-idiomatic use of `Result`/`Option`, excessive `unwrap()` or `panic!()` usage, missing memory safety considerations, or poor Cargo configuration. Report any findings with precise file paths, line numbers, and snippets."

3. **Consolidate Findings and Prevent Stalling (Asynchronous Processing)**:
   As the subagents report back, collect their findings into a master markdown artifact named `comprehensive_audit_report.md`.
   
   **Anti-Stall Protocol:** 
   - NEVER STOP to wait for a subagent. The system will wake you up automatically when they finish.
   - If a subagent doesn't reply, it might have crashed. Check `manage_subagents` if you suspect a hang, but otherwise, continue your work.
   - Stay busy! While subagents are running for Module A, you should be fixing the code for Module A based on reports you already received, or spawning subagents for Module B.
   - You can write and use python scripts (save them in `skills/comprehensive-audit/scripts/`) to help orchestrate fixes, refactoring, and state management.

4. **Fix the Identified Problems Automatically & Queue Reviews**:
   - Bypass standard planning mode blocking. DO NOT create an `implementation_plan.md` that waits for user approval.
   - For unambiguous findings (adding headers, fixing exception swallowing, using `zero_sudo`, optimizing loops), formulate and execute fixes immediately using your code editing tools or custom python scripts.
   - **Autonomous Decision Making**: If a question or architectural decision is clear based on context, standard development practices, or common sense, make the decision on your own and execute the fix automatically.
   - For truly controversial items or highly ambiguous questions requiring user input, log them in a persistent `review_queue.md` artifact. **DO NOT WAIT FOR THE USER to review the queue! You have lots of other work you can do! While waiting for the user to return and answer the queued items, you MUST continue processing other modules, applying unambiguous fixes, and keeping the subagents busy.**
   - IMMEDIATELY spawn the subagents for the next module in the list. You must stay continuously busy and process the rest of the codebase.

5. **Linter Verification**:
   After all code changes for a module are applied, run the workspace linter (`python3 hams_open/hams_shared/tools/run_linters.py`).
   - If the linter reports any errors (either related to your fixes or pre-existing), fix those errors immediately.
   - Continue running the linter and applying fixes until it reports 0 errors.

6. **Test Validation**:
   Once the codebase is clean according to the linter, run the Odoo test suite to ensure your fixes haven't broken any application logic.
   - Use the standard test runner command for the workspace.
   - If any tests fail (especially those where you modified exception handling), investigate and fix the root cause.
   - Iterate on fixing and testing until you achieve a 100% pass rate across the test suite.

7. **Continuous Execution (No Blocking)**:
   Because this is an all-day task, DO NOT STOP to ask the user for permission or updates, even in Planning Mode. Continue processing the directories, applying automated fixes, logging questions in `review_queue.md`, and running verification loops autonomously. Always keep subagents running for the next modules in the queue. Only stop once the entire codebase has been fully processed and passes all linters and tests.

## Notes
- Ensure that the subagents are given explicit instructions to use `list_dir` and `view_file` to read the actual code in the directories they are assigned.
- If you are running this interactively, advise the user to use the `/goal` command instead to prevent timeouts: `/goal run the comprehensive-audit skill`
