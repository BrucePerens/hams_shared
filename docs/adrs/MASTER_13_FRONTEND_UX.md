# MASTER 13: Frontend UX & Accessibility

## Status
Accepted (Consolidates ADR 0030)

## Context & Philosophy
The platform must remain accessible to visually impaired operators while rendering complex, high-velocity data streams.

## Decisions & Mandates

### 1. Accessible Real-Time DOM Mutation (0030)
* UI widgets rendering high-velocity data (like the live DX Cluster WebSockets) use `aria-live` to notify screen readers of changes.
* However, frequent updates trap screen readers in an infinite reading loop. These components MUST feature an accessible "Pause" toggle.
* Toggling pause MUST change the DOM to `aria-live="off"` and instruct the WebSocket payload listener to drop incoming state mutations, giving the user a static snapshot to navigate.

### 2. Always-On Dashboard Burn-In Protection
* Network Operations Center (NOC) dashboards (e.g., Pager Duty, Backup Management) are frequently displayed on dedicated, always-on physical monitors.
* To prevent OLED/Plasma pixel burn-in or image retention, these OWL components MUST render in strict Dark Mode.
* Furthermore, they MUST implement a continuous, pseudo-random CSS `transform: translate(x, y)` spatial drift on a slow (e.g., 20-second) linear transition, keeping the UI visually stable while constantly shifting the physical pixels illuminated by text edges.
