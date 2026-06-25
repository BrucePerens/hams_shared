---
name: goal
description: The goal in a file, so Antigravity won't forget.
---

# Antigravity Goal

[x] ACHIEVED

This is your goal. Once this goal has been achieved, mark it as so above,
so that you won't loop trying to achieve it again.

This must be done entirely non-interactively, the user will be asleep during
part of it. He wants to see this done when he awakens.

Start a sub-agent for each module in the hams_open and hams_com repositories, to perform a deep code-review of that individual module only, so that its context window is not saturated. Have them review the code for best practices, security, performance, latency and the possibility to reduce the number of turn-arounds per operation betwen the client and the software; latency and the possibility to reduce the number of turn-arounds between the database and Odoo and daemons. Keep in mind that the database may be a continent away from Odoo and daemons, etc. Database operations should be moved into database procedures wherever possible, so that most have just one turn-around. Have the sub-agents report to you, but don't have them
fix anything, because they would conflict when they break modules for other
sub-agents. Fix all of the issues they report. Run full tests on both hams_open
and hams_com and fix any remaining problems.
