# Changelog

All notable changes to `claude-subagent` are documented in this file.

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
