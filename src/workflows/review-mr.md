---
name: review-mr
kind: core
description: Run one adversarial multi-persona review pass over an open merge request
reads: [review.reviewed-head, review.cycle, run.active-personas, manifest.merge-authority, manifest.platform]
writes: [review.reviewed-head, review.cycle, review.verdicts]
emits: [progress, verdict, escalation]
mutates: none
requires: [mr-review-panel]
---

# (GLaDOS) Review MR

**Goal**: run one adversarial, multi-persona review pass over an open merge
request, produce a single validated tally, and synthesize that tally down to
its root cause before deciding. A clean pass ends the loop; a dirty pass
hands off to the address-review workflow — forming the loop
review-mr ⇄ address-review that runs until every panelist approves or the
cycle bound stops it. This workflow reads the author's branch and never
commits to it.

## Process

### 1. Locate the MR
- Identify the open MR for the current feature branch, via the project
  platform CLI (per `glados.yaml` `platform:`). If no open MR exists, this
  run produces an `escalation` outcome and stops.

### 2. Re-review guard
- Compare the MR's current HEAD SHA against `review.reviewed-head`. If the
  HEAD is unchanged since the last pass **and** the author has not responded
  to it, there is nothing new to review: this run produces a `progress`
  outcome saying so and stops. Do not re-review an unchanged HEAD.

### 3. Check the cycle bound

<!-- glados:include vocabulary/loop-bounds.md -->

- This pass is cycle `review.cycle + 1` for this MR (cycle 1 when unset).

### 4. Assemble the review brief
- Gather: the MR id, the full diff (`<base>...<head>`), the changed-file
  list, the spec or ticket, the test commands, and the personas active for
  this feature (`run.active-personas`, when present).
- Make the brief self-contained — each panelist runs with no authoring
  context and must be able to judge from the brief alone.

### 5. Run the panel
- Seat the panel and spawn one fresh agent per panelist, in parallel; the
  enabled review-panel behavior defines the roster and spawn mechanics.
- Each panelist reviews the brief against its mandate and returns a
  structured verdict object: `{ persona, verdict, root-cause, findings }`,
  each finding carrying a severity and the `root-cause` line naming the cause
  that lens believes its findings share (step 7 consumes it; a panelist that
  offers none is still a valid verdict).

### 6. Tally and validate

<!-- glados:include vocabulary/verdicts.md -->

- **Refute every `blocking` finding before counting it.** The enabled
  review-panel behavior defines the refutation stage; run it here, before the
  validation below and long before the decision. A finding a fresh refuter
  breaks is dropped, with its reasoning recorded; one it cannot break stands
  as written; one that resolves neither way stands at its severity, reworded
  as the question it actually is. Everything below runs over the surviving
  list.

The tally is a validation step, not a collection step. Check every returned
object before counting it:

| Returned | Treat as |
|----------|----------|
| A verdict word outside the vocabulary above | malformed ⇒ `ESCALATE` |
| `APPROVE` alongside any `blocking` finding | contradiction ⇒ `ESCALATE` |
| No verdict object from a seated panelist | missing ⇒ `ESCALATE` |

None of these may ever resolve toward approval — a tally that cannot be
validated escalates. The validated per-persona objects are this cycle's
`review.verdicts`; the HEAD SHA reviewed is the new `review.reviewed-head`;
the incremented counter is the new `review.cycle`.

### 7. Root-cause synthesis (mandatory)

This step is not optional and not conditional on the tally: run it on every
pass, before deciding anything.

<!-- glados:include vocabulary/root-cause.md -->

- Findings this step raises or consolidates are ordinary findings under the
  severity scale above — re-run the composition rules over the consolidated
  list before deciding. A `blocking` synthesis finding turns an
  otherwise-clean tally into `REQUEST_CHANGES`.
- Both answers, the clusters, and the consolidated list join this cycle's
  `review.verdicts` and ride in the composed `verdict` outcome. A pass whose
  record answers neither question is an incomplete pass, not a clean one.

### 8. Decide
- This step produces a `verdict` outcome carrying the per-persona verdicts,
  the root-cause synthesis, and the cycle's composed result.
- **Every panelist `APPROVE` (validated), and no `blocking` synthesis
  finding** → the MR is review-clean; the loop ends. What happens to the MR
  next is governed by `merge-authority`, resolved from `glados.yaml` — this
  document states no authority, and this workflow never merges.
- **Any `REQUEST_CHANGES`, or a `blocking` synthesis finding** → run the
  address-review workflow against the **consolidated** findings — a cluster
  goes over as its one consolidated finding, never as its members — then
  re-enter this workflow for the next cycle.
- **Any `ESCALATE`, or a failed validation above** → this run produces an
  `escalation` outcome carrying the open verdicts and stops the loop.

### 9. Handoff
- This run produces a `progress` outcome carrying the MR reference, the
  cycle number, and the composed result.
