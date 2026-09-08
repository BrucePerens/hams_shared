# Draft: GitHub issue for odoo/odoo

Not filed. Per project convention (Bruce handles all outward-facing/public actions himself, e.g.
pushing to remotes), this is a ready-to-file draft, not something filed automatically. To file it:
open https://github.com/odoo/odoo/issues/new and paste the title/body below. Checked first (not
assumed): searched `odoo/odoo`'s own issue tracker for `"Illegal invocation" navigator`,
`createMock navigator`, `mockNavigator platform`, `barcode_service "Illegal invocation"`, and `hoot
navigator.platform` -- no existing issue found, so this is genuinely new, not a duplicate.

---

**Title:** `hoot`'s mocked `window.navigator` throws `Illegal invocation` on `navigator.platform`
(missing from `createMock`'s override list) -- breaks any unit test that transitively loads
`barcode_service.js`

**Body:**

### Odoo Version

- [ ] 16.0
- [ ] 17.0
- [ ] 18.0
- [x] 19.0 (also present in the current `master`/`saas-*` `hoot/mock/navigator.js` and
  `feature_detection.js` sources -- neither has changed in a way that would fix this)

### Steps to Reproduce

Run *any* hoot unit-test suite in a module whose bundle transitively loads
`@barcodes/barcode_service` (which most `web.assets_unit_tests` bundles do, since hoot's own
dry-run discovery phase always loads every installed addon's test-adjacent modules regardless of
which `tag=` filter is requested -- so this reproduces on virtually any hoot run, not just ones
that test barcode scanning):

```
python3 odoo-bin --test-enable --test-tags /<any_module>:<AnyHootSuite> --stop-after-init
```

### Log Output

```
Error: Error while loading "@barcodes/barcode_service":
TypeError: Illegal invocation
    at isIOS (.../web.assets_unit_tests_setup.min.js:...)
    at isMobileOS (.../web.assets_unit_tests_setup.min.js:...)
    at Object.fn (.../web.assets_unit_tests_setup.min.js:...)  <- barcode_service.js's own
                                                                    isMobileChrome: isMobileOS() && ...
    at ModuleSetLoader.startModule (...)
    ...
    at async Runner.dryRun (...)
    at async runTests (...)
```

### Root cause

`web/static/lib/hoot/mock/window.js`'s `WINDOW_MOCK_DESCRIPTORS.navigator` installs
`hoot/mock/navigator.js`'s `mockNavigator` as `window.navigator` for the whole test session.
`mockNavigator` is built by `createMock(navigator, {...})`, whose override object explicitly lists
`clipboard`, `maxTouchPoints`, `permissions`, `sendBeacon`, `serviceWorker`, `userAgent`, and
`vibrate` -- but not `platform`.

`createMock()`'s own "copy original descriptors" loop only copies the *own* enumerable keys of
whatever object its "walk up the prototype chain while there are no own keys" search lands on
(`hoot_utils.js`, `createMock()`). `platform` is defined on `Navigator.prototype` (a native getter,
not an own key at the level that search reaches), so it is neither copied with a safe delegating
getter nor covered by the explicit override list. The result: `mockNavigator.platform` falls
through to plain prototype-chain lookup and invokes the **real, native**
`Navigator.prototype.platform` getter with `mockNavigator` (not a genuine `Navigator` instance) as
its receiver. Chrome's branded-accessor check on that native getter rejects the non-genuine
receiver with `TypeError: Illegal invocation`.

Any application code that reads `navigator.platform` while hoot's window mock is active hits this
-- `web/static/src/core/browser/feature_detection.js`'s `isIOS()` is one real, core example
(`browser.navigator.platform === "MacIntel"`), reached from `isMobileOS()`, reached from
`barcode_service.js`'s own top-level `isMobileChrome: isMobileOS() && isBrowserChrome()` -- which
runs unconditionally the moment that module's factory executes, which hoot's dry-run phase does for
every discoverable module regardless of the requested test tag.

### Suggested fix

Add an explicit `platform` override to `hoot/mock/navigator.js`'s `createMock(navigator, {...})`
call, delegating to the real navigator exactly the way the existing `userAgent`/`sendBeacon`/
`vibrate` overrides already do:

```js
export const mockNavigator = createMock(navigator, {
    // ...existing overrides...
    platform: { get: () => navigator.platform },
});
```

### Verified

Reproduced on a clean `19.0` checkout with no other modules installed beyond `web`/`base`. Applied
the one-line fix above directly to the installed package and confirmed a previously-failing hoot
suite (any suite whose bundle loads `barcode_service.js`) now runs to completion instead of
aborting with `Illegal invocation`.

Happy to open a PR with this exact fix if useful.
