# Changelog

All notable changes to `claude-subagent` are documented in this file.

## [0.3.0] - 2026-11-27

### Added
- **Native-grade minimal CLI syntax**: positional prompt (`claude-subagent "do X"`), automatic `stdin` reading when piped, smart `-w "<prompt>"` promotion, `-R`/`--readonly`, `--role research|review|implement`, and `-C`/`--workspace` aliases for `--cwd`.
- **Flexible model selection with preserved default**: added `-m` shorthand alongside `--model`, auto-routing native Claude aliases/models (`sonnet`, `opus`, `haiku`, `fable`, `claude-*`) when `--provider` is omitted while inheriting the persisted provider for all other model IDs, plus `claude-subagent models [--json]` to inspect all configured routes.
- **Native-style session verbs**: `send <id> "msg"` (aliases `reply`, `followup`, `resume`), `fork <id> "msg"`, `ps` (alias `list`, with `--all` across workspaces), `show <id>`, `stop <id>` (terminates running process group while preserving session), `rm <id>` (alias `close`), and `clear`.

### Changed
- Sessions remain cleanly bound to the target workspace (`-C` / `--cwd` / `--workspace`) via `realpath`, keeping behavior uniform across all host IDEs without host-specific adapters.
- Removed `.claude/skills/claude-subagent` and `${HOME}/.claude/skills` from `install.sh` (Claude Code already provides native `Agent` subagents), and updated `install.sh` to copy `skills/claude-subagent` as a real directory instead of symlinking.

## [0.2.0] - 2026-11-25

### Added
- Live NDJSON streaming execution (`--output-format stream-json --verbose`) with real-time `stderr` turn/tool progress updates (`-q`/`--quiet` to suppress) and crash-safe live telemetry capture.
- Crash-proof `running` session lifecycle pre-registration (`register_running_session`, `update_session_pid`, `finalize_session`) with automatic stale-PID detection (`interrupted (stale pid)`) in `sessions list` / `sessions show`.
- Composable CLI subcommands: `claude-subagent doctor [--json] [--cwd <dir>]`, `claude-subagent sessions [list|show <id>|delete <id>|clear] [--cwd <dir>] [--json]`, `claude-subagent list`, and `claude-subagent run`.
- Structured machine-readable `--json` delivery envelope (`ok`, `status`, `exitCode`, `session`, `route`, `metrics`, `git`, `report`, `lastActivity`, `error`).
- Fast headless defaults (`--strict-mcp-config` and `--no-chrome` enabled by default), with opt-in overrides `--with-mcp`, `--mcp-config <file|json>`, `--chrome`, and persistent `setup --strict-mcp-default` / `--with-mcp-default`.
- New Claude Code 2.1.x pass-through flags: `--fork` / `--fork-session` (creates a new numbered session branch), `--fallback-model`, `--json-schema` (inline or file), `--ephemeral` / `--no-session-persistence`, and `-- <extra claude args...>` raw escape hatch.

### Fixed
- Registry path canonicalization now uses `os.path.realpath(cwd)` with legacy `abspath` fallback, unifying symlinked workspaces (e.g. `/tmp` vs `/private/tmp` on macOS).
- Passing `--model` without `--provider` now inherits persisted `defaultRoute.backend` and `defaultRoute.provider` instead of falling back to `crouter run`.
- Resuming a worktree session (`-r <id>` without `-w`) now preserves `worktree_path` and inspects git changes inside the worktree rather than the main checkout.
- Partial `claude-subagent setup` updates now merge onto existing configuration instead of clobbering untouched fields (pass `--replace` to overwrite).
- Process-group teardown (`_terminate_process_group`) now checks descendant pipe/process-group liveness across `SIGINT -> SIGTERM -> SIGKILL` so backgrounded child processes cannot survive timeouts.
- Skipped redundant git subprocess calls on non-git directories and dirty-delta snapshots.

## [0.1.4] - 2026-11-24

### Changed
- Removed default delivery report truncation (`--max-output-chars` now defaults to unlimited and only caps output when explicitly passed).
- Simplified `skills/claude-subagent/` guidance to remove rigid backend-specific concurrency rules and keep the skill general-purpose.

## [0.1.3] - 2026-11-24

### Added
- Pre-assigned `--session-id <uuid>` for new sessions so interrupted or timed-out runs still preserve their numbered `#N` session entry and print an actionable `Resume:` command.
- Automatic crash/timeout transcript recovery (`recover_session_telemetry`) from Claude Code's native session JSONL to surface actual executed `Model`, completed `turns`, and `Last Activity Before Interruption`.
- Automatic archival of full uncut Markdown delivery reports to `~/.local/state/claude-subagent/reports/` whenever inline output hit the length ceiling.

### Changed
- Synchronized `README.md` and `README.zh-CN.md` version badges to `0.1.3`.

## [0.1.2] - 2026-11-24

### Changed
- Removed the hardcoded 300s default `--timeout` in `claude-subagent` (`cli.py`, `execute_subagent`, `run_subprocess_safely`) so local models and long cloud chains run without arbitrary interruption; `--timeout <sec>` remains available as an explicit opt-in (`0` or omitted disables timeout).
- Added project-level distribution/packaging skills (`python-pypi-package-builder`, `directory-submissions`, `clawhub`, `github-release`) under `.agents/skills/` and `.claude/skills/`.

## [0.1.1] - 2026-11-23

### Changed
- Simplified persistent configuration to a unified `defaultRoute` with free-form `policies.usagePolicy` and optional `scenarioOverrides` (removed rigid separate routing modes).
- Added repository-local `.agents/skills/claude-subagent` and `.claude/skills/claude-subagent` symlinks for automatic skill discovery inside checkouts.
- Polished `claude-subagent` skill guidance on proactive team spin-up, outcome-oriented briefing, and structured exception recovery.

## [0.1.0] - 2026-11-23

### Added
- Initial release of `claude-subagent` (`Skill + CLI` architecture, Python 3.9+ stdlib only).
- First-run discovery & persistent configuration (`claude-subagent setup --discover`, `setup`, `config show|path|reset`) stored at `~/.config/claude-subagent/config.json`:
  - Records confirmed default backend/provider/model, delegation posture (`proactive` / `ask-first` / `explicit-only`), free-form `usagePolicy`, and optional per-agent `scenarioOverrides`.
  - Enforces a first-run `SETUP_REQUIRED` gate (exit code `2`) so agents never silently pick an unconfirmed model.
- Standalone native `claude` support out of the box, plus optional seamless `crouter` pairing (`--once` preserves saved `crouter use`).
- File-locked, atomic numbered session registry (`#1`, `#2`, `--list`, `-r`, `-c`, `--delete-session`, `--clear-sessions`).
- Worktree-aware (`-w [name]`) and dirty-tree run-delta Git change reporting (including committed deltas).
- Process-group timeout teardown (`SIGINT -> SIGTERM -> SIGKILL`) and `TMUX` env stripping.
- Universal Agent Skill (`skills/claude-subagent/`) covering setup preflight, proactive team planning, concise outcome-oriented briefing, and structured exception handling (`references/exception-handling.md`), validated by official `skills-ref`.
