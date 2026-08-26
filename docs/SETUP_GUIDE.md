# GLaDOS + Wheatley Setup Guide

How to set up the full GLaDOS development stack on a project — from a bare machine to a working Kanban board backed by your repo's markdown files.

---

## Prerequisites

Install these on your machine before starting:

| Tool | Purpose | Install |
|------|---------|---------|
| **Git** | Version control | [git-scm.com](https://git-scm.com) |
| **Docker** | Runs Wheatley (board UI) | [docker.com](https://www.docker.com/products/docker-desktop) |
| **An LLM agent** | Runs GLaDOS workflows | Claude Code, Antigravity, or Gemini CLI |

Claude Code is the primary supported agent. Install it via:

```bash
npm install -g @anthropic-ai/claude-code
```

---

## Step 1: Clone GLaDOS

Clone the GLaDOS framework repo. This is the source — it doesn't get installed globally; you run the installer from it into each project.

```bash
git clone https://github.com/cruxdigital-llc/GLaDOS.git ~/GLaDOS
```

You'll reference this path when installing into projects.

---

## Step 2: Install GLaDOS into Your Project

Navigate to your project repo and run the installer. Pick your agent mode and whether you want SDA conformance.

### Fresh project (no existing GLaDOS)

```bash
cd /path/to/your/project

# Basic install (Claude Code)
~/GLaDOS/bin/glados-install.sh --mode claude

# Or with SDA conformance (recommended for new projects)
~/GLaDOS/bin/glados-install.sh --mode claude --sda
```

### Existing GLaDOS project — adding SDA

If your project already has `product-knowledge/` from a previous GLaDOS install but doesn't have SDA artifacts:

```bash
~/GLaDOS/bin/glados-install.sh --mode claude --sda --target /path/to/your/project
```

This is safe on brownfield projects:
- `claims.md` — created at repo root only if it doesn't exist
- `product-knowledge/SPEC_LOG.md` — created only if it doesn't exist
- `product-knowledge/ROADMAP.md` — if it already exists, an SDA version header is prepended (content is preserved)
- `product-knowledge/PROJECT_STATUS.md` — SDA header injected if missing (content preserved)
- SDA standard and profile docs — always copied to `product-knowledge/standards/` (reference material, safe to overwrite)

### What the installer creates

After installation, your project will have:

```
your-project/
├── .claude/commands/glados/    # GLaDOS workflow commands (Claude mode)
├── product-knowledge/
│   ├── PROJECT_STATUS.md       # Current project state
│   ├── ROADMAP.md              # Strategic roadmap (SDA-conformant if --sda)
│   ├── SPEC_LOG.md             # Archived spec history (if --sda)
│   ├── personas/               # PM, Architect, QA + your custom personas
│   ├── standards/              # Coding rules (+ SDA docs if --sda)
│   ├── philosophies/           # Design principles
│   ├── observations/           # Pattern observer staging
│   └── overlays/               # Local workflow customizations
├── claims.md                   # Multi-agent coordination (if --sda)
└── specs/                      # Work unit directories (created by workflows)
```

### Other agent modes

```bash
# Antigravity (IDE agent)
~/GLaDOS/bin/glados-install.sh --mode antigravity --sda

# Gemini CLI
~/GLaDOS/bin/glados-install.sh --mode gemini --sda
```

---

## Step 3: Initialize Your Project

Open your project in your agent (e.g., `claude` in the project directory) and choose a path:

### New project — build from scratch

```
/glados:mission           # Define why this project exists
/glados:plan-product      # Create ROADMAP.md and TECH_STACK.md
/glados:establish-standards   # Document 2-3 key coding standards
/glados:plan-feature      # Start your first feature
```

### Existing project — onboard GLaDOS

```
/glados:adopt-codebase    # Full guided onboarding (recommended)
```

Or step by step:

```
/glados:review-codebase       # Analyze structure, populate PROJECT_STATUS.md
/glados:establish-standards   # Extract tribal knowledge into standards/
/glados:mission               # Align the agent with your project's purpose
```

---

## Step 4: Set Up Wheatley (Project Board)

Wheatley gives you a visual Kanban board that reads directly from your repo's markdown files. No separate database — git is the source of truth.

### Clone Wheatley

```bash
git clone https://github.com/cruxdigital-llc/GLaDOS-Wheatley.git ~/Wheatley
```

### Run against your project (sidecar mode)

```bash
cd ~/Wheatley
REPO_PATH=/path/to/your/project docker compose up server frontend
```

Then open [http://localhost:5173](http://localhost:5173) (frontend dev server) or [http://localhost:3000](http://localhost:3000) (API server with built-in UI).

### Run against a GitHub repo (cloud mode)

```bash
cd ~/Wheatley
WHEATLEY_MODE=remote \
GITHUB_TOKEN=ghp_your_token \
GITHUB_OWNER=your-org \
GITHUB_REPO=your-repo \
docker compose up server frontend
```

### What Wheatley shows you

| Markdown Artifact | Board Representation |
|---|---|
| ROADMAP.md items | Cards in the **Unclaimed** column |
| specs/ directories | Cards in **Planning** through **Done** (detected by which files exist) |
| claims.md entries | "Assigned to" badges on cards |
| PROJECT_STATUS.md | Active task overlay |
| SPEC_LOG.md | Historical record (archived cards no longer appear on the board) |

### Enabling GLaDOS workflows from Wheatley

If you want the board to trigger GLaDOS workflows (Plan, Spec, Implement, Verify buttons on cards), set the GLaDOS command:

```bash
WHEATLEY_GLADOS_CMD=claude REPO_PATH=/path/to/your/project docker compose up server frontend
```

This lets Wheatley spawn `claude -p "Run /glados:plan-feature for card 1.2.3"` when you click workflow buttons.

---

## Step 5: Verify SDA Conformance

If you installed with `--sda`, verify your project is conformant:

### Check file presence

```bash
# Required for SDA Full conformance:
ls product-knowledge/ROADMAP.md      # Roadmap with hierarchical IDs
ls product-knowledge/PROJECT_STATUS.md  # Status document
ls claims.md                          # Claims file
ls product-knowledge/SPEC_LOG.md     # Work unit log

# Check SDA headers
head -3 product-knowledge/ROADMAP.md        # Should show <!-- SDA: v1.0 -->
head -3 product-knowledge/PROJECT_STATUS.md # Should show <!-- SDA: v1.0 -->
```

### Check ROADMAP.md structure

Your ROADMAP should follow the SDA three-level hierarchy:

```markdown
## Phase 1: Foundation          <-- Phase (H2)

**Goal**: Core infrastructure.

### 1.1 Authentication          <-- Section (H3)

- [x] 1.1.1 Token generation   <-- Item (checkbox with P.S.I ID)
- [ ] 1.1.2 OAuth integration
```

### Reference docs

The SDA standard and GLaDOS profile are in your project at:

```
product-knowledge/standards/sda-standard-v1.md
product-knowledge/standards/sda-profile-glados-v1.md
```

---

## Updating

### Update GLaDOS workflows

When the GLaDOS repo gets new features, pull and re-run the installer:

```bash
cd ~/GLaDOS && git pull
~/GLaDOS/bin/glados-install.sh --mode claude --sda --target /path/to/your/project
```

Or use the update script (auto-detects mode):

```bash
~/GLaDOS/bin/glados-update.sh --sda --target /path/to/your/project
```

### Update Wheatley

```bash
cd ~/Wheatley && git pull
docker compose up --build server frontend
```

---

## Troubleshooting

### "ROADMAP.md not found" errors in Wheatley

Your project needs a `product-knowledge/ROADMAP.md`. Run `/glados:plan-product` to create one, or install with `--sda` to get a template.

### Wheatley shows no cards

- Check that your ROADMAP.md follows the `## Phase N: ... ### N.M ... - [ ] N.M.K Title` format
- Run `curl http://localhost:3000/api/board` to see the raw board state and any parse warnings

### Workflow buttons do nothing

Set `WHEATLEY_GLADOS_CMD=claude` (or your agent command) in the Docker environment. The agent must be available inside the container or on the host.

### SDA header injection failed

If your existing ROADMAP.md or PROJECT_STATUS.md already has an `<!-- SDA: v1.0 -->` comment, the installer skips injection (idempotent). Check with `grep "SDA: v1.0" product-knowledge/ROADMAP.md`.

---

## Quick Reference

| Task | Command |
|------|---------|
| Install GLaDOS (fresh) | `~/GLaDOS/bin/glados-install.sh --mode claude --sda` |
| Install GLaDOS (existing project) | `~/GLaDOS/bin/glados-install.sh --mode claude --sda --target <path>` |
| Update GLaDOS | `~/GLaDOS/bin/glados-update.sh --sda --target <path>` |
| Start Wheatley (local) | `REPO_PATH=<path> docker compose up server frontend` |
| Start Wheatley (remote) | Set `WHEATLEY_MODE=remote` + GitHub vars |
| New project setup | `/glados:mission` then `/glados:plan-product` |
| Existing project onboarding | `/glados:adopt-codebase` |
| Start a feature | `/glados:plan-feature` |
| Archive a done card | Wheatley UI Archive button, or `POST /api/cards/:id/archive` |
