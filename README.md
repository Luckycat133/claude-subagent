# claude-subagent

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)

English | [简体中文](README.zh-CN.md)

**Skill + CLI architecture that turns Claude Code into a persistent, worktree-isolated, context-shielding subagent pool for any AI coding agent.**

Works out of the box with **native Claude Code**—no extra routing tool required. Pairs even better with [`crouter`](https://github.com/Luckycat133/crouter) if you want multi-provider, local Ollama, or free-tier model routing.

Compatible with **Antigravity**, **Codex**, **Cursor**, **Windsurf**, **Gemini CLI**, **OpenClaw**, **Cline**, and **Claude Code** itself—with **zero permanent MCP server overhead** and **zero third-party Python dependencies**.

```text
          +--------------------------------------------------+
          |         Any Orchestrator Agent / Developer       |
          |  - Checks persisted config & usage policy        |
          |  - Plans minimal team (2-4 workers) & scopes     |
          |  - Dispatches outcome briefs via `claude-subagent`|
          |  - Reviews report + actual `git diff`            |
          +-------------------------+------------------------+
                                    |
        Persistent default route    | Structured report, telemetry,
        + `--title` + `-w <branch>` | run-delta Git diff & `#1` ID
                                    v
          +--------------------------------------------------+
          |        Headless Claude Code Worker Pool          |
          |  - Native `claude` OR paired with `crouter`      |
          |  - Isolated process group (clean killpg timeout) |
          |  - Optional git worktree (`-w`) isolation        |
          |  - Numbered multi-turn sessions (`#1`, `#2`)     |
          +--------------------------------------------------+
```

## Key Features

- **Mandatory first-run setup & persistent policy** (`~/.config/claude-subagent/config.json`): On first run, discovers configured models (`claude` native and optional `crouter` providers), prompts the user to confirm a persistent default route + delegation/usage policy, and remembers it across every future agent session—so no subagent ever silently picks an unconfirmed model.
- **Works great standalone, even better with `crouter`**: Runs native `claude` out of the box. When [`crouter`](https://github.com/Luckycat133/crouter) is installed, supports provider/model selection and optional per-scenario overrides while always passing `--once` so your global `crouter use` default stays untouched.
- **Unattended execution with scoped safety**: Defaults to `bypassPermissions` so headless workers never stall on interactive TTY prompts (preserving the token-saving purpose of subagents), while enforcing safety via `--allowed-tools readonly|coding`, `-w <branch>` worktree isolation, and `--timeout`/`--budget`.
- **Zero idle token tax (`Skill + CLI`)**: No always-on MCP schemas; progressive Agent Skill (`skills/claude-subagent/`) teaches any host agent when to spin up a team proactively, how to write concise outcome-oriented briefs, and how to recover from exceptions.
- **Parallel-safe & clean teardown**: File-locked atomic numbered sessions (`#1`, `#2`, `-r 1`, `-c`), dirty-tree run-delta Git diffing, and process-group timeout cleanup (`SIGINT → SIGTERM → SIGKILL`).

## Installation

### 1. Install the Universal Agent Skill

Via [skills.sh](https://skills.sh):

```bash
npx skills add Luckycat133/claude-subagent
```

Or clone and install CLI + Skill + starter agents together:

```bash
git clone https://github.com/Luckycat133/claude-subagent.git
cd claude-subagent
./install.sh --all
```

### 2. Install the CLI

```bash
# uv tool / pip:
uv tool install git+https://github.com/Luckycat133/claude-subagent.git
# or:
pip install git+https://github.com/Luckycat133/claude-subagent.git

# Or local symlink:
./install.sh
```

### Prerequisites

- Python **3.9+** (standard library only)
- [Claude Code CLI](https://code.claude.com/) (`claude` in `$PATH` or `~/.local/bin/claude`)
- **Optional (recommended for multi-model setups)**: [`crouter`](https://github.com/Luckycat133/crouter)

## Quick Start

### Step 1 — First-Run Discovery & Setup (Required Once)

```bash
# Discover installed Claude CLI & optional crouter models:
claude-subagent setup --discover

# Interactive terminal wizard:
claude-subagent setup

# Or non-interactive setup:
# Option A: Native Claude Code directly (no crouter needed):
claude-subagent setup --backend native --provider claude --delegation-mode proactive

# Option B: Paired with crouter:
claude-subagent setup \
  --backend crouter \
  --provider openrouter \
  --model nvidia/nemotron-3.5-super-vl:free \
  --delegation-mode proactive \
  --usage-policy "Use proactively; remind me when special boundaries apply."
```

### Step 2 — Run, Resume & Review

```bash
# Preview resolved route & command without invoking Claude:
claude-subagent --cwd /path/to/repo --title "Dry run" --dry-run -p "Hello"

# Dispatch a scoped coding worker in an isolated git worktree:
claude-subagent \
  --cwd /path/to/repo \
  --title "Auth 401 error refactor" \
  --allowed-tools coding \
  --effort medium \
  -w "refactor-auth" \
  -p - <<'EOF'
Goal: Return structured 401 JSON errors for expired tokens in src/auth/.
Scope: src/auth/errors.py, src/auth/middleware.py, tests/test_auth_errors.py.
Verification: pytest tests/test_auth_errors.py -q
EOF

# List sessions & send a surgical follow-up to session #1:
claude-subagent --cwd /path/to/repo --list
claude-subagent --cwd /path/to/repo -r 1 -p "Handle missing Authorization header and re-run pytest."
```

## Project Layout (`Skill + CLI`)

| Path | Purpose |
|---|---|
| `src/claude_subagent/cli.py` | Zero-dependency Python CLI runner (`setup`, `config`, execution, session & git tracking) |
| `bin/claude-subagent` | Executable entrypoint |
| `skills/claude-subagent/SKILL.md` | Universal Agent Skill (setup preflight, proactive team planning, briefing rules) |
| `skills/claude-subagent/references/team-topologies.md` | Minimal effective topologies & concise brief contract |
| `skills/claude-subagent/references/exception-handling.md` | Structured recovery playbook (review loops, GUI contention, timeouts, outages) |
| `skills/claude-subagent/references/recipes-and-flags.md` | Complete flag, setup, and environment reference |
| `agents/workflow-*.md` | Optional starter Claude Code roles (`workflow-researcher`, `workflow-implementer`, `workflow-reviewer`) |
| `tests/test_claude_subagent.py` | Deterministic offline unit & integration tests |

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
