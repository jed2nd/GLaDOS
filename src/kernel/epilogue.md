<!-- GLaDOS kernel fragment: compiled epilogue.
     The compiler appends this to EVERY workflow core. Cores must not define
     their own ending; a core containing trace/publish/commit instructions of
     its own fails `glados lint`. -->

## Before ending this run (mandatory)

A run without a committed run record and a published outcome is an incomplete
run — finish these steps even if the work above was cut short, and say so in
the record rather than omitting it.

1. **Finalize the run record** at `.glados/runs/<YYYY-MM-DD>-<workflow>-<slug>.md`:
   outcome, key decisions (with their decision-rights class), verdicts if any,
   links (MR, issues, commits), and any state keys this run wrote
   (`review.reviewed-head`, `epic.progress` updates, …). If this run was cut
   short, record how far it got — a partial record beats a missing one.
2. **Yield check**: compare the recorded `work.base-sha` against the current
   base. If an external edit moved it, emit `yielded (external_edit)` in the
   record, then rebase or release — never force-push over someone else's work.
3. **SDA work-unit log (when `glados.yaml` sets `sda: true`)**: append this
   run's work-unit row to `product-knowledge/SPEC_LOG.md` — date, workflow,
   scope, outcome, links — and clear this run's entry in `claims.md`, so
   both ride the record commit below.
4. **Commit the record** on the current working branch:
   `chore(glados): record <workflow> run`. Review-only runs that must not
   touch the author's branch commit to the `glados/ledger` branch instead.
5. **Publish outcomes**: read `glados.yaml` → `channels:` (which sinks each
   outcome type goes to) and `sinks:` (how each sink behaves). For every
   outcome type this run emitted, deliver it to each bound sink using the
   project's own platform CLI/tooling (glab/gh/MCP), **rendering per that
   sink's declared config** — interpret its keys (`channel:`, `format:`,
   `grouping:`, `threads:`, …) sensibly for the sink's medium and this team's
   conventions. Built-in sinks are `mr-comment`, `issue`, `issue-comment`,
   `label`, and `ledger`; a project may declare others (e.g. `slack`). For a
   `verdict` where `sinks.mr-comment.grouping: per-persona`, post one comment
   per persona (as a resolvable thread when `threads: resolvable` and the
   platform supports it) rather than one aggregated comment; `summary:`
   controls whether a composed-verdict comment is also posted. `progress`
   always lands in the ledger at minimum.
   - **Delivery is verified, not assumed.** Every sink bound to an outcome
     type must actually receive it. If a bound sink is unreachable or the post
     fails, record the failure in the run record and emit an `escalation`.
     Delivery to a different sink does not cover the one that failed: a
     verdict that never reached the MR leaves the change looking unreviewed,
     however many other sinks got a copy. Never silently drop an outcome.
   - Reaching only the ledger is not itself a failure. `progress`, `decision`,
     and `observation` may be bound ledger-only by design, as may any outcome
     type in a project whose manifest confesses `visibility-acknowledged:
     ledger-only`. Escalate on a sink that failed, never on a binding the
     manifest deliberately declared.
6. **Release** anything held: leases (when enabled) and other in-flight
   markers — and delete `.glados/runs/current` unconditionally (the preamble
   always sets it; a leftover marker makes the run-record guard hooks block
   the next session).
