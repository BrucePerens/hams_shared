# ADR 0081: Native Odoo Tour Resilience Architecture

> **NOTICE:** The architectural decisions below have been aggregated into a unified developer guide. Please refer to `docs/LLM_WRITING_TOURS.md` for the complete, up-to-date manual on writing UI Tours.

## Status
Accepted

## Context
JavaScript UI Tours in Odoo are inherently brittle. Recent CI pipeline failures highlight severe issues with DOM race conditions, invisible layout elements, blocking native dialogs, Single Page Application (SPA) context loss, non-deterministic locale initializations, and asynchronous input evaluation.

Initially, there was an attempt to inject arbitrary `data-tour` tags to decouple selectors. This proved to be an architectural anti-pattern that caused backend XML schema validation (`jing`) failures. We must adapt the tours to the framework's native architecture to achieve resilience.

## Decision
Tours MUST be written using Odoo's native JS testing capabilities and schema-compliant selectors to deterministically handle race conditions and environment states.

### 1. Native-First Targeting and Namespaced Fallbacks
UI components MUST NOT rely on structural layout classes (e.g., `.col-md-6`) or translated text strings for test triggers, as these frequently change during UI polish.
* **Primary (The Backend Standard):** Tours MUST prioritize Odoo's native `name` attributes (`button[name="action_install"]`, `field[name="is_installed"]`). These are immutably bound to the Python backend RPC methods.
* **Secondary (Structural Singletons):** Tours may target native structural IDs (`#wrap`, `.o_form_sheet`).
* **Fallback (The Namespaced Class):** When an element lacks a natural `name` or `id` (e.g., frontend QWeb buttons, generic containers), developers MUST inject a dedicated CSS class starting with `o_tour_` (e.g., `class="btn btn-primary o_tour_create_site_btn"`). You MUST NOT use `data-*` attributes in backend XML views.

### 2. Handling Invisible Dropzones and Structural Elements
Odoo's tour framework inherently ignores elements with a 0x0 pixel dimension.
* Developers MUST NOT pollute UI CSS with `min-height: 1px` hacks to make an empty container visible to the test runner.
* If a 0x0 structural dropzone or invisible layout anchor MUST be targeted, the tour MUST explicitly append the Odoo pseudo-selector `:not(:visible)` to the trigger string.

### 3. The Page Unload Protocol
When a tour step executes a click that triggers a raw HTML `<form>` submission or a hard browser redirect (bypassing the Odoo SPA router), the test runner will interpret the browser's native `beforeUnload` event as a fatal crash if it isn't warned.
* The specific tour step that initiates the unload MUST explicitly declare `expectUnloadPage: true`.
* You MUST use Odoo's native `run: 'click'` helper on this step. Do not use custom JS closures (`run: () => { btn.click() }`) as it breaks the unload event binding.

### 4. Decoupling of Blocking Dialogs
Native JavaScript dialogs (`window.confirm`, `window.alert`) halt the browser's execution thread.
* If a tour must bypass a native dialog, it MUST isolate the window override into an independent, preceding step targeting the `body`.

### 5. Native Auto-Save and RPC Resolution (The "Dirty Form" Rule)
Native Odoo form buttons (e.g., `type="object"`) automatically and asynchronously save the form before invoking their backend methods. If a tour navigates away or terminates before this background save resolves, Odoo will halt the test with a "dirty form view" error.
* **RPC Returning True (Form Reloads):** Backend methods that return `True` silently reload the form view and **DO NOT** spawn notifications. Tours MUST NOT wait for `.o_notification` in these cases. Instead, they MUST explicitly wait for a verifiable DOM state change (e.g., a field transitioning from empty to populated: `.o_field_widget[name="last_rotated"]:not(.o_field_empty)`).
* **RPC with Actions:** If the backend explicitly returns a notification or action, tours must safely poll for that action before proceeding.

### 6. Action Button State Governance (The "Unsaved Record" Trap)
Relying on Odoo's native auto-save mechanism when clicking an action button on a newly created, dirty form introduces severe race conditions where partial text input is submitted to the backend because the DOM `blur` event hasn't fired.
* Native Odoo object buttons (`type="object"`) that execute backend Python methods MUST be hidden on new, unsaved records using `invisible="not id"` (or merged into existing visibility domains, e.g., `invisible="is_installed or not id"`).
* **The Neutral Click-Away:** Tours MUST include a neutral "click away" step after text entry to force the DOM to commit the record state, prior to interacting with save or action buttons.
* **Modal Safety:** When clicking away inside a modal dialog, you MUST target safe internal elements like `.modal-body`. Attempting to trigger a click on structural wrappers like `.modal-dialog` or `.modal-content` will trip test runner fragility checks; these wrappers can only be used for passive DOM polling (`run: function() {}`).

### 7. Language and Translation Determinism
Odoo's headless browser tests can crash with "no translation language is detected" if the environment's `Accept-Language` headers are ambiguous, especially before translation dictionaries are fully populated.
* The `--language` CLI flag and OS-level `LANG` variables are ignored by Odoo's web client and MUST NOT be used for this purpose.
* Developers MUST ensure deterministic locales by explicitly setting the `lang` field (e.g., `"lang": "en_US"`) on any `res.users` records created for testing.
* For backend tours executing as the `admin` user, the test `setUp` method MUST explicitly set `self.env.ref('base.user_admin').lang = 'en_US'`.
* For public (unauthenticated) frontend tours, use the explicit language routing prefix in the starting URL (e.g., `/en_US/web`).

### 8. Headless Browser Authentication Context & URL Anchoring
Odoo's `HttpCase.start_tour()` spawns its headless browser in an unauthenticated public state by default. The standard Python `self.authenticate()` method ONLY authenticates the local `requests` session utilized for backend scraping, NOT the headless browser.
* Tours executing authenticated flows MUST explicitly pass the `login` keyword argument directly to the tour execution command.
* **CRITICAL URL ANCHORING:** When authenticating via `start_tour`, Odoo's default login routing may drop the user onto unpredictable views (like the Discuss app) instead of the expected view. You MUST explicitly provide the starting URL path as the first argument to `start_tour` (e.g., `self.start_tour("/odoo?debug=1", "tour_name", login="admin")`) to guarantee a deterministic DOM state. Note that Odoo 19 migrated the backend route from `/web` to `/odoo`.

### 9. Deterministic Input Simulation (Bypassing the `edit` helper)
Odoo's tour `edit` helper simulates typing character-by-character. This can cause severe validation races (e.g., backend constraints triggering on partially typed strings like `htt` for a URL) if the framework auto-saves before the final DOM `blur` or `change` event fires.
* If a text input is subject to strict backend format validation, tours MUST bypass the `edit` helper.
* Developers MUST manually inject the string value and dispatch the input events via pure JavaScript inside a `run: () => { ... }` block (e.g., `input.dispatchEvent(new Event('change', { bubbles: true }));`) to ensure complete synchronization between the DOM and the JS framework before auto-saving.

## Consequences
* **Positive:** Tours rely entirely on the native framework APIs. Complete decoupling is achieved without triggering XML schema violations or polluting CSS, and headless browser environments are strictly deterministic.
