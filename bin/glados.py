#!/usr/bin/env python3
"""GLaDOS v2 toolchain — a dependency-free compiler, type-checker and installer.

One file, standard library only. Floor: Python 3.10 (uses ``pathlib`` newline=
kwarg on ``write_text``, ``str.removeprefix``; no ``match`` requirement, no
third-party deps, and — deliberately — no PyYAML: a minimal YAML-subset parser
lives in the PARSER section below).

GLaDOS v2 is a compiler for team attention. Each workflow *core* is a short
markdown file describing only its distinctive work; everything cross-cutting —
the mandatory preamble/epilogue, the outcome vocabulary, module presence — is
inlined at install time from one project manifest (``glados.yaml``). This tool
is the assembler + type-checker + per-runtime emitter that does the inlining.

Subcommands (see ``--help``):
    install         compile the sources against a manifest and emit one adapter
    check           CI mode: recompute the compile and diff it against install
    doctor          staleness + wiring report (never fails)
    migrate         guided v1 -> v2 migration: detect, generate, convert, clean
    verify-ledger   scan git history for silent-loss candidates (report-only)
    compile-plugin  shorthand: install --mode claude-plugin --target <repo>

Sections, in order:
    1.  CONSTANTS
    2.  PARSER            — YAML subset with line-numbered errors
    3.  SOURCE MODEL      — read cores/modules/kernel/registry/presets/aliases
    4.  MANIFEST          — load + phase-preset merge with provenance
    5.  COMPILE           — assemble one compiled core from the sources
    6.  TYPE CHECKS       — the eight fatal install-time checks
    7.  ASSEMBLY REPORT
    8.  ADAPTERS          — six emitters over the SAME compiled artifacts
    9.  VENDOR + SCAFFOLD
    10. COMMANDS          — install / check / doctor / migrate / verify-ledger
    11. CLI
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


# =============================================================================
# 1. CONSTANTS
# =============================================================================

VERSION = "2.2.3"

# The fifteen v2 cores, in canonical (pipeline-ish) order. The compiler touches
# ONLY these — other .md files under src/workflows are v1 leftovers deleted at
# integration and must be ignored.
CORES = [
    "intent",
    "adopt-codebase",
    "plan-feature",
    "spec-feature",
    "implement-feature",
    "verify-feature",
    "build-feature",
    "review-mr",
    "address-review",
    "run-epic",
    "fix-bug",
    "review-codebase",
    "retrospect",
    "steward",
    "brunch",
]

# The three v2 modules. Canonical inline order (stable → byte-stable output).
MODULES = ["mr-review-panel", "evaluator-spawn", "standards-gate"]

# Phase presets name "review-panel" (the params namespace); the module file is
# "mr-review-panel". This explicit alias — NOT a silent default — keeps the
# production/baseline presets compilable. Documented so nobody has to guess.
MODULE_ALIASES = {"review-panel": "mr-review-panel"}

# The four required phase values, named in the missing-phase fatal error.
PHASES = ["nascent", "evolving", "production", "sunset"]

# Two registry-documented reader fallbacks: a read of the state key is satisfied
# when the named manifest sub-key exists, even with no enabled writer.
READER_FALLBACKS = {
    "run.active-personas": "params.review-panel.personas",
    "epic.integration-branch": "branching.default-target",
}

# Keys the compiled preamble/epilogue always write — so reading them never fails
# the readers-without-writers check.
PREAMBLE_OWNED_WRITES = ["work.base-sha", "run.record"]

# Built-in sinks and whether each makes an outcome team-visible. Teams extend
# this set by declaring sinks under `sinks:` in glados.yaml — a declared sink is
# team-visible by default (it names an external destination people see), opt out
# with `team-visible: false`. The sink *name* set is therefore OPEN; only the
# built-ins are fixed. ``ledger`` is the one built-in that is NOT team-visible:
# it is the committed run record, valid alone only for progress/decision/observation.
BUILTIN_SINKS = {
    "mr-comment": True,
    "issue": True,
    "issue-comment": True,
    "label": True,
    "ledger": False,
}
# Built-in sinks that make an outcome team-visible (custom sinks are handled by
# _sink_is_team_visible, which honours their `team-visible:` declaration).
TEAM_VISIBLE_SINKS = {name for name, vis in BUILTIN_SINKS.items() if vis}
LEDGER_OK_TYPES = {"progress", "decision", "observation"}

# Merge-authority and decision strictness, STRICTEST first. "laxer = leftward"
# in the spec's own ``agent<record<escalate<forbidden`` ordering, so these lists
# read strict→lax and a lower index == stricter.
MERGE_AUTHORITY_ORDER = ["human", "agent-integration-only", "agent"]
DECISION_ORDER = ["forbidden", "escalate", "record", "agent"]

# AI Studio is a planning/review/spec surface: only advisory-safe cores get a
# paste bundle. fix-bug ships as its plan half, "fix-bug-advisory".
AISTUDIO_CORES = [
    "intent",
    "plan-feature",
    "spec-feature",
    "review-mr",
    "review-codebase",
    "retrospect",
]

INSTALL_MODES = ["claude", "claude-plugin", "direct", "gemini", "antigravity", "aistudio"]

INCLUDE_RE = re.compile(r"<!--\s*glados:include\s+(\S+?)\s*-->")

# Forbidden substrings in COMPILED core output (lint check 5).
FORBIDDEN_TOKENS = ["{{", "<!-- glados:include", "Invoke module", "OPTIMIZE_FOR"]

# Backticked `params.<ns>.<key>` literals in compiled text must resolve in the
# resolved manifest (type check 8) — a dangling literal is an unbounded loop.
PARAMS_LITERAL_RE = re.compile(r"`(params\.[a-z][a-z-]*\.[a-z][a-z-]*)`")

# The marker every generated claude-plugin SKILL.md stub carries. Skill pruning
# is marker-based: only a dir whose SKILL.md contains this string is ours to
# remove — user-authored skills are structurally untouchable.
PLUGIN_STUB_MARKER = "${CLAUDE_PLUGIN_ROOT}/compiled/claude-plugin/"

# Prepended to every claude-plugin compiled core. The plugin surface compiles
# from glados.yaml.example (there is no consuming repo at compile time), so
# these artifacts must never present the example's phase-resolved policy as
# the consumer's own: the compiler stamps the phase-neutral baseline
# optimize-for instead, and this guard makes the consuming repo's manifest
# govern at run time.
PLUGIN_BOOTSTRAP_GUARD = (
    "<!-- claude-plugin bootstrap guard — this artifact was compiled from\n"
    "     glados.yaml.example, not from the repo it runs in. -->\n"
    "**Bootstrap guard.** This workflow was compiled from `glados.yaml.example`,\n"
    "not this repo's manifest. Before step 1: if this repo has a `glados.yaml`,\n"
    "read it now — its `phase:`, decision rights, channels, merge authority, and\n"
    "params govern this run wherever they differ from the text below. If it has\n"
    "no `glados.yaml`, copy `glados.yaml.example` to `glados.yaml` (or run\n"
    "`/glados:init`) and set `phase:` before doing consequential work — do not\n"
    "guess a phase.\n\n"
)

# SDA (Structured Development Artifacts) — the version header stamped on
# conformant docs and the marker that makes the prepend idempotent. The
# manifest key `sda:` is an EXPLICIT-ONLY bool (a team declaration, default
# false): phase presets may never set it (_check_phase_invariants rejects
# one that tries), and `sda: true` scaffolds the conformance artifacts at
# install time (scaffold_sda, create-only). See docs/guides/sda.md.
SDA_MARKER = "SDA: v1.0"
SDA_HEADER = f"<!-- {SDA_MARKER} -->\n\n"

# The complete v1 name sets, for legacy-cleanup and v1-detection only. v1's
# direct mode installed these under product-knowledge/{workflows,modules}/;
# only files matching these names (or their underscore variants) may be
# removed from those shared directories — never unmatched user files.
KNOWN_V1_WORKFLOWS = frozenset({
    "address-review", "adopt-codebase", "autonomous-loop", "build-feature",
    "consolidate", "establish-standards", "identify-bug", "implement-feature",
    "implement-fix", "mission", "plan-feature", "plan-fix", "plan-product",
    "recombobulate", "retrospect", "review-codebase", "review-mr", "run-epic",
    "spec-feature", "verify-feature", "verify-fix",
})
KNOWN_V1_MODULES = frozenset({
    "capabilities", "evaluator-handoff", "evaluator-spawn", "interaction-proxy",
    "mr-review-panel", "observability", "pattern-observer", "persona-context",
    "persona-review", "standards-gate",
})

# migrate: markers that identify a v2-GENERATED artifact (a compiled core's
# provenance header, or an alias shim's rename sentence). The v1 and v2 name
# sets overlap (plan-feature is both a v1 workflow and a v2 core), so the
# v1 -> v2 migrate command may treat a name-matched file as a v1 leftover
# only when its CONTENT carries no v2 marker.
V2_ARTIFACT_MARKERS = ("GLaDOS v2 compiled artifact", "workflow was renamed")

# The phase line `migrate` writes into a generated glados.yaml. It parses to
# null in the YAML subset, so the very next install still fails fast with the
# four-phase message — migrate removes the blank-page problem without ever
# choosing a phase for the team.
MIGRATE_PHASE_LINE = ("phase: # REQUIRED - pick one: " + " | ".join(PHASES)
                      + " (who gets hurt when the agent is wrong; "
                        "see MIGRATION.md)")

# Advisory (never blocking) checklists printed when an install crosses a phase
# boundary relative to the previously vendored assembly report.
PHASE_TRANSITION_CHECKLISTS = {
    "nascent": [
        "confirm there are no users left to harm — nascent relaxes nearly "
        "every default",
    ],
    "evolving": [
        "standards tree seeded (product-knowledge/standards/) so the "
        "standards-gate has teeth",
        "review cadence agreed — verdicts land on MRs and someone reads them",
    ],
    "production": [
        "smoke suite seeded and green before the first production run",
        "CI check made blocking (drop --report-only in the .glados/ci/ job)",
        "escalation sink human-visible (channels.escalation reaches a queue "
        "a human triages)",
    ],
    "sunset": [
        "build-feature is disabled by the sunset preset — confirm no epic "
        "still depends on it",
        "every removal lands with a `decision` outcome visible on its "
        "MR/issue",
    ],
}


class Fatal(Exception):
    """A fatal, user-facing error. Message must name the file/key AND the fix."""


# =============================================================================
# 2. PARSER — a YAML subset with line-numbered errors
# =============================================================================
#
# Supported subset (enough for the restricted GLaDOS schema and no more):
#   * block maps and block lists, 2-space indentation
#   * inline flow lists: [a, b], []
#   * scalars: plain, single/double quoted, integers, true/false/null
#   * block scalars: > >- | |- (folded / literal, clip / strip chomping)
#   * comments ('# ...'), respecting quotes; '#' only starts a comment at line
#     start or after whitespace
# Anything outside the subset raises YamlError('<file>:<line>: <reason>').


class YamlError(Fatal):
    pass


def _strip_inline_comment(line: str) -> str:
    """Drop a trailing '# comment', honoring quotes and the space-before rule."""
    in_s = in_d = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            if i == 0 or line[i - 1] in " \t":
                return line[:i]
    return line


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unescape_double(s: str) -> str:
    """Minimal double-quoted-scalar escapes: \\\\ \\\" \\n \\t \\r; anything
    else keeps its backslash (lenient — the subset never emits more)."""
    out: list[str] = []
    i = 0
    escapes = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] in escapes:
            out.append(escapes[s[i + 1]])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_scalar(tok: str, filename: str, lineno: int):
    tok = tok.strip()
    if tok == "" or tok == "~" or tok == "null":
        return None
    if tok == "true":
        return True
    if tok == "false":
        return False
    if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
        return _unescape_double(tok[1:-1])
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return tok[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    return tok


def _split_flow_items(inner: str, filename: str, lineno: int) -> list[str]:
    """Split a flow-list body on commas, honoring quotes; reject nesting."""
    items: list[str] = []
    buf = ""
    in_s = in_d = False
    for ch in inner:
        if ch == "'" and not in_d:
            in_s = not in_s
            buf += ch
        elif ch == '"' and not in_s:
            in_d = not in_d
            buf += ch
        elif ch == "," and not in_s and not in_d:
            items.append(buf)
            buf = ""
        elif ch in "[{" and not in_s and not in_d:
            raise YamlError(f"{filename}:{lineno}: nested flow collections are "
                            f"not supported inside [...] — use a block list "
                            f"(one '- item' per line)")
        else:
            buf += ch
    if in_s or in_d:
        raise YamlError(f"{filename}:{lineno}: unterminated quote inside [...]")
    items.append(buf)
    return items


def _parse_flow_list(tok: str, filename: str, lineno: int):
    tok = tok.strip()
    if not tok.endswith("]"):
        raise YamlError(f"{filename}:{lineno}: unterminated inline list '{tok}' "
                        f"(the parser supports single-line flow lists only)")
    inner = tok[1:-1].strip()
    if inner == "":
        return []
    return [_parse_scalar(p, filename, lineno)
            for p in _split_flow_items(inner, filename, lineno)
            if p.strip() != ""]


def parse_yaml(text: str, filename: str = "<yaml>"):
    """Parse the restricted YAML subset into Python dict/list/scalar values."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n = len(lines)

    # YAML forbids tabs in indentation; silently misreading them re-parents
    # keys (a tab-indented sub-map becomes top-level keys). Fail loudly.
    for idx, line in enumerate(lines):
        if line.strip() == "":
            continue
        lead = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in lead:
            raise YamlError(f"{filename}:{idx + 1}: tab character in indentation "
                            f"— YAML forbids tabs; indent with 2 spaces")

    def skippable(i: int) -> bool:
        s = lines[i].strip()
        return s == "" or s.startswith("#")

    def next_meaningful(i: int) -> int:
        while i < n and skippable(i):
            i += 1
        return i

    def parse_block_scalar(i: int, parent_indent: int, style: str):
        collected: list[str] = []
        base = None
        while i < n:
            if lines[i].strip() == "":
                collected.append("")
                i += 1
                continue
            ci = _indent_of(lines[i])
            if ci <= parent_indent:
                break
            if base is None:
                base = ci
            collected.append(lines[i][base:])
            i += 1
        while collected and collected[-1] == "":
            collected.pop()
        if style[0] == ">":
            # Folded: our schema has no internal blank lines, so join on spaces.
            value = " ".join(s.strip() for s in collected)
        else:
            value = "\n".join(collected)
        if style.endswith("-"):
            value = value.rstrip("\n")
        return value, i

    def parse_map(i: int):
        i = next_meaningful(i)
        base = _indent_of(lines[i])
        result: dict = {}
        while i < n:
            if skippable(i):
                i += 1
                continue
            ci = _indent_of(lines[i])
            if ci < base:
                break
            if ci > base:
                raise YamlError(f"{filename}:{i + 1}: unexpected indent "
                                f"(expected {base} spaces, got {ci})")
            content = _strip_inline_comment(lines[i]).strip()
            if content.startswith("- "):
                raise YamlError(f"{filename}:{i + 1}: list item under a mapping")
            if ":" not in content:
                raise YamlError(f"{filename}:{i + 1}: expected 'key: value', "
                                f"got '{content}'")
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if key in result:
                raise YamlError(f"{filename}:{i + 1}: duplicate key '{key}' — "
                                f"the earlier value would be silently "
                                f"overwritten; remove one of the definitions")
            i += 1
            if val == "":
                j = next_meaningful(i)
                if j < n and _indent_of(lines[j]) > base:
                    child = _strip_inline_comment(lines[j]).strip()
                    if child.startswith("- ") or child == "-":
                        result[key], i = parse_list(j)
                    else:
                        result[key], i = parse_map(j)
                else:
                    result[key] = None
            elif val in (">", ">-", "|", "|-", ">+", "|+"):
                result[key], i = parse_block_scalar(i, base, val)
            elif val.startswith("["):
                result[key] = _parse_flow_list(val, filename, i)
            elif val.startswith("{"):
                raise YamlError(f"{filename}:{i}: flow mapping '{{...}}' is not "
                                f"supported — use a block map (nested 2-space-"
                                f"indented 'key: value' lines)")
            else:
                result[key] = _parse_scalar(val, filename, i)
        return result, i

    def parse_list(i: int):
        i = next_meaningful(i)
        base = _indent_of(lines[i])
        result: list = []
        while i < n:
            if skippable(i):
                i += 1
                continue
            ci = _indent_of(lines[i])
            if ci < base:
                break
            if ci > base:
                raise YamlError(f"{filename}:{i + 1}: unexpected indent in list")
            content = _strip_inline_comment(lines[i]).strip()
            if not (content.startswith("- ") or content == "-"):
                break
            item = content[1:].strip()
            i += 1
            if item == "":
                j = next_meaningful(i)
                if j < n and _indent_of(lines[j]) > base:
                    child = _strip_inline_comment(lines[j]).strip()
                    if child.startswith("- ") or child == "-":
                        sub, i = parse_list(j)
                    else:
                        sub, i = parse_map(j)
                    result.append(sub)
                else:
                    result.append(None)
            elif item.startswith("["):
                result.append(_parse_flow_list(item, filename, i))
            elif item.startswith("{"):
                raise YamlError(f"{filename}:{i}: flow mapping '{{...}}' is not "
                                f"supported — use a block map (nested 2-space-"
                                f"indented 'key: value' lines)")
            else:
                result.append(_parse_scalar(item, filename, i))
        return result, i

    start = next_meaningful(0)
    if start >= n:
        return {}
    first = _strip_inline_comment(lines[start]).strip()
    value, _ = (parse_list(start) if first.startswith("- ") else parse_map(start))
    return value


# =============================================================================
# 3. SOURCE MODEL — read cores, modules, kernel fragments, registry, presets
# =============================================================================


def read_text(path: Path) -> str:
    """Read a file as UTF-8, normalizing all newlines to LF for byte stability.

    Uses utf-8-sig so a BOM (PowerShell 5.1 writes one even for `-Encoding
    utf8`) is transparent; a UTF-16 file fails with a named fix instead of a
    traceback."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Fatal(f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start}) "
                    f"— re-save the file as UTF-8; PowerShell writes UTF-16 by "
                    f"default (use `Set-Content -Encoding utf8` or your "
                    f"editor's encoding menu)")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_frontmatter(text: str, filename: str = "<md>"):
    """Return (frontmatter_dict_or_None, body). Body has its leading blank
    lines trimmed."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return parse_yaml(fm_text, filename), body
    raise Fatal(f"{filename}: unterminated frontmatter — add a closing '---'")


def strip_leading_html_comment(text: str) -> str:
    """Drop a kernel fragment's leading '<!-- ... -->' authoring comment."""
    t = text.lstrip("\n")
    if t.startswith("<!--"):
        end = t.find("-->")
        if end != -1:
            return t[end + 3:].lstrip("\n")
    return text


def find_includes(text: str) -> list[str]:
    return INCLUDE_RE.findall(text)


class Source:
    """Everything the compiler reads from the GLaDOS source tree."""

    def __init__(self, root: Path):
        self.root = root
        self.src = root / "src"
        if not self.src.is_dir():
            raise Fatal(f"no src/ tree under {root} — pass --source <glados repo> "
                        f"(a vendored .glados/glados.py cannot find the sources "
                        f"on its own)")
        self.kernel = self.src / "kernel"
        self.preamble_raw = read_text(self._require(self.kernel / "preamble.md"))
        self.epilogue_raw = read_text(self._require(self.kernel / "epilogue.md"))
        self.registry = parse_yaml(
            read_text(self._require(self.kernel / "state-registry.yaml")),
            "state-registry.yaml")
        self.presets = parse_yaml(
            read_text(self._require(self.kernel / "presets" / "phases.yaml")),
            "phases.yaml")
        self.aliases = parse_yaml(
            read_text(self._require(self.kernel / "aliases.yaml")),
            "aliases.yaml").get("aliases", {})
        self.cores = {c: self._load_unit("workflows", c) for c in CORES}
        self.modules = {m: self._load_unit("modules", m) for m in MODULES}

    @staticmethod
    def _require(path: Path) -> Path:
        if not path.exists():
            raise Fatal(f"missing kernel source {path} — the GLaDOS source tree "
                        f"is incomplete; pass --source <a full glados checkout> "
                        f"or restore the file")
        return path

    def _load_unit(self, kind: str, name: str) -> dict:
        path = self.src / kind / f"{name}.md"
        if not path.exists():
            raise Fatal(f"missing {kind[:-1]} source {path} — the compiler needs "
                        f"all {len(CORES)} cores and {len(MODULES)} modules")
        fm, body = split_frontmatter(read_text(path), f"{name}.md")
        if fm is None:
            raise Fatal(f"{path}: no YAML frontmatter (needs reads/writes/emits)")
        # Type-validate the frontmatter fields the compiler consumes. A wrong
        # shape here otherwise surfaces as a raw AttributeError deep inside an
        # adapter (or, worse, a str silently list()-ed into characters).
        desc = fm.get("description", name)
        if not isinstance(desc, str):
            raise Fatal(f"{path}: frontmatter field 'description' must be a "
                        f"one-line string, got {type(desc).__name__} — quote it")
        lists: dict[str, list] = {}
        for field in ("reads", "writes", "emits", "requires"):
            val = fm.get(field)
            if val is None:
                lists[field] = []
            elif isinstance(val, str):
                raise Fatal(f"{path}: frontmatter field '{field}' must be a "
                            f"list of strings, got the bare string '{val}' — "
                            f"write [{val}] or a '- item' block list")
            elif isinstance(val, list) and all(isinstance(x, str) for x in val):
                lists[field] = list(val)
            else:
                raise Fatal(f"{path}: frontmatter field '{field}' must be a "
                            f"list of strings — got {val!r}")
        return {
            "name": name,
            "path": path,
            "frontmatter": fm,
            "body": body,
            "reads": lists["reads"],
            "writes": lists["writes"],
            "emits": lists["emits"],
            "requires": lists["requires"],
            "description": desc,
        }

    # -- registry helpers -----------------------------------------------------
    def registry_keys(self) -> set[str]:
        return set((self.registry.get("keys") or {}).keys())

    def outcome_types(self) -> set[str]:
        return set(self.registry.get("outcome-types") or [])

    def manifest_keys(self) -> set[str]:
        keys = self.registry.get("keys") or {}
        return {k for k, v in keys.items() if isinstance(v, dict) and v.get("home") == "manifest"}


def include_fragment(source: Source, rel: str, _stack: tuple = ()) -> str:
    if rel in _stack:
        chain = " -> ".join(_stack + (rel,))
        raise Fatal(f"glados:include cycle: {chain} — remove one of the "
                    f"includes to break the loop")
    if len(_stack) > 16:
        raise Fatal(f"glados:include recursion too deep at '{rel}' (chain: "
                    f"{' -> '.join(_stack)}) — flatten the fragment nesting")
    root = source.src.resolve()
    path = (source.src / rel).resolve()
    if not path.is_relative_to(root):
        raise Fatal(f"glados:include escapes the source tree: '{rel}' resolves "
                    f"to {path}, outside {root} — includes must stay within "
                    f"src/")
    if not path.exists():
        raise Fatal(f"dangling glados:include '{rel}' — no file at {path}; add "
                    f"the fragment or remove the directive")
    frag = read_text(path)
    return resolve_includes(source, frag, _stack + (rel,))


def resolve_includes(source: Source, text: str, _stack: tuple = ()) -> str:
    return INCLUDE_RE.sub(lambda m: include_fragment(source, m.group(1), _stack), text)


# =============================================================================
# 4. MANIFEST — load, merge phase presets with provenance
# =============================================================================


class Resolved:
    """A resolved manifest plus per-leaf provenance and relaxation bookkeeping."""

    def __init__(self):
        self.values: dict = {}
        self.provenance: dict[str, str] = {}   # leaf-path -> baseline|phase:X|explicit
        self.relaxed_phase: list[str] = []      # keys where phase is laxer than baseline
        self.phase = ""
        self.raw: dict = {}


def _is_laxer(order: list[str], a, b) -> bool:
    try:
        return order.index(a) > order.index(b)
    except ValueError:
        return False


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise Fatal(f"no manifest at {path} — copy glados.yaml.example to "
                    f"{path.name} in your repo root and edit it")
    return parse_yaml(read_text(path), path.name)


def resolve_manifest(raw: dict, presets: dict, manifest_name: str) -> Resolved:
    phase = raw.get("phase")
    if phase is None:
        raise Fatal(f"{manifest_name}: required key 'phase:' is missing — set it "
                    f"to one of: {', '.join(PHASES)}")
    if phase not in PHASES:
        raise Fatal(f"{manifest_name}: phase '{phase}' is not valid — use one of: "
                    f"{', '.join(PHASES)}")

    baseline = presets.get("baseline") or {}
    preset = presets.get(phase) or {}
    r = Resolved()
    r.phase = phase
    r.raw = raw
    plabel = f"phase:{phase}"

    def layer_scalar(key: str):
        val, prov = baseline.get(key), "baseline"
        if key in preset:
            val, prov = preset[key], plabel
        if key in raw:
            val, prov = raw[key], "explicit"
        r.values[key] = val
        r.provenance[key] = prov

    # disabled-workflows layers like the scalars (an explicit list replaces the
    # preset list wholesale — [] re-enables everything a preset disabled), so a
    # phase preset CAN flip workflow existence (sunset unloads build-feature).
    for key in ("optimize-for", "merge-authority", "default-modules",
                "disabled-workflows"):
        layer_scalar(key)

    # channels + decisions: per-leaf layering
    for group in ("channels", "decisions"):
        merged: dict = {}
        for leaf, val in (baseline.get(group) or {}).items():
            merged[leaf] = val
            r.provenance[f"{group}.{leaf}"] = "baseline"
        for leaf, val in (preset.get(group) or {}).items():
            merged[leaf] = val
            r.provenance[f"{group}.{leaf}"] = plabel
        for leaf, val in (raw.get(group) or {}).items():
            merged[leaf] = val
            r.provenance[f"{group}.{leaf}"] = "explicit"
        r.values[group] = merged

    # params: deep-ish merge (baseline<phase<explicit), review-panel.max-cycles tracked
    params: dict = {}
    for layer, label in ((baseline, "baseline"), (preset, plabel), (raw, "explicit")):
        for ns, cfg in (layer.get("params") or {}).items():
            params.setdefault(ns, {})
            if isinstance(cfg, dict):
                for k, v in cfg.items():
                    params[ns][k] = v
                    r.provenance[f"params.{ns}.{k}"] = label
    r.values["params"] = params

    # pass-through explicit-only keys. `sinks:` is project-declared like
    # branching: presets never set it (it is not in the anti-inflation
    # allowlist), and its bodies are freeform config the agent interprets at
    # run time — the compiler only checks structure and referential integrity.
    for key in ("platform", "phase", "branching", "workflows", "sinks",
                "visibility-acknowledged", "relaxation-acknowledged"):
        if key in raw:
            r.values[key] = raw[key]
            r.provenance[key] = "explicit"

    # sda: EXPLICIT-ONLY bool, default false. Conformance is a team
    # declaration, not a phase default — a preset naming it is rejected by
    # _check_phase_invariants; here only the manifest's own word counts.
    sda = raw.get("sda", False)
    if not isinstance(sda, bool):
        raise Fatal(f"{manifest_name}: key 'sda' must be a bool — got "
                    f"{sda!r}; write 'sda: true' or 'sda: false' (SDA "
                    f"conformance is declared, never implied)")
    r.values["sda"] = sda
    r.provenance["sda"] = "explicit" if "sda" in raw else "default"

    # RELAXED(phase): a value whose resolved provenance is the phase preset AND
    # that is laxer than the strict baseline. An explicit override (provenance
    # 'explicit') is NOT phase-derived, so it clears the marker.
    if r.provenance.get("merge-authority") == plabel and _is_laxer(
            MERGE_AUTHORITY_ORDER, r.values.get("merge-authority"),
            baseline.get("merge-authority")):
        r.relaxed_phase.append("merge-authority")
    for leaf, val in (r.values.get("decisions") or {}).items():
        if r.provenance.get(f"decisions.{leaf}") == plabel and _is_laxer(
                DECISION_ORDER, val, (baseline.get("decisions") or {}).get(leaf)):
            r.relaxed_phase.append(leaf)

    return r


def resolved_subkey_exists(r: Resolved, dotted: str) -> bool:
    """Does e.g. 'branching.default-target' or 'params.review-panel.personas'
    exist (non-empty) in the resolved manifest?"""
    node = r.values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return node is not None and node != [] and node != {}


def manifest_hash_of(path: Path) -> str:
    return hashlib.sha256(read_text(path).encode("utf-8")).hexdigest()


# =============================================================================
# 5. COMPILE — assemble one compiled core from the sources
# =============================================================================


def effective_modules(source: Source, core_name: str, r: Resolved) -> list[str]:
    """workflows[wf] (or default-modules if absent) ∪ core.requires, aliased,
    deduped, in canonical order."""
    workflows = r.values.get("workflows") or {}
    if core_name in workflows and workflows[core_name] is not None:
        base = list(workflows[core_name])
    else:
        base = list(r.values.get("default-modules") or [])
    requires = source.cores[core_name]["requires"]
    chosen = set()
    for m in base + requires:
        chosen.add(MODULE_ALIASES.get(m, m))
    return [m for m in MODULES if m in chosen]


def provenance_header(core_name: str, manifest_hash: str, modules: list[str]) -> str:
    mods = ", ".join(modules) if modules else "none"
    return (
        "<!-- GLaDOS v2 compiled artifact — generated by glados.py; "
        "edit the source, not this file.\n"
        f"     workflow: {core_name}\n"
        f"     glados-version: {VERSION}\n"
        f"     manifest-sha256: {manifest_hash}\n"
        f"     modules-inlined: {mods}\n"
        f"     compile-source: src/workflows/{core_name}.md\n"
        "-->"
    )


def compile_core(source: Source, core_name: str, r: Resolved, manifest_hash: str) -> str:
    core = source.cores[core_name]
    modules = effective_modules(source, core_name, r)

    optimize_for = r.values.get("optimize-for") or ""
    preamble = strip_leading_html_comment(source.preamble_raw)
    preamble = preamble.replace("{{OPTIMIZE_FOR}}", optimize_for).strip("\n")
    epilogue = strip_leading_html_comment(source.epilogue_raw).strip("\n")

    header = provenance_header(core_name, manifest_hash, modules)
    core_body = resolve_includes(source, core["body"]).strip("\n")

    parts = [preamble, header, core_body]
    for m in modules:
        parts.append(resolve_includes(source, source.modules[m]["body"]).strip("\n"))
    parts.append(epilogue)
    return "\n\n".join(parts) + "\n"


class Compilation:
    """The mode-independent compiled artifacts for one (source, manifest)."""

    def __init__(self, source: Source, r: Resolved, manifest_hash: str):
        self.source = source
        self.resolved = r
        self.manifest_hash = manifest_hash
        self.enabled = [c for c in CORES
                        if c not in set(r.values.get("disabled-workflows") or [])]
        self.cores = {c: compile_core(source, c, r, manifest_hash) for c in self.enabled}
        self.modules_for = {c: effective_modules(source, c, r) for c in self.enabled}


def compile_all(source: Source, r: Resolved, manifest_hash: str) -> Compilation:
    return Compilation(source, r, manifest_hash)


# =============================================================================
# 6. TYPE CHECKS — the eight fatal install-time checks
# =============================================================================


def _sinks_map(r: Resolved) -> dict:
    """The manifest's sinks: block as a dict, coerced to {} when malformed (a
    non-mapping is reported by run_type_checks; the helpers must not crash on it
    before that error surfaces)."""
    sinks = r.values.get("sinks")
    return sinks if isinstance(sinks, dict) else {}


def _declared_sinks(r: Resolved) -> set[str]:
    """Every sink name a channels: binding may reference: the built-ins plus
    whatever the manifest declares under sinks:."""
    return set(BUILTIN_SINKS) | set(_sinks_map(r).keys())


def _sink_is_team_visible(name: str, r: Resolved) -> bool:
    """Does delivery to this sink satisfy the team-visibility invariant? A
    built-in keeps its fixed classification (ledger is the only quiet one); a
    project-declared sink must say so itself.

    Fails CLOSED. run_type_checks requires `team-visible` on every declared
    sink, so a missing key here means the manifest did not pass checks (a
    --report-only run, or a malformed body reported separately) — and the safe
    reading of "we do not know whether anyone sees this" is that nobody
    does."""
    if name in BUILTIN_SINKS:
        return BUILTIN_SINKS[name]
    cfg = _sinks_map(r).get(name)
    if not isinstance(cfg, dict):
        return False
    return cfg.get("team-visible") is True


def run_type_checks(source: Source, comp: Compilation) -> list[str]:
    """Return a list of fatal error strings (empty == passes)."""
    r = comp.resolved
    errors: list[str] = []
    enabled = set(comp.enabled)

    # Enabled modules = union of effective modules across enabled cores.
    enabled_modules: set[str] = set()
    for c in comp.enabled:
        enabled_modules.update(comp.modules_for[c])

    units_cores = [source.cores[c] for c in comp.enabled]
    units_modules = [source.modules[m] for m in sorted(enabled_modules)]
    all_units = units_cores + units_modules

    registry_keys = source.registry_keys()
    outcome_types = source.outcome_types()

    # ---- (4) unknown tokens -------------------------------------------------
    for u in all_units:
        for tok in u["reads"] + u["writes"]:
            if tok not in registry_keys:
                errors.append(f"{u['name']}: '{tok}' in reads/writes is not a "
                              f"registered state key — add it to "
                              f"src/kernel/state-registry.yaml or fix the typo")
        for tok in u["emits"]:
            if tok not in outcome_types:
                errors.append(f"{u['name']}: emits '{tok}', not an outcome-type — "
                              f"use one of {', '.join(sorted(outcome_types))}")

    # ---- (1) readers without writers ---------------------------------------
    writers: set[str] = set(PREAMBLE_OWNED_WRITES) | source.manifest_keys()
    for u in all_units:
        writers.update(u["writes"])
    readers: dict[str, list[str]] = {}
    for u in all_units:
        for key in u["reads"]:
            readers.setdefault(key, []).append(u["name"])
    for key, who in readers.items():
        if key in writers:
            continue
        fallback = READER_FALLBACKS.get(key)
        if fallback and resolved_subkey_exists(r, fallback):
            continue
        fix = (f"enable a workflow that writes it, or set manifest '{fallback}'"
               if fallback else "enable a workflow that writes it")
        errors.append(f"state key '{key}' is read by [{', '.join(sorted(set(who)))}] "
                      f"but no enabled writer covers it — {fix}")

    # ---- (2) sink bindings + zero-sink emits --------------------------------
    channels = r.values.get("channels") or {}
    sinks_cfg = r.values.get("sinks")
    vis_ack = r.values.get("visibility-acknowledged")
    # Validate the sinks: block shape. Sink *bodies* are freeform config the
    # agent interprets at run time (channel, format, grouping, threads, …); the
    # compiler checks only that a declaration is a mapping and that an explicit
    # team-visible: is a bool — never the meaning of the other keys.
    if sinks_cfg is not None and not isinstance(sinks_cfg, dict):
        errors.append("sinks: must be a mapping of sink-name -> config (e.g. "
                      "'slack:\\n    channel: \"#reviews\"')")
        sinks_cfg = {}
    for name, cfg in (sinks_cfg or {}).items():
        if cfg is not None and not isinstance(cfg, dict):
            errors.append(f"sinks.{name}: must be a mapping (e.g. 'channel: ...') "
                          f"or empty — got {cfg!r}")
        elif isinstance(cfg, dict) and "team-visible" in cfg:
            # A built-in's visibility is fixed in BUILTIN_SINKS, and
            # _sink_is_team_visible never consults the body for one. Accepting
            # the key would install clean and change nothing — precisely the
            # silent no-op this checker exists to prevent. Say so instead.
            if name in BUILTIN_SINKS:
                fixed = "team-visible" if BUILTIN_SINKS[name] else "record-only"
                errors.append(f"sinks.{name}.team-visible: '{name}' is a built-in "
                              f"sink and its visibility is fixed ({fixed}); the "
                              f"key would be ignored — remove it, or declare a "
                              f"differently-named sink you own")
            elif not isinstance(cfg["team-visible"], bool):
                errors.append(f"sinks.{name}.team-visible: must be true or false — "
                              f"got {cfg['team-visible']!r}")
        elif name not in BUILTIN_SINKS:
            # The dangerous direction. Opening the sink vocabulary means a
            # name nobody recognises can be bound in channels:, and a body
            # that says nothing about visibility used to satisfy the
            # team-visibility invariant by default — so an outcome no human
            # ever sees passed the check that exists to prevent exactly that.
            # A project-declared sink states its visibility or it does not
            # install.
            errors.append(f"sinks.{name}: a project-declared sink must state "
                          f"`team-visible: true` or `team-visible: false` — "
                          f"without it, binding an outcome to '{name}' would "
                          f"satisfy the team-visibility check by default")
    declared = _declared_sinks(r)
    # channels: the outcome-type on the left must be real; each sink on the
    # right must be DECLARED (built-in or named in sinks:). A typo is silent
    # visibility loss — the referential-integrity check catches it statically
    # without the sink vocabulary being a closed enum.
    for otype in sorted(channels.keys()):
        if otype not in outcome_types:
            errors.append(f"channels.{otype}: not a registered outcome type — "
                          f"use one of {', '.join(sorted(outcome_types))}")
        for s in (channels.get(otype) or []):
            if s not in declared:
                errors.append(f"channels.{otype}: sink '{s}' is not declared — "
                              f"declare it under sinks: in glados.yaml, or fix "
                              f"the typo (declared: {', '.join(sorted(declared))})")
    emitted: set[str] = set()
    for u in all_units:
        emitted.update(u["emits"])
    for otype in sorted(emitted):
        sinks = channels.get(otype) or []
        if not sinks:
            errors.append(f"outcome '{otype}' is emitted but bound to no sink in "
                          f"channels: — add a sink under channels.{otype}")
            continue
        if otype in LEDGER_OK_TYPES:
            continue
        if any(_sink_is_team_visible(s, r) for s in sinks):
            continue
        if vis_ack == "ledger-only":
            continue
        errors.append(f"outcome '{otype}' has no team-visible sink "
                      f"(has {sinks}); bind a team-visible sink under "
                      f"channels.{otype} (built-in: "
                      f"{', '.join(sorted(TEAM_VISIBLE_SINKS))}), declare one in "
                      f"sinks:, or confess with 'visibility-acknowledged: ledger-only'")

    # ---- (3) requires satisfied --------------------------------------------
    # First: every module/workflow token the manifest names must exist. A typo
    # here otherwise SILENTLY drops the module from the compiled output —
    # exactly the v1 "mode that stops shipping modules" bug.
    workflows = r.values.get("workflows") or {}
    for tok in (r.values.get("default-modules") or []):
        if MODULE_ALIASES.get(tok, tok) not in MODULES:
            errors.append(f"default-modules: unknown module '{tok}' — modules "
                          f"are: {', '.join(MODULES)} (a typo here silently "
                          f"drops the module from every workflow)")
    for wf, mods in sorted(workflows.items()):
        if wf not in CORES:
            errors.append(f"workflows.{wf}: unknown workflow — cores are: "
                          f"{', '.join(CORES)}")
            continue
        for tok in (mods or []):
            if MODULE_ALIASES.get(tok, tok) not in MODULES:
                errors.append(f"workflows.{wf}: unknown module '{tok}' — "
                              f"modules are: {', '.join(MODULES)} (a typo here "
                              f"silently drops the module from the workflow)")
    for wf in (r.values.get("disabled-workflows") or []):
        if wf not in CORES:
            errors.append(f"disabled-workflows: unknown workflow '{wf}' — "
                          f"cores are: {', '.join(CORES)} (a typo here leaves "
                          f"the workflow enabled)")
    for c in comp.enabled:
        req = [MODULE_ALIASES.get(m, m) for m in source.cores[c]["requires"]]
        for m in req:
            if m not in MODULES:
                errors.append(f"{c}: requires unknown module '{m}'")
                continue
            if c in workflows and workflows[c] is not None:
                listed = [MODULE_ALIASES.get(x, x) for x in workflows[c]]
                if m not in listed:
                    errors.append(f"workflows.{c} must list required module "
                                  f"'{m}' — add it to the {c} module list in "
                                  f"the manifest")

    # ---- (5) forbidden-content lint on compiled output ---------------------
    for c, text in comp.cores.items():
        for tok in FORBIDDEN_TOKENS:
            if tok in text:
                errors.append(f"compiled {c}: contains forbidden token '{tok}' — "
                              f"an unresolved include/placeholder slipped through")

    # ---- (6) phase invariants ----------------------------------------------
    errors.extend(_check_phase_invariants(source, r))

    # ---- (7) alias-map integrity -------------------------------------------
    for alias, target in source.aliases.items():
        if alias in CORES:
            errors.append(f"aliases.yaml: alias '{alias}' shadows the real core "
                          f"of the same name — its shim would overwrite the "
                          f"compiled core; remove the alias (aliases exist only "
                          f"for dead v1 names)")
        if target not in CORES:
            errors.append(f"aliases.yaml: alias '{alias}' -> '{target}' is not a "
                          f"real core — fix the target or add the core")

    # ---- (8) dotted params literals resolve ---------------------------------
    # Compiled text tells the agent to resolve `params.<ns>.<key>` from
    # glados.yaml at run time; a literal with no resolved value is a safety
    # bound that resolves to nothing (an unbounded loop, a missing roster).
    params = r.values.get("params") or {}
    for c, text in comp.cores.items():
        for lit in sorted(set(PARAMS_LITERAL_RE.findall(text))):
            _, ns, key = lit.split(".")
            if (params.get(ns) or {}).get(key) is None:
                errors.append(f"compiled {c}: references `{lit}` but the "
                              f"resolved manifest has no value for it — set it "
                              f"under params: in glados.yaml or a phase-preset "
                              f"baseline")

    return errors


def _check_phase_invariants(source: Source, r: Resolved) -> list[str]:
    errors: list[str] = []

    # anti-inflation: a preset may only set keys present in glados.yaml.example
    # (+ the preset-native optimize-for).
    example_path = source.root / "glados.yaml.example"
    allowed = {"optimize-for"}
    if example_path.exists():
        allowed |= set(parse_yaml(read_text(example_path), "glados.yaml.example").keys())
    for pname, preset in source.presets.items():
        if not isinstance(preset, dict):
            continue
        for key in preset.keys():
            if key == "sda":
                # sda is in the manifest schema but is EXPLICIT-ONLY:
                # conformance is a team declaration, not a phase default.
                errors.append(f"phases.yaml: preset '{pname}' sets 'sda' — "
                              f"SDA conformance is a team declaration, not a "
                              f"phase default; remove it from the preset and "
                              f"set 'sda: true' explicitly in glados.yaml")
            elif key not in allowed:
                errors.append(f"phases.yaml: preset '{pname}' sets '{key}', which "
                              f"is not a glados.yaml schema key — presets may only "
                              f"spell existing defaults (anti-inflation cap)")

    # explicit keys laxer than the phase preset require relaxation-acknowledged
    raw = r.raw
    phase = r.phase
    preset = source.presets.get(phase) or {}
    baseline = source.presets.get("baseline") or {}
    acked = set(raw.get("relaxation-acknowledged") or [])

    if "merge-authority" in raw:
        floor = preset.get("merge-authority", baseline.get("merge-authority"))
        if _is_laxer(MERGE_AUTHORITY_ORDER, raw["merge-authority"], floor) \
                and "merge-authority" not in acked:
            errors.append(f"merge-authority '{raw['merge-authority']}' is laxer than "
                          f"phase '{phase}' ('{floor}') — add 'merge-authority' to "
                          f"relaxation-acknowledged: in the manifest to confess it")
    for leaf, val in (raw.get("decisions") or {}).items():
        floor = (preset.get("decisions") or {}).get(
            leaf, (baseline.get("decisions") or {}).get(leaf))
        if _is_laxer(DECISION_ORDER, val, floor) and leaf not in acked:
            errors.append(f"decision '{leaf}: {val}' is laxer than phase '{phase}' "
                          f"('{floor}') — add '{leaf}' to relaxation-acknowledged: "
                          f"in the manifest to confess it")

    return errors


# =============================================================================
# 7. ASSEMBLY REPORT
# =============================================================================


def build_assembly_report(source: Source, comp: Compilation) -> str:
    r = comp.resolved
    out: list[str] = []
    out.append("# GLaDOS assembly report")
    out.append("")
    out.append(f"- phase: `{r.phase}`")
    out.append(f"- manifest sha256: `{comp.manifest_hash}`")
    out.append(f"- glados version: {VERSION}")
    out.append(f"- enabled cores: {len(comp.enabled)} / {len(CORES)}")
    out.append("")

    out.append("## Resolved manifest keys")
    out.append("")
    out.append("| Key | Value | Provenance |")
    out.append("|-----|-------|------------|")
    for key in ("phase", "platform", "sda", "merge-authority", "optimize-for",
                "default-modules", "disabled-workflows"):
        if key in r.values and r.values[key] is not None:
            out.append(f"| {key} | {_fmt(r.values[key])} | "
                       f"({r.provenance.get(key, 'baseline')}) |")
    for group in ("channels", "decisions"):
        for leaf in sorted((r.values.get(group) or {}).keys()):
            path = f"{group}.{leaf}"
            out.append(f"| {path} | {_fmt(r.values[group][leaf])} | "
                       f"({r.provenance.get(path, 'baseline')}) |")
    for ns, cfg in sorted((r.values.get("params") or {}).items()):
        for k in sorted(cfg.keys()):
            path = f"params.{ns}.{k}"
            out.append(f"| {path} | {_fmt(cfg[k])} | "
                       f"({r.provenance.get(path, 'baseline')}) |")
    for leaf in sorted((r.values.get("branching") or {}).keys()):
        out.append(f"| branching.{leaf} | {_fmt(r.values['branching'][leaf])} | "
                   f"(explicit) |")
    out.append("")

    out.append("## Sinks")
    out.append("")
    out.append("Where outcomes land, and whether delivery counts as team-visible. "
               "Built-ins are always available; a project may declare more under "
               "`sinks:` (bodies are freeform config the agent interprets at run "
               "time). Runtime delivery is verified — a bound sink that fails "
               "to receive its outcome escalates, whatever the other sinks got.")
    out.append("")
    out.append("| Sink | Team-visible | Config | Source |")
    out.append("|------|--------------|--------|--------|")
    declared_sinks = _sinks_map(r)
    for name in sorted(BUILTIN_SINKS):
        vis = "yes" if _sink_is_team_visible(name, r) else "no (record-only)"
        # A built-in the manifest configured is still explicitly configured;
        # reporting it as plain "built-in" hides the one row a reader checks.
        src = "built-in (explicit config)" if declared_sinks.get(name) else "built-in"
        out.append(f"| {name} | {vis} | {_fmt(declared_sinks.get(name) or {})} | "
                   f"{src} |")
    for name in sorted(declared_sinks):
        if name in BUILTIN_SINKS:
            continue
        vis = "yes" if _sink_is_team_visible(name, r) else "no (record-only)"
        out.append(f"| {name} | {vis} | {_fmt(declared_sinks.get(name) or {})} | "
                   f"(explicit) |")
    out.append("")

    out.append(f"## RELAXED(phase) markers: {len(r.relaxed_phase)}")
    out.append("")
    if r.relaxed_phase:
        out.append("Values the phase preset relaxes below the strict baseline:")
        out.append("")
        for key in r.relaxed_phase:
            val = (r.values.get("decisions") or {}).get(key, r.values.get(key))
            out.append(f"- RELAXED(phase) `{key}` = `{_fmt(val)}`")
    else:
        out.append("_None — the phase preset is no laxer than baseline._")
    out.append("")

    out.append("## Per-workflow assembly")
    out.append("")
    for c in comp.enabled:
        mods = comp.modules_for[c]
        incs = find_includes(source.cores[c]["body"])
        for m in mods:
            incs += find_includes(source.modules[m]["body"])
        seen: list[str] = []
        for inc in incs:
            if inc not in seen:
                seen.append(inc)
        out.append(f"- **{c}** — core + modules[{', '.join(mods) or 'none'}] + "
                   f"fragments[{', '.join(seen) or 'none'}]")
    out.append("")

    out.append("## Waived / acknowledged confessions")
    out.append("")
    confessions = False
    for key in ("visibility-acknowledged", "relaxation-acknowledged"):
        if key in r.raw:
            confessions = True
            out.append(f"- `{key}: {_fmt(r.raw[key])}`")
    if not confessions:
        out.append("_None._")
    out.append("")

    if r.values.get("sda"):
        out.append("## SDA conformance")
        out.append("")
        out.append("`sda: true` — the install scaffolds these artifacts "
                   "(create-only, never clobbered):")
        out.append("")
        out.append("- `claims.md` (repo root) — the SDA claims file")
        out.append("- `product-knowledge/SPEC_LOG.md` — the SDA work-unit log")
        out.append("- `SDA: v1.0` headers on an existing "
                   "`product-knowledge/ROADMAP.md` and `PROJECT_STATUS.md`")
        out.append("- `product-knowledge/standards/sda-*.md` — the versioned "
                   "standard and profile docs")
        out.append("")
    return "\n".join(out)


def _fmt_cell(s: str) -> str:
    """One markdown-table-safe cell. A pipe ends the cell early and a newline
    ends the ROW, so a multi-line value — the normal shape of a freeform sink
    body the agent interprets at run time — has to be summarized rather than
    inlined, or it tears the table apart. Single-line values pass through with
    only pipes escaped, so a long-but-flat entry like optimize-for still reads
    in full."""
    lines = s.splitlines()
    if len(lines) <= 1:
        return s.replace("|", "\\|")
    head = lines[0].strip().replace("|", "\\|")
    if len(head) > 60:
        head = head[:60].rstrip()
    return f"{head}… ({len(lines)} lines)"


def _fmt(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, list):
        return "[" + ", ".join(str(v) for v in val) + "]"
    if isinstance(val, dict):
        return "{" + ", ".join(f"{k}: {_fmt(v)}" for k, v in val.items()) + "}"
    if isinstance(val, str):
        return _fmt_cell(val)
    return str(val)


# =============================================================================
# 8. ADAPTERS — six emitters over the SAME compiled artifacts
# =============================================================================
#
# Each adapter is a pure function returning a plan: {relpath: (kind, data)}
# where kind is 'text' (LF str) or 'bytes'. install applies the plan then runs
# directory-scoped cleanup; check recomputes the plan and diffs it.


def _alias_targets(source: Source, comp: "Compilation") -> dict[str, tuple[str, bool]]:
    """Alias -> (target, enabled). Every alias is always emitted: a shim whose
    target is disabled must SAY so — a silently vanished alias makes the model
    reconstruct the v1 workflow from training data, the exact ungoverned ghost
    the aliases exist to prevent. (Alias-map integrity guarantees every target
    is a real core name.)"""
    return {a: (t, t in comp.cores) for a, t in source.aliases.items()}


def _disabled_alias_body(alias: str, target: str) -> str:
    """Shim body for an alias whose target core is disabled in the manifest."""
    return (f"The `{alias}` workflow was renamed to `{target}`, which is "
            f"disabled in this project's glados.yaml (`disabled-workflows:`). "
            f"Do not reconstruct it from memory — ask a maintainer to "
            f"re-enable it or pick a different workflow.\n")


# ---- claude -----------------------------------------------------------------

def adapter_claude(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    base = ".claude/commands/glados"
    for c in comp.enabled:
        plan[f"{base}/{c}.md"] = ("text", comp.cores[c])
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        body = (f"The `{alias}` workflow was renamed. Run `/glados:{target}` "
                f"instead.\n" if enabled else _disabled_alias_body(alias, target))
        plan[f"{base}/{alias}.md"] = ("text", body)
    return plan


# ---- claude-plugin (repo-side) ---------------------------------------------

def adapter_claude_plugin(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    for c in comp.enabled:
        plan[f"compiled/claude-plugin/{c}.md"] = ("text", comp.cores[c])
        desc = _yaml_quote(source.cores[c]["description"])
        plan[f"skills/{c}/SKILL.md"] = ("text",
            f"---\ndescription: {desc}\n---\n\n"
            f"Read and follow the compiled workflow at "
            f"`{PLUGIN_STUB_MARKER}{c}.md`.\n")
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        if enabled:
            desc = _yaml_quote(f"(renamed) the '{alias}' workflow is now '{target}'")
            body = (f"`{alias}` was renamed to `{target}`. Read and follow "
                    f"`{PLUGIN_STUB_MARKER}{target}.md`.\n")
        else:
            desc = _yaml_quote(f"(renamed) the '{alias}' workflow is now "
                               f"'{target}', which is disabled in this project")
            # Still a generated stub: carries the marker (as the not-installed
            # compiled path) so marker-based pruning can retire it later.
            body = (f"`{alias}` was renamed to `{target}`, which is disabled "
                    f"in this project's glados.yaml (`disabled-workflows:`) — "
                    f"`{PLUGIN_STUB_MARKER}{target}.md` is not installed. Do "
                    f"not reconstruct it from memory; ask a maintainer to "
                    f"re-enable it or pick a different workflow.\n")
        plan[f"skills/{alias}/SKILL.md"] = ("text",
            f"---\ndescription: {desc}\n---\n\n{body}")
    return plan


# ---- direct -----------------------------------------------------------------

def adapter_direct(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    for c in comp.enabled:
        plan[f"product-knowledge/glados/{c}.md"] = ("text", comp.cores[c])
    # Direct mode has no invocation syntax — shims point at the target FILE.
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        body = (f"The `{alias}` workflow was renamed. Read and follow "
                f"`product-knowledge/glados/{target}.md` instead.\n"
                if enabled else _disabled_alias_body(alias, target))
        plan[f"product-knowledge/glados/{alias}.md"] = ("text", body)
    return plan


# ---- gemini -----------------------------------------------------------------

def _toml_multiline(s: str) -> str:
    """Emit s as a TOML multi-line string, byte-preserving under tomllib.

    Preferred form is the literal '''…''' (no escaping to get wrong). When the
    content itself contains ''' — a Python triple-quote in a code example —
    fall back to a basic \"\"\"…\"\"\" string with backslash/quote escaping."""
    if "'''" not in s and s.endswith("\n"):
        return "'''\n" + s + "'''"
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return '"""\n' + esc + '"""'


def _toml_basic(s: str) -> str:
    """Single-line TOML basic string; newlines/tabs become escapes."""
    esc = (s.replace("\\", "\\\\").replace('"', '\\"')
           .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"' + esc + '"'


def _one_line(s: str) -> str:
    """Collapse newlines/whitespace runs: descriptions are menu one-liners."""
    return " ".join(s.split())


def _yaml_quote(s: str) -> str:
    """One-line, double-quoted YAML scalar for emitted frontmatter. Collapses
    newlines/runs of whitespace so a block-scalar description cannot break the
    frontmatter of a generated SKILL.md / workflow file."""
    s = _one_line(s)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def adapter_gemini(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    base = ".gemini/commands/glados"
    for c in comp.enabled:
        desc = _one_line(source.cores[c]["description"])
        toml = (f"description = {_toml_basic(desc)}\n"
                f"prompt = {_toml_multiline(comp.cores[c])}\n")
        plan[f"{base}/{c}.toml"] = ("text", toml)
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        if enabled:
            desc = f"(renamed) {alias} is now {target}"
            body = f"The `{alias}` workflow was renamed. Run `/glados:{target}` instead.\n"
        else:
            desc = f"(renamed) {alias} is now {target} — disabled here"
            body = _disabled_alias_body(alias, target)
        toml = (f"description = {_toml_basic(desc)}\n"
                f"prompt = {_toml_multiline(body)}\n")
        plan[f"{base}/{alias}.toml"] = ("text", toml)
    return plan


# ---- antigravity ------------------------------------------------------------

def _agy_description(text: str) -> str:
    """Antigravity caps workflow description frontmatter at 250 chars."""
    text = text.replace("\n", " ").strip()
    return text[:247] + "..." if len(text) > 250 else text


def adapter_antigravity(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    base = ".agents/workflows"
    for c in comp.enabled:
        desc = _yaml_quote(_agy_description(source.cores[c]["description"]))
        body = f"---\ndescription: {desc}\n---\n\n{comp.cores[c]}"
        plan[f"{base}/glados-{c}.md"] = ("text", body)
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        if enabled:
            desc = _yaml_quote(_agy_description(f"(renamed) {alias} is now {target}"))
            shim = f"The `{alias}` workflow was renamed. Run `/glados-{target}` instead.\n"
        else:
            desc = _yaml_quote(_agy_description(
                f"(renamed) {alias} is now {target} — disabled here"))
            shim = _disabled_alias_body(alias, target)
        body = f"---\ndescription: {desc}\n---\n\n{shim}"
        plan[f"{base}/glados-{alias}.md"] = ("text", body)
    return plan


# ---- aistudio ---------------------------------------------------------------

def _aistudio_preamble(title: str, advisory: bool) -> str:
    banner = ("\n> **Advisory mode.** This is an execution-heavy workflow; in "
              "AI Studio it plans and reviews only. Do not treat emitted commands "
              "as run — a human or the api-runner applies them.\n") if advisory else ""
    return (
        f"# GLaDOS v2 — {title} (AI Studio edition)\n"
        "\n"
        "## Runtime contract (read first)\n"
        "\n"
        "You are running in Google AI Studio. You have NO file, git, or "
        "GitLab/GitHub access; the human operator (or the api-runner script) is "
        "your hands. Emit exact artifacts; they apply them.\n"
        "\n"
        "- When a step writes a file, emit a fenced block headed "
        "`=== WRITE FILE: <repo-relative-path> ===` with the full contents.\n"
        "- When a step performs a git/platform action, emit the exact command "
        "in a fenced block headed `=== RUN: ===`.\n"
        "- glados.yaml could not be read at runtime; its values were inlined at "
        "compile time (see the compile stamp in the provenance header). If the "
        "pasted repo context contradicts them, warn the operator.\n"
        "- End EVERY response with the `## Before ending this run` epilogue "
        "checklist as a mandatory response suffix; if you cannot complete it, "
        "say so explicitly rather than omitting it.\n"
        f"{banner}"
        "\n"
        "---\n"
    )


AISTUDIO_README = """# GLaDOS on Google AI Studio — paste kit + API runner

AI Studio has no filesystem, no plugin/skill/command surface, and no lifecycle
hooks. It is a **planning / review / spec** surface. This kit ships the
advisory-safe workflows as self-contained paste bundles plus a Gemini
Interactions API runner for unattended read-mostly routines.

## Paste flow

1. Open a new prompt in AI Studio.
2. Paste the contents of `bundles/<workflow>.aistudio.md` into the **System
   Instructions** field.
3. Paste (or attach from Drive) the repo context the workflow needs — the
   relevant files, the issue text — as your first user message.
4. Run. The model emits `=== WRITE FILE: ... ===` and `=== RUN: ... ===`
   blocks; you (the operator) apply them to the repo and commit the run record.

## Saved-prompt setup

Save the prompt (System Instructions + run settings) to reuse the workflow. AI
Studio autosaves prompts to your Google Drive.

## Data-governance note

Pasting proprietary repo content into free-tier AI Studio may expose it to
product-improvement use. Confirm your tier's data policy before pasting private
code. The api-runner path (paid API key) is the governed alternative.

## Staleness rule

These bundles inline `glados.yaml` values at compile time. Re-run
`glados.py install --mode aistudio` whenever the manifest changes; `MANIFEST.md`
records the compile hash each bundle was built against.
"""

AISTUDIO_RUN_RECORD_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "GLaDOS run record (AI Studio structured-output form)",
    "type": "object",
    "required": ["workflow", "outcome", "run_record", "epilogue_completed"],
    "properties": {
        "workflow": {"type": "string"},
        "outcome": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "object"}},
        "emitted_outcomes": {"type": "array", "items": {"type": "string"}},
        "write_file_blocks": {"type": "array", "items": {"type": "object"}},
        "run_command_blocks": {"type": "array", "items": {"type": "string"}},
        "run_record": {
            "type": "string",
            "description": "Full markdown body for .glados/runs/<id>.md",
        },
        "epilogue_completed": {"type": "boolean"},
    },
    "additionalProperties": True,
}

AISTUDIO_RUNNER = '''#!/usr/bin/env python3
"""GLaDOS AI Studio api-runner — stdlib only.

Runs a compiled AI Studio bundle as the system instruction against the Gemini
Interactions API (GA June 2026), gathering context files from argv and writing
the model output beside the run ledger. Requires GEMINI_API_KEY in the env.

    python run_workflow.py bundles/plan-feature.aistudio.md context1.md context2.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
            "models/{model}:generateContent")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_workflow.py <bundle.md> [context-file ...]", file=sys.stderr)
        return 2
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is not set — export it (or use Vertex ADC) before "
              "running a scheduled GLaDOS workflow on the Gemini API.", file=sys.stderr)
        return 1

    bundle = Path(sys.argv[1]).read_text(encoding="utf-8")
    context = ""
    for ctx in sys.argv[2:]:
        context += f"\\n\\n=== FILE: {ctx} ===\\n" + Path(ctx).read_text(encoding="utf-8")

    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    payload = {
        "system_instruction": {"parts": [{"text": bundle}]},
        "contents": [{"role": "user", "parts": [{"text": context or "(no context)"}]}],
    }
    url = ENDPOINT.format(model=model) + f"?key={api_key}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:  # pragma: no cover - network
        print(f"Gemini API request failed: {exc}", file=sys.stderr)
        return 1

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        print(json.dumps(data, indent=2))
        return 1

    runs = Path(".glados/runs")
    runs.mkdir(parents=True, exist_ok=True)
    out = runs / "aistudio-latest.md"
    out.write_text(text, encoding="utf-8", newline="\\n")
    print(text)
    print(f"\\n[written to {out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

AISTUDIO_SCHEDULE = """# Scheduling GLaDOS on the Gemini API

"Scheduled GLaDOS on AI Studio" means "scheduled GLaDOS on the Gemini API": the
runner gathers repo context, calls the model, and applies the output itself.

## cron (Linux/macOS)

    0 7 * * 1  cd /path/to/repo && GEMINI_API_KEY=... \\
        python glados/adapters/aistudio/api-runner/run_workflow.py \\
        glados/adapters/aistudio/bundles/retrospect.aistudio.md CHANGELOG.md

## Windows Task Scheduler

    schtasks /Create /SC WEEKLY /D MON /TR "python run_workflow.py ..." /ST 07:00

## GitLab CI (scheduled pipeline)

    glados-retrospect:
      rule: if: $CI_PIPELINE_SOURCE == "schedule"
      script:
        - python glados/adapters/aistudio/api-runner/run_workflow.py \\
            glados/adapters/aistudio/bundles/retrospect.aistudio.md
"""


def adapter_aistudio(source: Source, comp: Compilation) -> dict:
    plan: dict = {}
    base = "glados/adapters/aistudio"
    bundle_rows: list[tuple[str, str]] = []

    def emit_bundle(bundle_name: str, core_name: str, title: str, advisory: bool):
        if core_name not in comp.cores:
            return
        text = _aistudio_preamble(title, advisory) + "\n" + comp.cores[core_name]
        rel = f"{base}/bundles/{bundle_name}.aistudio.md"
        plan[rel] = ("text", text)
        bundle_rows.append((bundle_name, f"bundles/{bundle_name}.aistudio.md"))

    for c in AISTUDIO_CORES:
        emit_bundle(c, c, source.cores[c]["description"], advisory=False)
    emit_bundle("fix-bug-advisory", "fix-bug",
                "Fix Bug (plan/triage only)", advisory=True)

    # Alias shims are pointer stubs, not bundles (they never join MANIFEST.md).
    # AI Studio has no invocation syntax, so bodies point at bundle FILES.
    for alias, (target, enabled) in _alias_targets(source, comp).items():
        rel = f"{base}/bundles/{alias}.aistudio.md"
        if not enabled:
            plan[rel] = ("text", _disabled_alias_body(alias, target))
            continue
        if target in AISTUDIO_CORES:
            bundle_target = target
        elif target == "fix-bug":
            bundle_target = "fix-bug-advisory"
        else:
            bundle_target = None
        if bundle_target:
            body = (f"The `{alias}` workflow was renamed. Paste "
                    f"`bundles/{bundle_target}.aistudio.md` instead.\n")
        else:
            body = (f"The `{alias}` workflow was renamed to `{target}`, which "
                    f"is execution-heavy and ships no AI Studio bundle — run "
                    f"it from a full install mode (claude / gemini / direct) "
                    f"instead.\n")
        plan[rel] = ("text", body)

    plan[f"{base}/README.md"] = ("text", AISTUDIO_README)

    manifest_md = ["# GLaDOS AI Studio bundles", "",
                   f"Compile hash: `{comp.manifest_hash}`", "",
                   "| Workflow | Bundle | Compile hash |",
                   "|----------|--------|--------------|"]
    for name, rel in bundle_rows:
        manifest_md.append(f"| {name} | {rel} | `{comp.manifest_hash}` |")
    manifest_md.append("")
    plan[f"{base}/MANIFEST.md"] = ("text", "\n".join(manifest_md))

    plan[f"{base}/schemas/run_record.schema.json"] = (
        "text", json.dumps(AISTUDIO_RUN_RECORD_SCHEMA, indent=2) + "\n")
    plan[f"{base}/api-runner/run_workflow.py"] = ("text", AISTUDIO_RUNNER)
    plan[f"{base}/api-runner/schedule.example.md"] = ("text", AISTUDIO_SCHEDULE)
    return plan


ADAPTERS = {
    "claude": adapter_claude,
    "claude-plugin": adapter_claude_plugin,
    "direct": adapter_direct,
    "gemini": adapter_gemini,
    "antigravity": adapter_antigravity,
    "aistudio": adapter_aistudio,
}


# Directory prefixes each mode fully owns (for stale-file cleanup). A file under
# one of these prefixes that the current plan does not emit is a stale leftover.
OWNED_DIRS = {
    "claude": [".claude/commands/glados"],
    "claude-plugin": ["compiled/claude-plugin"],
    "direct": ["product-knowledge/glados"],
    "gemini": [".gemini/commands/glados"],
    "antigravity": [".agents/workflows"],   # scoped to glados-*.md, see cleanup
    "aistudio": ["glados/adapters/aistudio/bundles"],
}

# Vendored subtrees EVERY mode owns. Cleaned like OWNED_DIRS so a renamed
# source/persona/hook does not linger and skew the self-contained vendored
# checker. Deliberately excludes .glados/runs/ (run records are user history)
# and the .glados/ root files (always re-planned).
VENDOR_OWNED_DIRS = [".glados/src", ".glados/ci", ".glados/hooks",
                     ".glados/personas"]


# =============================================================================
# 9. VENDOR + SCAFFOLD
# =============================================================================


VENDORED_HOOKS = ["claude-stop-hook.py", "gemini-afteragent-guard.py",
                  "agy-hooks.json"]

# The two CI backstop templates, vendored to .glados/ci/ by every install mode
# (decision 9c: the backstop exists everywhere; platform: only selects which
# enable stanza the installer prints).
CI_TEMPLATES = ["glados-check.gitlab-ci.yml", "glados-check.github-actions.yml"]


def vendor_plan(source: Source, manifest_hash: str, report: str) -> dict:
    """Files vendored into <target>/.glados/, identical for every mode. The
    self-copy, presets and source tree must byte-match their sources; vendoring
    src/ makes the vendored checker self-contained (`.glados/glados.py check`
    needs no --source — _resolve_source finds `.glados/src/`)."""
    plan: dict = {}
    self_path = source.root / "bin" / "glados.py"
    if not self_path.exists():
        # A vendored .glados/ source tree carries the compiler at its root.
        self_path = source.root / "glados.py"
    if not self_path.exists():
        raise Fatal(f"cannot find glados.py under {source.root} (tried bin/ "
                    f"and the root) — the source tree is incomplete; pass "
                    f"--source <a full glados checkout>")
    presets_bytes = (source.kernel / "presets" / "phases.yaml").read_bytes()
    plan[".glados/glados.py"] = ("bytes", self_path.read_bytes())
    plan[".glados/presets.yaml"] = ("bytes", presets_bytes)
    plan[".glados/manifest-hash"] = ("text", manifest_hash + "\n")
    plan[".glados/assembly-report.md"] = ("text", report)
    # Vendor the full source tree so the CI backstop can recompute the compile
    # from the repo alone (self-contained check), plus the manifest example the
    # anti-inflation cap reads.
    for f in sorted(source.src.rglob("*")):
        if f.is_file():
            rel = f.relative_to(source.src).as_posix()
            plan[f".glados/src/{rel}"] = ("bytes", f.read_bytes())
    example = source.root / "glados.yaml.example"
    if example.exists():
        plan[".glados/glados.yaml.example"] = ("bytes", example.read_bytes())
    # Vendor the CI templates so MIGRATION step 5's "add the include" has a
    # real, repo-local file to include/copy.
    for name in CI_TEMPLATES:
        src = source.root / "ci" / name
        if src.exists():
            plan[f".glados/ci/{name}"] = ("bytes", src.read_bytes())
    # Vendor the SDA standard/profile docs. scaffold_sda copies these into a
    # consuming project, and without them `install` on an `sda: true` repo
    # dies with "pass --source <a full glados checkout>" — which made the
    # vendored tree self-contained for `check` but not for `install`, so the
    # one command a consumer needs to pick up a re-vendor could not be run
    # from the repo it was vendored into.
    sda_docs = source.root / "docs" / "standards"
    if sda_docs.is_dir():
        for f in sorted(sda_docs.glob("sda-*.md")):
            plan[f".glados/docs/standards/{f.name}"] = ("bytes", f.read_bytes())
    # Vendor the run-record guard scripts (and the agy hooks block of record)
    # so emitted hook references resolve.
    for name in VENDORED_HOOKS:
        src = source.root / "hooks" / name
        if src.exists():
            plan[f".glados/hooks/{name}"] = ("bytes", src.read_bytes())
    # Vendor the library persona definitions. Panels resolve a named persona
    # from the project's product-knowledge/personas/ first, then fall back to
    # this vendored library — without it, no install mode ships a persona.
    personas = source.src / "personas"
    if personas.is_dir():
        for f in sorted(personas.glob("*.md")):
            plan[f".glados/personas/{f.name}"] = ("bytes", f.read_bytes())
    return plan


def scaffold_product_knowledge(target: Path, source: Source) -> list[str]:
    """Create-only product-knowledge skeleton; NEVER deletes user content."""
    created: list[str] = []
    pk = target / "product-knowledge"
    for sub in ("observations", "standards", "philosophies", "personas"):
        d = pk / sub
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_text("", encoding="utf-8", newline="\n")
            created.append(str(keep.relative_to(target)))
    status = pk / "PROJECT_STATUS.md"
    tmpl = source.src / "templates" / "PROJECT_STATUS.md"
    if not status.exists() and tmpl.exists():
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(read_text(tmpl), encoding="utf-8", newline="\n")
        created.append(str(status.relative_to(target)))
    return created


def _prepend_sda_header(path: Path) -> bool:
    """Idempotently prepend the `SDA: v1.0` version header; True if written.

    read_text validates the encoding first (UTF-16 dies with the named fix
    instead of mojibake); the write is a byte-level prepend so the user's own
    bytes — CRLF endings included — survive untouched after the header."""
    if SDA_MARKER in read_text(path):
        return False
    raw = path.read_bytes().removeprefix(b"\xef\xbb\xbf")
    path.write_bytes(SDA_HEADER.encode("utf-8") + raw)
    return True


def _require_sda_template(source: Source, tmpl: Path) -> Path:
    if not tmpl.exists():
        raise Fatal(f"sda: true but the source tree is missing {tmpl} — pass "
                    f"--source <a full glados checkout> and re-run install")
    return tmpl


def _scaffold_spec_log(target: Path, source: Source, today: str) -> bool:
    """Create product-knowledge/SPEC_LOG.md from the template, dated today
    (the scaffold_sda date-fill precedent). Create-only; True when written."""
    spec_log = target / "product-knowledge" / "SPEC_LOG.md"
    if spec_log.exists():
        return False
    tmpl = _require_sda_template(source, source.src / "templates" / "SPEC_LOG.md")
    spec_log.parent.mkdir(parents=True, exist_ok=True)
    spec_log.write_text(read_text(tmpl).replace("YYYY-MM-DD", today),
                        encoding="utf-8", newline="\n")
    return True


def scaffold_sda(target: Path, source: Source) -> list[str]:
    """Create-only SDA conformance artifacts, run when the manifest sets
    `sda: true`. Never clobbers: existing files win, and the version-header
    prepend is marker-checked (idempotent). Returns repo-relative paths of
    what THIS run created/stamped — the vendored assembly report lists the
    artifact set instead, so re-installs stay byte-identical."""
    created: list[str] = []
    today = datetime.date.today().isoformat()

    def rel(p: Path) -> str:
        return p.relative_to(target).as_posix()

    # 1. claims.md at the repo root, from the template, dated today.
    claims = target / "claims.md"
    if not claims.exists():
        tmpl = _require_sda_template(source, source.src / "templates" / "CLAIMS.md")
        claims.write_text(read_text(tmpl).replace("YYYY-MM-DD", today),
                          encoding="utf-8", newline="\n")
        created.append(rel(claims))

    # 2. product-knowledge/SPEC_LOG.md — the SDA work-unit log the compiled
    #    epilogue appends to (per the standard's work-unit-log format).
    if _scaffold_spec_log(target, source, today):
        created.append(rel(target / "product-knowledge" / "SPEC_LOG.md"))

    # 3. Version headers on the roadmap/status docs IF they exist and lack
    #    one. Never creates the docs themselves — a missing roadmap is the
    #    team's call, not the installer's.
    for name in ("ROADMAP.md", "PROJECT_STATUS.md"):
        doc = target / "product-knowledge" / name
        if doc.is_file() and _prepend_sda_header(doc):
            created.append(rel(doc) + " (SDA header)")

    # 4. The versioned standard + profile docs, copied byte-identically
    #    (install_file fidelity) into the repo's own standards tree.
    docs = sorted((source.root / "docs" / "standards").glob("sda-*.md"))
    if not docs:
        raise Fatal(f"sda: true but no docs/standards/sda-*.md under "
                    f"{source.root} — pass --source <a full glados checkout> "
                    f"and re-run install")
    for doc in docs:
        dest = target / "product-knowledge" / "standards" / doc.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(doc.read_bytes())
            created.append(rel(dest))
    return created


# =============================================================================
# 10. COMMANDS
# =============================================================================


def _write_one(path: Path, kind: str, data) -> None:
    if kind == "bytes":
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8", newline="\n")


def _write_plan(target: Path, plan: dict) -> None:
    for rel, (kind, data) in plan.items():
        path = target / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_one(path, kind, data)
        except PermissionError:
            # Windows: an overwrite of a read-only leftover raises. The file is
            # inside a glados-owned dir, so clearing the attribute is in
            # contract; only a second failure is fatal.
            try:
                path.chmod(0o644)
                _write_one(path, kind, data)
            except OSError as exc:
                raise Fatal(f"cannot write {path}: {exc} — clear the read-only "
                            f"attribute (attrib -R) or delete the file, then "
                            f"re-run install")
        except OSError as exc:
            raise Fatal(f"cannot write {path}: {exc} — a conflicting "
                        f"file/directory is in the way; move it aside and "
                        f"re-run install")


def _force_unlink(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        try:
            path.chmod(0o644)
            path.unlink()
        except OSError as exc:
            raise Fatal(f"cannot remove stale file {path}: {exc} — clear the "
                        f"read-only attribute (attrib -R) or delete it by "
                        f"hand, then re-run install")


def _remove_tree_files(root: Path, target: Path) -> list[str]:
    """Remove every file under root and prune the emptied directories (root
    included). Returns target-relative paths of the removed files. Only ever
    called on trees GLaDOS owns outright (v1 layouts, converted specs/ dirs)."""
    removed: list[str] = []
    entries = sorted(root.rglob("*"), reverse=True)
    for f in entries:
        if f.is_file():
            _force_unlink(f)
            removed.append(str(f.relative_to(target)))
    for d in entries + [root]:
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    return removed


def _cleanup_owned(target: Path, mode: str, plan: dict, source: Source,
                   comp: Compilation) -> list[str]:
    """Directory-scoped stale-file removal, porting the bash cleanup semantics."""
    removed: list[str] = []
    planned = {str(Path(rel)) for rel in plan}

    for owned in OWNED_DIRS.get(mode, []) + VENDOR_OWNED_DIRS:
        d = target / owned
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(target))
            if mode == "antigravity" and owned == ".agents/workflows" \
                    and not f.name.startswith("glados-"):
                continue  # only glados-*.md are ours under .agents/workflows
            if rel not in planned:
                _force_unlink(f)
                removed.append(rel)

    # claude: migrate old top-level (un-namespaced) command files, porting
    # cleanup_old_toplevel — remove name-matching files from the parent dir.
    if mode == "claude":
        parent = target / ".claude" / "commands"
        expected = [f"{c}.md" for c in comp.enabled] + \
                   [f"{a}.md" for a in source.aliases]
        for name in expected:
            for variant in (name, name.replace("-", "_")):
                stale = parent / variant
                if stale.is_file():
                    _force_unlink(stale)
                    removed.append(str(stale.relative_to(target)))
    return removed


def _v1_stem_match(stem: str, names: frozenset) -> bool:
    """A file stem names a known v1 unit, in dash or underscore spelling."""
    return stem in names or stem.replace("_", "-") in names


def _cleanup_v1_legacy(target: Path, mode: str) -> list[str]:
    """Per-mode removal of v1-era layouts the current mode supersedes.

    - gemini: v1's install_gemini fully owned `.gemini/skills/glados/`; the
      whole tree goes.
    - direct: v1 shared `product-knowledge/{workflows,modules}/` with user
      content, so only files whose basenames (or underscore variants) match
      the known v1 workflow/module name sets are removed — never unmatched
      user files. The dirs are removed only if that leaves them empty.
    """
    removed: list[str] = []
    if mode == "gemini":
        root = target / ".gemini" / "skills" / "glados"
        if root.is_dir():
            removed.extend(_remove_tree_files(root, target))
    if mode == "direct":
        for sub, names in (("workflows", KNOWN_V1_WORKFLOWS),
                           ("modules", KNOWN_V1_MODULES)):
            d = target / "product-knowledge" / sub
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                if _v1_stem_match(f.stem, names):
                    _force_unlink(f)
                    removed.append(str(f.relative_to(target)))
            try:
                d.rmdir()  # only succeeds when nothing user-owned remains
            except OSError:
                pass
    return removed


def _resolve_source(args) -> Source:
    if getattr(args, "source", None):
        return Source(Path(args.source).resolve())
    here = Path(__file__).resolve().parent
    # Disambiguate on the GLaDOS source *shape* (src/kernel/), not the mere
    # presence of a src/ dir: a target repo with its own top-level src/ would
    # otherwise shadow the vendored .glados/src/. For bin/glados.py the repo
    # root (here.parent) has src/kernel/ and wins; for a vendored
    # .glados/glados.py, here.parent is the consuming repo (whose unrelated
    # src/ has no kernel/) so it falls through to here (.glados/), which does.
    for cand in (here.parent, here):
        if (cand / "src" / "kernel").is_dir():
            return Source(cand)
    raise Fatal("could not locate the GLaDOS source tree — pass --source <repo>")


def _prepare_compilation(source: Source, target: Path, mode: str):
    """Load the manifest for this mode, resolve it, compile, run checks."""
    if mode == "claude-plugin":
        manifest_path = target / "glados.yaml.example"
        if not manifest_path.exists():
            manifest_path = source.root / "glados.yaml.example"
    else:
        manifest_path = target / "glados.yaml"
    raw = load_manifest(manifest_path)
    resolved = resolve_manifest(raw, source.presets, manifest_path.name)
    if mode == "claude-plugin":
        # Never bake the example manifest's phase-resolved optimize-for into
        # artifacts every plugin consumer runs: stamp the phase-neutral
        # baseline sentence, and prepend the bootstrap guard to every core so
        # the consuming repo's own glados.yaml governs at run time.
        resolved.values["optimize-for"] = (
            source.presets.get("baseline") or {}).get("optimize-for")
        resolved.provenance["optimize-for"] = "baseline (claude-plugin is phase-neutral)"
    manifest_hash = manifest_hash_of(manifest_path)
    comp = compile_all(source, resolved, manifest_hash)
    if mode == "claude-plugin":
        for name in list(comp.cores):
            comp.cores[name] = PLUGIN_BOOTSTRAP_GUARD + comp.cores[name]
    errors = run_type_checks(source, comp)
    if errors:
        raise Fatal("install checks failed:\n  - " + "\n  - ".join(errors))
    return comp, manifest_hash


def cmd_install(args) -> int:
    source = _resolve_source(args)
    target = Path(args.target).resolve()
    mode = args.mode
    if mode not in ADAPTERS:
        raise Fatal(f"unknown mode '{mode}' — one of: {', '.join(INSTALL_MODES)}")
    if not target.is_dir():
        raise Fatal(f"target directory does not exist: {target}")

    # A target full of v1 leftovers but no manifest is a migration half-start:
    # hint at the prefilled path before load_manifest raises its Fatal.
    if mode != "claude-plugin" and not (target / "glados.yaml").exists() \
            and _has_v1_leftovers(target):
        print("glados: v1 GLaDOS files detected but no glados.yaml — this "
              "looks like a v1 -> v2 migration; run `glados.py migrate "
              "--target <repo>` to generate the manifest and convert specs/ "
              "history, then set phase: and re-run install (MIGRATION.md "
              "documents the details)", file=sys.stderr)

    comp, manifest_hash = _prepare_compilation(source, target, mode)
    report = build_assembly_report(source, comp)

    # Read the previously vendored report BEFORE the plan overwrites it, so a
    # phase change can print its advisory transition checklist.
    prev_report = None
    prev_report_path = target / ".glados" / "assembly-report.md"
    if prev_report_path.exists():
        try:
            prev_report = read_text(prev_report_path)
        except Fatal:
            prev_report = None

    plan = ADAPTERS[mode](source, comp)
    plan.update(vendor_plan(source, manifest_hash, report))
    _write_plan(target, plan)
    removed = _cleanup_owned(target, mode, plan, source, comp)
    removed += _cleanup_v1_legacy(target, mode)

    scaffolded: list[str] = []
    sda_scaffolded: list[str] = []
    sda_on = bool(comp.resolved.values.get("sda")) and mode != "claude-plugin"
    if mode not in ("claude-plugin",):
        scaffolded = scaffold_product_knowledge(target, source)
        if sda_on:
            sda_scaffolded = scaffold_sda(target, source)

    if mode == "claude-plugin":
        _regenerate_plugin_skills(target, source, comp)

    if mode == "antigravity":
        _emit_agy_hooks(target, source)

    print(f"glados: installed mode '{mode}' into {target}")
    print(f"glados: {len(comp.enabled)} cores, manifest {manifest_hash[:12]}…")
    if removed:
        print(f"glados: cleaned {len(removed)} stale file(s): {', '.join(removed)}")
    if scaffolded:
        print(f"glados: scaffolded {len(scaffolded)} product-knowledge file(s)")
    if sda_scaffolded:
        print(f"glados: sda: true — scaffolded {', '.join(sda_scaffolded)}")
    elif sda_on:
        print("glados: sda: true — all conformance artifacts already present")
    advisory = _phase_transition_advisory(prev_report, comp.resolved.phase)
    if advisory:
        print(advisory)
    try:
        ci_wired = _ci_check_wired(target)
    except Fatal:
        ci_wired = False
    if not ci_wired:
        print(_ci_enable_stanza(comp.resolved.values.get("platform")))
    print()
    print(report)
    print(f"glados: assembly report written to .glados/assembly-report.md")
    return 0


def _has_v1_leftovers(target: Path) -> bool:
    """Cheap detection of a v1 GLaDOS install in the target tree."""
    if (target / ".gemini" / "skills" / "glados").is_dir():
        return True
    for sub, names in (("workflows", KNOWN_V1_WORKFLOWS),
                       ("modules", KNOWN_V1_MODULES)):
        d = target / "product-knowledge" / sub
        if d.is_dir():
            for f in d.glob("*.md"):
                if _v1_stem_match(f.stem, names):
                    return True
    cmds = target / ".claude" / "commands"
    if cmds.is_dir():
        for name in KNOWN_V1_WORKFLOWS:
            if (cmds / f"{name}.md").is_file() or \
                    (cmds / f"{name.replace('-', '_')}.md").is_file():
                return True
    return False


def _phase_transition_advisory(prev_report, new_phase: str):
    """Advisory-only checklist when the phase differs from the previously
    vendored assembly report. Never blocks; returns None when not a transition."""
    if not prev_report:
        return None
    m = re.search(r"^- phase: `([a-z]+)`", prev_report, re.M)
    if not m:
        return None
    old = m.group(1)
    if old == new_phase or old not in PHASES or new_phase not in PHASES:
        return None
    lines = [f"glados: phase transition {old} -> {new_phase} — advisory "
             f"checklist (nothing here blocks the install):"]
    for item in PHASE_TRANSITION_CHECKLISTS.get(new_phase, []):
        lines.append(f"  [ ] {item}")
    if PHASES.index(new_phase) < PHASES.index(old):
        lines.append("  [ ] backward move — say why in the MR that changes "
                     "glados.yaml")
    return "\n".join(lines)


def _ci_enable_stanza(platform) -> str:
    """The one-paste instruction wiring the vendored CI backstop, selected by
    the manifest's platform: key (both templates are always vendored)."""
    gitlab = ("glados: CI backstop vendored to .glados/ci/ — enable it in "
              ".gitlab-ci.yml:\n"
              "    include:\n"
              "      - local: '.glados/ci/glados-check.gitlab-ci.yml'")
    github = ("glados: CI backstop vendored to .glados/ci/ — enable it with:\n"
              "    cp .glados/ci/glados-check.github-actions.yml "
              ".github/workflows/glados-check.yml")
    if platform == "github":
        return github
    if platform == "gitlab":
        return gitlab
    return gitlab + "\n" + github


def _regenerate_plugin_skills(target: Path, source: Source, comp: Compilation) -> None:
    """Prune stale skills/ dirs (keeping init), leave user skills untouched.

    Pruning is marker-based, not name-based: a dir is ours to remove only when
    its SKILL.md contains the generated-stub marker every stub emitted by
    adapter_claude_plugin carries. User skills lack the marker, so they are
    structurally untouchable — no name list to maintain, and a retired core or
    alias is pruned automatically."""
    skills = target / "skills"
    if not skills.is_dir():
        return
    keep = set(comp.enabled) | set(source.aliases) | {"init"}
    for d in sorted(skills.iterdir()):
        if not d.is_dir() or d.name in keep:
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.is_file():
            continue  # not a stub layout — leave it alone
        try:
            text = read_text(skill_md)
        except Fatal:
            continue  # unreadable = not provably ours — leave it alone
        if PLUGIN_STUB_MARKER not in text:
            continue  # user-authored skill — never touch
        for f in sorted(d.rglob("*"), reverse=True):
            if f.is_file():
                _force_unlink(f)
        try:
            d.rmdir()
        except OSError:
            pass


def _read_agy_hooks_block(source: Source) -> str:
    """hooks/agy-hooks.json is the single file of record for the antigravity
    hooks block (vendored to .glados/hooks/ with the guard scripts). Missing
    or invalid is a broken source tree, never a silent skip."""
    path = source.root / "hooks" / "agy-hooks.json"
    if not path.exists():
        raise Fatal(f"missing {path} — hooks/agy-hooks.json is the file of "
                    f"record for the antigravity hooks block; restore it")
    try:
        data = json.loads(read_text(path))
    except ValueError as exc:
        raise Fatal(f"{path}: invalid JSON ({exc}) — fix the file of record")
    return json.dumps(data, indent=2) + "\n"


def _emit_agy_hooks(target: Path, source: Source) -> None:
    """Write .agents/hooks.json ONLY if absent; never clobber a user's file."""
    hooks = target / ".agents" / "hooks.json"
    block = _read_agy_hooks_block(source)
    if hooks.exists():
        print("glados: .agents/hooks.json exists — add this glados block manually:\n"
              + block)
    else:
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text(block, encoding="utf-8", newline="\n")


def _detect_modes(target: Path) -> list[str]:
    modes: list[str] = []
    if (target / ".claude/commands/glados").is_dir():
        modes.append("claude")
    if (target / "product-knowledge/glados").is_dir():
        modes.append("direct")
    if (target / ".gemini/commands/glados").is_dir():
        modes.append("gemini")
    if (target / ".agents/workflows").is_dir() and \
            any((target / ".agents/workflows").glob("glados-*.md")):
        modes.append("antigravity")
    if (target / "glados/adapters/aistudio").is_dir():
        modes.append("aistudio")
    if (target / "compiled/claude-plugin").is_dir():
        modes.append("claude-plugin")
    return modes


def cmd_check(args) -> int:
    target = Path(args.target).resolve()
    report_only = args.report_only
    problems: list[str] = []

    # An unreadable/unresolvable source must not hard-exit a --report-only run:
    # it becomes a reported problem, and enforcing mode still fails cleanly.
    source = None
    try:
        source = _resolve_source(args)
    except Fatal as exc:
        problems.append(f"cannot resolve the GLaDOS source tree: {exc}")

    modes = _detect_modes(target)
    if not modes:
        # Deliberately fatal even under --report-only. Every install mode this
        # detects lands in a directory a project may gitignore, so in CI, on a
        # fresh clone, "no install detected" is the normal shape of a repo
        # whose compiled artifacts never travel — and returning 0 for it turns
        # the drift check, the manifest-hash check, and every type check into
        # a job that passes without reading anything. A checker that cannot
        # see the thing it checks reports that, loudly, in both modes.
        problems.append(
            f"no GLaDOS install detected under {target} — nothing was "
            f"checked. Install modes live in directories that are commonly "
            f"gitignored (.claude/, .gemini/); commit one install mode so "
            f"this job has an artifact to verify, or run check where the "
            f"install actually is")
        _emit_check(problems, report_only)
        return 1

    # manifest hash: recompute vs vendored
    try:
        hash_path = target / ".glados" / "manifest-hash"
        manifest_path = target / "glados.yaml"
        if not manifest_path.exists() and modes == ["claude-plugin"]:
            manifest_path = target / "glados.yaml.example"
        if hash_path.exists() and manifest_path.exists():
            vendored = read_text(hash_path).strip()
            current = manifest_hash_of(manifest_path)
            if vendored != current:
                problems.append(f"stale compile: manifest-hash {vendored[:12]}… != "
                                f"current {current[:12]}… — re-run glados install")
    except Fatal as exc:
        problems.append(f"cannot compare manifest hash: {exc}")

    for mode in (modes if source is not None else []):
        try:
            comp, manifest_hash = _prepare_compilation(source, target, mode)
        except Fatal as exc:
            problems.append(f"[{mode}] {exc}")
            continue
        report = build_assembly_report(source, comp)
        plan = ADAPTERS[mode](source, comp)
        plan.update(vendor_plan(source, manifest_hash, report))
        for rel, (kind, data) in plan.items():
            path = target / rel
            if not path.exists():
                problems.append(f"[{mode}] missing installed file: {rel}")
                continue
            if kind == "bytes":
                drift = path.read_bytes() != data
            else:
                drift = read_text(path) != data
            if drift:
                problems.append(f"[{mode}] drift in installed file: {rel}")

    _emit_check(problems, report_only)
    if report_only:
        return 0
    return 1 if problems else 0


def _emit_check(problems: list[str], report_only: bool) -> None:
    if not problems:
        print("glados check: OK — no drift, checks pass, manifest hash current")
        return
    header = "glados check: report-only" if report_only else "glados check: FAIL"
    print(header)
    for p in problems:
        print(f"  - {p}")


def cmd_doctor(args) -> int:
    source = _resolve_source(args)
    target = Path(args.target).resolve()
    print(f"glados doctor — {target}")

    modes = _detect_modes(target)
    print(f"  installed modes: {', '.join(modes) if modes else '(none)'}")

    hash_path = target / ".glados" / "manifest-hash"
    manifest_path = target / "glados.yaml"
    if not manifest_path.exists():
        manifest_path = target / "glados.yaml.example"
    try:
        if hash_path.exists() and manifest_path.exists():
            vendored = read_text(hash_path).strip()
            current = manifest_hash_of(manifest_path)
            state = "current" if vendored == current else "STALE — re-run install"
            print(f"  manifest hash: {state} ({current[:12]}…)")
        else:
            print("  manifest hash: not vendored (no .glados/manifest-hash)")
    except Fatal as exc:
        print(f"  manifest hash: UNREADABLE — {exc}")

    try:
        if _ci_check_wired(target):
            print("  CI check wired: yes")
        else:
            print("  CI check wired: no — enable the vendored template under "
                  ".glados/ci/ (see the install output)")
    except Fatal as exc:
        print(f"  CI check wired: UNREADABLE — {exc}")

    hooks = []
    if (target / ".claude" / "settings.json").exists():
        try:
            data = json.loads(read_text(target / ".claude" / "settings.json"))
            if "hooks" in data:
                hooks.append("claude")
        except (ValueError, OSError, Fatal):
            pass
    if (target / ".agents" / "hooks.json").exists():
        hooks.append("antigravity")
    if (target / ".gemini" / "settings.json").exists():
        hooks.append("gemini")
    print(f"  run-record hooks: {', '.join(hooks) if hooks else '(none installed)'}")

    # CODEOWNERS on glados.yaml — informational: agents propose phase changes,
    # humans approve them; a covered manifest makes that structural.
    found = None
    for rel in ("CODEOWNERS", ".github/CODEOWNERS", ".gitlab/CODEOWNERS",
                "docs/CODEOWNERS"):
        if (target / rel).is_file():
            found = rel
            break
    if found is None:
        print("  CODEOWNERS: none found — consider one covering glados.yaml "
              "(agents propose phase changes; humans approve)")
    else:
        try:
            mentions = "glados.yaml" in read_text(target / found)
        except Fatal:
            mentions = False
        print(f"  CODEOWNERS: {found} "
              + ("covers glados.yaml" if mentions else
                 "does not mention glados.yaml — consider protecting phase "
                 "changes"))

    if manifest_path.exists():
        try:
            raw = load_manifest(manifest_path)
        except Fatal as exc:
            print(f"  phase: UNREADABLE — {exc}")
        else:
            declared = raw.get("phase-declared")
            print(f"  phase: {raw.get('phase')} "
                  f"(declared: {declared or 'undated — add phase-declared:'})")
            for key in ("visibility-acknowledged", "relaxation-acknowledged"):
                if key in raw:
                    print(f"  confession restated: {key}: {_fmt(raw[key])}")
            for line in _sda_doctor_lines(target, raw):
                print(line)

    print("glados doctor: informational only — never fails")
    return 0


def _sda_doctor_lines(target: Path, raw: dict) -> list[str]:
    """Informational sda status: the declared value plus whether each
    scaffolded conformance artifact exists. Never raises."""
    sda = raw.get("sda", False)
    if not isinstance(sda, bool):
        return [f"  sda: INVALID — {sda!r} is not a bool; write 'sda: true' "
                f"or 'sda: false'"]
    if not sda:
        return ["  sda: false (SDA conformance not declared)"]
    lines = ["  sda: true (explicit) — scaffolded artifacts:"]
    lines.append("    - claims.md: "
                 + ("present" if (target / "claims.md").is_file()
                    else "missing — re-run install"))
    spec_log = target / "product-knowledge" / "SPEC_LOG.md"
    lines.append("    - product-knowledge/SPEC_LOG.md: "
                 + ("present" if spec_log.is_file()
                    else "missing — re-run install"))
    for name in ("ROADMAP.md", "PROJECT_STATUS.md"):
        doc = target / "product-knowledge" / name
        if not doc.is_file():
            lines.append(f"    - product-knowledge/{name}: absent (the header "
                         f"is added only when the file exists)")
            continue
        try:
            has = SDA_MARKER in read_text(doc)
        except Fatal:
            has = False
        lines.append(f"    - product-knowledge/{name}: SDA header "
                     + ("present" if has else "missing — re-run install"))
    std_dir = target / "product-knowledge" / "standards"
    n_std = len(list(std_dir.glob("sda-*.md"))) if std_dir.is_dir() else 0
    lines.append(f"    - product-knowledge/standards/sda-*.md: "
                 + (f"{n_std} doc(s) present" if n_std
                    else "missing — re-run install"))
    return lines


def _ci_check_wired(target: Path) -> bool:
    gitlab = target / ".gitlab-ci.yml"
    if gitlab.exists() and "glados" in read_text(gitlab):
        return True
    wf_dir = target / ".github" / "workflows"
    if wf_dir.is_dir():
        for f in wf_dir.glob("*.yml"):
            if "glados.py check" in read_text(f):
                return True
    return False


def cmd_verify_ledger(args) -> int:
    target = Path(args.target).resolve()
    print(f"glados verify-ledger (report-only) — {target}")

    def git(*a):
        try:
            out = subprocess.run(["git", "-C", str(target), *a],
                                 capture_output=True, text=True, timeout=30)
            return out.stdout if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    log = git("log", "--all", "--format=%H%x09%s", "-n", "200")
    if not log.strip():
        print("  no git history reachable (not a repo, or empty) — nothing to scan")
        return 0

    record_commits = 0
    verdict_commits: list[str] = []
    for line in log.strip().split("\n"):
        sha, _, subject = line.partition("\t")
        if subject.startswith("chore(glados): record"):
            record_commits += 1
        low = subject.lower()
        if any(w in low for w in ("verdict", "review", "escalat")):
            verdict_commits.append(f"{sha[:10]} {subject}")

    branches = git("branch", "--format=%(refname:short)").strip().split("\n")
    feature_branches = [b for b in branches if b and b not in ("main", "master")]

    print(f"  run-record commits (chore(glados): record …): {record_commits}")
    print(f"  feature/other branches: {len(feature_branches)}")
    print(f"  MR-less verdict/review commits (silent-loss candidates): "
          f"{len(verdict_commits)}")
    for v in verdict_commits[:20]:
        print(f"    - {v}")
    if record_commits == 0 and feature_branches:
        print("  WARNING: agent-authored branches exist with zero run records — "
              "this is the class-B silent loss verify-ledger exists to surface")
    print("glados verify-ledger: report-only in v2.0.0")
    return 0


# ---- migrate ----------------------------------------------------------------
#
# `glados.py migrate --target <repo>` — the guided v1 -> v2 path. Four steps:
# DETECT (v1 layouts, specs/ dirs, SDA artifacts; suggest the install mode),
# GENERATE (glados.yaml from the example — platform auto-detected, sda from
# the artifacts, phase left as a REQUIRED human edit), CONVERT (each specs/
# dir becomes a create-only .glados/runs/ digest record; SPEC_LOG rows when
# sda), REPORT (next steps + counts). Non-destructive by default and
# idempotent; --dry-run prints the full plan and writes nothing; --clean
# removes the converted specs/ dirs and the detected v1 layouts. Touches
# nothing outside specs/, the v1 GLaDOS-owned layouts, glados.yaml, .glados/,
# and product-knowledge/SPEC_LOG.md.


def _is_v2_artifact(path: Path) -> bool:
    """True when the file is provably v2-generated — or unreadable (an
    unreadable file is not provably a v1 leftover, so it is left alone,
    the same stance as plugin-skill pruning)."""
    try:
        text = read_text(path)
    except Fatal:
        return True
    return any(m in text for m in V2_ARTIFACT_MARKERS)


def _detect_v1_layouts(target: Path) -> dict[str, list[Path]]:
    """The v1 command layouts present, keyed by the v2 install mode that
    supersedes each. Only files provably v1 GLaDOS-owned are listed: a known
    v1 name (or its underscore variant) carrying no v2 artifact marker —
    the v1/v2 name overlap makes the content check load-bearing."""
    layouts: dict[str, list[Path]] = {}

    claude: list[Path] = []
    cmds = target / ".claude" / "commands"
    if cmds.is_dir():
        for f in sorted(cmds.glob("*.md")):   # v1 un-namespaced commands
            if _v1_stem_match(f.stem, KNOWN_V1_WORKFLOWS):
                claude.append(f)
        namespaced = cmds / "glados"
        if namespaced.is_dir():               # v1 content under the v2 dir
            for f in sorted(namespaced.glob("*.md")):
                if _v1_stem_match(f.stem, KNOWN_V1_WORKFLOWS) \
                        and not _is_v2_artifact(f):
                    claude.append(f)
    if claude:
        layouts["claude"] = claude

    gem = target / ".gemini" / "skills" / "glados"
    if gem.is_dir():                          # tree fully owned by v1
        layouts["gemini"] = sorted(f for f in gem.rglob("*") if f.is_file())

    agy: list[Path] = []
    for base in (".agents", ".agent"):
        d = target / base / "workflows"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            stem = f.stem.removeprefix("glados-")
            if _v1_stem_match(stem, KNOWN_V1_WORKFLOWS) \
                    and not _is_v2_artifact(f):
                agy.append(f)
    if agy:
        layouts["antigravity"] = agy

    direct: list[Path] = []
    for sub, names in (("workflows", KNOWN_V1_WORKFLOWS),
                       ("modules", KNOWN_V1_MODULES)):
        d = target / "product-knowledge" / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if _v1_stem_match(f.stem, names):
                direct.append(f)
    if direct:
        layouts["direct"] = direct
    return layouts


def _suggest_mode(layouts: dict):
    """The install mode the follow-up should use, from the detected layouts."""
    for mode in ("claude", "gemini", "antigravity", "direct"):
        if mode in layouts:
            return mode
    return None


def _detect_sda_artifacts(target: Path) -> list[str]:
    """The v1 SDA conformance artifacts present (prefills `sda: true`)."""
    found: list[str] = []
    if (target / "claims.md").is_file():
        found.append("claims.md")
    if (target / "product-knowledge" / "SPEC_LOG.md").is_file():
        found.append("product-knowledge/SPEC_LOG.md")
    for name in ("ROADMAP.md", "PROJECT_STATUS.md"):
        doc = target / "product-knowledge" / name
        if doc.is_file():
            try:
                if SDA_MARKER in read_text(doc):
                    found.append(f"product-knowledge/{name} (SDA header)")
            except Fatal:
                pass
    return found


def _detect_platform(target: Path):
    """platform: for the generated manifest, from the origin remote host —
    gitlab.com -> gitlab, github.com -> github; anything else (self-hosted,
    no remote, no git) -> None, leaving the example default plus a TODO."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    url = proc.stdout.strip()
    if proc.returncode != 0 or not url:
        return None
    m = re.search(r"(?:@|://)(?:[^/@]+@)?([^/:]+)", url)
    host = (m.group(1) if m else url).lower()
    if host == "gitlab.com" or host.endswith(".gitlab.com"):
        return "gitlab"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    return None


def _generated_manifest(example_text: str, platform, sda: bool) -> str:
    """glados.yaml content for a migrating repo: the example verbatim except
    platform: auto-filled, sda: set from detection, phase: left as the
    REQUIRED-edit line (parses to null, so install still fails fast until a
    human chooses), and the example's explicit `decisions:` overrides plus
    its `relaxation-acknowledged:` confession commented out. The example's
    decision values are written for its own `phase: evolving`; carried into
    a manifest whose phase a human picks LATER, they would fail the install
    for a stricter phase (production/sunset) with relaxation errors the team
    never chose. Commented out, the chosen phase's preset governs and every
    phase installs cleanly. Line-level edits only — the output stays inside
    the YAML subset the tool's own parser accepts."""
    out: list[str] = []
    done = {"phase": False, "platform": False, "sda": False}
    in_decisions = False
    for line in example_text.split("\n"):
        if in_decisions:
            if line.startswith("  ") and line.strip():
                out.append("# " + line)
                continue
            in_decisions = False
        if not done["phase"] and line.startswith("phase:"):
            out.append(MIGRATE_PHASE_LINE)
            done["phase"] = True
        elif not done["platform"] and line.startswith("platform:"):
            if platform:
                out.append(f"platform: {platform}            # gitlab | github "
                           f"(auto-detected from the origin remote)")
            else:
                out.append(line + "  # TODO(migrate): could not auto-detect "
                                  "from a git origin remote - verify")
            done["platform"] = True
        elif not done["sda"] and line.startswith("sda:"):
            out.append("sda: true" if sda else line)
            done["sda"] = True
        elif line.startswith("decisions:"):
            out.append("# NOTE(migrate): the example's explicit decision "
                       "overrides are commented out")
            out.append("# so whichever phase: you pick governs them (see the "
                       "presets in")
            out.append("# .glados/presets.yaml after install). Uncomment a "
                       "line only to override")
            out.append("# your phase deliberately - a laxer value then needs "
                       "relaxation-acknowledged.")
            out.append("# " + line)
            in_decisions = True
        elif line.startswith("relaxation-acknowledged:"):
            out.append("# " + line)
        else:
            out.append(line)
    missing = [k for k, ok in done.items() if not ok]
    if missing:
        raise Fatal(f"glados.yaml.example has no top-level "
                    f"{', '.join(missing)} line(s) — cannot generate a "
                    f"migrated glados.yaml from it; pass --source <a v2 "
                    f"glados checkout with the current example>")
    return "\n".join(out)


def _check_generated_manifest_phases(source: Source, text: str) -> None:
    """Generate-time self-check: the generated manifest must resolve with NO
    phase-invariant errors under EVERY phase — 'fill phase:, run install' is
    the guided path's contract, and a pre-seeded value laxer than the phase
    a human picks later would break it. Fails fast at generate time instead
    of at the user's install."""
    raw = parse_yaml(text, "glados.yaml (generated)")
    for phase in PHASES:
        trial = dict(raw)
        trial["phase"] = phase
        resolved = resolve_manifest(trial, source.presets,
                                    "glados.yaml (generated)")
        errors = _check_phase_invariants(source, resolved)
        if errors:
            raise Fatal("the generated glados.yaml would fail install under "
                        f"phase '{phase}':\n  - " + "\n  - ".join(errors)
                        + "\nglados.yaml.example and the phase presets have "
                          "drifted apart — fix the example (this is a GLaDOS "
                          "source bug, not a problem with your repo)")


def _existing_manifest_report(raw: dict, source: Source, platform,
                              sda_artifacts: list[str]) -> list[str]:
    """Report-only differences for a pre-existing glados.yaml (never edited)."""
    lines: list[str] = []
    if raw.get("phase") in (None, ""):
        lines.append("phase: unset — set it to one of: " + ", ".join(PHASES)
                     + " before running install")
    if platform and raw.get("platform") not in (None, platform):
        lines.append(f"platform: manifest says '{raw.get('platform')}' but the "
                     f"origin remote looks like {platform} — verify")
    if sda_artifacts and raw.get("sda") is not True:
        lines.append("sda: SDA artifacts detected ("
                     + ", ".join(sda_artifacts)
                     + ") but the manifest does not set 'sda: true' — "
                     "consider declaring it")
    example = source.root / "glados.yaml.example"
    if example.exists():
        ex_keys = parse_yaml(read_text(example), "glados.yaml.example").keys()
        absent = sorted(k for k in ex_keys if k not in raw)
        if absent:
            lines.append("keys in glados.yaml.example not present here "
                         "(informational): " + ", ".join(absent))
    if not lines:
        lines.append("no differences worth reporting")
    return lines


_SPEC_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[-_]+(.*))?$")


def _git_last_commit_date(target: Path, rel: str):
    """YYYY-MM-DD of the last commit touching rel, else None. History, never
    wall clock — a re-run derives the same date."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(target), "log", "-1", "--format=%cs", "--", rel],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().split("\n")[0].strip() if proc.stdout.strip() else ""
    return line if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line) else None


def _spec_dirs(target: Path) -> list[Path]:
    specs = target / "specs"
    if not specs.is_dir():
        return []
    return sorted(d for d in specs.iterdir() if d.is_dir())


def _spec_identity(target: Path, d: Path):
    """(date, date_source, slug) for one specs/ dir: date from the YYYY-MM-DD
    dirname prefix, else from git history of the dir, else None."""
    m = _SPEC_DATE_RE.match(d.name)
    if m:
        date, date_src = m.group(1), "from the directory name"
        rest = m.group(2) or ""
    else:
        date = _git_last_commit_date(target, f"specs/{d.name}")
        date_src = ("from git history" if date
                    else "unknown - no dirname date prefix and no git history")
        rest = d.name
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", rest.replace("_", "-")).strip("-.")
    return date, date_src, slug or "spec"


def _spec_excerpt(readme: Path):
    """First heading + last ~10 lines of a spec dir's README.md, or None."""
    if not readme.is_file():
        return None
    try:
        text = read_text(readme)
    except Fatal:
        return None
    lines = [l.rstrip() for l in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return None
    tail = lines[-10:]
    while tail and tail[0] == "":
        tail.pop(0)
    heading = next((l for l in lines if l.startswith("#")), None)
    parts: list[str] = []
    if heading is not None and heading not in tail:
        parts += [heading, "", "[...]", ""]
    parts += tail
    return "\n".join(parts)


def _spec_record_body(d: Path, date, date_src: str) -> str:
    """The digest run record one converted specs/ dir becomes."""
    name = d.name
    lines = [f"# Migrated spec: {name}", ""]
    lines.append(f"- Converted from `specs/{name}/` by `glados.py migrate` "
                 f"(v1 -> v2).")
    lines.append(f"- Date: {date or 'unknown'} ({date_src}).")
    lines.append(f"- The full history lives in git: `git log -- specs/{name}` "
                 f"— the specs/ tree can be deleted after migration "
                 f"(migrate --clean does it).")
    excerpt = _spec_excerpt(d / "README.md")
    if excerpt is None:
        lines += ["", f"_No README.md in `specs/{name}/` — no excerpt._"]
    else:
        lines += ["", f"## Excerpt (from `specs/{name}/README.md`)", "", excerpt]
    return "\n".join(lines) + "\n"


def _insert_spec_log_rows(path: Path, rows: list[str]) -> None:
    """Insert work-unit rows just under the table header separator (the log
    is newest-first); with no recognizable table, append them at the end."""
    lines = read_text(path).split("\n")
    idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and "-" in s \
                and set(s) <= set("|-: "):
            idx = i + 1
            break
    if idx is None:
        while lines and lines[-1] == "":
            lines.pop()
        lines += rows + [""]
    else:
        lines[idx:idx] = rows
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _migrate_clean(target: Path, layouts: dict, conversions: list) -> list[str]:
    """--clean: remove converted specs/ dirs (record on disk == converted)
    and the exact v1 files detection named; the fully v1-owned gemini/direct
    layouts go through the same _cleanup_v1_legacy the installer uses."""
    removed: list[str] = []
    for c in conversions:
        if c["path"].exists() and c["dir"].is_dir():
            removed += _remove_tree_files(c["dir"], target)
    specs = target / "specs"
    if specs.is_dir():
        try:
            specs.rmdir()   # only when every spec dir was converted + removed
        except OSError:
            pass
    for f in layouts.get("claude", []):
        if f.is_file():
            _force_unlink(f)
            removed.append(str(f.relative_to(target)))
    namespaced = target / ".claude" / "commands" / "glados"
    if namespaced.is_dir():
        try:
            namespaced.rmdir()   # keeps a dir that still has v2/user files
        except OSError:
            pass
    for f in layouts.get("antigravity", []):
        if f.is_file():
            _force_unlink(f)
            removed.append(str(f.relative_to(target)))
    for base in (".agents", ".agent"):
        d = target / base / "workflows"
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    removed += _cleanup_v1_legacy(target, "gemini")
    removed += _cleanup_v1_legacy(target, "direct")
    return removed


def cmd_migrate(args) -> int:
    target = Path(args.target).resolve()
    if not target.is_dir():
        raise Fatal(f"target directory does not exist: {target}")
    source = _resolve_source(args)
    dry = bool(args.dry_run)

    def act(verb: str) -> str:
        return f"would {verb}" if dry else verb

    def posix(p: Path) -> str:
        return p.relative_to(target).as_posix()

    print(f"glados migrate — {target}"
          + (" (dry-run: nothing is written)" if dry else ""))

    # -- 1. DETECT ------------------------------------------------------------
    layouts = _detect_v1_layouts(target)
    spec_dirs = _spec_dirs(target)
    sda_artifacts = _detect_sda_artifacts(target)
    suggested = _suggest_mode(layouts)
    platform = _detect_platform(target)

    print("\n== detected ==")
    if layouts:
        for mode, paths in layouts.items():
            print(f"  v1 layout [{mode}]: {len(paths)} file(s)")
            for p in paths:
                print(f"    - {posix(p)}")
    else:
        print("  v1 command layouts: none found")
    if spec_dirs:
        print(f"  specs/ directories: {len(spec_dirs)}")
        for d in spec_dirs:
            print(f"    - specs/{d.name}")
    else:
        print("  specs/ directories: none found")
    print("  SDA artifacts: "
          + (", ".join(sda_artifacts) if sda_artifacts else "none found"))
    if suggested:
        others = [m for m in layouts if m != suggested]
        print(f"  suggested install mode: {suggested}"
              + (f" (v1 layouts also found for: {', '.join(others)})"
                 if others else ""))
    else:
        print("  suggested install mode: none detected — pick one of: "
              + ", ".join(INSTALL_MODES))

    # -- 2. GENERATE glados.yaml (never overwrite) ------------------------------
    print("\n== manifest ==")
    gy = target / "glados.yaml"
    files_generated = 0
    phase_unset = True
    if gy.exists():
        raw = load_manifest(gy)
        sda_val = raw.get("sda", False)
        if not isinstance(sda_val, bool):
            raise Fatal(f"{gy.name}: key 'sda' must be a bool — got {sda_val!r}; "
                        f"write 'sda: true' or 'sda: false'")
        sda_effective = sda_val
        phase_unset = raw.get("phase") in (None, "")
        print("  glados.yaml: present — never overwritten (report-only):")
        for line in _existing_manifest_report(raw, source, platform,
                                              sda_artifacts):
            print(f"    - {line}")
    else:
        sda_effective = bool(sda_artifacts)
        example = source.root / "glados.yaml.example"
        if not example.exists():
            raise Fatal(f"cannot generate glados.yaml: no glados.yaml.example "
                        f"under {source.root} — pass --source <a full glados "
                        f"checkout>")
        text = _generated_manifest(read_text(example), platform, sda_effective)
        _check_generated_manifest_phases(source, text)  # any phase must install
        print(f"  glados.yaml: absent — {act('generate from glados.yaml.example')}:")
        if platform:
            print(f"    - platform: {platform} (auto-detected from the origin "
                  f"remote)")
        else:
            print("    - platform: gitlab (example default; no gitlab.com/"
                  "github.com origin remote found — a TODO comment marks the "
                  "line)")
        print(f"    - sda: {'true' if sda_effective else 'false'}"
              + (" (SDA artifacts detected)" if sda_effective else ""))
        print("    - phase: left REQUIRED — install fails fast until a human "
              "picks one")
        print("    - decisions: example overrides commented out — your phase's "
              "preset governs (uncomment to override)")
        if not dry:
            gy.write_text(text, encoding="utf-8", newline="\n")
        files_generated += 1

    # -- 3. CONVERT specs/ ------------------------------------------------------
    print("\n== convert specs/ ==")
    conversions: list[dict] = []
    for d in spec_dirs:
        date, date_src, slug = _spec_identity(target, d)
        rec_name = f"{date or 'undated'}-migrated-{slug}.md"
        rec_path = target / ".glados" / "runs" / rec_name
        existed = rec_path.exists()
        conversions.append({"dir": d, "date": date, "path": rec_path,
                            "rel": f".glados/runs/{rec_name}",
                            "existed": existed})
        if existed:
            print(f"  specs/{d.name}/ -> .glados/runs/{rec_name} "
                  f"(record exists — kept)")
            continue
        print(f"  specs/{d.name}/ -> .glados/runs/{rec_name} ({act('create')})")
        if not dry:
            rec_path.parent.mkdir(parents=True, exist_ok=True)
            rec_path.write_text(_spec_record_body(d, date, date_src),
                                encoding="utf-8", newline="\n")
        files_generated += 1
    if not spec_dirs:
        print("  nothing to convert")

    rows_added = 0
    if conversions and sda_effective:
        spec_log = target / "product-knowledge" / "SPEC_LOG.md"
        existing_text = read_text(spec_log) if spec_log.exists() else ""
        pending = [c for c in conversions
                   if c["path"].name not in existing_text]
        pending.sort(key=lambda c: c["date"] or "", reverse=True)  # newest first
        rows = [f"| {c['date'] or 'undated'} | migrate | specs/{c['dir'].name} "
                f"| migrated | {c['rel']} |" for c in pending]
        if rows:
            if not spec_log.exists():
                print(f"  product-knowledge/SPEC_LOG.md: "
                      f"{act('create from the template')}")
                if not dry:
                    _scaffold_spec_log(target, source,
                                       datetime.date.today().isoformat())
                files_generated += 1
            print(f"  product-knowledge/SPEC_LOG.md: "
                  f"{act(f'append {len(rows)} work-unit row(s)')}")
            if not dry:
                _insert_spec_log_rows(spec_log, rows)
            rows_added = len(rows)
        else:
            print("  product-knowledge/SPEC_LOG.md: every migrated row already "
                  "present")
    elif conversions:
        print("  SPEC_LOG rows: skipped (sda is not true)")

    # -- --clean / cleanup report -----------------------------------------------
    print("\n== cleanup ==")
    if args.clean:
        if dry:
            for c in conversions:
                print(f"  would remove specs/{c['dir'].name}/ (converted)")
            for mode, paths in layouts.items():
                for p in paths:
                    print(f"  would remove {posix(p)} (v1 {mode} layout)")
            if not conversions and not layouts:
                print("  nothing to clean")
        else:
            removed = _migrate_clean(target, layouts, conversions)
            for rel in removed:
                print("  removed " + rel.replace("\\", "/"))
            if not removed:
                print("  nothing to clean")
    else:
        if conversions or layouts:
            print("  nothing removed (non-destructive default); removable once "
                  "migrated:")
            for c in conversions:
                print(f"    - specs/{c['dir'].name}/ (converted to {c['rel']})")
            for mode, paths in layouts.items():
                for p in paths:
                    print(f"    - {posix(p)} (v1 {mode} layout)")
            print("  note: `install --mode <m>` cleans the v1 layout of its own "
                  "mode; `migrate --clean` removes all of the above")
        else:
            print("  nothing to clean")

    # -- 4. NEXT STEPS + counts ---------------------------------------------------
    print("\n== next steps ==")
    steps: list[str] = []
    if phase_unset:
        steps.append("edit glados.yaml — set phase: (one of: "
                     + ", ".join(PHASES) + ")")
    steps.append(f"python glados.py install --mode {suggested or '<mode>'} "
                 f"--target {target}")
    steps.append("commit the migrated tree")
    if not args.clean:
        steps.append(f"re-run: python glados.py migrate --target {target} "
                     f"--clean (removes the converted specs/ dirs and v1 "
                     f"layouts)")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")

    converted = sum(1 for c in conversions if not c["existed"])
    if dry:
        print(f"\nglados migrate (dry-run): would convert {converted} spec "
              f"dir(s), generate {files_generated} file(s), append "
              f"{rows_added} SPEC_LOG row(s)")
    else:
        print(f"\nglados migrate: {converted} spec dir(s) converted, "
              f"{files_generated} file(s) generated, {rows_added} SPEC_LOG "
              f"row(s) appended")
    return 0


def cmd_compile_plugin(args) -> int:
    source = _resolve_source(args)
    repo = Path(args.target).resolve() if args.target else source.root
    ns = argparse.Namespace(source=str(source.root), target=str(repo),
                            mode="claude-plugin")
    return cmd_install(ns)


# =============================================================================
# 11. CLI
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="glados.py",
        description="GLaDOS v2 compiler / type-checker / installer.")
    p.add_argument("--version", action="version", version=f"glados {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("install", help="compile + emit one adapter")
    pi.add_argument("--target", required=True)
    pi.add_argument("--mode", required=True, choices=INSTALL_MODES)
    pi.add_argument("--source")
    pi.set_defaults(func=cmd_install)

    pc = sub.add_parser("check", help="CI mode: recompute + diff installed files")
    pc.add_argument("--target", default=".")
    pc.add_argument("--source")
    pc.add_argument("--report-only", action="store_true")
    pc.set_defaults(func=cmd_check)

    pd = sub.add_parser("doctor", help="staleness + wiring report (never fails)")
    pd.add_argument("--target", required=True)
    pd.add_argument("--source")
    pd.set_defaults(func=cmd_doctor)

    pm = sub.add_parser(
        "migrate",
        help="guided v1 -> v2 migration: detect v1 layouts, generate "
             "glados.yaml (phase left for a human), convert specs/ history "
             "to run records — non-destructive and idempotent by default")
    pm.add_argument("--target", required=True)
    pm.add_argument("--source")
    pm.add_argument("--dry-run", action="store_true",
                    help="print the full migration plan; write nothing")
    pm.add_argument("--clean", action="store_true",
                    help="after successful conversion, remove the converted "
                         "specs/ dirs and the detected v1 command layouts")
    pm.set_defaults(func=cmd_migrate)

    pv = sub.add_parser("verify-ledger", help="scan git for silent-loss candidates")
    pv.add_argument("--target", required=True)
    pv.add_argument("--report-only", action="store_true")
    pv.set_defaults(func=cmd_verify_ledger)

    pp = sub.add_parser("compile-plugin",
                        help="shorthand: install --mode claude-plugin --target <repo>")
    pp.add_argument("--target")
    pp.add_argument("--source")
    pp.set_defaults(func=cmd_compile_plugin)
    return p


def main(argv=None) -> int:
    # Legacy Windows consoles (cp850/cp437) cannot encode the em dashes and
    # ellipses in reports; never let a print() kill an install that already
    # wrote files.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Fatal as exc:
        print(f"glados: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
