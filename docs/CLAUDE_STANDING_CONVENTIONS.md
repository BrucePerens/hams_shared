# Standing Conventions & Institutional Knowledge

This document consolidates durable, project-specific conventions, standing authorizations, and
architectural philosophy that Bruce (Bruce Perens, the project owner) has established across many
sessions on hams.com/hams_open/hams_shared. It exists because a dev box's own home directory
(`~bruce`) does not persist or migrate between machines or to production (see "Repository &
persistence conventions" below) — anything here is committed into `hams_shared` specifically so it
survives a fresh clone on any machine, not just the box where it was first learned. It is a
companion to `hams_shared/agents/skills/project-experience/SKILL.md` (which is a Trap/Solution log
of narrow technical bugs and CI/CD gotchas), `hams_shared/agents/skills/claude-memory/` (the
committed backup of Claude's own broader per-lesson cross-session memory files, one topic per
file), and `hams_shared/docs/adrs/` (which holds formal, numbered Architecture Decision Records
for structural mandates) — this file is for standing conventions and policy decisions that don't
fit any of those shapes as neatly, along with a few pointers to fuller treatments that already
live elsewhere.

## Legal & licensing

- **LGPL is fine; GPL is not.** hams_com/hams_open/hams_local_relay is a proprietary,
  trade-secret-licensed codebase (hams_com) alongside an AGPL-3.0-or-later open-source codebase
  (hams_open). Bruce has explicitly cleared LGPL-licensed third-party code (e.g. Phil Karn's
  `libfec`) as fine to vendor or link into hams_com without treating it as a case-by-case legal
  question — "We don't have a problem using LGPL code." GPL and stronger copyleft remain treated as
  incompatible with vendoring into hams_com, per the existing precedent in `wspr.rs`'s own module
  doc. The dividing line is GPL-and-up vs. LGPL-and-more-permissive, not "any copyleft is fine."
- **hams_com may vendor copyrighted reference documents; hams_open may not.** hams_com is a private
  repository, and Bruce has stated there is a good fair-use case for storing actual copyrighted
  reference material (PDFs, papers) there for internal engineering use — e.g. a QEX/ARRL paper
  needed to correctly implement a protocol. hams_open is public/open-source and doesn't have the
  same footing: for hams_open, extract the factual/mathematical content by reading the source, cite
  it clearly (title, authors, URL), and do not commit the document itself.
- **Patent disclosure drafting has two recurring scope traps to actively avoid** (from work on
  `hams_com/docs/proposals/patent_disclosures/`): (1) never describe a safety/oversight/human-approval
  gate as a *required* element of an invention — a competitor can design around the patent by simply
  omitting the gate, and it would also box out the patent holder's own more-autonomous future
  version. Describe such a feature as a configurable posture across a range (fully autonomous to
  fully human-gated), with any particular posture stated as a deployment choice, not an architectural
  requirement. (2) avoid unnecessary multiplicity limits — phrasing like "monitors multiple channels"
  inadvertently requires plurality when a single instance already embodies the same inventive
  mechanism; use "one or more" unless the mechanism is structurally two-or-more by nature (e.g. a
  peer-to-peer handshake). Scan specifically for "requires/must/never/always/mandatory" and
  "multiple/several/many" near the end of drafting any disclosure.

## Repository & persistence conventions

- **`~bruce` (the dev box home directory) does not persist.** Bruce's own words: "~bruce won't
  migrate with the server. Anything we want to be persistent should be in one of our three
  repositories." Every file, script, cron job, or config created under `/home/bruce/` on the current
  dev box is ephemeral relative to the real hams.com deployment. When building a tool or piece of
  automation for a real, ongoing operational need, commit it into the appropriate repository (e.g.
  `hams_shared/tools/` for a general-purpose script) rather than leaving it to live only under
  `~bruce`. Secrets themselves can't be committed, but the *mechanism* that uses them should be.
- **Claude Code's own cross-session memory files are `~bruce`-local too, and must be committed
  the same way (policy set 2026-09-08).** Claude's persistent, file-based memory
  (`~/.claude/projects/<encoded-cwd>/memory/*.md` — one Markdown file per durable lesson, with a
  `name`/`description` frontmatter header) lives under the dev box's own home directory, exactly
  the ephemeral location the bullet above already warns about; it does not migrate with the
  server, is not visible to a session running against a fresh clone on another machine, and is not
  backed up anywhere. Whenever a memory saved there records a durable fact about this project (as
  opposed to a purely personal, cross-project preference of Bruce's that has nothing to do with
  hams.com/hams_open/hams_shared), also commit a copy into
  `hams_shared/agents/skills/claude-memory/` (indexed in that skill's own `SKILL.md`, one entry
  per file) — it lives alongside the other skills, not under `docs/`, because it is AI-consumed
  durable memory of the same kind as `hams_shared/agents/skills/project-experience/SKILL.md` (that
  one stays narrowly scoped to mechanical technical traps/CI gotchas; `claude-memory` is the
  broader set of standing conventions and behavioral preferences). This is the repository copy
  that actually persists and travels with the codebase; the `~/.claude/` copy remains the one the
  harness itself reads at the start of a session and is not replaced by this, only backed up. When
  starting a session against a freshly cloned checkout with no local `~/.claude/` memory yet,
  check `hams_shared/agents/skills/claude-memory/` for standing lessons the same way this file
  itself is checked.
- **Credentials live in `~/.secrets/`, outside any git repo.** Convention is `~/.secrets/<name>.env`
  (mode 400, `KEY=value` shape) — check there first before concluding a given service's credentials
  don't exist yet or need to be created.
- **A cross-cutting standing policy is an ADR; a scoped feature/fix is a proposal doc.**
  `hams_com/docs/proposals/*.md` tracks one specific, not-yet-built feature or fix, with its own
  "Still open" section. `hams_shared/docs/adrs/` (now consolidated into numbered `MASTER_NN_*.md`
  documents, see `hams_shared/docs/adrs/README.md`) is for a durable, cross-cutting policy governing
  a whole category of situation going forward. The tell: if the ask is "we need a standing document
  on this topic" / "a general policy," it's an ADR; if it's "fix this one thing," it's a proposal.
  Since `hams_shared` is a git submodule shared by hams_com and hams_open, a new ADR needs its own
  submodule commit plus a separate submodule-pointer-bump commit in whichever parent repo(s)
  reference it.
- **`night_shift_todo.md` (in `hams_com`) is the durable, always-consulted cross-session index for
  this project** — individual proposal docs are not reliably re-read by a future session picking up
  the backlog, and their own status headers can go stale even after the true outcome was recorded
  elsewhere. Whenever the user answers a question that unblocks a proposal or backlog item, write the
  question and answer into `night_shift_todo.md` immediately as its own dated entry, in addition to
  updating the specific proposal doc — do this before moving on, not as a later cleanup pass. Before
  asking the user any proposal-blocker question or presenting a proposal survey as current status:
  (1) grep `night_shift_todo.md` for the topic — its own record is the ground truth on what's already
  been asked and answered; (2) check the actual code or `git log -- <path>` for whether the feature
  already exists, since a proposal doc can simply never get updated when the work lands. A proposal
  doc's header is a claim to verify, not a source of truth.

## Autonomy, night-shift sessions, and originating work

- **The full autonomous-session protocol lives in the `night-shift` skill**
  (`hams_shared/agents/skills/night-shift/SKILL.md`, shared into both hams_com and hams_open) — check
  there first as the canonical, evolving copy. In short: commit locally as verified, coherent batches
  of work complete (never push, never take another destructive/high-blast-radius action without
  asking); when a known task list runs out, continue productively (another round of review, fix,
  add-coverage, review again) rather than stopping; the moment nothing is actively in flight and a
  ready item exists on a known punch list, start it immediately without waiting to be re-prompted,
  even mid-conversation about something else.
- **Don't be too cautious about originating work.** Bruce wants Claude to propose and build real,
  tested, bounded work on its own initiative rather than defaulting to caution and gating a
  self-assigned build behind an accept/drop review just because nothing was explicitly requested —
  "Do all of them. Don't feel too cautious about originating something." This has recurred in two
  more specific shapes worth watching for: (1) when several independent items are all already cleared
  to build, a genuine-but-non-blocking open question (like build *order*) doesn't license inaction on
  items that don't depend on its answer — start one immediately rather than waiting for a sequencing
  preference; (2) when a scoping document already states a reasonable default or a "smaller, safer
  first build" for an open sub-question, that default IS the answer to build against, not merely one
  option awaiting Bruce's input — don't write "X would be the safer choice" and then withhold X
  because "there's an open question about X." None of this cancels ordinary engineering judgment:
  still write real tests, still record decisions in `night_shift_todo.md`, still flag a genuine
  product-judgment call with real tradeoffs that only Bruce can make, and this doesn't extend to
  pushing to remotes (below) or override the deployed-system caution question (below) where that
  caution is still warranted for reasons other than self-authorization.
- **Breaking changes are fine — nothing is deployed yet, and everything is recoverable via git.**
  Bruce's own words: "The system is not deployed yet and you are allowed to make breaking changes,"
  later broadened to: "We can break anything we want on this system as long as things get pushed to
  github." The underlying principle is recoverability via git, not merely the absence of a
  deployment — it extends to the dev box's own system-level state (file ownership, permissions,
  installed packages, service configuration), not just application code. This means Claude should not
  be shy about proposing or requesting broader system-level permissions when a real investigation or
  fix needs them, rather than working around a blocked command or leaving a real bug undiagnosed —
  though Claude cannot edit its own `~/.claude/settings.json` to grant itself broader permissions
  (blocked by the permission classifier through every tool); that specific change is Bruce's own step.
  This doesn't remove the general discipline of clean changes and clear git history, or the practice
  of leaving genuinely ambiguous product-direction decisions for Bruce's own judgment.
- **Gap surveys: keep a written record, then close gaps autonomously, in whatever order judged
  best** — don't route "which of these should I do, and in what order" back to the user via a
  multiple-choice prompt. This doesn't override flagging a gap that's genuinely blocked on a resource
  only Bruce has (credentials, a primary source to verify against, a hardware/money decision) or a
  real product/architecture call with no clear default.
- **Work needing approval stays uncommitted until reviewed.** For work that would normally need
  Bruce's sign-off before being finalized, do the work in the working tree but don't `git commit` it
  until he's actually approved it — leave it as an uncommitted diff and say so, rather than committing
  first and asking afterward. This lets him review real diffs instead of relitigating a description
  after the fact, and keeps history clean of things that might still get reverted. Routine, clearly
  safe work still gets committed as you go — this only refines that default for the subset that
  specifically needs approval.
- **Bruce does all the pushing.** After switching these repositories to SSH auth with a hardware
  Yubikey, Bruce stated: "I will do all of the pushing." A session should keep committing locally as
  usual but never push to any remote, and doesn't need to raise pushing as a question — that step
  belongs to him by his own explicit claim, not just the ordinary default caution around remote-
  affecting actions. He has separately confirmed this is current practice rather than a fixed rule he
  can't revise ("I might tell you to push sometime. For now, I will keep doing it.") — so a future
  explicit instruction to push should be followed, not treated as an exception needing pushback.
- **Standing permission for dev/test dependencies and major facilities.** Blanket, multi-session
  permission to run `sudo apt-get install` for reasonable dev/test dependencies (protocol daemons,
  certificate tooling, DSP libraries) without asking each time, later broadened explicitly to "major
  facilities" (additional browser engines, alternate language toolchains, similar substantial
  installs) needed for real dev/test work during an unsupervised session. Still mention what was
  installed and why when reporting the work; this doesn't extend to destructive operations or
  unrelated system configuration changes.
- **The anti-summation-bias pre-commit hook can be bypassed once a flagged reduction is personally
  verified as a genuine refactor.** The hook flags a sharp file-size drop ("SUMMATION BIAS DETECTED")
  as a heuristic guard against an AI quietly dropping logic while claiming to just clean up — it
  cannot distinguish that from a legitimate extraction (e.g. moving inline controller logic into a
  reusable model method). Bypassing (`git commit --no-verify`) is authorized once verified (confirm
  the removed code now lives elsewhere in the diff, run the real tests covering it) — state plainly
  why the reduction is safe when bypassing. This is also recorded in the `night-shift` skill.
- **The AWS CLI session (`hams-com` profile) needs touching at least once every 12 hours** or it
  expires and needs Bruce's own interactive browser re-authentication (`aws login --remote`, which
  only a human with a browser can complete). During any long/overnight session, periodically run a
  cheap call (e.g. `aws --profile hams-com sts get-caller-identity`) to keep it alive.

## Architecture philosophy

- **Fail fast: never let an error path silently substitute a default that can mask a real bug.**
  Stated after a concrete case: a service-account whitelist check in `get_param()` silently returned
  the caller-supplied `default` instead of raising when a non-whitelisted parameter was blocked — one
  call site crashed downstream, another (LoTW transfer-token signing) silently signed every auth
  token with an empty key, a full auth-bypass that shipped with a passing test suite because nothing
  ever surfaced the substitution. Prefer raising an explicit exception over returning a default,
  `None`, `False`, or an empty collection — especially in access control, credential/secret handling,
  or signature verification, where a silent fallback can turn a caught bug into an unnoticed
  vulnerability.
- **Zero-sudo / minimum privilege: `.sudo()` and `SUPERUSER_ID` are banned outright**, enforced by an
  AST burn-list linter, because they grant far more access than any single operation needs. Elevated
  access goes through specialized, narrowly-scoped service accounts
  (`zero_sudo.security.utils._get_service_uid()` + `with_user()`), each holding exactly the group
  memberships its one job requires — enforced at the database level too
  (`zero_sudo_get_service_uid()` raises if a service account is ever granted `base.group_system` or
  `base.group_erp_manager`). The same philosophy applies to the `ir.config_parameter` service-account
  whitelist: only ever whitelist a parameter once its safety can be affirmatively argued (a plain
  non-secret operational setting core Odoo reads incidentally), not reactively just to unblock a
  failing test — a real security justification is expected before expanding it.
- **No-bricking: a safety mechanism must never simply refuse to run as its default failure mode.**
  For any mechanism that can block a user's software from running (e.g. a relay daemon's
  revocation kill-switch), the required order is: first attempt to auto-update to a known-safe
  version; if that fails, warn the operator as loudly and visibly as possible; only fall back to
  blocking/refusing to run as the last resort when there's no way to get the user to a safe or
  clearly-marked-unsafe working state. The goal is a loud, informed operator, not a silently
  non-functional radio.
- **Tests exist to guard against LLM-introduced regressions, not just ordinary code bugs.** Stated
  while designing a privilege-escalation fix: because LLM-driven changes are a distinct, recurring
  risk (an agent widening an ACL row, dropping a `groups` field from an `ir.rule`, loosening a
  security boundary without recognizing the consequence), any security-relevant rule change (
  `ir.rule`, `ir.model.access.csv`, group membership, service-account scoping) should get an explicit
  regression-guarding test whose purpose is to fail loudly if a *future* change narrows or widens
  access — including "exclusion" tests (a tenant/persona provably cannot see what isn't theirs) even
  when nothing currently indicates the code is broken. Multi-persona coverage (different tenants,
  portal users, service accounts getting correctly different outcomes from the same code path) is a
  standing expectation for security-relevant work, not a one-off ask. (Bruce asked for this to become
  a formal ADR; confirm whether that ADR exists yet before assuming this is only recorded here.)
- **Extend hams.com's own models directly; reserve `_inherit` for genuinely third-party or
  genuinely-optional-module models.** Formalized as ADR 0086 (now folded into the consolidated
  `MASTER_*` ADR series — check `hams_shared/docs/adrs/README.md` for its current location) after
  cross-module `_inherit` of an in-house model repeatedly produced real, hard-to-diagnose bugs
  invisible from reading either module alone: a model missing itself from `_inherit` crashing
  registry loading, two modules declaring the same `_name` with no `_inherit` relationship at all
  (Odoo silently merges by load order), two `init()` overrides on an `_auto=False` SQL-view model
  with no `super()`-chaining contract (only one `CREATE VIEW` ever runs, silently dropping the
  loser's columns/filters), and a base model that permanently blocks `create()` leaving an extending
  module's fields dead on arrival. `_auto=False` SQL-view models get an absolute ban on cross-module
  extension, no exemption. The exemption that remains: when the extension references the *extending*
  module's own models/utilities, merging into the base would require the base to depend on the module
  that already depends on it, which Odoo's acyclic module graph can't support — in that case
  `_inherit` is correct and must stay.
- **`hams_open/knowledge` is an intentional open-source work-alike, not an accidental duplicate.** It
  exists specifically to reimplement Odoo Enterprise's proprietary "Knowledge" module for Odoo
  Community, so its `knowledge.article` model is a deliberate, standalone primary definition — don't
  flag it as a consolidation candidate on the basis of looking like a duplicate of some other
  knowledge-like concept.
- **`hams_local_relay`'s browser-mixed-content problem has a settled architectural answer, not an
  open question.** Browsers block `ws://` from an `https://` page with no exception for `127.0.0.1`,
  so the local/NAT-direct legs of a `hams_local_relay` connection can't establish from a real browser
  tab today. The settled plan (not yet built): a dedicated daemon maintains a Let's Encrypt cert for a
  shared hostname (e.g. `localhost.hams.com`) via DNS-01 (hams.com already controls its DNS zone,
  likely via `ham_dns`), that hostname is mapped via a normal A/AAAA record to `127.0.0.1` (the same
  loopback trick vendors like Plex use), and the renewing daemon hands the cert/key to each running
  `hams_local_relay` instance over the existing daemon-token/API-key trust relationship. In the
  interim, `hams_local_relay` generates its own self-signed cert (`tls.rs`, via `rcgen`) purely so
  `wss://`/`https://` work end-to-end for dev/test, with an expected untrusted-cert browser warning
  until the real one replaces it.

## Coordinating with the concurrent Gemini ingestion pipeline

- A separate, Gemini-driven automation pipeline performs ham radio and ICS training story/course
  ingestion, tuned and run independently by Bruce, concurrently with any Claude session — its
  processes (`ingest/accessory_daemon.py`, `forms_daemon.py`, `curriculum_daemon.py`,
  `visual_daemon.py`, `narrative_daemon.py`) and file churn (large data files like
  `ham_training/data/course_HAM_TECH.json`, related `agents/skills/*_training/*` files) can appear
  mid-session with no connection to anything Claude did. Never edit, stash, or build new work on top
  of files this pipeline is actively touching — check `ps aux` for the relevant daemons before
  assuming uncommitted changes there are stray. Its output can be committed without review once Bruce
  explicitly says so; don't assume an earlier commit message describing the pipeline as "stopped" is
  still accurate later in the same session — his current word takes precedence.
- Claude has standing, fully-autonomous authorization to watch live Gemini/Antigravity pipeline runs
  via `hams_shared/tools/mcp_watchdog.py` (registered as an MCP server) and to message the Gemini
  orchestrator on its own initiative if it spots a real problem — this was a deliberate choice of the
  broadest of three offered options. Two classes of tool matter operationally: read-only transcript/
  state watching (`wait_for_agent_state_change` and similar) is safe to use freely for passive
  observation; queue-consuming/injecting tools (`wait_for_inbox`, `send_ipc_message`) need care —
  never use `wait_for_inbox` for passive observation (it can steal a message meant for Gemini's own
  Conductor), and only use `send_ipc_message` deliberately, to a queue confirmed to be the real
  orchestrator's inbox, when there's something real to report.

## Environment quirks worth remembering

- **The dev box has a live, visible desktop session**, not a fully headless inert sandbox — a command
  whose whole purpose is to prompt a human via OS-level UI (e.g. `pkexec`, any privilege-escalation
  dialog) can pop a real, visible authentication dialog on Bruce's actual screen, however briefly.
  Before running such a command, either ask first or design the test so it cannot possibly trigger a
  live prompt (e.g. check the binary's documented behavior rather than invoking it for real). The
  opposite mistake is equally real: never assume a capability (a browser, a display, a particular
  tool) is *absent* from this sandbox without checking directly (`which`, `echo $DISPLAY`, a real
  launch attempt) — real Chrome/Chromium/Firefox, a live `$DISPLAY`, screenshot tools, and Playwright
  have all been found already installed when a prior summary assumed otherwise.
- **`/tmp` on the dev box is a small, dedicated 2.7G partition**, not backed by the same headroom as
  `/` or `/home`. Every `hams_shared/tools/test.py` run redirected to a log file produces roughly
  70MB regardless of how few tests actually ran (it captures full verbose Odoo boot/module-load
  output) — these accumulate fast across a long session. Delete a test log promptly once whatever's
  needed from it (pass/fail summary, tracebacks) has been extracted, and check `df -h /tmp`
  specifically before a large build (e.g. `go build`) that might use it for cache/temp files.
- Always flush higher-level buffered Python IO explicitly (text-mode file objects,
  `socket.makefile()`, `print()` to a stream) in daemon and IPC code on this project — buffering can
  silently delay or drop data another process is actively blocked waiting to see. Raw
  `socket.send()`/`sendall()` is already unbuffered and doesn't need this.
- When a fixture/data provenance question turns out to hinge on Bruce's own personal knowledge of
  specific people or a project's norms (rather than something checkable by public search), ask him
  directly rather than treating an inconclusive web search as final — he may have context (personal
  acquaintance, project history) no search can surface. (Resolved example: five Codec2/FreeDV-team
  named test WAVs in `daemons/ham_digital_modes/src/codec2_3200/tests/fixtures/` are cleared for use.)
- When a strict lint/policy rule blocks an otherwise-legitimate pattern in a narrow, specific case,
  the preferred fix is one small, well-named, narrowly-scoped utility function that does the
  "impure" thing exactly once, backed by a real test proving it behaves correctly (including its
  failure/fallback path), with every other call site calling that clean wrapper instead of repeating
  the banned pattern. Prefer this over teaching the linter to distinguish "our code" from a safe
  exception case (fragile, easy to get subtly wrong) or granting a bypass tag at every site
  individually (harder to audit, doesn't eliminate the risky pattern — just tags it repeatedly).

## Related, more detailed treatments elsewhere

- **Debugging a component that silently never mounts/completes with no visible error**: see
  `hams_shared/agents/skills/debugging-silent-js-failures/SKILL.md` — a full playbook built after
  multiple sessions failed to root-cause exactly this shape of bug (Odoo/Owl interaction mounting)
  before one finally succeeded by instrumenting Odoo/Owl's own core source directly.
- **Narrow technical CI/CD traps and framework gotchas** (Flake8 rules, Odoo ORM footguns, tour
  test flakiness, and similar) are logged as they're found in
  `hams_shared/agents/skills/project-experience/SKILL.md`, in that file's own Trap/Solution format —
  check there rather than duplicating that material here.
- **The autonomous "night-shift" session protocol** (commit cadence, what to do when the known task
  list runs dry) is canonically documented in `hams_shared/agents/skills/night-shift/SKILL.md`.
