---
name: linter-compliance
description: Activated when the AI is writing code to ensure strict adherence to the AST linter rules and zero-sudo mandates.
---

# 🚨 LLM LINTER GUIDE & ANTI-EVASION REFERENCE

*Copyright © Bruce Perens K6BP.*

<system_role>
**Purpose:** This document is the ultimate reference sheet for the platform's DevSecOps pipeline.
It exhaustively details every syntax pattern, AST structure, and architectural anti-pattern that the custom linters (`check_burn_list.py`, `verify_anchors.py`) will physically reject.
You MUST consult this guide to understand the *intent* of the rules and format your code to pass the CI/CD pipeline on the first attempt.
**CRITICAL ANTI-EVASION MANDATE:** This document is a blueprint for *architectural alignment and secure design*, NOT a recipe book for bypassing security checks.
You are strictly forbidden from using this guide to engineer semantic tricks, obfuscations, or workarounds that evade the AST linters without fixing the underlying architectural flaw.
**DEAD CODE, LOOP, & MOCK EVASION IS BANNED:** You MUST NOT place required method calls (like `send_mail()`, `_trigger()`) inside unreachable execution blocks (e.g., `if False:` or after a `return`, `raise`, `break`, or `continue`) or use empty context managers.
Additionally, wrapping assertions (like `get_view` or `url_open`) inside `for` or `while` loops is strictly forbidden.
You MUST NOT mock required functions (via `patch` or `patch.object`); the test must legitimately invoke the targeted logic sequentially.
</system_role>

---

<critical_guardrails>
## 1. 🛡️ Privilege Escalation & Security (Zero-Sudo)

The AST linter recursively tracks assignments and function calls to block absolute privilege escalation.
You MUST use the **Service Account Pattern** (`with_user(svc_uid)`) or the **Public User Idiom**.
* **`sudo()` is Blocked:** Any use of `.sudo()` on recordsets, environments, or intermediate variables is physically blocked.
* **Obfuscation is Caught:** The linter tracks `getattr(..., 'sudo')` and intermediate variable assignments.
* **Environment Evasions:** Calling `env(su=True)` to forcefully escalate to root privileges natively is completely forbidden and will fail the build.
* **Shell Injection:** `subprocess.run` MUST explicitly use `shell=False` and pass arguments as lists.
* **`os.system()` RCE Vector:** The `os.system` function is strictly banned because it executes via a subshell and is vulnerable to string injection. You MUST use `subprocess.run` with `shell=False` and array arguments.
* **Path Traversal Prevention (CWE-22):** RPC methods (`@api.model`) and HTTP controllers (`@http.route`) that perform filesystem operations (`open`, `os.open`, `os.remove`, etc.) MUST strictly sanitize user inputs. You must check for directory traversal attempts (e.g., `".." in path.split(os.path.sep)`) and validate against a mandatory base directory using `os.path.realpath`.
* **Code Execution:** `eval()`, `exec()`, `pickle.loads/dumps`, and `yaml.load` are strictly banned. Use `ast.literal_eval()`, `odoo.tools.safe_eval()`, or `json`.
* **Service Account Base Groups:** You MUST NOT grant `base.group_user` to domain-specific Service Accounts.
Only a *special* user (`odoo_facility_service_internal`) may possess `base.group_user`, and it MUST only be assumed via `with_user()` when strictly necessary.
* **Background Task Identity (Cron/Daemons):** You MUST NOT use `self.env.user` or `self.env.uid` inside background methods (e.g., methods containing `cron`, `daemon`, or starting with `_run_`). During scheduled executions, this resolves to `__system__` (root) or the cron owner, bypassing access controls. You MUST manually resolve and elevate to a designated service account identity via `with_user()`.
* **Weak Cryptography:** `md5`, `sha1`, and the `random` module are banned for security tokens.
Use `hashlib.sha256` and the `secrets` module.
* **RPC Bearer Tokens:** The use of the Odoo facility to allocate RPC bearer tokens (`res.users.apikeys`) will immediately break the build.
The `daemon_key_manager` must be the only facility used, and is the only module allowed to internally allocate keys.
* **Sandbox Evasion & AI Cheating:** You are strictly **FORBIDDEN** from using `os.environ.get("IN_JULES_VM")` or `JULES_SESSION_ID` to skip tests or alter behavior when running inside the AI evaluation sandbox. Tests must execute authentically. Any attempt to bypass tests using these environment variables will fatally fail the build.
</critical_guardrails>

---

<database_rules>
## 2. 🗄️ Database & ORM Integrity

The AST linter defends PostgreSQL from lock exhaustion, OOM crashes, and SQL injection.
* **SQL Injection (SQLi) & Dynamic Queries:** You MUST use parameterized queries for data values (`cr.execute("SELECT * FROM table WHERE id = %s", (my_id,))`).
* The linter recursively traces and physically blocks string concatenation (`+`), `%` formatting, `.format()`, and `f-strings` applied to `cr.execute()`.
* **Dynamic Schema Mandate:** If you must dynamically inject identifiers (like column names or table names), you are strictly **FORBIDDEN** from using f-strings.
You MUST use the `psycopg2.sql` module.
* **Timezone / Native Datetime Trap:** You MUST NOT use `datetime.datetime.now()` or `datetime.date.today()`. These bypass the ORM's timezone context and lead to subtle global data corruption. Force the use of `odoo.fields.Datetime.now()` and `odoo.fields.Date.context_today(self)`.
* **N+1 Loops:** Calling `.search()`, `.search_count()`, or `.read_group()` inside a `for` loop is banned.
You MUST pre-fetch data into memory-mapped dictionaries.
* **Unbounded Searches:** Calling `.search()` without a `limit=` keyword argument is flagged as a potential Out-Of-Memory (OOM) vector.
You MUST paginate or limit bulk searches.
* **Cursor Mismanagement:** Using `env.cr.commit()` or `env.cr.rollback()` directly inside a `with registry.cursor():` block breaks psycopg2 state.
You MUST use `cr = registry.cursor()` followed by `try/except/finally`.
* **Multi-Tenant Context Management (ADR-0083):** You MUST NOT manually inject `allowed_company_ids` into the context dictionary via `.with_context()`. The linter actively blocks this. You MUST use the native ORM abstraction `.with_company(company_id)`.
* **Proxy Ownership Constraints:** When assigning proxy ownership to a dictionary payload in Python, assigning BOTH `owner_user_id` and `user_websites_group_id` simultaneously will trigger an AST trap.
They are mutually exclusive.
* **RPC Mass Assignment:** Passing `kwargs` directly into `.create(**kwargs)` or `.write(kwargs)` inside a controller routes is blocked.
You MUST explicitly map and whitelist fields into a new dictionary.
* **Non-Deterministic Hashes:** Using Python's native `hash()` is banned because it is salted per-process. You MUST use `env['zero_sudo.security.utils']._get_deterministic_hash(val)`.
* **JSON-RPC Kwargs Crash:** Passing a dictionary of kwargs as a positional argument to `client.execute()` for read operations is banned.
You MUST use explicit kwargs (e.g., `fields=[...]`).
* **Cache Purging:** `.clear_caches()` is deprecated in Odoo 19. You MUST use `self.env.registry.clear_cache()` or `self.method_name.clear_cache(self)`.
* **Test Cursor Corruption:** Odoo 19 tests run in a single transaction.
Calling `env.cr.commit()` or `env.cr.rollback()` inside a `test_` file will raise an `AssertionError`.
If testing background loop functions, you MUST utilize the `RealTransactionCase`. You are strictly **FORBIDDEN** from using `odoo.tools.config.get('test_enable')` or similar checks to bypass logic during a test.
* **Controller Caching:** Using `@tools.ormcache` on an `@http.route` controller method is banned.
</database_rules>

---

<python_standards>
## 3.5 📜 Imports

* **Top of File Requirement:** All Python imports MUST be at the top of the file.
The linters enforce Flake8 rule `E402`.
* **Local Imports Banned:** Local Imports (imports inside functions, methods, or classes) are completely banned.
* **Circular Dependency Bypass:** The use of `# noqa` to bypass local import restrictions or any other linter rules is strictly forbidden.
Refactor architecture to avoid circular dependencies instead of using inline imports.
**EXCEPTION (The E402 Daemon Rule):** The strict ban on `# noqa` has one explicit exception.
When writing tests for isolated background daemons that require `sys.path.insert()` to resolve sibling imports, you MUST append `  # noqa: E402` to the module imports that occur after the path modification to satisfy Flake8.
* **The SUDO Override:** You may use `# burn-ignore-sudo` to bypass the strict `.sudo()` AST ban ONLY for legitimately approved administrative operations (like API key rotation).
* **Zero-Sudo Architecture:** The use of `su=True` and `SUPERUSER_ID` are strictly forbidden for bypassing access rights.
Use explicit `.sudo()  # burn-ignore-sudo: <reason>` when absolutely required.
* **Daemon Decoupling:** Standalone daemons (and their tests) located in `daemon/` or `daemons/` directories MUST NOT import any Odoo libraries or testing decorators (e.g., `from odoo.tests.common import tagged`).
They must be completely decoupled from the Odoo framework.

## 3. 🐍 Python Odoo 19 Core Deprecations & Formatting

* **Single Statement Per Line & Short Lines:** You MUST NOT use multiple statements on a single line (no semicolons).
You MUST proactively shorten lines by extracting complex logic to prevent the Black formatter from wrapping lines and detaching inline linter comments (`# burn-ignore`).
* **Long String Formatting:** Strings longer than 40 characters MUST NOT be defined inline.
Extract them to variables using multi-line triple-quotes.
* **Empty F-Strings (F541):** You MUST NOT prefix static strings with `f` if they do not contain variables.
Flake8 will fatally reject this. (LLM Generation Bias).
* **String Concatenation Ban:** Using the `+` operator to concatenate two string literals or f-strings together (e.g., `"a" + "b"`) is strictly forbidden to prevent linter evasion.
Concatenating strings with variables is permitted.
* **Constraints:** `_sql_constraints = [...]` is banned. Use `models.Constraint(...)` class attributes.
* **File Reading & Resources (Odoo 19):** `get_module_resource` is completely removed in Odoo 19 and will crash the server. You MUST use `odoo.tools.file_open`.
* **Security Groups Mapping:** When mapping users to groups in Python dictionaries or XML, you MUST use `group_ids` (for `res.users`) and `user_ids` (for `res.groups`).
Legacy `groups_id` and `users` strings are hard-blocked.
* **Hierarchy Recursion:** `_check_recursion()` is banned. Use `_has_cycle()`.
* **Field Attributes:** `oldname=...` is banned. `select=True` is banned (use `index=True`).
* **Survey States:** The `state` field on `survey.survey` was completely removed in Odoo 19. You MUST use the native `active` boolean field instead.
* **Product Types:** The `detailed_type` field on `product.template` was reverted to `type` in Odoo 19. Do not use `detailed_type`.
* **Trigram Indexes:** `index='trgm'` is banned. Use `index='trigram'`.
* **API Decorators:** `@api.returns` is deprecated and banned.
* **HTTP Routes:** `type='json'` is banned for routes. Use `type='jsonrpc'`.
* **Search Count Parameter:** `search(..., count=True)` is banned. Use `search_count(...)`.
* **Hardcoded Localhost Ban:** You MUST NOT hardcode `127.0.0.1` in Python files. Local loop-back is prohibited;
use a name that can be resolved using Docker or `/etc/hosts` .
* **Thread Blocking:** `time.sleep()` in main application code is banned..
If used in a background daemon for rate-limiting, it MUST be appended with `# audit-ignore-sleep`.
* **Thread Spawning:** `threading.Thread` is banned as a DoS vector. Use `concurrent.futures.ThreadPoolExecutor`.
* **Import Error Evasion:** Wrapping imports in `try...except ImportError` blocks is strictly forbidden (ADR-0073).
You MUST declare dependencies in `__manifest__.py` and let the system fail-fast.
* **Dynamic Data-Type Introspection Banned:** The use of `hasattr()` or `getattr(..., 'column_type')` to dynamically check for fields, methods, or database columns at runtime is strictly forbidden ("CRITICAL AI LAZINESS"). You MUST rely on explicit schema contracts and hard dependencies. If a module requires a method or field from another module, declare it in the `depends` array of your `__manifest__.py`.
* **Hallucinatory sys.path Manipulation:** You MUST NOT use `sys.path.append` or `sys.path.insert` to resolve sibling imports using `..` or to redundantly append the script directory (using `__file__`).
Python naturally resolves local imports. Isolated background daemons are the only permitted exception.
* **The AI Laziness Catch-All Trap:** Using a bare `except:` or `except Exception:` block is strictly forbidden and is flagged as AI laziness. You MUST target specific exceptions (e.g., `KeyError`, `ValueError`, `urllib.error.URLError`).
* **The AI Laziness `hasattr`/`getattr` Trap:** The use of `hasattr()` and 3-argument `getattr(..., default)` are strictly forbidden. AI models frequently use them to mask type uncertainties or architectural flaws. Access explicit fields and let missing schema contracts fail loudly.
* **The Context Manager Silence:** `contextlib.suppress` is strictly banned. Do not use it to silence exceptions; use a dedicated `except` block with an explicit logging call.
* **Ghost Privilege Cheat:** Calling `.with_user(1)` or `.with_user(SUPERUSER_ID)` on recordsets is tracked and blocked as an absolute Zero-Sudo violation. Query for designated Service Accounts instead.
* **The Soft-Dependency Trap:** The use of `'model.name' in self.env` is physically blocked. You MUST explicitly declare external dependencies in the `depends` list of `__manifest__.py`.
* **Catch-All Exception Bypass:** If you are writing a top-level daemon loop or external RPC boundary where an operation must continue past failure, you MUST append the `# audit-ignore-catch-all` bypass tag to the except line. Furthermore, even with this bypass, the block MUST contain a `logging` method call (e.g. `_logger.exception(...)`) to prevent silently swallowed tracebacks.
</python_standards>

---

<frontend_standards>
## 4. 🎨 XML, QWeb, and UI Elements

* **XSS Prevention:** `<t t-raw...>` is banned. Use `<t t-out...>`.
* **SSTI Prevention:** Using `request.env` anywhere inside an XML QWeb template is a critical Server-Side Template Injection vector and is banned.
Compute values in Python controllers and pass them to the rendering context.
* **Legacy View Tags:** `<tree>` is banned (use `<list>`). `t-name="kanban-box"` is banned (use `t-name="card"`).
* **Deprecated Directives:** `t-esc` is banned.
Use `t-out`.
* **Search Views:** `<group expand="0">` and `<group string="...">` are banned. Odoo 19 requires clean group tags.
* **Snippet Options Deprecation:** Inheriting `website.snippet_options` or `web_editor.snippet_options` is highly volatile and leads to `ValueError: External ID not found` in Odoo 19. Do not implement custom snippet option menus.
* **Snippet Anchors:** Targeting `id="snippet_structure"` via XPath is banned as fragile. Target `/*` instead.
* **Fragile Form XPaths:** Targeting `hasclass('field-*')` (e.g., `field-login`, `field-name`) or generic structural classes like `hasclass('card')` is banned.
Odoo 19 refactored frontend templates and removed/altered these wrappers. Target semantic elements instead (e.g., specific wrapper IDs or custom classes).
* **Label Targeting Banned:** Targeting `//label[@for='...']` is strictly banned.
Target the `//input[@name='...']` element directly instead.
* **Button String Targeting Banned:** Targeting `//button[@string='...']` is strictly banned.
Target the button by its method name (`//button[@name='...']`).
* **Legacy `attrs` Banned:** The `attrs` attribute (e.g., `attrs="{'invisible': ...}"`) was removed in Odoo 17+.
Use `invisible`, `readonly`, and `required` directly with Python expressions.
* **Parent Axis Traversals:** Using `..` (e.g., `//input[@name='login']/..`) or complex container predicates (`//div[input[@name='login']]`) is strictly banned.
Odoo's XML compiler often fails to resolve these when patching inherited views.
* **Cross-Module Custom View Targets (Dropzones):** When using `<xpath>` to extend our *own* custom modules (not native Odoo core views), you MUST target the explicitly designated "Dropzone" containers (e.g., an empty `div` designated for injection) defined in the target module's `README.md`.
Arbitrary structural targeting inside our own custom views is banned to prevent cross-module fragility.
* **Security Categories:** Using `name="category_id"` in `<record model="res.groups">` is banned. Use `privilege_id`.
* **Record Rules (ir.rule):** Every `<record model="ir.rule">` MUST specify a `<field name="groups" ...>`.
Global rules (rules without a group) are deprecated and banned.
* **Cron Infinity:** Specifying `numbercall` in an `ir.cron` XML record is banned. Odoo 18+ runs crons indefinitely when `active="True"`.
* **WCAG Accessibility (Icons):** Any `<i>` tag utilizing FontAwesome (`fa`) or Odoo Icons (`oi`) MUST possess a `title`, `aria-label`, or `aria-hidden="true"` attribute to satisfy screen readers.
* **WCAG Accessibility (Images):** Any `<img>` tag MUST possess an `alt` attribute.
* **WCAG Accessibility (Buttons & Links):** Empty `<button>` or `<a>` tags lacking text content, a `string` attribute, `title`, or `aria-label` will trigger an audit warning.
</frontend_standards>

---

<javascript_standards>
## 5. 🖥️ Frontend JavaScript

* **jQuery Ban:** The `$` identifier is banned.
You MUST use Vanilla JS or modern OWL components.
* **DOM XSS:** Passing template literals (backtick strings) into `.innerHTML` or `.bindPopup` is flagged.
Ensure all dynamic data injected into the DOM is sanitized.
* **Deprecated Services:** `useService('company')` is banned.
* **OWL `rpc` Service Deprecation:** The raw `useService('rpc')` method is banned in Odoo 19 frontend components. You MUST use `useService('orm')` which securely handles batching, caching, and model security, unless explicitly burning this rule for a custom controller.
* **The /web/ Asset & Login Mandate:** While general routing has moved to `/odoo`, core static assets (`/web/assets/`), images (`/web/image`), and the authentication endpoint (`/web/login`) MUST remain under the `/web` path. You are strictly FORBIDDEN from refactoring these specific paths to `/odoo`. The Cloudflare and Caching modules rely on `/web/assets/` for edge caching. If necessary, use `# burn-ignore-route` to bypass linters for these valid exceptions.

> **NOTICE (UI TOURS):** All strict architectural mandates, workarounds, and syntax rules required to write stable UI Tours have been relocated to the dedicated **`docs/LLM_WRITING_TOURS.md`** manual. You MUST consult that document for all tour-related directives.
</javascript_standards>

---

<ci_cd_bypasses>
## 6. 🚦 CI/CD Bypasses & Automated Test Audits (The `ignore` Protocol)

The linter outputs `[AUDIT]` warnings for specific architectural patterns.
You MUST silence these by appending specific `audit-ignore` tags, but **ONLY** if you write an automated Python test to mathematically verify the constraint.
The AST parser physically reads your test files to verify the assertions exist.

| Audit Target | Bypass Tag | Required AST Assertion in Test |
| :--- | :--- | :--- |
| Catch-All Exceptions | `# audit-ignore-catch-all` | MUST ONLY be used where an operation must continue past failure, and MUST contain a logging call. |
| Path Traversal | `# audit-ignore-path` | The test MUST execute the RPC method with a directory traversal payload (e.g., `../etc/passwd`) and assert that it raises a `UserError` or `AccessError`. |
| `ir.cron` XML | `<!-- audit-ignore-cron: Tested by [@ANCHOR: example_name] -->` | The test MUST execute `_trigger()` to prove batching. |
| `send_mail()` | `# audit-ignore-mail: Tested by [@ANCHOR: example_name]` | The test MUST execute `send_mail` or `message_post`. **CRITICAL TRAP:** The integer `res_id` passed to `send_mail(res_id)` MUST match an existing record of the exact model defined in the template's `model_id`. |
| `.search()` | `# audit-ignore-search: Tested by [@ANCHOR: example_name]` | The test MUST pass `limit=` or utilize `patch.object(self.env.cr, 'execute')` to assert caching behavior. |
| `@tools.ormcache` | N/A (Tested implicitly by logic) | To verify a cache hit, NEVER use `self.assertQueryCount(0)`. You MUST use `with patch.object(self.env.cr, 'execute', wraps=self.env.cr.execute) as mock_execute:` and assert `self.assertNotIn("target_table", query)` in `mock_execute.call_args_list`. |
| Boolean Checks | N/A (Flake8 E712) | NEVER use `== True` or `== False`. You MUST use `is True`, `is False`, or `if cond:`. |
| `<xpath>` | `<!-- audit-ignore-xpath: Tested by [@ANCHOR: example_name] -->` | The test MUST execute `get_view`, `url_open`, or `_get_combined_arch` to prove DOM injection. |
| `time.sleep()` | `# audit-ignore-sleep` | (Visual check only; indicates daemon rate-limiting). |
| `ir.ui.view` | `<!-- audit-ignore-view: Tested by [@ANCHOR: example_name] -->` | MUST be placed on the EXACT same line as the `<record>` or `<template>` node. Test MUST execute `get_view` or `url_open`. |
| I18N Strings | `# audit-ignore-i18n: Tested by [@ANCHOR: example_name]` | Safely ignore headless API translations (ADR-0065). |
| Legacy Web Routes | `# burn-ignore-route` | Explicitly permits the use of native `/web` routing prefixes (like `/web/login` or `/web/assets`) instead of the Odoo 19 `/odoo` prefix in tests and controllers. |

### 🚨 Critical Formatting & Placement Rules for Bypasses
1. **The Python Formatter (`# fmt: skip`) Trap:** The Black code formatter will wrap long lines and detach your inline linter comments, causing the AST linter to fail.
**Whenever you apply an `# audit-ignore-*` or `# burn-ignore` comment to a multi-line structure, you MUST append `  # fmt: skip` to the exact same line.**
2. **The Internal XML Child-Node Anchor Placement:** To satisfy both the XML architecture linter and the bidirectional traceability linter simultaneously without falling victim to line-wrapping fragility, you MUST place both the traceability anchor and the burn list bypass **INSIDE** the `<record>` or `<template>` tags as direct child nodes.
* Do NOT place them above the tag or inline on the same line as the opening bracket.
Auto-formatters and long attributes (like `model` or `inherit_id`) will wrap the line and break the AST parser's line-number correlation.
* **Required Structure:**
```xml
<record id="my_view" model="ir.ui.view">
    <!-- [@ANCHOR: example_source_anchor] (Only if a base anchor is needed) -->
    <!-- audit-ignore-view: Tested by [@ANCHOR: test_my_view] -->
    <field name="name">...</field>
</record>
```
3. **The Web UI Destruction Trap (XML Protection):** When writing the XML comments shown in Rule 2, the Web UI might silently intercept and delete them from your output before they are saved to disk if formatted as standard markdown.
To survive the UI parser, you MUST ensure your entire Parcel payload is wrapped exclusively inside a `python` markdown code block, which prevents the UI from evaluating the internal HTML/XML tags.
</ci_cd_bypasses>

---

<semantic_anchors>
## 7. ⚓ Semantic Anchors & UI Tour Mandate

The `verify_anchors.py` script enforces strict documentation traceability:

1. **Bidirectional Verification:** Any execution logic marked with `# Verified by [@ANCHOR: example_name]` MUST possess a corresponding test file containing `# Tests [@ANCHOR: example_name]`.
2. **Documentation Mandate:** Any anchor embedded in source code MUST be referenced somewhere within the `docs/` folder (Runbooks, Stories, Journeys, or Modules).
These documentation references MUST be placed inline, immediately adjacent to the relevant descriptive text.
3. **The View-Tour Mandate:** Every `<template>` or `<record model="ir.ui.view">` MUST contain a UI Tour link.
4. **Tour Validation:** The corresponding JavaScript tour file MUST contain the matching anchor and explicitly utilize the `trigger:` keyword to prove it evaluates the DOM.
</semantic_anchors>

## 7. Shebang Usage & `__manifest__.py` Formatting
Shebangs (`#!/usr/bin/env python3`) are strictly prohibited in standard Odoo module files (e.g., `models/`, `controllers/`, `__init__.py`, `__manifest__.py`).
They can interfere with packaging and execution expectations inside standard Odoo modules.
This restriction does not apply to isolated daemon scripts in the `daemons/` or `tools/` directories.
Additionally, `__manifest__.py` files must strictly conform to dictionary structures without shebangs.
Odoo's `ast.literal_eval` parser requires valid, strict Python dictionary syntax, and any extraneous bash-style lines will cause fatal `ParseError: while parsing None:101` exceptions during test or registry initialization.
