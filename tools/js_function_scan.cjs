#!/usr/bin/env node
// This software is distributed under the terms of the Affero General Public License (AGPL-3).
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Real AST-based function-boundary scanner for check_js_function_test_anchors.py (ADR 0090's
// JS sub-track). Reads a JSON array of absolute file paths on stdin, parses each with acorn
// (already vendored under hams_shared/node_modules as an eslint transitive dependency -- no new
// dependency added), and writes a JSON array of {file, functions: [{name, start, end}]} to
// stdout, one entry per file that parsed successfully. A file that fails to parse (real syntax
// error, or a syntax acorn's own ecmaVersion doesn't cover) is silently omitted -- matching
// check_function_test_anchors.py's own scan_file behavior on a Python SyntaxError, not crashing
// the whole batch over one bad file.
//
// Scope, matching check_function_test_anchors.py's own Python scope as closely as JS's much
// richer function-expression grammar allows: only NAMED, independently-callable units --
// function declarations, a class's own methods (including getters/setters, excluded here since
// they're rarely independently meaningful test targets and Odoo's own OWL components lean on
// plain methods), and a function/arrow-function expression assigned directly to a variable,
// object property, or class field. An anonymous function passed inline as a callback argument
// (`el.addEventListener("click", () => {...})`, `.then(function () {...})`) is deliberately NOT
// counted -- the same "not an independently testable unit" reasoning
// check_function_test_anchors.py's own module docstring gives for excluding Python's nested
// closures, since a JS inline callback is architecturally the same thing.

const acorn = require(require("path").join(__dirname, "..", "node_modules", "acorn"));
const fs = require("fs");

function functionName(node, fallback) {
    if (node.id && node.id.name) return node.id.name;
    return fallback;
}

// Real, tailored recursive walk (no acorn-walk dependency installed) -- visits every node
// reachable via any own-enumerable property so a function nested at any real depth (inside an
// if-block, a ternary, an object literal passed to a call) is still found, while only YIELDING
// the specific named-unit shapes described above.
function walk(node, classStack, out, seen) {
    if (node === null || typeof node !== "object") return;
    if (seen.has(node)) return;
    seen.add(node);

    if (Array.isArray(node)) {
        for (const child of node) walk(child, classStack, out, seen);
        return;
    }

    if (typeof node.type === "string") {
        // Every match below `return`s immediately after recording itself --
        // deliberately NOT falling through to the generic recursion loop --
        // so a closure defined inside a matched function/method's own body
        // is never independently counted as its own unit, the same "nested
        // closures are not independently testable" rule
        // check_function_test_anchors.py's own `_direct_functions` enforces
        // for Python by design (never descending into a FunctionDef's own
        // body). Confirmed live: without this, `function outer() { const
        // helper = () => {...}; }` counted `helper` as a second, separate
        // gap/unit, which is wrong -- it's a private implementation detail
        // of `outer`, not something a test would call directly.
        if (node.type === "FunctionDeclaration") {
            if (node.id) out.push({ name: node.id.name, node });
            return;
        } else if (node.type === "MethodDefinition") {
            const keyName = node.key && (node.key.name || node.key.value);
            if (keyName && node.value) {
                const qualname = classStack.length
                    ? `${classStack[classStack.length - 1]}.${keyName}`
                    : keyName;
                out.push({ name: qualname, node: node.value });
            }
            return;
        } else if (
            node.type === "PropertyDefinition" &&
            node.value &&
            (node.value.type === "FunctionExpression" ||
                node.value.type === "ArrowFunctionExpression")
        ) {
            // A class field initialized to an arrow/function expression --
            // Odoo OWL's own common `onClick = () => {...}` class-field-
            // handler idiom, a real named, independently testable unit
            // just like a MethodDefinition, not a nested closure.
            const keyName = node.key && (node.key.name || node.key.value);
            if (keyName) {
                const qualname = classStack.length
                    ? `${classStack[classStack.length - 1]}.${keyName}`
                    : keyName;
                out.push({ name: qualname, node: node.value });
            }
            return;
        } else if (node.type === "VariableDeclarator" && node.init) {
            if (
                (node.init.type === "FunctionExpression" ||
                    node.init.type === "ArrowFunctionExpression") &&
                node.id &&
                node.id.name
            ) {
                out.push({ name: node.id.name, node: node.init });
                return;
            }
        } else if (
            node.type === "Property" &&
            node.value &&
            (node.value.type === "FunctionExpression" ||
                node.value.type === "ArrowFunctionExpression")
        ) {
            const keyName = node.key && (node.key.name || node.key.value);
            if (keyName) {
                out.push({ name: keyName, node: node.value });
                return;
            }
        } else if (node.type === "ClassDeclaration" && node.id) {
            classStack = classStack.concat([node.id.name]);
        }
    }

    for (const key of Object.keys(node)) {
        if (key === "loc" || key === "start" || key === "end" || key === "range") continue;
        walk(node[key], classStack, out, seen);
    }
}

function scanFile(filepath) {
    let content;
    try {
        content = fs.readFileSync(filepath, "utf-8");
    } catch (e) {
        return null;
    }
    let tree;
    try {
        tree = acorn.parse(content, {
            ecmaVersion: "latest",
            sourceType: "module",
            locations: true,
            allowHashBang: true,
        });
    } catch (e) {
        return null;
    }
    const found = [];
    walk(tree, [], found, new Set());
    const functions = found.map((f) => ({
        name: f.name,
        start: f.node.loc.start.line,
        end: f.node.loc.end.line,
    }));
    return { file: filepath, functions };
}

function main() {
    let input = "";
    process.stdin.on("data", (chunk) => {
        input += chunk;
    });
    process.stdin.on("end", () => {
        let files;
        try {
            files = JSON.parse(input);
        } catch (e) {
            process.stderr.write("Invalid JSON input: " + e.message + "\n");
            process.exit(1);
        }
        const results = [];
        for (const filepath of files) {
            const result = scanFile(filepath);
            if (result) results.push(result);
        }
        process.stdout.write(JSON.stringify(results));
    });
}

main();
