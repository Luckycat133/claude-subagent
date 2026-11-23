# Repository Instructions (`claude-subagent`)

This repository contains the `claude-subagent` Python CLI, its universal Agent Skill (`skills/claude-subagent/`), optional starter Claude Code agent definitions (`agents/`), and offline test suite (`tests/`).

## Engineering Rules

- **Zero external runtime dependencies**: `src/claude_subagent/` must use Python 3.9+ standard library only so users can run it anywhere without virtualenv setup.
- **No hardcoded user paths or personal data**: Never commit machine-specific home paths, API keys, or private repo names. Resolve executables and state dirs via environment variables, `$PATH`, and XDG conventions.
- **Preserve user `crouter` defaults**: Whenever `crouter` is used with `--provider` or `--model`, pass `--once` and `--` before Claude Code flags.
- **Keep `skills/claude-subagent/SKILL.md` concise and agent-agnostic**: Detailed recipes and templates belong in `skills/claude-subagent/references/`.

## Verification

Before committing changes, run:

```bash
python3 -m unittest discover -s tests -v
./bin/claude-subagent --version
./bin/claude-subagent --dry-run -p "smoke test"
```
