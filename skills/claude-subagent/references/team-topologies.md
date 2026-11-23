# Team Topologies & Concise Briefing Guide

Add a worker only when parallelism saves wall-clock time, context shielding protects the orchestrator, or independent verification reduces blind spots. Most tasks are best served by **2–4 focused workers**.

## Proven Topologies

### 1. Scout → Parallel Implementers
Best when the target area spans many files or unfamiliar subsystems.
1. Dispatch 1 read-only scout (`--allowed-tools readonly --strict-mcp --effort low`) to map entrypoints, contracts, and test commands.
2. Partition implementation into disjoint slices (or `-w <slice>` worktrees) and dispatch 1–3 writers concurrently.
3. Coordinator integrates, resolves cross-slice interfaces, and runs combined checks.

### 2. Parallel Writers → Independent Read-Only Reviewer
Best for refactors, security-sensitive logic, or multi-module features.
1. Dispatch writers with exclusive path ownership or separate `-w <name>` worktrees.
2. Dispatch a fresh read-only reviewer (`--allowed-tools readonly --strict-mcp`, optionally `--agent workflow-reviewer`) given **only** the requirement, exact diff/paths, and 1–3 targeted risk questions—never the implementer's self-evaluation.
3. Feed actionable findings back to the owning writer via `-r <id>` and re-verify until clean.

### 3. Persistent Implementer Loop
Best for deep single-module features or iterative test-fix work.
1. Start session `#1` with a clear `--title` and verification command.
2. Inspect real `git diff` and test results after each turn.
3. Send focused deltas via `-r 1` (or `-c`) so the worker retains full context without re-reading the codebase.

### 4. Multi-Repo Fan-Out
Best when a change or audit spans multiple repositories.
1. Preflight `git remote -v` to separate user-owned repos from third-party clones.
2. Launch 1 `claude-subagent` per target repo concurrently in the background.

## Outcome-Oriented Brief Template

Keep briefs **short, concrete, and free of micromanagement**. Tell the worker *what done looks like* and *what boundaries hold*—not how to write every line. Omit lines that do not apply.

```text
Goal: <concrete outcome & acceptance criteria>
Scope: <owned files/dirs to edit, OR "READ-ONLY">
Boundaries: <off-limits files/services, compatibility invariants, or non-obvious constraints>
Verification: <exact test/lint/build command(s) to run>
```

### What to Leave Out
- Do **not** paste file contents the worker can read directly in `--cwd`.
- Do **not** prescribe step-by-step micro-edits unless a specific algorithm or API signature is mandated.
- Do **not** repeat generic boilerplate ("be careful", "write clean code").

## Integration & Closeout Checklist

- **Shared working tree**: Changes made without `-w` are already on disk—inspect `git diff` directly; never re-apply prose snippets.
- **Worktree workers (`-w`)**: Passing tests on a worktree branch do **not** update the main branch. Inspect the worktree diff, merge or cherry-pick into the target branch, run integrated verification, and clean up temporary worktrees.
- **Dirty repos**: `claude-subagent` snapshots pre-existing dirty files before non-worktree runs and reports only files touched during the run. Always double-check `git diff` before final delivery.
