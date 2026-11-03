# ADR 0113: Anchors Explain Their Function or Data Structure Completely

## Status
Accepted (Bruce, 2026-11-03)

## Scope
Every semantic anchor (`[@ANCHOR: <id>]`, see ADR 0090 and `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md`) in any of
the three repositories, in any language. It extends ADR 0090, which requires an anchor, a test and a
documentation reference for every function: this ADR says what the documentation reference must contain, and
adds anchors for data structures.

## Context
The anchor ratchet reached zero findings, but many documentation entries are one line ("does X") that name the
anchor so the linter is satisfied and tell a reader nothing. A person (or an agent) who wants to call a function
or change a data structure has to read the code to learn its arguments, its return values, whether it blocks,
what it must not be called with, or what a field means. Bruce's decision: the anchors must carry the
explanation, formatted for people.

## Decision
1. **What is anchored.** Every function (as ADR 0090) **and every data structure**: structs, enums, classes,
   dataclasses, type aliases, database models and tables, wire and file formats (JSON messages, config files,
   protocol frames), and sets of related constants. A data structure gets an `[@ANCHOR: ...]` at its
   definition and a documentation entry, like a function.
2. **Where it is explained.** In the module's `README.md` (or the story document ADR 0090 names), one entry per
   anchor, under the anchor id. The entry is written for a human reader and is complete without opening the
   code. The code comment at the anchor may be short and points to the entry; the README entry is the source
   of truth, and a change to the code changes the entry in the same pull request.
3. **A function's entry contains all of:**
   - **Purpose:** what it is for, why it exists, who calls it and when.
   - **Call interface:** the full signature; every argument (name, type, units, valid range or values, whether
     it is borrowed, owned, mutated or retained); the return value (type, meaning, units, every special value);
     every error, exception or panic and what causes it.
   - **Special concerns:** whether it is asynchronous, blocking or must run on a particular thread or task; locks
     it takes and the order; side effects (files, network, hardware, the transmitter, the database); idempotence
     and retry safety; ordering and timing requirements; performance cost where it matters; security and secret
     handling; and safety limits (anything that can key a transmitter must say how the lock, band check and
     transmit gate apply).
   - **Example** where it makes the use clearer than the prose.
   - **See also:** the test anchors, related functions and data structures (links).
4. **A data structure's entry contains all of:** its purpose and where it lives; the full definition; **every
   field** (name, type, units, meaning, valid values, default, who sets it and who reads it); invariants that
   always hold; lifetime and ownership (who creates, shares, persists and discards it); the serialized or wire
   form with versioning and compatibility rules; concurrency (who may mutate it and under what lock); and the
   functions that create, read and change it (links).
5. **Formatting for human reading.** One consistent template per README: a heading per anchor, a short
   one-paragraph purpose, then tables for arguments and fields (columns: name, type, meaning/units/range),
   then short labelled paragraphs for returns, errors, concurrency and notes. Code in fences, ids in
   backticks, plain sentences, no stub text, no copy-pasted signature as the whole explanation. A README that
   would take a reader more than a screen to find an entry gets a table of contents.
6. **From now on.** New and changed anchored code meets this standard in the same pull request that adds or
   changes it; an entry that is a stub does not pass review. When you touch an anchored function or structure
   that still has a short entry, bring the entry up to this standard in that pull request. Agents are told in
   `AGENTS.md`.
7. **Existing entries** are brought up to the standard by the low-priority to-do
   `night_shift_todo/low/document-every-anchor-completely-*.md`, module by module, in small pull requests.
8. **Enforcement.** Existence of the anchor, the test and the entry stays enforced by `verify_anchors.py`.
   Extending it to find data-structure definitions without anchors, and to check the shape of an entry
   (required headings present, argument and field tables not empty, no stub entry), is part of that to-do.
   No baseline file is created for the new checks: findings go to zero and stay at zero, as ADR 0090 decided.

## Consequences
More documentation work up front and per change; much less reading of code to use or change it; better material
for tests and reviews. Entries can drift from the code, so a change to the code must change its entry.
