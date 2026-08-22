// This software is distributed under the terms of the Affero General Public License (AGPL-3).
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Shared ESLint config for hams_com and hams_open, run by
// tools/run_linters.py as a required (fail, not warn) CI gate -- the
// direct JS-side analogue of flake8 on the Python side. Every rule below
// is pinned to "error" explicitly, including ones the underlying presets
// only set to "warn" by default, because a warning nobody is required to
// act on is not a linter, it's a suggestion.
//
// Two promise-safety layers, verified empirically to be genuinely
// complementary, not redundant:
//   - eslint-plugin-promise catches syntactic .then()-chain mistakes
//     (missing .catch(), nested chains, wrong resolve/reject names).
//   - @typescript-eslint/no-floating-promises (type-aware, via
//     ../tsconfig.json + checkJs -- works on plain .js files, no
//     TypeScript migration needed) catches a *different* mistake:
//     a bare, unawaited call to a function that returns a Promise, with
//     no .then()/.catch() involved at all. Confirmed by hand that
//     eslint-plugin-promise does NOT fire on that second pattern, which
//     is exactly the shape of the real bug in web_transceiver.js that
//     motivated adding this config in the first place.

const js = require("@eslint/js");
const promisePlugin = require("eslint-plugin-promise");
const globals = require("globals");
const tseslint = require("typescript-eslint");

// Elevate "warn" -> "error" (the actual "fail rather than warn" ask).
// Rules the preset deliberately leaves "off" (no-native, avoid-new -- both
// stylistic opinions about whether to use the Promise constructor at all,
// not correctness issues) stay off; this must not be the thing that
// silently turns them on.
const promiseRulesAllErrors = Object.fromEntries(
  Object.entries(promisePlugin.configs["flat/recommended"].rules)
    .filter(([, severity]) => severity !== "off")
    .map(([rule]) => [rule, "error"])
);

module.exports = tseslint.config(
  js.configs.recommended,
  {
    plugins: { promise: promisePlugin },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2021,
        // Odoo OWL module system globals referenced throughout this
        // codebase's static assets.
        odoo: "readonly",
        owl: "readonly",
        // Third-party visualization libraries loaded as page-level
        // globals via Odoo asset bundles (script tags), not ES imports.
        THREE: "readonly",
        d3: "readonly",
        L: "readonly",
        topojson: "readonly",
        satellite: "readonly",
      },
    },
    rules: {
      ...promiseRulesAllErrors,
      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "no-undef": "error",
    },
  },
  {
    // caching/static/src/sw/sw.js is served through a controller that does
    // literal string substitution on these two tokens before the file ever
    // reaches a browser (see caching/controllers/main.py) -- they're build
    // -time placeholders, not real identifiers, so they can never be
    // imported/declared in the source file itself.
    files: ["**/caching/static/src/sw/sw.js"],
    languageOptions: {
      globals: {
        __MAX_FILE_SIZE_BYTES__: "readonly",
        __MAX_STORAGE_BYTES__: "readonly",
      },
    },
  },
  {
    // ics_forms's ics-form.js is genuinely dual-context: a normal browser
    // component, but its `process.argv[1].endsWith('ics-form.js')`-guarded
    // block is a real Node CLI validator, actually invoked via
    // `subprocess.run(["node", node_script, ...])` in
    // ingest/validate_forms.py -- confirmed before carving this out.
    // (story-components.js had the identical-looking pattern but no
    // real caller anywhere in the repo; that was dead scaffolding, since
    // removed, not a second legitimate case of this.) Scoped to this one
    // file rather than weakening no-undef's ability to catch an
    // *accidental* Node-global reference anywhere else.
    files: ["**/ics_forms/static/src/js/components/ics-form.js"],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
  {
    // Type-aware layer: only the two rules that actually justify the
    // extra weight of type-checking plain JS, not the full
    // recommendedTypeChecked preset (which assumes a codebase designed
    // for strict typing and would be noisy here for no real benefit).
    files: ["**/*.js"],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        project: "./tsconfig.json",
        tsconfigRootDir: __dirname,
      },
    },
    plugins: { "@typescript-eslint": tseslint.plugin },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
    },
  },
  {
    ignores: [
      "**/node_modules/**",
      "**/target/**",
      "**/*.min.js",
      "**/static/lib/**",
      // Python virtualenvs can bundle their own vendored JS (e.g.
      // urllib3's Emscripten/Pyodide worker) as installed-package data,
      // not application code -- same reasoning as node_modules/ above.
      // First found via daemons/hams_simulated_bots/.venv/.
      "**/.venv/**",
      "**/venv/**",
      // This config file is itself a Node/CommonJS tooling script, not
      // application code -- not covered by tsconfig.json's static/**
      // scope, and not meant to follow the app-code ruleset (browser
      // globals, promise rules aimed at Odoo JS) at all.
      "**/eslint.config.js",
    ],
  }
);
