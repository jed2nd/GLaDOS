#!/usr/bin/env python3
"""GLaDOS v2 self-test — stdlib unittest, no third-party deps.

Run from the repo root (or anywhere):  python tests/run_tests.py

Compiles every adapter over the fixtures and exercises the type-checker,
cleanup, determinism, drift detection and the runtime artifacts. A mode that
stops shipping modules — v1's flagship bug — fails this build.
"""

import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GLADOS_PY = REPO / "bin" / "glados.py"
FIXTURE = REPO / "tests" / "fixture-project"
LEGACY = REPO / "tests" / "fixtures-legacy"
EXAMPLE = REPO / "glados.yaml.example"


def _load_module():
    spec = importlib.util.spec_from_file_location("glados_mod", GLADOS_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


glados = _load_module()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_cli(*args, env=None):
    """Invoke the real CLI in a subprocess; return (returncode, stdout+stderr)."""
    proc = subprocess.run(
        [sys.executable, str(GLADOS_PY), *[str(a) for a in args]],
        capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout + proc.stderr


def tmpdir(prefix="glados-test-"):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    return d


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_target(manifest_text=None, from_example=True):
    """A fresh install target with a glados.yaml."""
    d = tmpdir()
    if manifest_text is not None:
        write(d / "glados.yaml", manifest_text)
    elif from_example:
        shutil.copyfile(EXAMPLE, d / "glados.yaml")
    return d


def make_plugin_target():
    """A plugin-mode target: needs glados.yaml.example present."""
    d = tmpdir("glados-plugin-")
    shutil.copyfile(EXAMPLE, d / "glados.yaml.example")
    return d


def install(mode, target, extra=(), source=None, env=None):
    return run_cli("install", "--mode", mode, "--target", target,
                   "--source", source or REPO, *extra, env=env)


def make_doctored_source():
    """A private copy of the GLaDOS source tree that tests may mutate."""
    d = tmpdir("glados-src-")
    shutil.copytree(REPO / "src", d / "src")
    shutil.copytree(REPO / "bin", d / "bin",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "hooks", d / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "ci", d / "ci")
    shutil.copyfile(EXAMPLE, d / "glados.yaml.example")
    return d


def all_files(root: Path):
    return sorted(p for p in root.rglob("*") if p.is_file())


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestCompile(unittest.TestCase):

    def test_compile_all_modes_no_dangling(self):
        """All six modes compile with zero unresolved includes/placeholders."""
        forbidden = ["{{", "<!-- glados:include"]
        for mode in ["claude", "direct", "gemini", "antigravity", "aistudio"]:
            with self.subTest(mode=mode):
                t = make_target()
                rc, out = install(mode, t)
                self.assertEqual(rc, 0, f"{mode} install failed:\n{out}")
                self._scan(t, mode, forbidden)
        # plugin mode is repo-side
        with self.subTest(mode="claude-plugin"):
            t = make_plugin_target()
            rc, out = install("claude-plugin", t)
            self.assertEqual(rc, 0, f"plugin install failed:\n{out}")
            self._scan(t / "compiled", "claude-plugin", forbidden)

    def _scan(self, root, mode, forbidden):
        # Scan only emitted adapter artifacts, never the vendored compiler/presets
        # (which legitimately contain the token strings) or copied sources.
        skip_dirs = {".glados", "src", "bin", "hooks"}
        checked = 0
        for f in all_files(root):
            if any(part in skip_dirs for part in f.relative_to(root).parts):
                continue
            if f.suffix not in (".md", ".toml"):
                continue
            text = read(f)
            checked += 1
            for tok in forbidden:
                self.assertNotIn(tok, text, f"{mode}: '{tok}' in {f.name}")
        self.assertGreater(checked, 0, f"{mode}: no artifacts scanned")

    def test_toml_roundtrip(self):
        """Gemini TOML parses with tomllib; prompt carries the epilogue text."""
        t = make_target()
        rc, out = install("gemini", t)
        self.assertEqual(rc, 0, out)
        toml_path = t / ".gemini" / "commands" / "glados" / "build-feature.toml"
        data = tomllib.loads(read(toml_path))
        self.assertIn("description", data)
        self.assertIn("prompt", data)
        self.assertIn("Before ending this run", data["prompt"])   # epilogue
        self.assertIn("Before doing anything else", data["prompt"])  # preamble
        # an alias TOML states the rename
        alias = t / ".gemini" / "commands" / "glados" / "plan-fix.toml"
        adata = tomllib.loads(read(alias))
        self.assertIn("fix-bug", adata["prompt"])

    def test_plugin_cores_phase_neutral_with_bootstrap_guard(self):
        """claude-plugin compiles from glados.yaml.example, so its cores must
        not bake the example's phase-resolved optimize-for: they carry the
        phase-neutral baseline sentence plus a bootstrap guard deferring to
        the consuming repo's own glados.yaml."""
        t = make_plugin_target()
        rc, out = install("claude-plugin", t)
        self.assertEqual(rc, 0, out)
        for core in (t / "compiled" / "claude-plugin").glob("*.md"):
            text = read(core)
            self.assertTrue(text.startswith("<!-- claude-plugin bootstrap guard"),
                            f"{core.name}: missing bootstrap guard")
            self.assertIn("Optimize for doing no harm", text, core.name)
            # the example's evolving-phase sentence must NOT be baked in
            self.assertNotIn("Optimize for velocity with a memory", text,
                             f"{core.name}: example's phase leaked into plugin")

    def test_determinism(self):
        """Two installs of the same mode produce byte-identical trees."""
        for mode in ("claude", "direct", "gemini", "antigravity", "aistudio",
                     "claude-plugin"):
            with self.subTest(mode=mode):
                if mode == "claude-plugin":
                    a, b = make_plugin_target(), make_plugin_target()
                else:
                    a, b = make_target(), make_target()
                self.assertEqual(install(mode, a)[0], 0)
                self.assertEqual(install(mode, b)[0], 0)
                fa = {f.relative_to(a).as_posix(): f for f in all_files(a)}
                fb = {f.relative_to(b).as_posix(): f for f in all_files(b)}
                self.assertEqual(set(fa), set(fb), "file sets differ")
                for rel in fa:
                    self.assertEqual(fa[rel].read_bytes(), fb[rel].read_bytes(),
                                     f"bytes differ for {rel}")


class TestTypeChecks(unittest.TestCase):

    def test_missing_phase_fatal(self):
        t = make_target("glados: 2\nplatform: gitlab\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 1)
        self.assertIn("phase", out)
        for value in ("nascent", "evolving", "production", "sunset"):
            self.assertIn(value, out, "fatal message must name the four phases")

    def test_reader_without_writer_fails(self):
        # Disable run-epic (the only writer of epic.integration-branch) and omit
        # branching.default-target (its documented fallback): build-feature still
        # reads epic.integration-branch, so the read is genuinely uncovered.
        manifest = (
            "glados: 2\nplatform: gitlab\nphase: evolving\n"
            "disabled-workflows: [run-epic]\n"
            "channels:\n  progress: [ledger]\n  verdict: [mr-comment]\n"
            "  escalation: [issue]\n  bug: [issue]\n  decision: [ledger]\n"
            "  observation: [ledger]\n"
            "branching:\n  feature: \"feat/<slug>\"\n"
            "  epic-integration: \"feature/<epic-slug>\"\n"
            "default-modules: [standards-gate]\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, out)
        self.assertIn("epic.integration-branch", out)
        self.assertIn("build-feature", out)

    def test_reader_without_writer_passes_with_fallback(self):
        # Same as above but WITH branching.default-target: the fallback covers it.
        manifest = (
            "glados: 2\nplatform: gitlab\nphase: evolving\n"
            "disabled-workflows: [run-epic]\n"
            "channels:\n  progress: [ledger]\n  verdict: [mr-comment]\n"
            "  escalation: [issue]\n  bug: [issue]\n  decision: [ledger]\n"
            "  observation: [ledger]\n"
            "branching:\n  feature: \"feat/<slug>\"\n  default-target: main\n"
            "default-modules: [standards-gate]\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)

    def test_zero_sink_emit_fails(self):
        text = read(EXAMPLE).replace("  verdict:     [mr-comment]",
                                     "  verdict:     [ledger]")
        t = make_target(text)
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, out)
        self.assertIn("verdict", out)
        self.assertIn("team-visible", out)

    def test_zero_sink_emit_passes_with_confession(self):
        text = read(EXAMPLE).replace("  verdict:     [mr-comment]",
                                     "  verdict:     [ledger]")
        text += "\nvisibility-acknowledged: ledger-only\n"
        t = make_target(text)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)

    def test_relaxation_confession_required(self):
        manifest = (
            "glados: 2\nplatform: gitlab\nphase: production\n"
            "merge-authority: agent\n"
            "branching:\n  feature: \"feat/<slug>\"\n  default-target: main\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, out)
        self.assertIn("merge-authority", out)
        self.assertIn("relaxation-acknowledged", out)

    def test_relaxation_confession_accepted(self):
        manifest = (
            "glados: 2\nplatform: gitlab\nphase: production\n"
            "merge-authority: agent\n"
            "relaxation-acknowledged: [merge-authority]\n"
            "branching:\n  feature: \"feat/<slug>\"\n  default-target: main\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)

    def test_alias_integrity(self):
        source = glados.Source(REPO)
        for alias, target in source.aliases.items():
            self.assertIn(target, glados.CORES, f"{alias} -> {target} not a core")
        # a doctored bad alias is caught by the checker
        r = glados.resolve_manifest(
            glados.load_manifest(EXAMPLE), source.presets, "glados.yaml.example")
        comp = glados.compile_all(source, r, "deadbeef")
        source.aliases["bogus-alias"] = "not-a-core"
        errors = glados.run_type_checks(source, comp)
        self.assertTrue(any("bogus-alias" in e for e in errors), errors)


class TestReporting(unittest.TestCase):

    def test_assembly_report_provenance(self):
        # nascent, with no explicit overrides, is phase-derived and laxer than
        # baseline — the report must show RELAXED(phase) markers and provenance.
        manifest = ("glados: 2\nplatform: gitlab\nphase: nascent\n"
                    "branching:\n  feature: \"feat/<slug>\"\n"
                    "  default-target: main\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("RELAXED(phase) markers:", report)
        # count must be > 0 for nascent
        line = [l for l in report.splitlines() if "RELAXED(phase) markers:" in l][0]
        count = int(line.rsplit(":", 1)[1].strip())
        self.assertGreater(count, 0, report)
        self.assertIn("RELAXED(phase) `merge-authority`", report)
        self.assertIn("(phase:nascent)", report)   # provenance tag
        self.assertIn("(explicit)", report)        # branching came from manifest

    def test_check_detects_drift(self):
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        rc, _ = run_cli("check", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0, "clean install should pass check")
        # hand-edit an installed compiled file
        core = t / "product-knowledge" / "glados" / "intent.md"
        core.write_text(read(core) + "\nHAND EDIT\n", encoding="utf-8", newline="\n")
        rc, out = run_cli("check", "--target", t, "--source", REPO)
        self.assertEqual(rc, 1, "drift must fail check")
        self.assertIn("drift", out)
        self.assertIn("intent.md", out)

    def test_check_detects_stale_manifest(self):
        # A structural manifest edit after install must be flagged stale.
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        gy = t / "glados.yaml"
        gy.write_text(read(gy) + "\n# structural edit\n",
                      encoding="utf-8", newline="\n")
        rc, out = run_cli("check", "--target", t, "--source", REPO)
        self.assertEqual(rc, 1)
        self.assertIn("stale compile", out)


class TestVendor(unittest.TestCase):

    def test_vendor_byte_match(self):
        """Self-vendored copies byte-match their sources."""
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        self.assertEqual((t / ".glados" / "glados.py").read_bytes(),
                         GLADOS_PY.read_bytes(), "vendored glados.py must byte-match")
        self.assertEqual((t / ".glados" / "presets.yaml").read_bytes(),
                         (REPO / "src" / "kernel" / "presets" / "phases.yaml").read_bytes(),
                         "vendored presets.yaml must byte-match")
        # manifest-hash file matches the recomputed hash
        vend = read(t / ".glados" / "manifest-hash").strip()
        self.assertEqual(vend, glados.manifest_hash_of(t / "glados.yaml"))
        # the FULL src/ tree is vendored byte-identically (the CI backstop
        # recomputes the compile from it), plus the manifest example
        src_files = sorted(p for p in (REPO / "src").rglob("*") if p.is_file())
        self.assertGreater(len(src_files), 0)
        for s in src_files:
            v = t / ".glados" / "src" / s.relative_to(REPO / "src")
            self.assertTrue(v.exists(), f"missing vendored source {v}")
            self.assertEqual(v.read_bytes(), s.read_bytes(),
                             f"vendored {v.name} must byte-match")
        self.assertEqual((t / ".glados" / "glados.yaml.example").read_bytes(),
                         EXAMPLE.read_bytes())

    def test_vendored_checker_self_contained(self):
        """`python .glados/glados.py check --target .` works with NO --source:
        the vendored src/ tree makes the CI backstop self-contained, and a
        clean install passes it in enforcing mode."""
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        proc = subprocess.run(
            [sys.executable, str(t / ".glados" / "glados.py"),
             "check", "--target", str(t)],
            capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("glados check: OK", out)

    def test_vendored_checker_ignores_target_src_layout(self):
        """A target repo with its own top-level src/ (a src-layout project)
        must not shadow the vendored .glados/src/. _resolve_source keys on the
        src/kernel/ shape, so the vendored checker still resolves .glados/ with
        NO --source and passes in enforcing mode. Regression for #14, where the
        app's src/ won and check --report-only went silently green."""
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        # Plant an unrelated application source tree at the repo root.
        write(t / "src" / "app" / "main.py", "print('hello')\n")
        proc = subprocess.run(
            [sys.executable, str(t / ".glados" / "glados.py"),
             "check", "--target", str(t)],
            capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("glados check: OK", out)
        self.assertNotIn("cannot resolve the GLaDOS source tree", out)
        # report-only must genuinely check (and pass), not go green while
        # never resolving its own sources.
        proc = subprocess.run(
            [sys.executable, str(t / ".glados" / "glados.py"),
             "check", "--target", str(t), "--report-only"],
            capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertNotIn("cannot resolve the GLaDOS source tree", out)

    def test_vendored_tree_stale_files_cleaned(self):
        """A leftover file in a vendored .glados/ subtree (e.g. a renamed
        source after an upgrade) is cleaned on reinstall — it would otherwise
        skew the self-contained checker's recompute forever."""
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        stale_src = t / ".glados" / "src" / "vocabulary" / "old-fragment.md"
        write(stale_src, "renamed away two versions ago\n")
        stale_persona = t / ".glados" / "personas" / "retired.md"
        write(stale_persona, "retired persona\n")
        run_marker = t / ".glados" / "runs" / "2026-07-03-run.md"
        write(run_marker, "# run record — user history, never touched\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertFalse(stale_src.exists(), "stale vendored source lingers")
        self.assertFalse(stale_persona.exists(), "stale persona lingers")
        self.assertTrue(run_marker.exists(), ".glados/runs/ must never be touched")
        # antigravity's glados-* name scoping applies to .agents/workflows
        # only, never to the vendored subtrees
        t2 = make_target()
        self.assertEqual(install("antigravity", t2)[0], 0)
        stale2 = t2 / ".glados" / "src" / "vocabulary" / "old-fragment.md"
        write(stale2, "stale\n")
        self.assertEqual(install("antigravity", t2)[0], 0)
        self.assertFalse(stale2.exists(),
                         "antigravity must clean vendored subtrees too")

    def test_ci_templates_vendored_and_sourceless(self):
        """Both CI templates are vendored to .glados/ci/, run the vendored
        checker without --source, and carry the separate non-blocking
        verify-ledger step; the install prints the enable stanza."""
        for name in ("glados-check.gitlab-ci.yml",
                     "glados-check.github-actions.yml"):
            tmpl = read(REPO / "ci" / name)
            self.assertIn("python .glados/glados.py check --target . "
                          "--report-only", tmpl, name)
            self.assertNotIn("--source .", tmpl,
                             f"{name} must not need a source checkout")
            self.assertIn("verify-ledger --target . --report-only", tmpl, name)
        gitlab = read(REPO / "ci" / "glados-check.gitlab-ci.yml")
        self.assertIn("allow_failure: true", gitlab,
                      "verify-ledger job must be non-blocking")
        github = read(REPO / "ci" / "glados-check.github-actions.yml")
        self.assertIn("continue-on-error: true", github,
                      "verify-ledger step must be non-blocking")
        t = make_target()
        rc, out = install("claude", t)
        self.assertEqual(rc, 0, out)
        for name in ("glados-check.gitlab-ci.yml",
                     "glados-check.github-actions.yml"):
            self.assertEqual((t / ".glados" / "ci" / name).read_bytes(),
                             (REPO / "ci" / name).read_bytes(),
                             f"vendored {name} must byte-match")
        # example manifest says platform: gitlab -> the GitLab stanza prints
        self.assertIn(".glados/ci/glados-check.gitlab-ci.yml", out)
        self.assertIn("include:", out)

    def test_agy_hooks_single_source(self):
        """hooks/agy-hooks.json is the one source of the agy hooks block: it
        stays dumps(indent=2)-normalized, the README fenced block matches it,
        the antigravity install emits it verbatim, and it is vendored."""
        record_path = REPO / "hooks" / "agy-hooks.json"
        record = record_path.read_text(encoding="utf-8")
        data = json.loads(record)
        self.assertEqual(
            record, json.dumps(data, indent=2) + "\n",
            "hooks/agy-hooks.json must stay json.dumps(..., indent=2)-"
            "normalized — _emit_agy_hooks re-emits it through json.dumps")
        readme = (REPO / "hooks" / "README.md").read_text(encoding="utf-8")
        blocks = [b for b in re.findall(r"```json\n(.*?)```", readme, re.S)
                  if "glados-run-record-guard" in b]
        self.assertEqual(len(blocks), 1,
                         "hooks/README.md must show exactly one agy block")
        self.assertEqual(json.loads(blocks[0]), data,
                         "hooks/README.md fenced agy block drifted from "
                         "hooks/agy-hooks.json — update the README from the "
                         "file of record")
        t = make_target()
        rc, out = install("antigravity", t)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(t / ".agents" / "hooks.json"), record,
                         "emitted .agents/hooks.json must match the record")
        self.assertEqual(
            (t / ".glados" / "hooks" / "agy-hooks.json").read_bytes(),
            record_path.read_bytes(), "agy-hooks.json must be vendored")

    def test_personas_vendored(self):
        """Every install mode vendors the full src/personas/ library into
        <target>/.glados/personas/, byte-identical — panels resolve library
        personas from there (project product-knowledge/personas/ wins)."""
        src_personas = sorted((REPO / "src" / "personas").glob("*.md"))
        self.assertGreater(len(src_personas), 0, "library personas missing")
        for mode in ("claude", "claude-plugin", "direct", "gemini",
                     "antigravity", "aistudio"):
            with self.subTest(mode=mode):
                t = make_plugin_target() if mode == "claude-plugin" else make_target()
                rc, out = install(mode, t)
                self.assertEqual(rc, 0, out)
                vendored = sorted((t / ".glados" / "personas").glob("*.md"))
                self.assertEqual([p.name for p in vendored],
                                 [p.name for p in src_personas],
                                 f"{mode}: vendored persona set differs")
                for s, v in zip(src_personas, vendored):
                    self.assertEqual(v.read_bytes(), s.read_bytes(),
                                     f"{mode}: {v.name} not byte-identical")


class TestCleanup(unittest.TestCase):

    def test_cleanup_directory_scoped(self):
        # Install claude over the legacy tree; assert directory-scoped cleanup.
        t = tmpdir("glados-legacy-")
        shutil.copytree(LEGACY, t, dirs_exist_ok=True)
        rc, out = install("claude", t)
        self.assertEqual(rc, 0, out)

        owned = t / ".claude" / "commands" / "glados"
        parent = t / ".claude" / "commands"

        # v1 leftovers INSIDE owned dirs are cleaned
        self.assertFalse((owned / "plan_feature.md").exists(),
                         "underscore v1 file inside owned dir should be removed")
        self.assertFalse((owned / "old-removed-workflow.md").exists(),
                         "stale core inside owned dir should be removed")
        # top-level un-namespaced v1 file matching a core name is migrated away
        self.assertFalse((parent / "plan-feature.md").exists(),
                         "top-level v1 command should be migrated away")

        # files OUTSIDE glados-owned dirs are untouched
        self.assertTrue((parent / "my-user-command.md").exists(),
                        "user command must survive")
        self.assertTrue((t / "README.md").exists(), "repo README must survive")
        self.assertTrue((t / ".gemini" / "skills" / "glados" / "SKILL.md").exists(),
                        "v1 gemini layout outside owned dirs must survive")

        # the real cores + alias shims landed
        self.assertTrue((owned / "plan-feature.md").exists())
        self.assertTrue((owned / "fix-bug.md").exists())
        self.assertTrue((owned / "mission.md").exists(), "alias shim expected")

    def test_v1_legacy_cleanup_gemini(self):
        # The gemini install removes the fully v1-owned .gemini/skills/glados/
        # tree; a claude install (above) leaves it alone.
        t = tmpdir("glados-v1-gem-")
        shutil.copytree(LEGACY, t, dirs_exist_ok=True)
        rc, out = install("gemini", t)
        self.assertEqual(rc, 0, out)
        self.assertFalse((t / ".gemini" / "skills" / "glados").exists(),
                         "v1 gemini tree must be removed by the gemini install")
        self.assertTrue(
            (t / ".gemini" / "commands" / "glados" / "intent.toml").exists())
        self.assertIn(".gemini/skills/glados", out.replace("\\", "/"),
                      "removed v1 files must be reported")

    def test_v1_legacy_cleanup_direct(self):
        # The direct install removes ONLY known-v1-named files from the shared
        # product-knowledge/{workflows,modules}/ dirs — never user files.
        t = tmpdir("glados-v1-dir-")
        shutil.copytree(LEGACY, t, dirs_exist_ok=True)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        pk = t / "product-knowledge"
        self.assertFalse((pk / "workflows" / "plan_feature.md").exists(),
                         "known v1 name (underscore variant) must be removed")
        self.assertFalse((pk / "modules").exists(),
                         "emptied v1 modules dir must be removed")
        self.assertTrue((pk / "workflows" / "my-notes.md").exists(),
                        "unmatched user file must survive")
        self.assertTrue((pk / "workflows").is_dir(),
                        "dir with surviving user files must not be removed")
        # non-direct modes leave the shared dirs alone
        t2 = tmpdir("glados-v1-cla-")
        shutil.copytree(LEGACY, t2, dirs_exist_ok=True)
        rc, out = install("claude", t2)
        self.assertEqual(rc, 0, out)
        self.assertTrue((t2 / "product-knowledge" / "workflows" /
                         "plan_feature.md").exists(),
                        "claude install must not touch product-knowledge/workflows")


class TestDisabledWorkflows(unittest.TestCase):

    MINIMAL = ("glados: 2\nplatform: gitlab\nphase: {phase}\n"
               "branching:\n  feature: \"feat/<slug>\"\n"
               "  default-target: main\n")

    def test_sunset_preset_disables_build_feature(self):
        t = make_target(self.MINIMAL.format(phase="sunset"))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertFalse(
            (t / "product-knowledge" / "glados" / "build-feature.md").exists(),
            "sunset must not install build-feature")
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("| disabled-workflows | [build-feature] | (phase:sunset) |",
                      report, report)

    def test_explicit_empty_list_overrides_preset(self):
        t = make_target(self.MINIMAL.format(phase="sunset")
                        + "disabled-workflows: []\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertTrue(
            (t / "product-knowledge" / "glados" / "build-feature.md").exists(),
            "explicit [] must re-enable what the preset disabled")
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("| disabled-workflows | [] | (explicit) |", report)

    def test_disabled_target_alias_shim_says_disabled(self):
        text = read(EXAMPLE).replace("disabled-workflows: []",
                                     "disabled-workflows: [fix-bug]")
        t = make_target(text)
        rc, out = install("claude", t)
        self.assertEqual(rc, 0, out)
        owned = t / ".claude" / "commands" / "glados"
        self.assertFalse((owned / "fix-bug.md").exists())
        shim = read(owned / "plan-fix.md")
        self.assertIn("disabled", shim)
        self.assertNotIn("Run `/glados:fix-bug`", shim,
                         "a disabled target must not be invoked")
        # direct mode's disabled shim says so too
        t2 = make_target(text)
        rc, out = install("direct", t2)
        self.assertEqual(rc, 0, out)
        self.assertIn("disabled",
                      read(t2 / "product-knowledge" / "glados" / "plan-fix.md"))
        # plugin mode: disabled alias stub says disabled AND keeps the
        # generated-stub marker so pruning can still identify it
        t3 = make_plugin_target()
        write(t3 / "glados.yaml.example", text)
        rc, out = install("claude-plugin", t3)
        self.assertEqual(rc, 0, out)
        stub = read(t3 / "skills" / "plan-fix" / "SKILL.md")
        self.assertIn("disabled", stub)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/compiled/claude-plugin/", stub)


class TestAliasShims(unittest.TestCase):

    def test_direct_and_aistudio_alias_shims(self):
        # direct: file-pointer bodies, never slash-command syntax
        t = make_target()
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        shim = read(t / "product-knowledge" / "glados" / "plan-fix.md")
        self.assertIn("product-knowledge/glados/fix-bug.md", shim)
        self.assertNotIn("/glados:", shim,
                         "direct mode has no slash-command syntax")
        self.assertTrue((t / "product-knowledge" / "glados" / "mission.md")
                        .exists(), "every alias gets a shim")
        # aistudio: pointer stubs beside the bundles
        t2 = make_target()
        rc, out = install("aistudio", t2)
        self.assertEqual(rc, 0, out)
        bundles = t2 / "glados" / "adapters" / "aistudio" / "bundles"
        self.assertIn("bundles/fix-bug-advisory.aistudio.md",
                      read(bundles / "plan-fix.aistudio.md"))
        self.assertIn("bundles/intent.aistudio.md",
                      read(bundles / "mission.aistudio.md"))
        self.assertIn("no AI Studio bundle",
                      read(bundles / "autonomous-loop.aistudio.md"))
        # alias stubs are pointers, not bundles: they stay out of MANIFEST.md
        manifest = read(bundles.parent / "MANIFEST.md")
        self.assertNotIn("mission", manifest)


class TestParamsResolution(unittest.TestCase):

    def test_baseline_evaluator_bound_resolves(self):
        # A minimal manifest with no params: block still resolves both loop
        # bounds (and the roster) from the baseline preset.
        manifest = ("glados: 2\nplatform: gitlab\nphase: evolving\n"
                    "branching:\n  feature: \"feat/<slug>\"\n"
                    "  default-target: main\n")
        t = make_target(manifest)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("| params.evaluator.max-cycles | 3 | (baseline) |", report)
        self.assertIn("| params.review-panel.personas | [] | (baseline) |", report)

    def test_dangling_params_literal_fails(self):
        # A compiled `params.<ns>.<key>` literal with no resolved value is an
        # unbounded loop / missing roster — the install must refuse it.
        src = make_doctored_source()
        core = src / "src" / "workflows" / "intent.md"
        core.write_text(read(core) + "\nBound: `params.bogus.max-cycles`.\n",
                        encoding="utf-8", newline="\n")
        rc, out = install("direct", make_target(), source=src)
        self.assertEqual(rc, 1, out)
        self.assertIn("params.bogus.max-cycles", out)
        self.assertIn("intent", out)
        self.assertNotIn("Traceback", out)


class TestVerifyLedger(unittest.TestCase):

    def test_verify_ledger_reports(self):
        # Init a git repo with an agent-authored branch and no run records.
        t = tmpdir("glados-ledger-")
        shutil.copyfile(EXAMPLE, t / "glados.yaml")

        def git(*a):
            subprocess.run(["git", "-C", str(t), *a], capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("checkout", "-q", "-b", "main")
        (t / "a.txt").write_text("x", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "feat: initial")
        git("checkout", "-q", "-b", "feat/thing")
        (t / "b.txt").write_text("y", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "feat: work without a run record")
        rc, out = run_cli("verify-ledger", "--target", t)
        self.assertEqual(rc, 0)
        self.assertIn("run-record commits", out)


class TestYamlParserAttacks(unittest.TestCase):
    """Malformed-manifest attacks: each must die with a line-numbered
    YamlError, never a silent misparse or a raw traceback."""

    def test_yaml_tab_indent_fatal(self):
        # A tab-indented sub-map silently re-parents keys if tolerated.
        t = make_target("glados: 2\nplatform: gitlab\nphase: evolving\n"
                        "branching:\n\tfeature: x\n\tdefault-target: main\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, "tab indentation must be fatal:\n" + out)
        self.assertIn("glados.yaml:5", out)
        self.assertIn("tab", out.lower())
        self.assertNotIn("Traceback", out)

    def test_yaml_flow_map_fatal(self):
        t = make_target("glados: 2\nplatform: gitlab\nphase: evolving\n"
                        "branching: {feature: x, default-target: main}\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, "flow map must be fatal:\n" + out)
        self.assertIn("glados.yaml:4", out)
        self.assertIn("flow mapping", out)
        self.assertNotIn("Traceback", out)

    def test_yaml_duplicate_key_fatal(self):
        t = make_target("glados: 2\nplatform: gitlab\nphase: evolving\n"
                        "phase: nascent\nbranching:\n  default-target: main\n")
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, "duplicate key must be fatal:\n" + out)
        self.assertIn("glados.yaml:4", out)
        self.assertIn("duplicate key 'phase'", out)
        self.assertNotIn("Traceback", out)

    def test_yaml_quoted_comma_flow_list(self):
        parsed = glados.parse_yaml(
            'personas: ["security, compliance", architect]\n', "t")
        self.assertEqual(parsed["personas"], ["security, compliance", "architect"])

    def test_manifest_bom_tolerated(self):
        # PowerShell 5.1 writes a BOM even for `-Encoding utf8`.
        t = tmpdir()
        (t / "glados.yaml").write_bytes(
            b"\xef\xbb\xbf" + EXAMPLE.read_bytes())
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, "BOM manifest must install:\n" + out)
        # and the vendored hash agrees with a re-check (no false stale)
        rc, out = run_cli("check", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0, out)

    def test_manifest_utf16_fatal_names_fix(self):
        t = tmpdir()
        text = read(EXAMPLE)
        (t / "glados.yaml").write_bytes(text.encode("utf-16"))
        rc, out = install("direct", t)
        self.assertEqual(rc, 1)
        self.assertIn("glados.yaml", out)
        self.assertIn("UTF-8", out)
        self.assertNotIn("Traceback", out)


class TestManifestTokenAttacks(unittest.TestCase):
    """Typo'd manifest tokens must fail the install, not silently drop
    modules/workflows/sinks — the v1 flagship bug class."""

    def test_unknown_module_in_manifest_fails(self):
        # a) per-workflow list on a core with NO requires (nothing else catches it)
        text = read(EXAMPLE).replace(
            "workflows:\n", "workflows:\n  intent:         [standards-gatee]\n")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "module typo must fail:\n" + out)
        self.assertIn("standards-gatee", out)
        self.assertIn("intent", out)
        # b) default-modules typo drops the module from EVERY workflow
        text = read(EXAMPLE).replace("default-modules: [standards-gate]",
                                     "default-modules: [standard-gate]")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "default-modules typo must fail:\n" + out)
        self.assertIn("standard-gate", out)
        # c) unknown workflow name in workflows:
        text = read(EXAMPLE).replace(
            "workflows:\n", "workflows:\n  reviw-mr:       [mr-review-panel]\n")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "workflow-name typo must fail:\n" + out)
        self.assertIn("reviw-mr", out)
        # d) unknown workflow in disabled-workflows leaves it enabled
        text = read(EXAMPLE).replace("disabled-workflows: []",
                                     "disabled-workflows: [run-epicc]")
        self.assertIn("run-epicc", text, "example must carry the key to edit")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "disabled-workflows typo must fail:\n" + out)
        self.assertIn("run-epicc", out)

    def test_unknown_channel_type_or_sink_fails(self):
        # a) typo'd outcome type key
        text = read(EXAMPLE).replace("  observation: [ledger]",
                                     "  observaton: [ledger]")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "channels outcome-type typo must fail:\n" + out)
        self.assertIn("observaton", out)
        # b) typo'd sink on a ledger-ok type (previously invisible)
        text = read(EXAMPLE).replace("  progress:    [ledger]",
                                     "  progress:    [legder]")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "channels sink typo must fail:\n" + out)
        self.assertIn("legder", out)

    def test_declared_custom_sink_passes(self):
        # A team-declared sink, bound alongside a built-in, installs cleanly.
        text = read(EXAMPLE).replace(
            "  verdict:     [mr-comment]", "  verdict:     [mr-comment, slack]")
        text += '\nsinks:\n  slack:\n    channel: "#code-reviews"\n    format: terse\n'
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 0, "declared custom sink must install:\n" + out)

    def test_undeclared_sink_in_channels_fails(self):
        # Binding a sink that is neither built-in nor declared is a typo/hole.
        text = read(EXAMPLE).replace(
            "  verdict:     [mr-comment]", "  verdict:     [mr-comment, slack]")
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "undeclared sink must fail:\n" + out)
        self.assertIn("slack", out)
        self.assertIn("not declared", out)

    def test_custom_team_visible_sink_satisfies_visibility(self):
        # A declared sink is team-visible by default, so it alone satisfies a
        # non-ledger-ok outcome's visibility requirement.
        text = read(EXAMPLE).replace(
            "  verdict:     [mr-comment]", "  verdict:     [slack]")
        text += '\nsinks:\n  slack:\n    channel: "#code-reviews"\n'
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 0, "team-visible custom sink must satisfy:\n" + out)

    def test_record_only_sink_alone_fails_visibility(self):
        # team-visible: false opts a sink out; alone on verdict it must fail the
        # same team-visibility check the built-in ledger fails.
        text = read(EXAMPLE).replace(
            "  verdict:     [mr-comment]", "  verdict:     [audit-log]")
        text += '\nsinks:\n  audit-log:\n    team-visible: false\n'
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "record-only sink alone must fail:\n" + out)
        self.assertIn("team-visible", out)

    def test_malformed_sink_body_fails_cleanly(self):
        # A scalar where a sink config belongs must be a clean install error,
        # never a stack trace from the visibility helpers.
        text = read(EXAMPLE).replace(
            "  verdict:     [mr-comment]", "  verdict:     [mr-comment, slack]")
        text += '\nsinks:\n  slack: "#code-reviews"\n'
        rc, out = install("direct", make_target(text))
        self.assertEqual(rc, 1, "malformed sink body must fail:\n" + out)
        self.assertIn("slack", out)
        self.assertNotIn("Traceback", out)

    def test_alias_shadowing_core_fails(self):
        src = make_doctored_source()
        aliases = src / "src" / "kernel" / "aliases.yaml"
        aliases.write_text(read(aliases) + "  intent: run-epic\n",
                           encoding="utf-8", newline="\n")
        t = make_target()
        rc, out = install("claude", t, source=src)
        self.assertEqual(rc, 1, "core-shadowing alias must fail:\n" + out)
        self.assertIn("shadows", out)
        self.assertIn("intent", out)


class TestRootCauseSynthesis(unittest.TestCase):

    def test_synthesis_step_compiled_into_review_mr(self):
        # The postamble is mandatory and unconditional: it must land in the
        # compiled review-mr text, with both checks and the panel's root-cause
        # field, on a stock manifest.
        t = make_target(read(EXAMPLE))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        core = read(t / "product-knowledge" / "glados" / "review-mr.md")
        self.assertIn("Root-cause synthesis (mandatory)", core)
        self.assertIn("Did the change attack the underlying cause?", core)
        self.assertIn("Does the finding set converge on one cause?", core)
        self.assertIn("root-cause", core)

    def test_synthesis_runs_before_the_decide_step(self):
        # Ordering is the whole point: synthesize, then decide. A compile that
        # puts the decision first has lost the postamble.
        t = make_target(read(EXAMPLE))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        core = read(t / "product-knowledge" / "glados" / "review-mr.md")
        self.assertLess(core.index("Root-cause synthesis (mandatory)"),
                        core.index("### 8. Decide"))

    def test_address_review_consumes_the_synthesis(self):
        # The synthesis must not land inert: the fix half is told to treat a
        # consolidated cluster as one design change.
        t = make_target(read(EXAMPLE))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        core = read(t / "product-knowledge" / "glados" / "address-review.md")
        self.assertIn("root-cause synthesis", core)

    def test_synthesis_introduces_no_new_verdict_vocabulary(self):
        # One severity scale, one verdict vocabulary - the synthesis reuses
        # them rather than inventing a third tier or a fourth verdict word.
        body = read(REPO / "src" / "vocabulary" / "root-cause.md")
        for invented in ("CONVERGED", "ROOT_CAUSE", "SYMPTOM", "structural`"):
            self.assertNotIn(invented, body)
        self.assertIn("`blocking`", body)
        self.assertIn("`advisory`", body)


class TestSinkConfig(unittest.TestCase):
    """Sink bodies are freeform config the agent interprets, but a key that
    cannot possibly do anything must fail the install instead of resolving to
    a silent no-op, and a multi-line body must not tear apart the assembly
    report's table."""

    MULTILINE = (
        "sinks:\n"
        "  mr-comment:\n"
        "    grouping: aggregate\n"
        "    voice: |\n"
        "      Lead with the verdict.\n"
        "      One finding per paragraph | even with a pipe in it.\n"
        "\n"
        "      No praise, no emoji headers.\n"
    )

    def _manifest(self, extra=""):
        return read(EXAMPLE) + "\n" + extra

    def test_team_visible_rejected_on_builtin(self):
        # BUILTIN_SINKS fixes a built-in's visibility and _sink_is_team_visible
        # never reads the body for one, so accepting the key would install
        # clean and change nothing.
        t = make_target(self._manifest(
            "sinks:\n  mr-comment:\n    team-visible: false\n"))
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, out)
        self.assertIn("mr-comment", out)
        self.assertIn("built-in", out)

    def test_team_visible_honored_on_a_declared_sink(self):
        # The same key on a sink the project owns is real config, not a no-op:
        # reject the built-in case without over-rejecting this one.
        t = make_target(self._manifest(
            "sinks:\n  audit-log:\n    team-visible: false\n"))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("audit-log", report)
        self.assertIn("no (record-only)", report)

    def test_multiline_body_keeps_the_report_table_intact(self):
        t = make_target(self._manifest(self.MULTILINE))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        report = read(t / ".glados" / "assembly-report.md")
        section = self._sinks_section(report)
        header = next(i for i, l in enumerate(section)
                      if l.startswith("| Sink |"))
        # The section's own prose must state the rule the epilogue implements,
        # in full: this sentence spans several string literals and a partial
        # edit leaves a grammatical-looking half-sentence behind.
        intro = " ".join(l for l in section[:header] if l.strip())
        self.assertIn("a bound sink that fails to receive its outcome escalates",
                      intro)
        self.assertNotIn("reaches no team-visible sink", intro)
        rows = [l for l in section[header:] if l.strip()]
        # A raw newline in a cell ends the ROW: the body's remaining lines
        # would land here as non-table text.
        for line in rows:
            self.assertTrue(line.startswith("|"),
                            f"torn table — stray line in the Sinks table: {line!r}")
        mr = [l for l in rows if l.startswith("| mr-comment ")]
        self.assertEqual(len(mr), 1, rows)
        self.assertIn("lines)", mr[0], "multi-line body must be summarized")
        self.assertNotIn("No praise", mr[0], "body must not be inlined")

    def test_compiled_core_escalates_on_a_failed_sink(self):
        # The compiled core is the artifact the agent actually reads; a bound
        # sink that fails must escalate, never downgrade to a warning because
        # some other sink got a copy.
        t = make_target()
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        core = read(t / "product-knowledge" / "glados" / "review-mr.md")
        self.assertIn("emit an `escalation`", core)
        self.assertNotIn("as a warning and continue", core)

    @staticmethod
    def _sinks_section(report: str) -> list:
        lines = report.splitlines()
        start = lines.index("## Sinks")
        out = []
        for line in lines[start + 1:]:
            if line.startswith("## "):
                break
            out.append(line)
        return out


class TestSourceTreeAttacks(unittest.TestCase):

    def test_include_cycle_fatal_names_cycle(self):
        src = make_doctored_source()
        vocab = src / "src" / "vocabulary"
        write(vocab / "cycle-a.md", "<!-- glados:include vocabulary/cycle-b.md -->\n")
        write(vocab / "cycle-b.md", "<!-- glados:include vocabulary/cycle-a.md -->\n")
        core = src / "src" / "workflows" / "intent.md"
        core.write_text(
            read(core) + "\n<!-- glados:include vocabulary/cycle-a.md -->\n",
            encoding="utf-8", newline="\n")
        rc, out = install("direct", make_target(), source=src)
        self.assertEqual(rc, 1)
        self.assertIn("cycle", out)
        self.assertIn("cycle-a.md", out)
        self.assertNotIn("Traceback", out)

    def test_missing_kernel_file_fatal(self):
        src = make_doctored_source()
        (src / "src" / "kernel" / "aliases.yaml").unlink()
        rc, out = install("direct", make_target(), source=src)
        self.assertEqual(rc, 1)
        self.assertIn("aliases.yaml", out)
        self.assertNotIn("Traceback", out)

    def test_include_escape_fatal(self):
        # A ../ include resolving OUTSIDE src/ must die naming the directive,
        # even though the escaped-to file exists.
        src = make_doctored_source()
        core = src / "src" / "workflows" / "intent.md"
        core.write_text(
            read(core) + "\n<!-- glados:include ../glados.yaml.example -->\n",
            encoding="utf-8", newline="\n")
        rc, out = install("direct", make_target(), source=src)
        self.assertEqual(rc, 1, out)
        self.assertIn("escapes the source tree", out)
        self.assertIn("../glados.yaml.example", out)
        self.assertNotIn("Traceback", out)

    def test_frontmatter_type_validation(self):
        # a) non-string description must fail with the file + field named,
        # not crash an adapter with a raw AttributeError.
        src = make_doctored_source()
        core = src / "src" / "workflows" / "intent.md"
        core.write_text(
            read(core).replace(
                "description: Establish or refresh the product mission and roadmap",
                "description: [not, a, string]"),
            encoding="utf-8", newline="\n")
        rc, out = install("gemini", make_target(), source=src)
        self.assertEqual(rc, 1, out)
        self.assertIn("description", out)
        self.assertIn("intent.md", out)
        self.assertNotIn("Traceback", out)
        # b) a bare-string list field must fail (list() on a str would
        # silently produce a per-character list)
        src2 = make_doctored_source()
        core2 = src2 / "src" / "workflows" / "fix-bug.md"
        core2.write_text(
            read(core2).replace("reads: [manifest.branching, manifest.platform]",
                                "reads: manifest.branching"),
            encoding="utf-8", newline="\n")
        rc, out = install("direct", make_target(), source=src2)
        self.assertEqual(rc, 1, out)
        self.assertIn("reads", out)
        self.assertIn("fix-bug.md", out)
        self.assertNotIn("Traceback", out)


class TestDoctor(unittest.TestCase):

    def test_doctor_never_fails(self):
        # doctor's contract is 'never fails': a malformed manifest and an
        # unreadable CI file become report lines, not exits.
        t = make_target("glados: 2\nplatform: gitlab\nphase: evolving\n"
                        "branching:\n\tfeature: x\n")  # tab -> YamlError
        (t / ".gitlab-ci.yml").write_bytes("glados\n".encode("utf-16"))
        rc, out = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0, "doctor must never fail:\n" + out)
        self.assertIn("phase: UNREADABLE", out)
        self.assertIn("CI check wired: UNREADABLE", out)
        self.assertIn("never fails", out)
        self.assertNotIn("Traceback", out)

    def test_doctor_reports_codeowners(self):
        t = make_target()
        rc, out = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0)
        self.assertIn("CODEOWNERS: none found", out)
        write(t / "CODEOWNERS", "/glados.yaml @maintainers\n")
        rc, out = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0)
        self.assertIn("covers glados.yaml", out)
        write(t / "CODEOWNERS", "* @maintainers\n")
        rc, out = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0)
        self.assertIn("does not mention glados.yaml", out)


class TestSda(unittest.TestCase):
    """The explicit-only `sda:` manifest key — install-time scaffolding,
    the assembly-report row, doctor status, and the two guardrails (presets
    may never set it; non-bool values die naming the key)."""

    def _sda_manifest(self) -> str:
        text = read(EXAMPLE)
        self.assertIn("sda: false", text,
                      "glados.yaml.example must document sda: false")
        return text.replace("sda: false", "sda: true")

    def test_sda_true_install_scaffolds_all(self):
        t = make_target(self._sda_manifest())
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        # claims.md at the repo root, from the template, dated today
        claims = read(t / "claims.md")
        self.assertIn("# Claims", claims)
        self.assertIn("SDA: v1.0", claims)
        self.assertNotIn("YYYY-MM-DD", claims, "template date must be filled")
        self.assertIn(datetime.date.today().isoformat(), claims)
        # SPEC_LOG.md carries the work-unit-log table header whose columns
        # match the epilogue's row fields (date, workflow, scope, outcome,
        # links), dated today
        spec = read(t / "product-knowledge" / "SPEC_LOG.md")
        self.assertIn("| Date | Workflow | Scope | Outcome | Links |", spec)
        self.assertNotIn("YYYY-MM-DD", spec, "template date must be filled")
        self.assertIn(datetime.date.today().isoformat(), spec)
        # the freshly scaffolded PROJECT_STATUS.md gets the version header
        status = read(t / "product-knowledge" / "PROJECT_STATUS.md")
        self.assertTrue(status.startswith("<!-- SDA: v1.0 -->"), status[:80])
        # ROADMAP.md is NOT created — headers only stamp existing files
        self.assertFalse((t / "product-knowledge" / "ROADMAP.md").exists(),
                         "sda scaffolding must not invent a roadmap")
        # every shipped standards doc is copied byte-identically
        docs = sorted((REPO / "docs" / "standards").glob("sda-*.md"))
        self.assertGreater(len(docs), 0, "repo must ship sda standards docs")
        for d in docs:
            dest = t / "product-knowledge" / "standards" / d.name
            self.assertTrue(dest.exists(), f"missing scaffolded {d.name}")
            self.assertEqual(dest.read_bytes(), d.read_bytes(),
                             f"{d.name} must be copied byte-identically")
        # assembly report: the sda row plus the artifact listing
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("| sda | true | (explicit) |", report)
        self.assertIn("## SDA conformance", report)
        self.assertIn("claims.md", report)
        self.assertIn("SPEC_LOG.md", report)
        # the install output says what this run scaffolded
        self.assertIn("sda: true", out)
        self.assertIn("claims.md", out)
        # doctor reports status + artifact presence, informationally
        rc, dout = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0, dout)
        self.assertIn("sda: true", dout)
        self.assertIn("claims.md: present", dout)
        self.assertIn("SPEC_LOG.md: present", dout)

    def test_sda_false_install_unchanged(self):
        t = make_target()   # the example manifest carries sda: false
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertFalse((t / "claims.md").exists(),
                         "sda: false must scaffold no claims.md")
        self.assertFalse((t / "product-knowledge" / "SPEC_LOG.md").exists())
        std = t / "product-knowledge" / "standards"
        self.assertEqual(list(std.glob("sda-*.md")), [],
                         "sda: false must copy no standards docs")
        status = read(t / "product-knowledge" / "PROJECT_STATUS.md")
        self.assertNotIn("SDA: v1.0", status, "no header without sda: true")
        report = read(t / ".glados" / "assembly-report.md")
        self.assertIn("| sda | false | (explicit) |", report)
        self.assertNotIn("## SDA conformance", report)
        rc, dout = run_cli("doctor", "--target", t, "--source", REPO)
        self.assertEqual(rc, 0, dout)
        self.assertIn("sda: false", dout)

    def test_sda_reinstall_idempotent(self):
        t = make_target(self._sda_manifest())
        self.assertEqual(install("direct", t)[0], 0)
        snap1 = {f.relative_to(t).as_posix(): f.read_bytes() for f in all_files(t)}
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertIn("already present", out,
                      "re-install must report the artifacts as present")
        snap2 = {f.relative_to(t).as_posix(): f.read_bytes() for f in all_files(t)}
        self.assertEqual(snap1, snap2,
                         "sda re-install must be a byte-wise no-op")

    def test_sda_header_prepend_idempotent(self):
        # A pre-existing ROADMAP.md gets exactly one header, its content
        # survives byte-for-byte, and a re-install never stacks a second.
        t = make_target(self._sda_manifest())
        roadmap = t / "product-knowledge" / "ROADMAP.md"
        original = "# Roadmap\n\n## Phase 1: Foundation\n\n**Goal**: Ship.\n"
        write(roadmap, original)
        self.assertEqual(install("direct", t)[0], 0)
        text = read(roadmap)
        self.assertTrue(text.startswith("<!-- SDA: v1.0 -->"), text[:80])
        self.assertIn(original, text, "existing roadmap content must survive")
        self.assertEqual(text.count("SDA: v1.0"), 1)
        self.assertEqual(install("direct", t)[0], 0)
        self.assertEqual(read(roadmap), text,
                         "re-install must not stack a second header")
        # a doc already carrying the (multi-line) header form is left alone
        t2 = make_target(self._sda_manifest())
        headered = ("<!--\nSDA: v1.0\nLast Updated: 2026-01-01\n-->\n\n"
                    "# Roadmap\n")
        write(t2 / "product-knowledge" / "ROADMAP.md", headered)
        self.assertEqual(install("direct", t2)[0], 0)
        self.assertEqual(read(t2 / "product-knowledge" / "ROADMAP.md"),
                         headered, "an existing header must be respected")

    def test_preset_setting_sda_rejected(self):
        # sda is in the manifest schema, so the anti-inflation cap alone would
        # admit it — the explicit-only rule must reject a preset naming it.
        src = make_doctored_source()
        presets = src / "src" / "kernel" / "presets" / "phases.yaml"
        text = read(presets).replace("nascent:\n", "nascent:\n  sda: true\n")
        self.assertIn("nascent:\n  sda: true", text, "doctoring failed")
        presets.write_text(text, encoding="utf-8", newline="\n")
        rc, out = install("direct", make_target(), source=src)
        self.assertEqual(rc, 1, "a preset must never set sda:\n" + out)
        self.assertIn("sda", out)
        self.assertIn("nascent", out)
        self.assertIn("team declaration", out)
        self.assertNotIn("Traceback", out)

    def test_sda_non_bool_fatal(self):
        text = read(EXAMPLE).replace("sda: false", 'sda: "yes"')
        t = make_target(text)
        rc, out = install("direct", t)
        self.assertEqual(rc, 1, "non-bool sda must be fatal:\n" + out)
        self.assertIn("'sda'", out)
        self.assertIn("bool", out)
        self.assertIn("yes", out)
        self.assertNotIn("Traceback", out)


class TestInstallUx(unittest.TestCase):

    MINIMAL = ("glados: 2\nplatform: gitlab\nphase: evolving\n"
               "branching:\n  feature: \"feat/<slug>\"\n"
               "  default-target: main\n")

    def test_phase_transition_advisory_printed(self):
        t = make_target(self.MINIMAL)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("phase transition", out)
        write(t / "glados.yaml",
              self.MINIMAL.replace("phase: evolving", "phase: production"))
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertIn("phase transition evolving -> production", out)
        self.assertIn("smoke suite", out)
        self.assertIn("advisory", out)
        # a repeat install in the same phase prints no checklist
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("phase transition", out)
        # a backward move adds the confession line
        write(t / "glados.yaml", self.MINIMAL)
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        self.assertIn("phase transition production -> evolving", out)
        self.assertIn("backward move", out)

    def test_v1_leftovers_hint_before_missing_manifest_fatal(self):
        t = tmpdir("glados-v1-hint-")
        shutil.copytree(LEGACY, t, dirs_exist_ok=True)
        (t / "glados.yaml").unlink()
        rc, out = install("claude", t)
        self.assertEqual(rc, 1)
        self.assertIn("v1 -> v2 migration", out)   # the prefilled hint
        self.assertIn("MIGRATION.md", out)
        self.assertIn("no manifest", out)          # the Fatal still fires

    def test_scaffold_includes_personas(self):
        t = make_target()
        rc, out = install("direct", t)
        self.assertEqual(rc, 0, out)
        for sub in ("observations", "standards", "philosophies", "personas"):
            self.assertTrue(
                (t / "product-knowledge" / sub / ".gitkeep").exists(),
                f"product-knowledge/{sub}/ must be scaffolded")


class TestCheckResilience(unittest.TestCase):

    def test_check_report_only_survives_unresolvable_source(self):
        t = make_target()
        self.assertEqual(install("direct", t)[0], 0)
        empty = tmpdir("glados-empty-src-")
        rc, out = run_cli("check", "--target", t, "--source", empty,
                          "--report-only")
        self.assertEqual(rc, 0, "--report-only must not hard-exit:\n" + out)
        self.assertIn("report-only", out)
        self.assertIn("cannot resolve the GLaDOS source tree", out)
        self.assertNotIn("Traceback", out)
        # enforcing mode still fails, cleanly
        rc, out = run_cli("check", "--target", t, "--source", empty)
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve the GLaDOS source tree", out)
        self.assertNotIn("Traceback", out)


class TestPluginSkills(unittest.TestCase):

    def test_plugin_skills_pruned_by_marker_only(self):
        t = make_plugin_target()
        write(t / "skills" / "my-custom" / "SKILL.md",
              "---\ndescription: mine\n---\n\nA user skill, no marker.\n")
        write(t / "skills" / "stale-stub" / "SKILL.md",
              "---\ndescription: stale\n---\n\nRead "
              "`${CLAUDE_PLUGIN_ROOT}/compiled/claude-plugin/stale-stub.md`.\n")
        write(t / "skills" / "no-skill-md" / "notes.txt", "keep me\n")
        rc, out = install("claude-plugin", t)
        self.assertEqual(rc, 0, out)
        self.assertTrue((t / "skills" / "my-custom" / "SKILL.md").exists(),
                        "user skill without the marker must survive")
        self.assertTrue((t / "skills" / "no-skill-md" / "notes.txt").exists(),
                        "dir without a SKILL.md must survive")
        self.assertFalse((t / "skills" / "stale-stub").exists(),
                         "stale generated stub must be pruned by its marker")
        # every generated stub carries the marker, so a retired core/alias
        # can be identified structurally on the next install
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/compiled/claude-plugin/",
                      read(t / "skills" / "intent" / "SKILL.md"))
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/compiled/claude-plugin/",
                      read(t / "skills" / "mission" / "SKILL.md"))


class TestHookGuards(unittest.TestCase):
    """The run-record guard scripts, driven as real subprocesses through their
    three states: no marker / marker + uncommitted record / committed."""

    def _repo(self):
        t = tmpdir("glados-hooks-")

        def git(*a):
            subprocess.run(["git", "-C", str(t), *a],
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("checkout", "-q", "-b", "main")
        (t / "a.txt").write_text("x", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "feat: initial")
        return t, git

    def _run_claude(self, repo):
        # claude-stop-hook takes the repo root from the event's "cwd" field
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / "claude-stop-hook.py")],
            input=json.dumps({"cwd": str(repo)}),
            capture_output=True, text=True)

    def _run_gemini(self, repo):
        # gemini-afteragent-guard uses the process cwd
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / "gemini-afteragent-guard.py")],
            input="{}", capture_output=True, text=True, cwd=str(repo))

    def test_no_marker_allows(self):
        repo, _ = self._repo()
        p = self._run_claude(repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "", "must emit nothing (allow)")
        p = self._run_gemini(repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr.strip(), "", "must stay silent (allow)")

    def test_marker_with_uncommitted_record_blocks(self):
        repo, _ = self._repo()
        write(repo / ".glados" / "runs" / "current", "run-1.md\n")
        write(repo / ".glados" / "runs" / "run-1.md", "# run record\n")
        p = self._run_claude(repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        payload = json.loads(p.stdout)
        self.assertEqual(payload["decision"], "block")
        self.assertIn("run-1.md", payload["reason"])
        p = self._run_gemini(repo)
        self.assertEqual(p.returncode, 2, "exit 2 retries the gemini turn")
        self.assertIn("run-1.md", p.stderr)

    def test_marker_with_committed_record_allows(self):
        repo, git = self._repo()
        write(repo / ".glados" / "runs" / "run-1.md", "# run record\n")
        git("add", ".glados/runs/run-1.md")
        git("commit", "-q", "-m", "chore(glados): record test run")
        write(repo / ".glados" / "runs" / "current", "run-1.md\n")
        p = self._run_claude(repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "",
                         "committed record must not block")
        p = self._run_gemini(repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr.strip(), "",
                         "committed record must not retry the turn")

    def test_toml_special_chars_roundtrip(self):
        # Backslashes, quotes and a Python triple-quote inside a core body must
        # survive the gemini TOML byte-for-byte under tomllib.
        src = make_doctored_source()
        core = src / "src" / "workflows" / "intent.md"
        doctored = ("\n## Doctored edge cases\n\n"
                    "Windows path: C:\\Users\\test and regex \\d+\\.\\d+\n"
                    "Quotes: \"double\" and 'single' and '''triple''' inline.\n")
        core.write_text(read(core) + doctored, encoding="utf-8", newline="\n")
        t = make_target()
        rc, out = install("gemini", t, source=src)
        self.assertEqual(rc, 0, "special chars must not break gemini:\n" + out)
        data = tomllib.loads(read(t / ".gemini/commands/glados/intent.toml"))
        self.assertIn(doctored, data["prompt"], "content must be byte-preserved")

    def test_emitted_frontmatter_descriptions_safe(self):
        # A block-scalar description with colons/quotes/backslashes must emit
        # valid TOML and valid one-line YAML frontmatter in every adapter.
        src = make_doctored_source()
        core = src / "src" / "workflows" / "intent.md"
        nasty = ('description: |-\n  Establish mission: refresh "roadmap"\n'
                 "  and C:\\paths on line two")
        core.write_text(
            read(core).replace(
                "description: Establish or refresh the product mission and roadmap",
                nasty),
            encoding="utf-8", newline="\n")
        expected = 'Establish mission: refresh "roadmap" and C:\\paths on line two'
        # gemini: description must parse and round-trip
        t = make_target()
        rc, out = install("gemini", t, source=src)
        self.assertEqual(rc, 0, out)
        data = tomllib.loads(read(t / ".gemini/commands/glados/intent.toml"))
        self.assertEqual(data["description"], expected)
        # antigravity: frontmatter must be parseable and single-line
        t = make_target()
        rc, out = install("antigravity", t, source=src)
        self.assertEqual(rc, 0, out)
        fm, _ = glados.split_frontmatter(
            read(t / ".agents/workflows/glados-intent.md"), "glados-intent.md")
        self.assertEqual(fm["description"], expected)
        # claude-plugin: SKILL.md frontmatter must be parseable and single-line
        t = make_plugin_target()
        rc, out = install("claude-plugin", t, source=src)
        self.assertEqual(rc, 0, out)
        fm, _ = glados.split_frontmatter(
            read(t / "skills/intent/SKILL.md"), "SKILL.md")
        self.assertEqual(fm["description"], expected)


class TestInstallRobustness(unittest.TestCase):

    def test_readonly_file_in_owned_dir_overwritten(self):
        # A read-only leftover inside a glados-owned dir must be overwritten
        # (the dir is ours), not crash with a PermissionError traceback.
        t = make_target()
        stale = t / ".claude" / "commands" / "glados" / "intent.md"
        write(stale, "old read-only leftover\n")
        os.chmod(stale, 0o444)
        rc, out = install("claude", t)
        self.assertEqual(rc, 0, "read-only leftover must not break install:\n" + out)
        self.assertNotIn("old read-only leftover", read(stale))

    def test_owned_path_conflict_fatal_no_traceback(self):
        # .claude/commands/glados existing as a FILE blocks the owned dir.
        t = make_target()
        write(t / ".claude" / "commands" / "glados", "i am a file\n")
        rc, out = install("claude", t)
        self.assertEqual(rc, 1)
        self.assertIn("glados", out)
        self.assertNotIn("Traceback", out)

    def test_install_survives_legacy_console_encoding(self):
        # cp850 console (legacy Windows) cannot encode the report's em dashes;
        # printing must never crash an install that already wrote files.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp850"
        t = make_target()
        rc, out = install("direct", t, env=env)
        self.assertEqual(rc, 0, "cp850 console must not crash install:\n" + out)
        self.assertNotIn("UnicodeEncodeError", out)

    def test_reinstall_same_target_idempotent(self):
        t = make_target()
        self.assertEqual(install("claude", t)[0], 0)
        snap1 = {f.relative_to(t).as_posix(): f.read_bytes() for f in all_files(t)}
        self.assertEqual(install("claude", t)[0], 0)
        snap2 = {f.relative_to(t).as_posix(): f.read_bytes() for f in all_files(t)}
        self.assertEqual(snap1, snap2, "second install must be a byte-wise no-op")

    def test_planted_files_outside_owned_dirs_survive(self):
        # Files a user plants JUST OUTSIDE owned dirs must survive install +
        # cleanup; glados-prefixed strays INSIDE .agents/workflows are owned.
        t = tmpdir("glados-planted-")
        shutil.copytree(LEGACY, t, dirs_exist_ok=True)
        write(t / ".claude" / "commands" / "glados.md", "user file\n")
        write(t / ".gemini" / "commands" / "glados-mine.toml", "user toml\n")
        write(t / ".agents" / "workflows" / "my-workflow.md", "user workflow\n")
        write(t / ".agents" / "workflows" / "glados-stale-v1.md", "stale v1\n")
        for mode in ("claude", "gemini", "antigravity"):
            rc, out = install(mode, t)
            self.assertEqual(rc, 0, f"{mode}:\n{out}")
        self.assertTrue((t / ".claude" / "commands" / "glados.md").exists())
        self.assertTrue((t / ".gemini" / "commands" / "glados-mine.toml").exists())
        self.assertTrue((t / ".agents" / "workflows" / "my-workflow.md").exists())
        self.assertFalse(
            (t / ".agents" / "workflows" / "glados-stale-v1.md").exists(),
            "glados-prefixed stray inside the owned namespace must be cleaned")


class TestMigrate(unittest.TestCase):
    """The guided v1 -> v2 `migrate` command: detection + suggested mode,
    manifest generation (platform/sda auto-filled, phase left REQUIRED),
    specs/ -> run-record conversion with SPEC_LOG rows, idempotence,
    --dry-run, --clean, and create-only semantics end to end."""

    REC1 = ".glados/runs/2026-05-01-migrated-login-timeout.md"
    REC2 = ".glados/runs/2026-06-10-migrated-reporting.md"

    LOGIN_README = (
        "# Login timeout fix\n\nIntro line.\n\n## Findings\n\n"
        "- finding one\n- finding two\n- finding three\n- finding four\n"
        "- finding five\n- finding six\n\n## Outcome\n\n"
        "Shipped in MR !41.\n")
    REPORTING_README = ("# Reporting spec\n\nSuperseded by workspace "
                        "reports.\nOutcome: merged in MR !77.\n")

    def _v1_repo(self, remote="git@gitlab.com:acme/thing.git", specs=True,
                 sda=True):
        """A rich v1-era repo: git history, a gitlab origin, the v1 claude
        layout (namespaced + un-namespaced + a user file), SDA claims.md,
        and two date-prefixed specs/ dirs."""
        t = tmpdir("glados-migrate-")

        def git(*a):
            subprocess.run(["git", "-C", str(t), *a],
                           capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        git("checkout", "-q", "-b", "main")
        if remote:
            git("remote", "add", "origin", remote)
        write(t / "README.md", "# my project\n")
        write(t / ".claude" / "commands" / "plan-feature.md",
              "# Plan Feature\n\nv1 un-namespaced command body.\n")
        write(t / ".claude" / "commands" / "glados" / "plan_feature.md",
              "# Plan Feature\n\nv1 namespaced command body.\n")
        write(t / ".claude" / "commands" / "my-user-command.md",
              "a user command, not glados's\n")
        if sda:
            write(t / "claims.md", "<!-- SDA: v1.0 -->\n\n# Claims\n\n- one\n")
        if specs:
            write(t / "specs" / "2026-05-01_login-timeout" / "README.md",
                  self.LOGIN_README)
            write(t / "specs" / "2026-06-10_reporting" / "README.md",
                  self.REPORTING_README)
        git("add", "-A")
        git("commit", "-q", "-m", "v1 state")
        return t, git

    def _snapshot(self, t):
        return {f.relative_to(t).as_posix(): f.read_bytes()
                for f in all_files(t)
                if ".git" not in f.relative_to(t).parts}

    def _migrate(self, t, *extra):
        return run_cli("migrate", "--target", t, "--source", REPO, *extra)

    @staticmethod
    def _row(date, dirname, rec):
        return f"| {date} | migrate | specs/{dirname} | migrated | {rec} |"

    def test_dry_run_writes_nothing_and_prints_plan(self):
        t, _ = self._v1_repo()
        before = self._snapshot(t)
        rc, out = self._migrate(t, "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._snapshot(t), before,
                         "--dry-run must write nothing")
        self.assertFalse((t / "glados.yaml").exists())
        self.assertIn("suggested install mode: claude", out)
        self.assertIn("platform: gitlab", out)
        self.assertIn("sda: true", out)
        self.assertIn("2026-05-01-migrated-login-timeout.md", out)
        self.assertIn("2026-06-10-migrated-reporting.md", out)

    def test_generate_convert_fail_fast_then_install(self):
        t, _ = self._v1_repo()
        rc, out = self._migrate(t)
        self.assertEqual(rc, 0, out)
        # generated manifest: platform + sda auto-filled, phase left REQUIRED
        gy = read(t / "glados.yaml")
        self.assertIn("platform: gitlab", gy)
        self.assertIn("sda: true", gy)
        self.assertIn(glados.MIGRATE_PHASE_LINE, gy)
        # the generated text parses in the tool's own YAML subset, phase unset
        raw = glados.load_manifest(t / "glados.yaml")
        self.assertIsNone(raw.get("phase"))
        # fail-fast: install refuses until a human picks a phase
        rc, iout = install("claude", t)
        self.assertEqual(rc, 1, iout)
        self.assertIn("phase", iout)
        self.assertIn("nascent", iout)
        # converted records: title, date, excerpt (heading + tail), git pointer
        rec1 = read(t / self.REC1)
        self.assertIn("# Migrated spec: 2026-05-01_login-timeout", rec1)
        self.assertIn("Date: 2026-05-01 (from the directory name)", rec1)
        self.assertIn("# Login timeout fix", rec1)
        self.assertIn("Shipped in MR !41.", rec1)
        self.assertIn("git log", rec1)
        rec2 = read(t / self.REC2)
        self.assertIn("# Migrated spec: 2026-06-10_reporting", rec2)
        self.assertIn("Date: 2026-06-10", rec2)
        self.assertIn("merged in MR !77", rec2)
        # SPEC_LOG created from the template with one row per dir, newest first
        spec = read(t / "product-knowledge" / "SPEC_LOG.md")
        self.assertIn("| Date | Workflow | Scope | Outcome | Links |", spec)
        row_new = self._row("2026-06-10", "2026-06-10_reporting",
                            ".glados/runs/2026-06-10-migrated-reporting.md")
        row_old = self._row("2026-05-01", "2026-05-01_login-timeout",
                            ".glados/runs/2026-05-01-migrated-login-timeout.md")
        self.assertIn(row_new, spec)
        self.assertIn(row_old, spec)
        self.assertLess(spec.index(row_new), spec.index(row_old),
                        "SPEC_LOG rows must land newest-first")
        # fill phase -> the full install succeeds over the migrated tree
        write(t / "glados.yaml",
              gy.replace(glados.MIGRATE_PHASE_LINE, "phase: evolving"))
        self.assertEqual(
            glados.load_manifest(t / "glados.yaml")["phase"], "evolving")
        rec_bytes = (t / self.REC1).read_bytes()
        claims_bytes = (t / "claims.md").read_bytes()
        rc, iout = install("claude", t)
        self.assertEqual(rc, 0, iout)
        # converted records + carried-over SDA artifacts survive (create-only)
        self.assertEqual((t / self.REC1).read_bytes(), rec_bytes,
                         "run records must survive install untouched")
        self.assertEqual((t / "claims.md").read_bytes(), claims_bytes,
                         "an existing claims.md must never be clobbered")
        self.assertIn(row_old, read(t / "product-knowledge" / "SPEC_LOG.md"))

    def test_rerun_is_byte_wise_noop(self):
        t, _ = self._v1_repo()
        self.assertEqual(self._migrate(t)[0], 0)
        snap1 = self._snapshot(t)
        rc, out = self._migrate(t)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._snapshot(t), snap1,
                         "re-run must be a byte-wise no-op")
        self.assertIn("never overwritten", out)
        self.assertIn("record exists", out)

    def test_existing_manifest_never_overwritten(self):
        t, _ = self._v1_repo()
        shutil.copyfile(EXAMPLE, t / "glados.yaml")
        before = (t / "glados.yaml").read_bytes()
        rc, out = self._migrate(t)
        self.assertEqual(rc, 0, out)
        self.assertEqual((t / "glados.yaml").read_bytes(), before,
                         "an existing glados.yaml must never be rewritten")
        self.assertIn("never overwritten", out)
        # differences are reported: SDA artifacts exist but sda: false
        self.assertIn("sda: true", out)
        # conversion still runs; SPEC_LOG rows skipped (manifest sda wins)
        self.assertTrue((t / self.REC1).exists())
        self.assertFalse(
            (t / "product-knowledge" / "SPEC_LOG.md").exists(),
            "sda: false in the manifest must skip SPEC_LOG rows")
        self.assertIn("skipped (sda is not true)", out)

    def test_clean_removes_converted_and_v1_only(self):
        t, _ = self._v1_repo()
        rc, out = self._migrate(t, "--clean")
        self.assertEqual(rc, 0, out)
        # converted specs/ dirs are gone, records + everything else stay
        self.assertFalse((t / "specs").exists(),
                         "emptied specs/ tree must be removed")
        self.assertTrue((t / self.REC1).exists())
        self.assertTrue((t / self.REC2).exists())
        self.assertTrue((t / "glados.yaml").exists())
        self.assertTrue((t / "README.md").exists())
        self.assertTrue((t / "claims.md").exists())
        # v1 claude layout removed; the user's own command survives
        self.assertFalse((t / ".claude" / "commands" / "plan-feature.md").exists())
        self.assertFalse((t / ".claude" / "commands" / "glados").exists())
        self.assertTrue(
            (t / ".claude" / "commands" / "my-user-command.md").exists(),
            "--clean must never touch user files")

    def test_clean_never_removes_v2_artifacts(self):
        # After a real install, .claude/commands/glados/plan-feature.md is a
        # v1-KNOWN NAME with v2 content — the content check must protect it.
        t, _ = self._v1_repo()
        self.assertEqual(self._migrate(t)[0], 0)
        write(t / "glados.yaml",
              read(t / "glados.yaml").replace(glados.MIGRATE_PHASE_LINE,
                                              "phase: evolving"))
        self.assertEqual(install("claude", t)[0], 0)
        owned = t / ".claude" / "commands" / "glados"
        v2_files = sorted(p.name for p in owned.glob("*.md"))
        self.assertIn("plan-feature.md", v2_files)
        rc, out = self._migrate(t, "--clean")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(p.name for p in owned.glob("*.md")), v2_files,
                         "migrate --clean must never remove v2 compiled files")
        self.assertFalse((t / "specs").exists())

    def test_spec_dir_without_readme_converts(self):
        t, _ = self._v1_repo(specs=False)
        write(t / "specs" / "2026-04-01_no-readme" / "notes.txt", "notes\n")
        rc, out = self._migrate(t)
        self.assertEqual(rc, 0, out)
        rec = read(t / ".glados" / "runs" / "2026-04-01-migrated-no-readme.md")
        self.assertIn("# Migrated spec: 2026-04-01_no-readme", rec)
        self.assertIn("No README.md", rec)

    def test_generated_manifest_installs_under_every_phase(self):
        # The example's explicit decisions: block is written for its own
        # `phase: evolving` (it confesses exactly delete-code); carried
        # verbatim into a generated manifest it fails production/sunset
        # installs with relaxation errors the team never chose. Migrate must
        # comment those overrides out so the ONE human edit the guide
        # promises — fill phase:, any phase — always yields a green install.
        t, _ = self._v1_repo()
        self.assertEqual(self._migrate(t)[0], 0)
        gy = read(t / "glados.yaml")
        self.assertIn("# decisions:", gy)
        self.assertIn("#   schema-migration: record", gy)
        self.assertIn("# relaxation-acknowledged:", gy)
        self.assertNotIn("\ndecisions:", gy,
                         "the active decisions block must be commented out")
        raw = glados.load_manifest(t / "glados.yaml")
        self.assertNotIn("decisions", raw)
        self.assertNotIn("relaxation-acknowledged", raw)
        for phase in glados.PHASES:
            write(t / "glados.yaml",
                  gy.replace(glados.MIGRATE_PHASE_LINE, f"phase: {phase}"))
            rc, out = install("claude", t)
            self.assertEqual(rc, 0,
                             f"phase '{phase}' must install cleanly over the "
                             f"generated manifest:\n{out}")

    def test_platform_github_and_no_remote_todo(self):
        t, _ = self._v1_repo(remote="git@github.com:acme/thing.git")
        self.assertEqual(self._migrate(t)[0], 0)
        self.assertIn("platform: github", read(t / "glados.yaml"))
        t2, _ = self._v1_repo(remote=None)
        rc, out = self._migrate(t2)
        self.assertEqual(rc, 0, out)
        line = re.search(r"(?m)^platform:.*$", read(t2 / "glados.yaml")).group(0)
        self.assertIn("platform: gitlab", line,
                      "no remote must keep the example default")
        self.assertIn("TODO", line)

    def test_git_date_fallback_and_create_only_records(self):
        # a) no dirname date prefix -> the dir's last commit date is used
        t, git = self._v1_repo(specs=False)
        write(t / "specs" / "no-date-prefix" / "README.md", "# No date\n\nBody.\n")
        git("add", "-A")
        git("commit", "-q", "-m", "add spec")
        proc = subprocess.run(
            ["git", "-C", str(t), "log", "-1", "--format=%cs", "--",
             "specs/no-date-prefix"], capture_output=True, text=True)
        expected = proc.stdout.strip()
        self.assertRegex(expected, r"^\d{4}-\d{2}-\d{2}$")
        rc, out = self._migrate(t)
        self.assertEqual(rc, 0, out)
        rec = t / ".glados" / "runs" / f"{expected}-migrated-no-date-prefix.md"
        self.assertTrue(rec.exists(), out)
        self.assertIn("from git history", read(rec))
        # b) create-only: a pre-existing record wins, but its SPEC_LOG row
        # still lands (the dir counts as converted)
        t2, _ = self._v1_repo()
        write(t2 / self.REC1, "HUMAN OWNED RECORD\n")
        rc, out = self._migrate(t2)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(t2 / self.REC1), "HUMAN OWNED RECORD\n",
                         "an existing record must win (create-only)")
        self.assertIn("kept", out)
        self.assertIn("2026-05-01-migrated-login-timeout.md",
                      read(t2 / "product-knowledge" / "SPEC_LOG.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
