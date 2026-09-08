---
name: hams-devbox-has-live-desktop-session
description: "The hams.com/hams_open dev box has a live, visible desktop/authentication session -- privilege-escalation test commands (pkexec, polkit, etc.) can pop real dialogs on the user's actual screen, not just run inertly in a sandbox."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-05T19:44:51.591Z
---

While designing a "fix serial device permissions" feature for hams_local_relay (a button that would
run `pkexec usermod -aG dialout,audio $USER` to add a Linux user to the groups needed for serial/audio
device access), Claude ran `pkexec /bin/true` directly in a Bash tool call on the hams.com dev box, as
a quick test of pkexec's behavior before writing the real implementation -- assuming, without
checking, that this sandboxed environment was headless and the command would simply hang or fail with
no live polkit authentication agent to respond to it.

That assumption was wrong. The user reported, verbatim: "I saw an authorization panel for a moment."
The dev box has a real, live desktop session with an active polkit authentication agent, and the test
command popped a genuine system authentication dialog on the user's own actual screen, however briefly
(a `timeout 3` wrapper killed the underlying process a few seconds later, which likely dismissed it).

The command itself was harmless in its effect (`/bin/true` is a pure no-op -- even full authentication
would not have changed any real system state), and no credentials were entered by Claude. But the
methodology was the real mistake: Claude tested a privilege-escalation command by directly executing it
against a live environment, rather than either checking first whether authentication prompts in this
environment are visible/interactive to the user, or finding a way to verify pkexec's behavior that
doesn't risk surfacing a real system dialog on the user's screen unannounced.

The generalized lesson: this specific dev box is not a fully headless, inert sandbox for the purposes
of commands that trigger interactive OS-level UI (graphical authentication dialogs, desktop
notifications, or anything else that would normally require a human at a real desktop session) --
running such a command directly, even as a "quick test," can have a real, visible, potentially
startling effect on the user's actual screen. Before running any command in this category again (not
just pkexec -- any command whose whole purpose is to prompt a human via the OS's own UI), Claude
should either ask the user first, or design the test so it cannot possibly trigger a live prompt (e.g.,
checking for the binary's presence and reading its documented behavior, rather than invoking it for
real against a live session).

**Recurrence, 2026-09-05, the same root mistake in the opposite direction: assuming a capability is
absent instead of assuming it's live.** While verifying a new browser-facing UI feature (a
serial-permissions banner in hams_local_relay's frontend), Claude wrote in its own summary to the user
that it "could not click through the new banner in a real browser" and had only verified it via API
endpoints, JS parsing, and served-markup checks -- without ever actually checking whether a browser
was available. Bruce's correction, verbatim: "Huh? You should have a browser in a sandbox." Checking
directly confirmed real Chrome, Chromium, and Firefox binaries, a live X display (`$DISPLAY=:0`),
screenshot tools (`import`, `gnome-screenshot`, `xdotool`), and Playwright already installed -- a full
real-or-headless browser test was available the entire time.

The lesson generalizes past pkexec specifically: on this dev box, never assert what the sandbox
does or doesn't have -- a display, a browser, a particular tool, network access to some host -- from
a generic assumption about what "a sandbox" is typically like. Check directly (`which`, `echo
$DISPLAY`, a real launch attempt) before claiming a capability is missing, the same way the original
lesson above says to check before assuming a capability (a live authentication agent) is absent in
the other direction. When a task calls for testing a UI in a browser, actually look for one (Chrome/
Chromium/Firefox binaries, Playwright/Puppeteer, a live `$DISPLAY`) before falling back to a
lesser form of verification and describing that fallback as the sandbox's own limitation.
