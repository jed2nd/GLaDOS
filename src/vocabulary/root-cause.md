## Root-cause synthesis

Two questions no panelist can answer from inside its own lens: one about the
change, one about the finding set. Both are answered once, over the whole diff
and every panelist's findings together, and both are answered on **every** pass
— including a pass where the tally came back clean, which is exactly where a
shared cause hides. Neither may be skipped: "the change removes the cause" and
"the findings do not converge" are *answers*, stated with their reasoning.
Silence is not an answer.

Both answers belong in the run record. On the merge request the synthesis
appears only as a consolidated finding, when it produced one; a pass whose
findings do not converge says nothing about convergence to the author.

**1. Did the change attack the underlying cause?**

Name the underlying cause in one sentence first — the condition in the code
that made the reported problem possible, not a restatement of the problem —
then judge the diff against that sentence:

| What the diff does to the named cause | Reading |
|---|---|
| Removes it, or makes the bad state unrepresentable (a constraint, a type, one owner for the invariant) | **root-cause fix** |
| Leaves it in place and blocks the manifestations that were noticed | **symptom patch** |
| Leaves it in place because removing it is out of this change's scope, and says so | **scoped deferral** |

- A **symptom patch** whose cause is within this change's reach is a
  `blocking` finding: cite the cause, and name the change that would remove
  it.
- A **scoped deferral** is `advisory` only when the deferral is deliberate and
  written down — the cause named, the follow-up recorded so it outlives this
  MR. An undeclared deferral is a symptom patch.
- The ticket's framing does not settle this. A change that does exactly what
  the ticket asked can still be a symptom patch: the ticket is where the
  problem was noticed, not necessarily where it lives.

**2. Does the finding set converge on one cause?**

Cluster the panel's findings by the cause each one implies — ignoring which
persona raised it and which file it landed in. Several lenses reporting
several different defects around one bad seam is the signal this check exists
to catch. Where panelists returned a `root-cause` line, cluster those first;
where they did not, cluster from the findings themselves. A long finding list
is a reason to run this check harder, not evidence that the change is merely
sloppy.

A cluster is real when **one** change to the design would dissolve every
member of it. Test it that way: name that change, then walk each member and
say whether it survives. If members survive, they were never one cluster.

- A real cluster of two or more findings becomes **one** consolidated finding:
  the shared cause, the design change that dissolves it, and the member
  findings listed beneath it as evidence. It carries the highest severity
  among its members.
- The consolidated finding replaces its members as the unit of work — the
  remediation is that one design change, not one patch per member. The
  members stay listed so nothing is lost when the change lands.
- Two clusters, or none, is a normal result. Do not manufacture a shared cause
  to make a tidy story: an unforced cluster sends the author refactoring
  around a theory the code does not support. If the findings only rhyme, say
  they do not converge and leave them separate.
- Synthesis never lowers a severity and never collapses `blocking` findings
  into a single advisory suggestion.
