---
name: claude-subagent
description: "Plan minimal effective agent teams and delegate heavy exploration, multi-file implementation, iterative test-fix loops, independent reviews, or parallel worktree work to headless Claude Code via the `claude-subagent` CLI. Use whenever 2+ independent tracks, noisy 15+ file investigations, independent reviews, or stateful numbered session resumptions (#1, #2) would keep the main context clean and cut wall-clock time; skip trivial single-step edits."
---

# `claude-subagent`: Native-Grade Headless Subagent Delegation

Call `claude-subagent` like a native subagent—pass your goal (and `-C <target_dir>` when targeting another folder) and let the CLI handle defaults, target-workspace session binding, flexible model selection, fast headless startup, and live progress.

Works out of the box with **native Claude Code**, and pairs seamlessly with [`crouter`](https://github.com/Luckycat133/crouter) for multi-provider or local model routing. (Designed for external host agents like Codex, Antigravity, Cursor, and Gemini CLI—Claude Code already has native `Agent` subagents.)

## 1. Zero-Ceremony Usage & Target-Workspace Binding

You do **not** need to pass `--title`, `--allowed-tools`, `--strict-mcp`, `--effort`, or `--provider` on routine calls:
- **Target-workspace binding**: sessions (`#1`, `#2`, `send`, `fork`, `ps`, `stop`, `-c`) bind strictly to the target workspace (`-C` / `--cwd` / `--workspace`, defaulting to current directory, canonicalized via `realpath`). Use `ps --all` to inspect sessions across all workspaces.
- **Default route & free model selection**: defaults to your persisted config (`~/.config/claude-subagent/config.json`, e.g., `openrouter` / `nvidia/nemotron-3.5-super-vl:free`). Unconfigured runs exit with code `2` (`SETUP_REQUIRED`). Override per call anytime with `-m <model>` (native aliases `sonnet`/`opus`/`haiku`/`fable`/`claude-*` auto-route to native `claude`, while other model IDs inherit your saved provider) or `--provider <p>`, and list options with `claude-subagent models`.
- **Fast headless defaults**: `--strict-mcp-config`, `--no-chrome`, and `bypassPermissions` apply automatically (~0.4s local startup). Pass `--with-mcp`, `--mcp-config <file>`, or `--chrome` when a task needs them.

```bash
claude-subagent "Audit src/auth/ and summarize token validation gaps."
claude-subagent -C /path/to/repo --readonly "Find all places touching session cleanup."
claude-subagent -m sonnet "Review this tricky concurrency change carefully."
claude-subagent -w "Refactor auth 401 errors and run pytest tests/test_auth.py -q."
claude-subagent send 1 "Also cover missing Authorization header and re-run pytest."
claude-subagent fork 1 "Try an alternative middleware design without touching #1."
claude-subagent ps
claude-subagent stop 1
```

## 2. First-Run Setup & Persistent Policy

If `claude-subagent` exits with code `2` (`SETUP_REQUIRED`), or if `claude-subagent` is not yet on `PATH` (install via `uv tool install git+https://github.com/Luckycat133/claude-subagent.git` or `pip install ...`):
1. Run `claude-subagent setup --discover` to inspect available models (`claude` native and any configured `crouter` providers).
2. Ask the user to confirm before running any subagent—noting their choice is saved persistently:
   - **Default subagent model/provider**
   - **Delegation & usage policy** (`proactive` / `ask-first` / `explicit-only`, plus any reminders for `usagePolicy`)
3. Persist via `claude-subagent setup ...` (see [Recipes & Flags](references/recipes-and-flags.md)). Subsequent partial `setup` calls merge onto the existing config.

Always honor `policies.usagePolicy` (inspect anytime with `claude-subagent doctor --json`).

## 3. When to Spin Up a Team vs. Work Directly

When `delegationMode` is `proactive`, delegate on your own whenever any of these hold:
- **2+ independent tracks** with disjoint write scopes (launch concurrently in the same turn).
- **Heavy context load**: reading 15+ files, verbose build/test logs, or multi-file refactors.
- **Independent verification**: dispatch `--role review` (or `--readonly`) after implementation so fresh eyes check the diff without confirmation bias.
- **Specialized local agents**: offload dedicated workflows (`--agent video-watcher`, `test-runner`, `link-auditor`, `repo-hygiene-cleaner`, etc.).

Execute directly instead for single-file edits, quick lookups, or strictly sequential 1-step changes.

## 4. Briefing, Parallelism & Verification

- **Brief for outcomes, not micromanagement**: state the goal, acceptance criteria, write boundaries, and verification command—do not dump repo contents the worker can read itself. Use `-p - <<'EOF' ... EOF` (or piped `stdin`) for multi-line briefs. See [Team Topologies](references/team-topologies.md).
- **Isolate parallel writers**: give each concurrent writer disjoint paths or `-w "<task>"` / `-w <branch> "<task>"`.
- **Live progress & unlimited runtime**: timeout and output length are unlimited by default—avoid artificial short `--timeout` caps; `stderr` streams live turn/tool progress (`-q` silences) and `claude-subagent show <id>` / `ps` inspects live or interrupted state. See [Exception Handling](references/exception-handling.md).
- **Verify independently**: inspect actual `git status --short`, `git diff --stat`, worktree merge state (worktree edits stay on the worktree branch until merged), and test exit codes before declaring completion.
