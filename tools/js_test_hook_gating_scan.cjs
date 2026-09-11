#!/usr/bin/env node
// This software is distributed under the terms of the Affero General Public License (AGPL-3).
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Real AST-based scanner for check_js_test_hook_gating.py (ADR 0094). Reads a JSON array of
// absolute file paths on stdin, parses each with acorn (already vendored under
// hams_shared/node_modules as an eslint transitive dependency -- no new dependency added, same
// reuse js_function_scan.cjs already established), and writes a JSON array of
// {file, violations: [{line, snippet}]} to stdout, one entry per file that parsed successfully.
// A file that fails to parse (real syntax error) is silently omitted, matching
// js_function_scan.cjs's own behavior on a bad file -- check_js_syntax.py is the linter
// responsible for catching a genuine syntax error, not this one.
//
// What this looks for: a string-literal comparison against a `.type` member ending in a name
// starting with TEST_ (`event.data.type === 'TEST_FOO'`, `message.type == "TEST_BAR"`) sitting
// directly in an `if` statement at the top level of some block, with no `if (!TEST_HOOKS_ENABLED)
// return;`-shaped guard appearing earlier among that SAME block's own direct statements. Scoped
// to top-level-of-block (not descending into arbitrary nesting depth to find a comparison
// buried three ifs deep) -- ADR 0094's own real motivating examples both structure their guard
// and every TEST_* branch as direct siblings in one addEventListener('message', ...) handler's
// top-level block, and this deliberately narrow scope keeps the check exact rather than
// heuristic-guessing across arbitrary nesting.

const acorn = require(require("path").join(__dirname, "..", "node_modules", "acorn"));
const fs = require("fs");

// Recursively searches `node` (an expression subtree, e.g. an IfStatement's own `.test`) for a
// BinaryExpression matching `<expr>.type === 'TEST_...'` (either operand order, === or ==).
// Bounded to expression nodes only (never crosses into a nested function/block) so a `.type`
// comparison inside an unrelated nested callback isn't mistakenly attributed to this statement.
function findTestTypeLiteral(node) {
    if (!node || typeof node !== "object") return null;
    if (node.type === "BinaryExpression" && (node.operator === "===" || node.operator === "==")) {
        for (const [a, b] of [
            [node.left, node.right],
            [node.right, node.left],
        ]) {
            if (
                a &&
                a.type === "MemberExpression" &&
                a.property &&
                (a.property.name === "type" || a.property.value === "type") &&
                b &&
                b.type === "Literal" &&
                typeof b.value === "string" &&
                /^TEST_/.test(b.value)
            ) {
                return { literal: b.value, node };
            }
        }
    }
    // Only descend into pure-expression combinators (&&, ||, parens via no extra node in acorn,
    // unary !) -- never into a nested function/arrow, which would be a different scope entirely.
    if (node.type === "LogicalExpression") {
        return findTestTypeLiteral(node.left) || findTestTypeLiteral(node.right);
    }
    if (node.type === "UnaryExpression" && node.operator === "!") {
        return findTestTypeLiteral(node.argument);
    }
    return null;
}

// Does `stmt` look like `if (!TEST_HOOKS_ENABLED) return;` or
// `if (!TEST_HOOKS_ENABLED) { return; ...}` -- the exact guard shape ADR 0094 mandates.
function isTestHooksGuard(stmt) {
    if (stmt.type !== "IfStatement") return false;
    const test = stmt.test;
    if (!test || test.type !== "UnaryExpression" || test.operator !== "!") return false;
    if (!test.argument || test.argument.type !== "Identifier" || test.argument.name !== "TEST_HOOKS_ENABLED") {
        return false;
    }
    const consequent = stmt.consequent;
    if (!consequent) return false;
    if (consequent.type === "ReturnStatement") return true;
    if (consequent.type === "BlockStatement" && consequent.body.length && consequent.body[0].type === "ReturnStatement") {
        return true;
    }
    return false;
}

// Checks one block's own direct statement list (not nested blocks -- those are visited
// independently when the outer walk reaches them as their own BlockStatement node).
function checkBlock(block, violations) {
    let guarded = false;
    for (const stmt of block.body) {
        if (isTestHooksGuard(stmt)) {
            guarded = true;
            continue;
        }
        if (stmt.type === "IfStatement") {
            const match = findTestTypeLiteral(stmt.test);
            if (match && !guarded) {
                const loc = stmt.loc ? stmt.loc.start.line : null;
                violations.push({ line: loc, literal: match.literal });
            }
        }
    }
}

// Generic recursive walk (no acorn-walk dependency installed, matching js_function_scan.cjs's
// own precedent) -- visits every node reachable via any own-enumerable property, calling
// checkBlock on every BlockStatement found at any depth.
function walk(node, violations, seen) {
    if (node === null || typeof node !== "object") return;
    if (seen.has(node)) return;
    seen.add(node);

    if (Array.isArray(node)) {
        for (const child of node) walk(child, violations, seen);
        return;
    }

    if (node.type === "BlockStatement") {
        checkBlock(node, violations);
    }

    for (const key of Object.keys(node)) {
        if (key === "loc" || key === "range" || key === "start" || key === "end") continue;
        walk(node[key], violations, seen);
    }
}

function main() {
    const input = fs.readFileSync(0, "utf-8");
    const files = JSON.parse(input);
    const results = [];
    for (const file of files) {
        let source;
        try {
            source = fs.readFileSync(file, "utf-8");
        } catch {
            continue;
        }
        let tree;
        try {
            tree = acorn.parse(source, { ecmaVersion: "latest", sourceType: "module", locations: true });
        } catch {
            continue;
        }
        const violations = [];
        walk(tree, violations, new Set());
        if (violations.length) {
            results.push({ file, violations });
        }
    }
    process.stdout.write(JSON.stringify(results));
}

main();
