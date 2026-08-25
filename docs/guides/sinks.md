# Sinks: where outcomes land, and how

> New v2 terms used here (*outcome type*, *channel*, *sink*, *lane*) are
> defined in [concepts.md](../concepts.md).

A **sink** is the destination half of a `channels:` binding. A workflow emits a
typed **outcome** — `verdict`, `bug`, `escalation`, `decision`, … — and never
says where it goes; `channels:` maps each outcome type to one or more sinks.
GLaDOS never posts anything itself: the compiled epilogue tells the agent to
deliver to each bound sink *"using the project's own platform CLI/tooling"*
(glab/gh/MCP). A sink is an abstraction over "a kind of place a result becomes
visible"; your agent's runtime supplies the muscle.

## Built-in sinks

Five ship declared, usable with no `sinks:` block at all:

| Sink | Team-visible | Typical use |
|------|--------------|-------------|
| `mr-comment` | yes | review verdicts, decisions on the MR thread |
| `issue` | yes | escalations, bug reports a human will see |
| `issue-comment` | yes | follow-ups on an existing issue |
| `label` | yes | machine-readable state on the MR/issue |
| `ledger` | no | the committed run record only (the quiet sink) |

## Declaring your own

The sink *name* set is open. Declare a new destination under `sinks:` and bind
it in `channels:`:

```yaml
sinks:
  slack:
    channel: "#code-reviews"     # the agent posts here via its Slack tooling
    format: terse
channels:
  verdict: [mr-comment, slack]   # durable record on the MR, a ping on Slack
```

Two rules make this safe without freezing the vocabulary:

1. **Declare before you bind.** Every sink named in `channels:` must be built-in
   or declared in `sinks:`. A typo like `channels: verdict: [slak]` fails the
   install — *"sink 'slak' is not declared"* — so you keep typo-safety without a
   closed enum.
2. **Delivery is verified, not assumed.** At run time, every sink bound to an
   outcome must actually receive it. A bound sink that fails escalates, however
   many other sinks got a copy — a verdict that never reached the MR leaves the
   change looking unreviewed. Reaching only the ledger is not a failure when
   that is what the manifest declared (`progress`, `decision`, `observation`,
   or a confessed `visibility-acknowledged: ledger-only`). All of a run's
   delivery failures ride in **one** escalation, and that escalation is never
   itself escalated — otherwise one unreachable platform produces an unbounded
   chain.

Bodies are yours. Everything under a sink name — `channel:`, `format:`,
`grouping:`, `threads:`, anything — is freeform config the **agent** interprets
at run time for that sink's medium and your team's conventions. The compiler
never parses it; it only checks that the declaration is a mapping.

## Visibility

A sink you declare **must state its own visibility**. There is no default:
`team-visible: true` for a destination the team actually reads,
`team-visible: false` for a record-only one (an audit log nobody watches).

```yaml
sinks:
  audit-log:
    team-visible: false
  slack:
    team-visible: true
    channel: "#code-reviews"
```

Omitting the key fails the install. It used to default to *visible*, which
meant a sink nobody had thought about satisfied the very check that exists to
stop an outcome disappearing — the silent-default failure this guide warns
about two sections down. The key is rejected on a **built-in** sink, whose
visibility is fixed and never read from the body.

A `team-visible: false` sink cannot, by itself, satisfy an outcome's
visibility requirement — bind a team-visible sink alongside it, or the install
fails (or confess `visibility-acknowledged: ledger-only`, the same escape hatch
the built-in `ledger` uses). The assembly report prints every sink's resolved
team-visibility so there are no surprises.

## Rendering a verdict: per-persona comments

The review panel emits a single composed `verdict` outcome that carries both
the composed result *and* each panelist's per-persona verdict. How that renders
at a sink is a sink property:

```yaml
sinks:
  mr-comment:
    grouping: per-persona    # aggregate | per-persona | per-finding
    threads: resolvable      # one resolvable thread per comment, if supported
    summary: false           # also post one composed-verdict comment?
channels:
  verdict: [mr-comment]
```

With `grouping: per-persona` + `threads: resolvable`, each panelist's verdict
becomes its own resolvable thread on the MR — and the review ⇄ address loop
turns per-persona: each thread stays open until its persona approves. The
internal composed tally still drives the loop decision and still lands in the
ledger; only the *rendering* changes. Nothing about the panel module changes —
this is purely how the sink presents the outcome.

## What the compiler does and doesn't guarantee

- **Owns the graph:** who emits what, that every binding references a real sink,
  and that every outcome can reach someone.
- **Leaves to the agent:** how a sink actually formats and delivers a result.

That split is deliberate — hard structure where a static check depends on it,
interpretation everywhere else. It's why a project can invent a `slack` sink in
two lines without a compiler release, and why a typo still can't cost you a
lost review verdict.
