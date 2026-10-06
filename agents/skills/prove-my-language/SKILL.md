---
name: prove-my-language
description: >-
  Audit technical or professional writing for explanatory completeness, vocabulary discipline,
  internal consistency, conceptual traceability, and cold-reader comprehensibility. Use when the
  author wants to prove that every material idea is either expressed in established vocabulary or
  adequately explained in the document itself. Triggers: prove my language, language audit, writing
  audit, explanatory completeness check, cold-reader pass, vocabulary discipline review.
version: 2
---

# Prove My Language

Reusable writing-audit skill, version 2.

## Purpose

Audit a piece of writing to determine whether a competent reader can reconstruct the author's
material ideas from the text itself, without relying on knowledge that exists only in the author's
head.

This is not primarily a style-editing skill. Its purpose is explanatory completeness, vocabulary
discipline, internal consistency, conceptual traceability, and precise use of coined or specialized
language.

## Core Test

For every material noun phrase, technical term, acronym, coined label, metaphor, state name,
mathematical symbol, framework name, imported concept, or specialized use of ordinary language,
prove one of the following:

1. The term is established vocabulary being used in its ordinary established sense; or
2. The writing itself adequately explains:
   - what the term means;
   - what function it performs;
   - what distinguishes it from adjacent concepts;
   - what inputs, outputs, states, relationships, or effects it involves where applicable; and
   - if the name is metaphorical or coined, why that name describes the mechanism.

If neither can be established, flag the term or passage.

## Operating Principles

- Do not manufacture issues merely to populate the report.
- Do not replace sophisticated language merely because it is specialized.
- Do not rewrite the author into a different voice.
- Do not remove useful technical detail.
- Prefer the smallest correction that makes the idea self-contained.
- Distinguish true ambiguity from language that is simply advanced but adequately defined.
- Treat the document as the primary source of truth. Do not silently supply missing explanations
  from outside knowledge.
- If outside knowledge is used to classify a term as established vocabulary, say so when that
  classification materially affects the result.
- Apply a materiality cutoff: prioritize language that controls system behavior, claim or argument
  scope, state transitions, authority allocation, technical causality, input/output semantics, or
  cross-document dependency. Consolidate repetitive low-severity issues rather than reporting every
  uncommon word.
- Stop after material coverage is achieved. Exhaustiveness means all material conceptual gaps are
  addressed, not that every unusual word receives its own finding.

## Audit Procedure

### 1. Build the Material-Term Inventory

Identify material:

- technical terms;
- coined terms;
- metaphors carrying technical meaning;
- abbreviations and acronyms;
- named states, gates, layers, stages, and transitions;
- formulas, variables, thresholds, and symbols;
- framework and architecture names;
- imported terminology from another document, discipline, or system;
- ordinary words being used in a specialized technical sense.

Ignore incidental words that do not carry conceptual load.

### 2. First-Use Test

Determine whether each material term is defined, established by context, or clearly inferable
before the document materially depends on it.

Flag:

- terms materially used before definition;
- acronyms used before expansion;
- symbols introduced without meaning;
- state names appearing before the state model is explained;
- imported concepts that assume the reader has read another document.

### 3. Coined-Term Test

For invented terminology, verify both:

- the formal or operational meaning; and
- why the chosen name fits.

A definition that explains only the mechanism but not the metaphor may be incomplete.

Example: if a mechanism is called a ratchet, the writing should explain why it is a ratchet:
validated knowledge is moved into a more durable deterministic state so the system does not
routinely fall backward into repeated rediscovery of the same known class.

### 4. Metaphor Test

Flag metaphors that carry technical meaning without a corresponding literal explanation.

Common examples include: airlock, frontier, ratchet, microscope, telescope, gate, lock, funnel,
memory, spine, envelope.

A metaphor passes when the text makes the represented mechanism explicit.

### 5. Established-Vocabulary Test

Do not demand definitions for field-standard terminology merely to create a glossary.

However, flag an established term when:

- it is used in a nonstandard sense;
- the document assigns it a specialized function;
- different relevant fields use it differently;
- misunderstanding it would materially alter the architecture or argument; or
- the author uses a familiar word as a term of art.

### 6. Overloaded-Term Test

Identify terms used with more than one meaning. Determine whether the distinction is intentional
and explicit.

Common examples: claim, state, authority, current, evidence, review, validation, closure,
admissibility, assurance, control.

Flag cases where the reader could reasonably interpret the same term differently in different
sections.

### 7. State-Model Test

For documents containing named states, statuses, stages, gates, or lifecycle conditions, identify
every state and determine:

- what causes entry;
- what permits exit;
- what transitions are allowed;
- what transitions are prohibited;
- whether two states overlap improperly;
- whether a state is used elsewhere with a different meaning;
- whether undefined states appear later in the document;
- whether the state is descriptive, advisory, or authority-conferring.

Flag transitions that depend on unstated guards.

### 8. Relationship Test

For every important pair of concepts, determine whether the relationship between them is actually
stated.

Common missing relationships include: input versus evidence, evidence versus conclusion,
confidence versus authority, uncertainty versus falsity, review versus approval, detection versus
enforcement, semantic result versus operational action, current state versus historical state,
proposal versus committed state, risk score versus routing decision.

Do not assume the reader will infer an architectural relationship merely because both concepts are
individually defined.

### 9. Authority-Boundary Test

Identify every component, actor, or process that can propose, classify, score, recommend, commit,
authorize, deny, promote, invalidate, release, or close a state. Determine:

- which component has authority for each action;
- what evidence, predicate, or gate is required;
- whether a probabilistic component can directly alter authoritative state;
- whether an advisory result can be mistaken for a committed result;
- whether denial, override, escalation, or exception authority is explicit; and
- whether the same authority boundary is described consistently throughout the document.

Flag any passage where the text states that a component "determines," "approves," "promotes,"
"closes," or "authorizes" something without making clear whether that act is advisory or
authority-conferring.

### 10. Causality Test

Where the author claims that A produces B, determine whether the mechanism connecting A and B is
stated.

Flag vague causality such as: improves robustness, increases trust, provides security, reduces
cost, ensures reliability, increases efficiency, prevents drift, improves accuracy — unless the
writing identifies the technical, logical, procedural, computational, or economic mechanism
producing the effect.

### 11. Internal-Consistency Test

Search for propositions that conflict with definitions or statements elsewhere.

Examples:

- one section says stages occur "in order" while another says order is optional;
- a component is mandatory in one section and optional in another;
- the same state has two different transition requirements;
- an element is described as deterministic in one section and probabilistic in another;
- a preferred embodiment later appears as though it were universally required;
- a generalized term later silently narrows to one implementation.

Treat these as high-value findings.

### 12. Scope and Abstraction Test

Determine whether the writing silently changes abstraction level.

Flag:

- software-specific terminology suddenly being used as if universal;
- a general mechanism narrowing to one implementation without saying so;
- an example becoming a requirement;
- a preferred embodiment being described later as mandatory;
- a portfolio-specific implementation being treated as synonymous with the generalized parent
  concept;
- a narrower child concept replacing its broader parent without explanation.

### 13. Implied-Knowledge Test

Look for ideas that are obvious to the author but not actually present in the text.

For each important passage ask:

- What would the author say if a technically competent stranger asked, "Why?"
- What exactly do you mean by that?
- How does that happen?
- What distinguishes this from the adjacent concept?
- What causes that state?
- What happens next?

If the likely answer contains material information absent from the document, flag it.

### 14. Symbol and Formula Test

For every formula, variable, threshold, score, or symbolic expression, verify that:

- every symbol is defined;
- units or scale are clear where material;
- directionality is clear;
- the consequence of crossing a threshold is stated;
- the formula is tied to the mechanism it controls;
- optional or illustrative formulas are identified as such when appropriate.

### 15. Example-to-Rule Test

Determine whether an example adequately illustrates the general concept without silently becoming
the definition of the concept.

Flag:

- examples broader than the stated rule;
- rules that cannot actually produce the example;
- examples introducing mechanisms not disclosed elsewhere;
- examples whose vocabulary is later treated as though it were mandatory architecture.

### 16. Negative-Case Test

For each load-bearing mechanism, determine whether the document explains at least one denial,
failure, ambiguity, exception, non-qualification, escalation, or unresolved path where such a path
is technically meaningful.

Ask:

- What causes the mechanism to refuse or defer?
- What happens when evidence is insufficient or contradictory?
- What happens when a threshold is not met?
- What happens when a downstream action is prohibited?
- Is unresolved state preserved, or does the document silently force a success/failure result?
- Does recovery preserve prior history or overwrite it?

Flag mechanisms whose success path is explained but whose failure or non-qualification path is
necessary to understand actual operation.

### 17. Cold-Reader Pass

After the detailed audit, perform one final pass as a knowledgeable reader with no access to the
author.

Identify every passage where that reader would reasonably ask:

- What does that mean?
- Why is it called that?
- How does that happen?
- What is the difference between those two things?
- What causes that state?
- What happens next?
- Why does that result follow?
- Is that a requirement or merely an example?
- Is this term being used conventionally or specially?

## Output Format

Produce the following sections.

### Section 1 — Overall Result

Assign one result:

- CLEAN
- MINOR LANGUAGE GAPS
- MATERIAL EXPLANATION GAPS
- STRUCTURAL EXPLANATION PROBLEM

Briefly explain the grade.

### Section 2 — High-Value Findings

For each meaningful issue provide:

- TERM / PASSAGE
- CATEGORY
- SEVERITY: Low / Moderate / High
- WHY IT WAS FLAGGED
- WHAT A COLD READER IS MISSING
- MINIMUM FIX

Order findings by severity and usefulness, not by page order.

Use these severity rules consistently:

- **High**: The gap prevents a competent reader from determining the described mechanism, state
  transition, authority boundary, or technical causality; creates a material internal
  contradiction; or materially weakens implementation or claim/argument support.
- **Moderate**: The concept can be inferred, but a competent reader could reasonably implement,
  interpret, or scope it differently because an important relationship, boundary, transition, or
  term is underexplained.
- **Low**: The term or passage is understandable in context but would benefit from a short
  definition, first-use expansion, cross-reference, or explanation of why a coined/metaphorical
  name fits.

If many low-severity findings repeat the same defect pattern, consolidate them into one pattern
finding with representative examples.

### Section 3 — Undefined or Underdefined Vocabulary

Provide a compact table with columns: Term, First material use, Classification, Adequately
explained?, Recommended action.

Classification should be one of: Established, Coined, Metaphorical, Specialized ordinary language,
Symbolic / mathematical, Imported concept.

### Section 4 — Internal Inconsistencies

For each true inconsistency:

- identify the first proposition;
- identify the conflicting proposition;
- explain the conflict;
- state whether the conflict is substantive or terminological;
- propose the smallest correction.

Do not treat merely different examples, optional embodiments, or levels of abstraction as
contradictions unless the text makes them conflict.

### Section 5 — Proposed Language

For every issue worth correcting, provide the smallest useful addition or replacement.

Prefer one or two sentences over wholesale rewriting.

Preserve the author's terminology and voice unless the terminology itself is the defect.

## Final Determination

End with this question and answer:

**Could a competent cold reader reconstruct what the author means, how the important concepts
relate, and why the named mechanisms are called what they are without asking the author for
missing information?**

Answer one of: YES / MOSTLY / NO.

Then identify only the remaining blockers.
