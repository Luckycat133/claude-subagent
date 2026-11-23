# Changelog

All notable changes to `claude-subagent` are documented in this file.

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
