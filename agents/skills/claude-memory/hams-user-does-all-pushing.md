---
name: hams-user-does-all-pushing
description: "On hams.com/hams_open/hams_shared, the user pushes to remotes themselves via SSH with a hardware Yubikey -- Claude should never push, and shouldn't need to ask, since the user has claimed that step entirely for himself."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-25T21:30:58.721Z
---

After an earlier session flagged a GitHub personal access token sitting in plaintext in a repo's local git remote URL, the user revoked that token and switched the hams.com/hams_open/hams_shared repositories to authenticate over SSH using a hardware Yubikey. He then stated explicitly: "I will do all of the pushing."

This goes beyond the general standing rule of asking before any push -- the user has claimed pushing as something he does himself, personally, using hardware-backed SSH auth that only he can trigger. In practice this means a Claude session working on these repositories should keep committing locally as usual, but should not push to any remote, and doesn't need to raise pushing as a question or offer to do it -- that step belongs to the user by his own explicit declaration, not just by the ordinary default caution around destructive/remote-affecting actions.

Later, in a separate session, Claude discovered that its authenticated `gh` CLI token was actually scoped with push-capable `repo` access, and asked the user directly whether that meant Claude could push. His answer confirmed this is current practice, not an unchangeable rule: "That's fine, I might tell you to push sometime. For now, I will keep doing it." So the default -- never push unless explicitly told to, for now -- stands unchanged, but Claude should not treat a future explicit instruction to push as some kind of exception needing pushback or double-confirmation; he has already said he may hand that step over deliberately at a time of his choosing.
