## Review-panel roster

The panel = the standing lenses + project personas + feature personas.

Every lens reviews the code and the architecture. Prose — comments,
docstrings, READMEs, commit messages — is advisory territory for all of them:
a lens that finds a stale sentence fixes it in passing or says so in one
line, and no lens blocks on one.

**Standing lenses (always seated):**

| Lens | Mandate |
|------|---------|
| **UAT** | Does the change actually do what the spec/ticket promises? Exercise it as a user would; verify each acceptance criterion. |
| **Adversarial** | Attack the change: edge cases, error paths, race conditions, auth/tenancy holes, injection, data loss. |
| **Standards** | Audit the change against the project's standards tree, citing the standard. A breach blocks only when it changes behaviour or crosses a boundary the standard draws; otherwise it is one advisory line. |
| **Philosophy** | Audit the change's behaviour and structure against the project's philosophies — fail-fast, root-cause-not-symptom, and the project's stated values. |
| **Dead-code** | Hunt leftovers in the code: unused symbols, orphaned files, debug artifacts, commented-out code. |

**Project personas:** resolve `params.review-panel.personas` from `glados.yaml`;
load each named definition and seat it with its Responsibilities and Key
Questions as its mandate. A persona name resolves against two directories, in
order: the project's `product-knowledge/personas/` (project-authored personas —
searched first, so a project file wins a name collision), then the library
personas vendored by install into `.glados/personas/`.

**Feature personas:** when the state key `run.active-personas` is present (set
by plan-feature for this feature), load and seat those personas the same way,
in addition to the roster above. When absent, the manifest roster alone stands.

A persona named in the manifest or in `run.active-personas` whose definition
file is missing from both directories is a malformed panelist — handle per the
verdict composition rules, never by silently dropping the seat.
