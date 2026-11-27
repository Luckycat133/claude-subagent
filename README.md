# claude-subagent

[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)

English | [简体中文](README.zh-CN.md)

**Skill + CLI architecture that turns Claude Code into a persistent, worktree-isolated, context-shielding subagent pool for any AI coding agent.**

Works out of the box with **native Claude Code**—no extra routing tool required. Pairs even better with [`crouter`](https://github.com/Luckycat133/crouter) if you want multi-provider, local Ollama, or free-tier model routing.

Compatible with **Antigravity**, **Codex**, **Cursor**, **Windsurf**, **Gemini CLI**, **OpenClaw**, and **Cline**—with **zero permanent MCP server overhead** and **zero third-party Python dependencies**. (Claude Code already has built-in `Agent` subagents, so the Skill is aimed at external orchestrators.)

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

- **Native-grade zero-ceremony CLI**: Spawn with `claude-subagent "do X"`, `claude-subagent -C /repo --readonly "..."`, or `claude-subagent -w "..."` without repeating boilerplate flags—defaults handle route, title, `--strict-mcp-config`, and `--no-chrome` (~0.4s local startup).
- **Target-workspace session binding (IDE-agnostic)**: Sessions (`#1`, `#2`, `-c`, `send`, `ps`, `stop`) bind strictly to the specified target workspace (`-C` / `--cwd` / `--workspace`) via `realpath`, working identically across every IDE without host-specific adapters (`ps --all` lists across all workspaces).
- **Flexible model selection with preserved default**: Keeps your configured default route while allowing one-shot `-m <model>` overrides (`sonnet`/`opus`/`haiku`/`fable`/`claude-*` auto-route native, others inherit your saved provider), `--provider <p>`, and `claude-subagent models`.
- **Native-style worker verbs & live progress**: `send <id> "msg"`, `fork <id> "msg"`, `ps`, `show <id>`, `stop <id>`, `rm <id>`, `doctor --json`, and structured `--json` output, backed by real-time `stream-json` `stderr` progress and stale-PID detection.
- **Mandatory first-run setup & persistent policy** (`~/.config/claude-subagent/config.json`): Discovers configured models (`claude` native and optional `crouter` providers), confirms a persistent default route + delegation/usage policy once, and merges partial `setup` updates safely.
- **Works great standalone, even better with `crouter`**: Runs native `claude` out of the box. When [`crouter`](https://github.com/Luckycat133/crouter) is installed, supports provider/model routing while always passing `--once` so your global `crouter use` default stays untouched.
- **Zero idle token tax (`Skill + CLI`)**: No always-on MCP schemas; progressive Agent Skill (`skills/claude-subagent/`) and stdlib-only Python 3.9+ CLI.

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
# Inspect health, models & workspace sessions:
claude-subagent doctor --json
claude-subagent models

# Minimal zero-ceremony spawn & target-workspace scout:
claude-subagent "Fix the off-by-one bug in parser.py and run pytest -q."
claude-subagent -C /path/to/repo --readonly "Audit src/auth/ for missing token checks."
claude-subagent -m sonnet "Review this critical concurrency fix."

# Scoped coding worker in an isolated git worktree:
claude-subagent -w "Refactor auth 401 errors in src/auth/ and run pytest tests/test_auth_errors.py -q."

# Inspect sessions, follow up, fork, or stop:
claude-subagent ps -C /path/to/repo
claude-subagent show 1 -C /path/to/repo
claude-subagent send 1 -C /path/to/repo "Handle missing Authorization header and re-run pytest."
claude-subagent fork 1 -C /path/to/repo "Try alternative middleware design."
claude-subagent stop 1 -C /path/to/repo
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
