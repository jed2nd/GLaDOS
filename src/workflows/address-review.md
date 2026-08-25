---
name: address-review
kind: core
description: Resolve every open review finding in one coherent fix pass, then hand back for re-review
reads: [review.verdicts]
writes: [work.base-sha]
emits: [progress, escalation]
mutates: branch
requires: []
---

# (GLaDOS) Address Review

**Goal**: resolve every open finding from a review-mr pass in one coherent fix
pass — fixing from the main agent, gating on the full test suite, and mapping
each finding to its resolution — then hand back to review-mr for the next
pass. This is the fix half of the review loop
(build-feature → review-mr ⇄ address-review).

## Process

### 1. Load the verdicts
- Read `review.verdicts` from the run record: the tallied per-persona verdict
  objects for the latest cycle, with their findings. If no verdict demands
  changes, there is nothing to address — this run produces an `escalation`
  outcome (a mis-sequenced loop is worth a human glance) and stops.
- The MR HEAD you fix from is this run's base (`work.base-sha`, already
  recorded at run start).

### 2. Consolidate the findings

<!-- glados:include vocabulary/verdicts.md -->

<!-- glados:include vocabulary/root-cause.md -->

- Start from the cycle's **consolidated** finding list, which rides with
  `review.verdicts`. The root-cause synthesis has already clustered the
  panel's findings, and a cluster arrives as the single consolidated finding
  it produced — never re-expand one into its members in order to re-dedupe
  it. **Dedupe** only the findings the synthesis left standalone, from the
  verdict objects and their published copies: several personas often flag the
  same root cause; merge those into one finding that credits each lens.
- **When `review.verdicts` carries no synthesis**, consolidate here instead of
  guessing: collect every open finding across all panelists and dedupe them
  yourself, then say in the run record that you did so and why. This is the
  normal shape for a review that predates the synthesis step, for a review a
  human wrote by hand, and for records written by an older install. An absent
  synthesis is a stated condition with a stated response — never a reason to
  stop, and never a silent fallback to a different behavior.
- **Rank** the consolidated list by the severity tiers above, `blocking`
  first.
- For each finding decide: **fix now**, or **reasoned exception** — a finding
  you deliberately decline (it conflicts with in-tree house style, or is
  out of this change's scope by design). Every exception carries a one-line
  justification.
- Declining a `blocking` finding is not the author's call: record the
  disagreement and emit an `escalation` outcome rather than spending cycles
  arguing with the panel.
- Fix the **root cause**, not the symptom. Prefer making the bad state
  impossible (a constraint, a type) over defensively tolerating it. A review
  finding is a real defect until proven otherwise.
- **Honor the cycle's root-cause synthesis**, which rides with
  `review.verdicts`. Where the panel consolidated a cluster of findings into
  one, the unit of work is the single design change it names — make that
  change, then check each member off against the result. Do not reopen the
  cluster into one patch per member. If the design change leaves a member
  standing, that member is a separate finding again — say so, and fix it on
  its own.
- Where the synthesis read the change as a **symptom patch**, patching the
  listed symptoms does not resolve it: remove the cause it named, or record
  the disagreement and escalate. Do not spend a cycle re-fixing symptoms.

### 3. Fix from the main agent
- Apply the fixes yourself, as one coherent edit set — do **not** fan out to
  subagents (they reviewed; you fix). Keep changes minimal and idiomatic;
  match the closest in-tree precedent.
- Add or update tests that pin each fixed behavior — every `blocking` fix
  gets a test that would have caught it.

### 4. Full-suite gate
- Run the project linters and the **full test suite** — not just the files
  you touched — before committing. Distinguish pre-existing or known-flaky
  failures from regressions this fix pass introduced, and say which is which.
- Do not proceed with an unexplained failure: a fix pass that breaks
  something else has not resolved the review.

### 5. Commit and push

<!-- glados:include vocabulary/git-conventions.md -->

- Push the branch — the next review pass runs against the new HEAD.

### 6. Report resolutions
- This step produces a `progress` outcome mapping **each finding →
  resolution**: fixed (how, citing the commit) or declined (the one-line
  reason). Every consolidated finding appears exactly once; an unmentioned
  finding is an unresolved one.

### 7. Handoff

<!-- glados:include vocabulary/loop-bounds.md -->

- Check the cycle bound, then run the review-mr workflow against the new
  HEAD. The loop continues until the panel approves or the bound escalates.
