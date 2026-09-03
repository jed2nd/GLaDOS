---
name: mr-review-panel
kind: module
description: Run one adversarial, parallel, fresh-agent review panel over the open merge request
reads: [run.active-personas, manifest.panel-personas, standards.index]
writes: []
emits: [verdict]
mutates: none
requires: []
---

## Review panel

Run one adversarial, parallel, multi-persona review panel over the open merge
request. Every panelist is a fresh agent with no authoring context; each
returns a structured verdict object; the validated objects feed one composed
`verdict` outcome emitted by the workflow this panel serves.

Two invariants govern the panel:

- **Context isolation** (inherited from the evaluator-spawn contract): a
  panelist starts with a clean context window — no conversation history, no
  reasoning or decisions from the agent that wrote the change, only the
  self-contained brief. An agent that implemented the code is predisposed to
  approve it; a fresh agent is not.
- **Adversarial stance**: panelists are briefed to try to **break** the
  change, not to rubber-stamp it. Approval must be earned.

<!-- glados:include vocabulary/verdicts.md -->

<!-- glados:include vocabulary/panel-roster.md -->

### Assemble the brief

Panelists cannot ask questions, so the brief must be self-contained. It
carries: the MR id, the diff (`<base>...<head>`), the changed-file list, the
spec or ticket, the commands to run tests and linters, each panelist's seated
mandate, and the severity scale, verdict words, and composition rules above,
copied verbatim. If anything is missing, assemble it before spawning — a
panelist that has to guess reviews the wrong change.

### Spawn the panel (parallel)

Spawn **one fresh agent per panelist, all in parallel**. Each panelist's
prompt contains the brief, its persona mandate, and these standing orders:

```
You are the [Persona] reviewer on an adversarial MR review panel.
You have no knowledge of how this change was written — only the brief.
Your job is to find real problems, not to confirm success.

1. Read the brief, the spec, and the diff. Read surrounding source as needed.
2. Review strictly through your persona's lens, and review the code: a
   finding about prose is advisory, one line. Cite file + line per finding.
3. Classify each finding and choose your verdict using ONLY the severity
   scale, verdict words, and composition rules in the brief.
4. Before returning, step back from the individual findings: in one line,
   name the underlying cause you believe they share — the condition in the
   code that made them possible, not a restatement of the symptoms. Write
   `none` when they share no cause, or when you found nothing.
5. Return the structured verdict object:
   { persona, verdict, root-cause, findings: [{ severity, file, line,
     description }] }
   Report an explicit empty findings list rather than omitting the field.
```

The `root-cause` line is a hypothesis from one lens, not a verdict. It binds
nothing on its own: it is raw material for whatever root-cause synthesis the
workflow this panel serves runs over the panel as a whole.

### Collect and validate

Validation happens at the tally, not on trust in the panelist prompt:

- Gather every panelist's verdict object. Respawn any panelist that died
  without returning one — a missing verdict is never counted as approval. A
  seat that still lacks a well-formed verdict after respawn is a missing
  panelist verdict under the composition rules.
- Check each object against the composition rules: recompute the verdict its
  findings imply. Where a panelist's self-declared verdict disagrees with its
  own findings, the composition rules win.
- A missing, empty, or `none` `root-cause` line is **not** a malformed
  verdict and never escalates — it means that lens offered no hypothesis, and
  the synthesis clusters that panelist's findings from their descriptions
  instead. Only the verdict word and the findings list govern validity.

### Refute before believing

Validation above checks a verdict's *shape*. Nothing in it checks whether a
finding is **true**, and a panelist briefed to break the change will produce
confident findings about code that is not there. A wrong blocking finding costs more than a missed one: it sends
the author to refactor around a defect that does not exist, and it teaches the
team to skim the next review.

So every `blocking` finding earns its place before the tally counts it:

- Spawn **one fresh agent per blocking finding**, in parallel, with a brief
  carrying only that finding, the diff, and the cited file — never the panel's
  other findings and never the panelist's reasoning. Its instruction is to
  **refute**: read the cited lines at this HEAD and show the finding is wrong.
- Three outcomes, and only these: **confirmed** (the refuter could not break
  it) — the finding stands as written; **refuted** (the refuter showed the
  cited code does not do what the finding says) — the finding is dropped, and
  the drop is recorded with the refuter's reasoning; **unconfirmed** (neither)
  — the finding stands at its severity, reworded to state what could not be
  established, so the author answers a question instead of chasing a defect.
- A refuted finding is the only way a finding leaves the list. Severity is
  never lowered by this step, an `advisory` is never promoted, and nothing
  downstream — no sink, no renderer, no publish step — may drop a finding the
  tally retained. Rendering governs wording; this step governs truth.
- Refutation runs on `blocking` findings. Advisories are cheap to state and
  cheap to decline, and are not worth a seat.

Record each verdict per finding in the run record, and only there: a refuted
finding is not mentioned on the merge request. A finding that reached the MR
without a recorded verdict is a process failure worth naming in the record.
- The validated objects are the panel's output. They do not become outcomes
  one per persona: tallying them into the single composed `verdict` outcome —
  the per-persona verdicts plus the cycle's composed result — and making the
  approve/loop decision happen downstream in the workflow this panel serves.
