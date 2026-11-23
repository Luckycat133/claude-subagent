---
name: claude-subagent
description: "Plan minimal effective agent teams and delegate heavy exploration, multi-file implementation, iterative test-fix loops, independent reviews, or parallel worktree work to headless Claude Code via the `claude-subagent` CLI. Covers first-run model & usage policy setup, proactive team spin-up, outcome-oriented briefing, numbered session resumption (#1, #2), and structured exception recovery; skip trivial single-step edits."
---

# `claude-subagent`: Team Planning & Headless Claude Code Delegation

Use `claude-subagent` as a **Context Shield** and **stateful worker pool**. Offload noisy exploration, multi-file implementation, test-fix iterations, and independent reviews to isolated headless Claude Code workers so your main context stays focused on planning, integration, and verification.

Works out of the box with **native Claude Code** directly, and pairs even better with [`crouter`](https://github.com/Luckycat133/crouter) for multi-provider or local model routing.

## 1. Mandatory First-Run Setup & Persistent Policy

Before delegating for the first time, check persisted configuration:

```bash
claude-subagent config show
```

If unconfigured (exit code `2` / `SETUP_REQUIRED`):
1. Run `claude-subagent setup --discover` to inspect available models (`claude` native and, if installed, all configured `crouter` providers and default models).
2. **Always ask the user before running any subagent**—even when `crouter` is already installed. Confirm and explicitly inform the user their choices will be saved as the persistent default:
   - **Default subagent model/provider** (used automatically on all future runs unless the user or orchestrator explicitly overrides it).
   - **Delegation & usage policy**: whether to spin up subagents proactively (`proactive` / `ask-first` / `explicit-only`), and any personal usage boundaries or reminders to save in `usagePolicy` (e.g., when to remind the user before delegating, or optional per-agent overrides).
3. Persist via `claude-subagent setup ...` (see [Recipes & Flags](references/recipes-and-flags.md)).

On every subsequent run, respect the persisted `config.json`—never silently switch away from the user's confirmed default model, and honor any reminders or boundaries recorded in `policies.usagePolicy`.

## 2. When to Proactively Spin Up a Team

When `delegationMode` is `proactive`, **initiate delegation on your own without waiting to be asked** whenever any of these hold:

- **2+ independent tracks** with disjoint write scopes (parallelism cuts wall-clock time).
- **Heavy context load**: reading 15+ files, digging through verbose build/test logs, or multi-file refactors that would bloat the orchestrator's context window.
- **Consequential changes needing fresh eyes**: dispatch an independent read-only reviewer after implementation to break confirmation bias.
- **Specialized local agents**: offload dedicated workflows (e.g. `--agent video-watcher` for multi-frame media inspection).

**Execute directly instead** for single-file edits, quick lookups, strictly sequential 1-step changes, or tight interactive back-and-forth with the user.

## 3. Plan the Team & Write Outcome-Oriented Briefs

See [Team Topologies](references/team-topologies.md) for topologies and handoff templates.

1. **Preflight**: Check `git status --short`, `git worktree list`, and `git remote -v` (in multi-repo workspaces, do not edit third-party clones unless explicitly requested).
2. **Partition by Write Boundaries & Dependencies**:
   - Keep teams small (usually **2–4 workers**).
   - Enforce **single-writer ownership** per file, artifact, port, or service. When parallel writers touch the same git repo, isolate each in a worktree (`-w <name>`).
   - Stage dependent phases cleanly (e.g. scout → parallel implementers → independent reviewer → coordinator merge).
3. **Brief for Outcomes, Not Micromanagement**:
   - State the **goal, acceptance criteria, write scope (owned vs. off-limits paths), and verification command**.
   - **Do not micromanage** step-by-step instructions or dump redundant prose the worker can read from the repo itself. Only add non-obvious constraints, repro details, or required invariants.
   - Always pass `--cwd <abs_path>`, a distinct `--title "<short task>"`, and `-p - <<'EOF' ... EOF` for multi-line briefs.
4. **Permissions & Least-Privilege Scoping**:
   - Headless `claude -p` defaults to `bypassPermissions` so workers execute unattended without stalling on interactive prompts or burning parent tokens.
   - Enforce safety through **scope, not prompts**: `--allowed-tools readonly --strict-mcp` for scouts/reviewers, `--allowed-tools coding` for writers, `-w <name>` for git isolation, and `--timeout` / `--budget` bounds.

## 4. Handle Exceptions & Verify Before Delivery

See [Exception Handling Playbook](references/exception-handling.md) for full recovery patterns.

- **Review / test failures**: Do not stop at a failing review—resume the owning session (`-r <id>`) with concrete findings and iterate until checks pass. Break out only on external blockers, genuine design ambiguity, or repeated identical failures requiring a new strategy.
- **User / host contention** (e.g. GUI / Computer Use or shared active workspace): If the user is actively operating the desktop or target app, **yield and pause immediately** rather than fighting for focus.
- **Timeouts / partial output**: Inspect actual disk and git state first; resume `-r <id>` with narrowed scope or split the task instead of restarting blindly.
- **Always verify independently**: Inspect actual `git status --short`, `git diff --stat`, worktree merge state (worktree edits are not on the main branch until merged), and test exit codes before declaring completion.
