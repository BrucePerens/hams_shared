# ADR 0084: Strict Content Security Policy (CSP) Preparation and Inline Style Prohibition

**Date:** 2026-07-23  
**Status:** Accepted  

## Context

As the platform evolves, security against Cross-Site Scripting (XSS) and data injection vulnerabilities becomes a paramount concern, particularly for user-facing applications and auto-generated content (e.g., dynamically generated ICS HTML forms, SVG renderings). 

A critical layer of defense against these vulnerabilities is the implementation of a strict Content Security Policy (CSP). A strict CSP typically forbids the use of the `unsafe-inline` directive for both scripts (`script-src`) and styles (`style-src`). 

Historically, some components and backend parsers (such as the PDF to HTML rendering scripts) relied heavily on inline `style="..."` attributes to compute and apply dynamic positioning coordinates (e.g., `style="left: 10%; top: 20%;"`). While functionally convenient during development, a strict CSP will silently block these styles, causing severe layout degradation, hidden content, or total visual collapse of the interface.

To prepare the site for full CSP compliance without relying on `unsafe-inline`, we must proactively eliminate the architectural reliance on inline styles and un-nonced script injections across all codebases.

## Decision

1. **Prohibition of Inline Styles:** The use of inline `style="..."` attributes within HTML elements is strictly forbidden for all new frontend code, auto-generated templates, and dynamically injected DOM elements. 
2. **Centralized Style Blocks:** Dynamic layout requirements (such as absolutely positioned elements with variable coordinates) must be handled by aggregating styles into centralized `<style>` blocks (e.g., `<style id="dynamic-styles">`) placed in the `<head>` or within a Declarative Shadow DOM. 
3. **Class and ID-based CSS Binding:** Layout and styling logic must be decoupled from literal HTML tags. Elements must be styled strictly via CSS classes (e.g., `.form-field`) or unique IDs. 
4. **SVG Presentation Attributes Exempt:** SVG-specific presentation attributes (e.g., `fill="..."`, `stroke="..."`, `font-size="..."` on `<text>` or `<path>` elements) do not violate CSP `style-src` restrictions and are exempt from this mandate. They should continue to be used where appropriate.
5. **JSON Data and Script Centralization:** Just like styles, JSON payloads and JavaScript logic must not be distributed as inline handlers (e.g., `onclick="..."`) or scattered haphazardly throughout the document.
   - **JSON Data:** Structured metadata must be centralized into dedicated `<script type="application/json" id="...">` blocks rather than attached to individual elements as massive data attributes or parsed out of inline JavaScript.
   - **Executable Scripts:** JavaScript must be consolidated into distinct `<script>` blocks or external files.
   - **Nonce Preparedness:** All dynamically injected `<style>`, executable `<script>`, and `<script type="application/json">` blocks must be architected so that a server-generated cryptographic nonce (e.g., `nonce="..."`) can be effortlessly appended to the tag.

## Consequences

### Positive
- **Security Posture:** Paves the way for the immediate implementation of a strict, modern CSP, vastly reducing the surface area for DOM-based XSS attacks.
- **Maintainability:** Decoupling styling from HTML tags via class-based architecture ensures that component refactors (e.g., changing a `<textarea>` to a `<div>`) do not break visual presentation.
- **Performance:** Centralized stylesheets and class selectors can be processed and painted more efficiently by the browser engine than thousands of inline style evaluations.

### Negative
- **Development Overhead:** Requires refactoring legacy parsing scripts (such as `new_parse_pdfs.py`) to maintain a separate internal state for styling and injecting it at the end of the parsing loop.
- **Dynamic Calculation Complexity:** React or Vanilla JS components that require highly dynamic, frame-by-frame styles (like drag-and-drop elements) may require specialized handling (e.g., CSS Custom Properties/Variables) to remain compliant without thrashing the DOM with `<style>` block updates.
