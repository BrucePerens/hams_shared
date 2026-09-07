---
name: debugging-silent-js-failures
description: Playbook for a component/interaction that silently never mounts or completes in Odoo/Owl, with no visible error -- built after multiple sessions failed to root-cause exactly this shape of bug before finally succeeding. Use before re-deriving hypotheses from scratch.
---

# Debugging a Silent Owl/Odoo Failure (No Visible Error, Nothing Ever Completes)

**The bottom line, in Bruce's own words**: when there's a mystery interaction inside a large body
of code, instrumenting the big piece of code directly is a good way to go -- even when "the big
piece of code" is third-party or framework code (Odoo core, Owl itself), not just this project's
own files. Don't default to reasoning about unfamiliar framework internals from outside via
hypothesis after hypothesis; open the actual source, find the real call chain, and add real
instrumentation directly into it.

This exact shape of bug -- a component (or interaction, or async chain) that simply never
finishes, with no console error, no rejected-promise warning, nothing -- cost multiple separate
debugging sessions on `ham_shack.web_shack` before the real root cause (a QWeb template compile
error, `docs/proposals/OFFLINE_HAM_OPERATION.md`, 2026-09-07) was finally found. The earlier
sessions, and this one's own first several hours, all made the same category of mistake: guessing
a plausible hypothesis (registration ordering, console suppression, a network probe hanging,
the component sitting outside the wrong DOM root) and testing it via static code reading or a
single live check, one at a time, rather than directly and exhaustively instrumenting the actual
call chain. Follow this order instead.

## Step -1: search this project's own accumulated history for prior attempts, before anything else

This exact bug (`ham_shack.web_shack` never mounting) had already been investigated across at
least two prior sessions before this one found the real cause -- not just the "several iterations"
a proposal doc's own status header mentioned in passing. A `grep` of `night_shift_history.md` for
the component/file names involved turned up a 2026-09-03 session that had already ruled out *six*
separate hypotheses (a QWeb context-key collision, a plain JS syntax error, a `DuplicatedKeyError`,
the wrong DOM scan root, a `KeyNotFoundError` from a missing registration, and a lazy-bundle
load-order race) and had **explicitly named the real next step** -- "whether `App.createRoot()`/
`root.mount()` can itself throw for a reason specific to `WebShackTemplate`... not yet checked for
a *compile-time* QWeb error" -- as item (b) of its own honest "still open" list, before correctly
stopping (per `advisor()`'s own guidance) rather than inventing a seventh guess. This session did
not read that history closely enough before starting, and as a direct result re-derived several of
the same already-ruled-out hypotheses (registration ordering, a JS syntax error, the wrong DOM scan
root) from scratch, instead of going straight to the one lead the prior session had already pointed
at.

**Before forming a single hypothesis of your own, grep the project's own accumulated history
(`night_shift_history.md`, or wherever this project's own session-to-session institutional memory
lives) for the component/file/symptom names involved.** A prior session's own honest "still open,
here's exactly what to check next" note is not narrative color -- it is often the single fastest
path to the real answer, and skipping it is how the same ground gets re-covered by session after
session. If a prior write-up names specific untested candidates, check those *first*, in the order
given, before reasoning up new ones.

## Step 0: check whether a fast, offline verification tool already exists or should

Before touching a live browser at all: is the suspected failure a **compile-time** problem (a
template, a script, a config) rather than a **runtime logic** problem? A compile/syntax error is
enormously cheaper to find with a direct, standalone check than by instrumenting a live app.
`hams_shared/tools/check_owl_templates.py` (built exactly because of this investigation) compiles
every real Owl `[t-name]` template via a real headless Chrome and reports the *complete,
untruncated* error for any that fail -- run it first:

```bash
python3 hams_shared/tools/check_owl_templates.py <module_dir>
```

This is wired into `run_linters.py` as a required gate, so a genuinely new template bug should
already fail CI before it ever reaches a "why doesn't this mount" investigation -- but if you're
debugging an *existing* silent failure, still run it directly against the specific module first.
It would have found this exact bug (a JS numeric separator, `1_000_000`, inside a `t-out`
expression -- Owl's own expression compiler mangles it into invalid generated JS) in about five
seconds, with a clear file/template-name/error report, instead of the many hours the live
debugging path below actually took.

## Step 1: if it's a real runtime mystery, bracket every suspected call with BOTH sync and async error capture

The single mistake that cost the most real time in this investigation: wrapping only the
**asynchronous** half of a suspected call (`somePromise.then(onOk, onErr)`) without also wrapping
the **synchronous** half (the call that *produces* the promise) in its own `try/catch`. A function
can throw synchronously before it ever returns a promise for you to attach a rejection handler to
-- in that case, a `.then(ok, err)` attached to whatever *did* get returned never fires at all,
and the real exception can get silently absorbed several frames up the call chain if nothing there
awaits or catches it either (exactly what happened here: `Colibri.mountComponent()`'s own real,
unmodified source calls `root.mount()` with no `await` and no `.catch()`, several async frames
deep inside `Interaction.start()` -> `Colibri.start()` -> `startInteraction()`, so a synchronous
throw from `prepareRoot()`/`root.mount()`'s own `validateProps` step became a promise rejection
nobody in the whole chain ever awaited or caught).

The concrete pattern, once you've identified the suspected call site:

```js
let result;
try {
    result = suspectFunction(...);
    console.error("DIAG: suspectFunction returned synchronously (no throw)");
} catch (syncErr) {
    console.error("DIAG: suspectFunction THREW SYNCHRONOUSLY: " + syncErr.message + "\n" + syncErr.stack);
    throw syncErr;
}
if (result && typeof result.then === "function") {
    result.then(
        () => console.error("DIAG: RESOLVED"),
        (asyncErr) => console.error("DIAG: REJECTED: " + asyncErr.message + "\n" + asyncErr.stack)
    );
}
```

Do this at **every** suspected link in the chain, working backward from the last point you have
confirmed evidence something ran, toward the point you don't yet know completed -- a real
bisection, not a single guess. In this investigation that meant instrumenting, in order:
`InteractionService.startInteractions()` (confirmed: scan ran, found the target, dispatched to
`_startInteraction`) -> `_startInteraction()` (confirmed: reached the `Colibri` branch) ->
`Colibri.mountComponent()` (confirmed: called) -> only at this point did wrapping `prepareRoot()`
itself in a sync try/catch (not just the `.then()` on its return value) surface the real,
previously-invisible exception.

## Step 2: editing real Odoo/Owl core files for temporary instrumentation

Sometimes the suspected failure is inside Odoo core or Owl itself (`/usr/lib/python3/
dist-packages/odoo/...`), not this codebase's own files. That's fine to do -- this dev box's own
"not yet deployed, recoverable via git" posture doesn't extend to *this* directory (it's not a git
repo at all, it's an installed system package), so the discipline here is manual backup, not git:

```bash
# 1. Back up the real file before touching it.
sudo cp /usr/lib/python3/dist-packages/odoo/.../interaction_service.js /path/to/scratchpad/backup/

# 2. Copy it somewhere writable to edit (the real file is root-owned).
sudo cp /usr/lib/python3/dist-packages/odoo/.../interaction_service.js /path/to/scratchpad/edit.js
sudo chown $(whoami):$(whoami) /path/to/scratchpad/edit.js

# 3. Edit the writable copy, then validate syntax before deploying.
node --check /path/to/scratchpad/edit.js

# 4. Deploy back with the original ownership restored.
sudo cp /path/to/scratchpad/edit.js /usr/lib/python3/dist-packages/odoo/.../interaction_service.js
sudo chown root:root /usr/lib/python3/dist-packages/odoo/.../interaction_service.js
sudo chmod 644 /usr/lib/python3/dist-packages/odoo/.../interaction_service.js

# 5. Once you have your answer, restore the real backup the same way, and diff to confirm
#    the restored file is byte-identical to the original before considering the revert done.
```

Never leave temporary instrumentation like this deployed at the end of a session -- restore from
the real backup and `diff` to confirm, every time.

## Step 3: don't console.log/console.error large diagnostic payloads through CDP

A real, separate trap this investigation hit: Chrome DevTools Protocol's own console-message
transport previews/truncates large string arguments before they ever reach a `Runtime.
consoleAPICalled` event -- a `console.error()` call with a large string (this investigation's own
case: an Owl compile error embedding tens of KB of generated JS) gets **silently cut off**, with no
indication that truncation happened. This looks exactly like "the real data is incomplete" and can
send you chasing a phantom (this investigation briefly suspected the *generated code itself* was
malformed/incomplete, when it was only the console transport that was incomplete). A first attempt
at working around this by chunking the string into many small `console.error()` calls (each
prefixed with an index) *also* failed, non-obviously: under load, multiple concurrent/rapid console
events do not reliably preserve call order in the resulting log, so naive reassembly by log order
(or even by re-sorting on an embedded index, if more than one compile attempt is in flight at once)
can interleave content from different calls into a garbled mess that still looks plausible.

The real fix: don't route large diagnostic data through `console.*` at all. Use `Runtime.evaluate`
with `returnByValue: true` and have your instrumented code `return` the data directly (as the
expression's own value, or via an `async` IIFE) -- this comes back as the complete, untruncated
result of that one CDP call, with no console transport, no truncation, no ordering ambiguity.
`hams_shared/tools/check_owl_templates.py` is a full worked example of this: it loads a real
`owl.js` into a real headless Chrome exactly this way, and gets the complete generated-code error
back as one clean `Runtime.evaluate` result.

## Step 4: when you find the fix, ask whether it deserves a permanent, fast linter rule

A live-instrumentation debugging session is expensive and shouldn't need repeating for the same
bug shape. Once you've found a real root cause, ask: could a cheap, targeted static check (a
regex in `check_burn_list.py`, or a small standalone script) catch this exact pattern in the future
without needing a live browser at all? The numeric-separator bug this investigation found got both:
a fast regex rule in `check_burn_list.py` (catches the specific known trigger instantly) *and* the
general `check_owl_templates.py` tool (catches any Owl template compile error, not just this one
pattern) -- the two are complementary, not redundant: the regex is a fast, narrow tripwire for a
known mistake; the full-compile check is the slower, thorough safety net for anything else.
