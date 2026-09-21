---
name: linter-compliance
description: Activated when the AI is writing code to ensure strict adherence to the AST linter rules and zero-sudo mandates.
---

# 🚨 LLM LINTER GUIDE & ANTI-EVASION REFERENCE

*Copyright © Bruce Perens K6BP.*
SPDX-License-Identifier: AGPL-3.0-or-later

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
* **Sandbox Evasion & AI Cheating:** You are strictly **FORBIDDEN** from checking any environment variable to detect the AI evaluation sandbox and skip tests or alter behavior based on it. Tests must execute authentically. Any attempt to bypass tests this way will fatally fail the build.
</critical_guardrails>

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

<javascript_standards>
## 5. 🖥️ Frontend JavaScript

* **jQuery Ban:** The `$` identifier is banned.
You MUST use Vanilla JS or modern OWL components.
* **DOM XSS:** Passing template literals (backtick strings) into `.innerHTML` or `.bindPopup` is flagged.
Ensure all dynamic data injected into the DOM is sanitized.
* **Deprecated Services:** `useService('company')` is banned.
* **OWL `rpc` Service Deprecation:** The raw `useService('rpc')` method is banned in Odoo 19 frontend components. You MUST use `useService('orm')` which securely handles batching, caching, and model security, unless explicitly burning this rule with `# burn-ignore-rpc-non-orm` for a custom, non-ORM controller route.
* **The /web/ Routing Deprecation:** `/web` (bare, or `/web#...` hash-routing) is deprecated and forcefully redirected to `/odoo` in Odoo 19, losing query parameters. This IS enforced: any `/web/...` or `/web#...` string literal in a `.py`/`.js` file is flagged, except sub-paths already known to be legitimate, non-deprecated Odoo routes (`login`, `signup`, `assets`, `static`, `tests`, `database`, `image`, `session`, `dataset`, `content`). A literal outside that allowlist that is genuinely not a navigation target to the deprecated entrypoint (e.g. a cache-control prefix classifier) may use `# burn-ignore-route: <reason>`; a literal that IS a legitimate, undocumented `/web/` sub-route should instead be added to the allowlist in `check_burn_list.py` directly.

> **NOTICE (UI TOURS):** All strict architectural mandates, workarounds, and syntax rules required to write stable UI Tours have been relocated to the dedicated **`hams_shared/agents/skills/odoo-ui-tours/SKILL.md`** skill. You MUST consult that skill for all tour-related directives.
</javascript_standards>

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
    * **Loading-Flag Gates:** Gating runtime behaviour on Odoo's command-line loading flags --
    `config['init']`, `config['update']`, `config['stop_after_init']`, or their `config.get(...)`
    forms -- is banned outside test files. Odoo 19 sets them from the command line and never
    clears them once loading finishes, so an Odoo started as `odoo -u some_module` that then goes
    on to serve keeps them set for its entire lifetime, and anything gated on them never runs
    again. That silently disabled `distributed_redis_cache`'s cache-invalidation poll for the
    whole life of any server deployed with a one-step upgrade-and-restart. To ask "is this
    registry still loading", use `registry.ready` (via `cls.pool` in a registry-composed model
    class), which is False for exactly the install/upgrade window and True again once the process
    is serving. Test files are exempt so a test can assert what the flags really are in its own
    process, which is how that regression is reproduced with nothing patched.
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
        * **Import Error Evasion:** Wrapping imports in `try...except ImportError` (or `ModuleNotFoundError`) is strictly forbidden (ADR-0073). No exemption tag exists and no directory (tools, tests, scripts) is excluded; a function-local import is likewise never exempt.
        You MUST declare dependencies in `__manifest__.py` and let the system fail-fast.
        * **Dynamic Data-Type Introspection Banned:** The use of `hasattr()` or `getattr(..., 'column_type')` to dynamically check for fields, methods, or database columns at runtime is strictly forbidden ("CRITICAL AI LAZINESS"). You MUST rely on explicit schema contracts and hard dependencies. If a module requires a method or field from another module, declare it in the `depends` array of your `__manifest__.py`.
        * **Hallucinatory sys.path Manipulation:** You MUST NOT use `sys.path.append` or `sys.path.insert` to resolve sibling imports using `..` or to redundantly append the script directory (using `__file__`).
        Python naturally resolves local imports. Isolated background daemons are the only permitted exception.
        * **The AI Laziness Catch-All Trap:** Using a bare `except:` or `except Exception:` block is strictly forbidden and is flagged as AI laziness. You MUST target specific exceptions (e.g., `KeyError`, `ValueError`, `urllib.error.URLError`).
        * **The AI Laziness `hasattr`/`getattr` Trap:** The use of `hasattr()` and 3-argument `getattr(..., default)` are strictly forbidden by default. AI models frequently use them to mask type uncertainties or architectural flaws. If you must use them, you MUST append `# burn-ignore-introspection` and link a test via `[@ANCHOR: ...]`. Do not use this to bypass missing Odoo `depends` entries.
        * **The Context Manager Silence:** `contextlib.suppress` is strictly banned. Do not use it to silence exceptions; use a dedicated `except` block with an explicit logging call.
        * **Ghost Privilege Cheat:** Calling `.with_user(1)` or `.with_user(SUPERUSER_ID)` on recordsets is tracked and blocked as an absolute Zero-Sudo violation. Query for designated Service Accounts instead.
        * **The Soft-Dependency Trap:** The use of `'model.name' in self.env` is physically blocked. You MUST explicitly declare external dependencies in the `depends` list of `__manifest__.py`.
        * **Catch-All Exception Bypass:** If you are writing a top-level daemon loop or external RPC boundary where an operation must continue past failure, you MUST append the `# audit-ignore-catch-all` bypass tag to the except line. Furthermore, even with this bypass, the block MUST contain a `logging` method call (e.g. `_logger.exception(...)`) to prevent silently swallowed tracebacks.
</python_standards>

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
| `/web/...` Routing | `# burn-ignore-route: <reason>` | Only for a `/web/...` or `/web#...` string literal that is flagged by the routing-deprecation check but is genuinely not a navigation target to the deprecated bare `/web` entrypoint (e.g. a cache-control prefix classifier). Do NOT use this for a legitimate, undocumented `/web/` sub-route -- add it to the allowlist in `check_burn_list.py` instead. |
| `useService('rpc')` | `# burn-ignore-rpc-non-orm: <reason>` | Only for a raw `rpc` service call whose target is a custom, non-ORM HTTP controller route, which `useService('orm')` cannot reach. |

### 🚨 Critical Formatting & Placement Rules for Bypasses
1. **The Python Formatter (`# fmt: skip`) Trap:** The Black code formatter will wrap long lines and detach your inline linter comments, causing the AST linter to fail.
**Whenever you apply an `# audit-ignore-*` or `# burn-ignore` comment to a multi-line structure, you MUST append `  # fmt: skip` to the exact same line.**
2. **The Internal XML Child-Node Anchor Placement:** To satisfy both the XML architecture linter and the bidirectional traceability linter simultaneously without falling victim to line-wrapping fragility, you MUST place both the traceability anchor and the burn list bypass **INSIDE** the `<record>` or `<template>` tags as direct child nodes.
* Do NOT place them above the tag or inline on the same line as the opening bracket.
Auto-formatters and long attributes (like `model` or `inherit_id`) will wrap the line and break the AST parser's line-number correlation.
* **Required Structure:**
```xml
<record id="my_view" model="ir.ui.view">(Only if a base anchor is needed) -->
    <!-- audit-ignore-view: Tested by [@ANCHOR: test_my_view] -->
    <field name="name">...</field>
</record>
```
3. **The Web UI Destruction Trap (XML Protection):** When writing the XML comments shown in Rule 2, the Web UI might silently intercept and delete them from your output before they are saved to disk if formatted as standard markdown.
To survive the UI parser, you MUST ensure your entire Parcel payload is wrapped exclusively inside a `python` markdown code block, which prevents the UI from evaluating the internal HTML/XML tags.
</ci_cd_bypasses>

## 6.5 Complete bypass-tag reference

`check_burn_list.py` accepts a hyphenated tag only if it is on one of its allow-lists; any other
`burn-ignore-*` or `audit-ignore-*` spelling is itself an **UNAUTHORIZED BYPASS** error. The tables
above explain the common tags. This section lists every other tag the linter accepts, so the list
is complete. `tools/test_linter_compliance_skill_drift.py` fails when the linter gains a tag this
file does not mention, so add the tag here in the same change that adds it to the linter.

Each tag is a narrow exception, not a general escape hatch. Put the reason (and, where a test
backs it, the `[@ANCHOR: ...]`) after the tag on the same line.

### Security and privilege exceptions

| Tag | What it suppresses, and when it is legitimate |
| :--- | :--- |
| `# burn-ignore-financial` | CRITICAL FINANCIAL EXPOSURE: touching `account.move`, `account.payment`, `res.partner.bank`, `payment.token`, `payment.transaction`, `.bank_ids` or `.payment_token_ids`. Requires an anchor to the test covering the access. |
| `# burn-ignore-csrf-token` | CRITICAL CSRF on an XML `<form method="post">` with no `csrf_token` input, for a form that genuinely does not post to an Odoo CSRF-checked route. |
| `# burn-ignore-exec-own-source` | CRITICAL RCE on `exec()` when a `hams_shared/tools` test executes a function's source text extracted from this repository's own committed file, never external input. |
| `# burn-ignore-legacy-protocol-hash` | WEAK CRYPTO on `hashlib.md5`/`sha1` when reproducing an existing external protocol's fixed algorithm for interoperability (for example Winlink's Secure Gateway Login challenge in `test_rmsgw_protocol.py`), not a token this codebase chooses. |
| `# audit-ignore-weak-random` | WEAK CRYPTO on the `random` module for deliberately non-security, seedable output such as deterministic exam generation. Say why in a comment. |
| `# burn-ignore-superuser-rejection-test` | The `SUPERUSER_ID` import ban, in a regression test proving a caller-supplied superuser identity is rejected. |
| `# audit-ignore-superuser-bootstrap-for-service-uid-resolution` | The CRITICAL ZERO-SUDO check on `Environment(cr, SUPERUSER_ID, ...)`, only for a transient bootstrap that resolves a service account's uid and then drops to it (first used by `list_routes.py`). |
| `audit-ignore-service-account-admin-group` | (XML comment inside the `res.users` record.) A service account granted a human administrator group such as `base.group_system`. Only for a reviewed case where the account's job genuinely needs it. |
| `# audit-ignore-service-uid-cursorless` | CRITICAL FAST FAIL on the broad `try/except` around `_get_service_uid(...)`. Must be on the exact call line. Only `content_security_policy/models/ir_http.py`'s `_post_dispatch`, which runs on cursor-less routes. |
| `# audit-ignore-ssti` | CRITICAL SSTI (`request.env` inside a QWeb template) for a static, developer-written expression with no reachable untrusted input. |
| `# audit-ignore-gdpr-hand-rolled-unlink` | `check_gdpr_erasure_uses_service_utility.py`'s requirement to erase through `_erase_via_service_account`, for erasure that needs production-scale batching, savepoints and mid-loop commits (`user_websites`' page and blog erasure). |
| `# audit-ignore-outbound-fetch` | [%AUDIT] OUTBOUND FETCH (possible SSRF) when the URL's host is genuinely fixed and trusted. Name the host. A local health-check poll to loopback is the usual case, since `urlopen_ssrf_safe` rejects loopback by design. |
| `# burn-ignore-env` | CRITICAL HARDCODED CREDENTIAL DEFAULT on a credential environment-variable read whose literal fallback is genuinely safe, such as a test-only placeholder. Give the reason after the tag. |
| `# audit-ignore-sql` | Accepted on a reviewed `cr.execute(...)` whose parameters are passed separately, with a test anchor. |
| `# audit-ignore-sql-savepoint` | CRITICAL RAW SQL WITHOUT SAVEPOINT: a `cr.execute("select zero_sudo_...(")` call to a Postgres function that can RAISE EXCEPTION, inside a `try`, with no `with <cursor>.savepoint():` around it (a caught exception still poisons the transaction). Only for a call that genuinely cannot raise; put a comment citing the evidence. Otherwise wrap it in a savepoint as `_get_service_uid()` does. |
| `# audit-ignore-retry-method` | [%AUDIT] RETRY REPLAYS NON-IDEMPOTENT METHODS: a `urllib3` `Retry` whose `allowed_methods` includes POST or PATCH (or is empty, meaning every method), which would replay a request the server may already have applied on a 5xx. Only when the endpoint is genuinely idempotent; say why in the comment. Otherwise subclass `Retry` as `IdempotencyAwareRetry` in `cloudflare/utils/cloudflare_api.py` does. |
| `# audit-ignore-get-param-fallthrough` | CRITICAL TEST FALLTHROUGH: in a real-transaction test case, a `patch` / `safe_patch` of `get_param` whose fixed `return_value`, or `side_effect` returning its own default, answers EVERY config key (including `database.secret` and request-size limits). Only when answering every key is really intended (rare); explain why in a comment. Otherwise use `ir.config_parameter.set_param()` or a `side_effect` that falls through to the real `get_param`. |

### Network-hardcoding exceptions (loopback and literal addresses)

The CRITICAL NETWORK HARDCODING rule exists because a service reaching another service through a
hardcoded loopback address breaks across containers. These tags cover the cases where that concern
does not apply. Pick the one that matches; they are deliberately distinct.

| Tag | Legitimate use |
| :--- | :--- |
| `# burn-ignore-bind-address-default` | A daemon's own default listen address (loopback by default, widened by an environment variable). Lives in the one shared `resolve_bind_addr()`. |
| `# burn-ignore-cloudflared-ingress` | A Cloudflare Tunnel ingress target: `cloudflared` runs on the same host as the services it fronts. |
| `# burn-ignore-tunnel-peer-check` | Comparing an incoming request's transport peer to loopback to decide whether it came through our own tunnel (`_get_trusted_client_ip()`). |
| `# burn-ignore-relay-loopback` | The allow-list of redirect hosts for a relay login, where `localhost` is the user's own `hams_local_relay`. |
| `# burn-ignore-self-hosted-server` | A standalone verification script connecting to a server it started itself moments earlier. |
| `# burn-ignore-local-debug-script` | A one-shot personal debugging script, never shipped or deployed, reaching a service on the same machine. |
| `# burn-ignore-test-local-server` | A test's own fixture HTTP server on loopback, standing in for a device that really does live on the user's machine or LAN. |
| `# burn-ignore-ssrf-test-value` | A private or loopback address used as attack-payload data in an SSRF-rejection test, never connected to. |
| `# burn-ignore-unreachable-sentinel` | A loopback address with a reserved, closed port, used to force a real connection failure instead of mocking one. |

### Dependency and soft-dependency exceptions

| Tag | Legitimate use |
| :--- | :--- |
| `# burn-ignore-optional-oca-dep` | The `'model' in self.env` presence check for an optional OCA addon an administrator may install later (`hams_s3` and `storage.backend`). It does not cover `.sudo()` on the same line. |
| `# burn-ignore-optional-cross-repo-dep` | The same presence check for a model from the other repository, where a real manifest dependency would stop `hams_open` installing on its own (`pager_duty` and `ham.dns.record`). |
| `# burn-ignore-skiptest-soft-dependency` | The fast-fail rule against skipping a test (`skipTest`) when an optional binary or module is missing, for a reviewed case. It does NOT exempt `try/except ImportError` / `ModuleNotFoundError`, which no tag or directory can exempt. |
| `# burn-ignore-pika` | Accepted on test lines that open a real RabbitMQ (`pika`) connection in `backup_management`'s tests. |

### Test-fixture and linter-self-test exceptions

The linter matches raw text in several rules, so a string describing a violation can trip the rule
it describes. These tags cover that, and nothing else.

| Tag | Legitimate use |
| :--- | :--- |
| `burn-ignore-anchor-example` | An `[@ANCHOR-BEGIN: ...]` example inside a docstring or fixture that is not a real, unterminated anchor marker. Exempts that line only. |
| `burn-ignore-noqa-example` | A fixture string containing `# noqa`, in a test of the noqa ban itself. |
| `burn-ignore-vendor-patch-text` | A string literal used as search-and-replace data for patching a vendored third-party file, or a linter fixture containing forbidden text. |
| `# burn-ignore-test-daemon-thread` | `threading.Thread(..., daemon=True)` in a test that drives a real infinite-loop daemon function end to end. |
| `# burn-ignore-test-tags` | `check_test_tags.py`'s required-tags check, for a test file that deliberately carries none. |

### Odoo structure exceptions

| Tag | Legitimate use |
| :--- | :--- |
| `# burn-ignore-company-scoped-loop` | N+1 `.search()` inside a loop that calls `.with_company(company)` per iteration, where a single grouped query would require widening the service account's company membership. |
| `burn-ignore-global-rule` | An `ir.rule` with no `groups`, when a global rule is required because a group-scoped deny rule is OR-ed with other rules and can be outvoted (`ham_crm_security`'s `crm_lead_block_portal`). |
| `burn-ignore-tour` | The view-tour mandate for a view with no UI tour. `audit-ignore-view` does not satisfy this on its own. |
| `audit-ignore-view-resolution` | [%AUDIT] IMPLICIT VIEW RESOLUTION on an `ir.actions.act_window` with no explicit `view_id`/`view_ids`, when default resolution by model and type is intended. |
| `burn-ignore-hoot-runner-coverage` | Grandfathers the modules that already registered `*.test.js` files with no Python `browser_js()` runner when that check was added. New modules must add a runner instead. |
| `# burn-ignore-os-account-probe` | The fast-fail try/except ban around an operating-system account lookup (`pwd`/`grp`, `groupadd`/`useradd` existence checks) in provisioning and the test runner. These never touch Odoo. |

<semantic_anchors>
## 7. ⚓ Semantic Anchors & UI Tour Mandate

The `verify_anchors.py` script enforces strict documentation traceability:

1. **Bidirectional Verification:** Any execution logic marked with `# Verified by [@ANCHOR: example_name]` MUST possess a corresponding test file containing `# Tests [@ANCHOR: example_name]`.
2. **Documentation Mandate:** Any anchor embedded in source code MUST be referenced somewhere within the `hams_shared/docs/` folder (Runbooks, Stories, Journeys, or Modules).
These documentation references MUST be placed inline, immediately adjacent to the relevant descriptive text.
3. **The View-Tour Mandate:** Every `<template>` or `<record model="ir.ui.view">` MUST contain a UI Tour link.
4. **Tour Validation:** The corresponding JavaScript tour file MUST contain the matching anchor and explicitly utilize the `trigger:` keyword to prove it evaluates the DOM.
</semantic_anchors>
## 7.5 🥾 Hoot unit suites: a green wrapper must mean tests actually ran

A hoot suite that executes ZERO tests is reported by hoot exactly like a passing one. Its runner
prints `Passed 0 tests` and then `Test suite succeeded`, and `Test suite succeeded` is precisely the
string `HamsHttpCase.browser_js()` waits for. So a Python wrapper over an empty suite passes, every
time, while testing nothing. Five `ham_shack` suites did that for weeks; when they were finally
bundled (hams_com `bb25842d`) 24 tests ran for the first time and 6 of them failed -- assertions that
had never once been executed against the code they describe.

Four ways a suite ends up empty, three of them static and one only visible at runtime:

1. The `*.test.js` file is not listed in any manifest's `web.assets_unit_tests` bundle. `/web/tests`
   only loads what a manifest names.
2. It is bundled, but no `browser_js()` wrapper ever asks for the tag its `describe` declares.
3. Its `describe` block contains no `test()` call at all.
4. Every test in it is skipped at runtime.

`check_hoot_runner_coverage.py` catches the first three mechanically. **Cite the checker, not just a
green run** -- a module-level "the suite is green" result cannot distinguish a passing suite from an
absent one, because a suite the runner was never asked for is missing from both the numerator and
the denominator. Prefer "these N tests ran and passed" over "the suite is green": the first is a
claim about what executed, the second implies coverage the run never measured.

### The `expect_empty=True` escape hatch -- use it deliberately, never weaken the check

`browser_js()` fails on a run that executed zero tests. A deliberately-skipped suite will therefore
start failing its wrapper. **That is the guard working, not a false positive**: a green wrapper over
an all-skipped suite is the same false coverage claim, just self-inflicted.

Two legitimate ways out, in order of preference:

1. **Delete the wrapper along with the skip.** If a suite is not meant to run, a Python wrapper
   asserting that it succeeded is claiming something untrue. This is almost always the right answer.
2. **Pass `expect_empty=True` to `browser_js()`** where an empty run is genuinely intended. It must
   be stated explicitly at the call site, so the claim "this suite is expected to execute nothing"
   is visible in review rather than inferred from a silence.

What is NOT acceptable: removing or loosening the guard, broadening it to tolerate empty runs
generally, or reaching for the nearest workaround because the failure arrived at an inconvenient
moment. This is the same standing rule this codebase applies to every failing test -- fix the code,
never weaken the test -- and the guard exists precisely because the failure it reports is otherwise
invisible.

## 7. Shebang Usage & `__manifest__.py` Formatting
Shebangs (`#!/usr/bin/env python3`) are strictly prohibited in standard Odoo module files (e.g., `models/`, `controllers/`, `__init__.py`, `__manifest__.py`).
They can interfere with packaging and execution expectations inside standard Odoo modules.
This restriction does not apply to isolated daemon scripts in the `daemons/` or `tools/` directories.
Additionally, `__manifest__.py` files must strictly conform to dictionary structures without shebangs.
Odoo's `ast.literal_eval` parser requires valid, strict Python dictionary syntax, and any extraneous bash-style lines will cause fatal `ParseError: while parsing None:101` exceptions during test or registry initialization.
