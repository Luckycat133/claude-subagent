# Recipes, Setup & Flag Reference (`claude-subagent`)

## 1. First-Run Setup & Config Management

```bash
# Inspect available Claude CLI, optional crouter providers/models, local agents & config status:
claude-subagent setup --discover
claude-subagent setup --discover --json

# Interactive setup wizard (in a terminal):
claude-subagent setup

# Non-interactive setup (e.g. persisted by an agent after user confirmation):
# Example A — Native Claude Code directly (no crouter needed):
claude-subagent setup \
  --backend native \
  --provider claude \
  --delegation-mode proactive

# Example B — Paired with crouter:
claude-subagent setup \
  --backend crouter \
  --provider openrouter \
  --model nvidia/nemotron-3.5-super-vl:free \
  --delegation-mode proactive \
  --usage-policy "Use proactively; remind user when special boundaries apply."

# Inspect or reset persisted configuration:
claude-subagent config show
claude-subagent config path
claude-subagent config reset
```

## 2. Everyday Delegation Recipes

### Scoped Implementation in an Isolated Worktree
```bash
claude-subagent \
  --cwd "/abs/path/to/repo" \
  --title "Auth 401 error refactor" \
  --allowed-tools coding \
  --effort medium \
  -w "refactor-auth" \
  -p - <<'EOF'
Goal: Return structured 401 JSON errors for expired tokens in src/auth/.
Scope: src/auth/errors.py, src/auth/middleware.py, tests/test_auth_errors.py.
Boundaries: Do not modify src/billing/ or public response keys for valid tokens.
Verification: pytest tests/test_auth_errors.py -q
EOF
```

### Fast Read-Only Scout or Independent Reviewer
```bash
claude-subagent \
  --cwd "/abs/path/to/repo" \
  --title "Review auth refactor" \
  --agent workflow-reviewer \
  --allowed-tools readonly \
  --strict-mcp \
  --effort medium \
  -p "Review git diff in src/auth/ against requirement X. Report concrete defects with file:line, trigger, impact, and minimal fix."
```

### Stateful Follow-Up & Session Management
```bash
claude-subagent --cwd "/abs/path/to/repo" --list
claude-subagent --cwd "/abs/path/to/repo" -r 1 -p "Fix missing Authorization header edge case in middleware.py:42 and re-run pytest."
claude-subagent --cwd "/abs/path/to/repo" -c -p "Re-run full auth test suite and report exit code."
claude-subagent --cwd "/abs/path/to/repo" --delete-session 1
claude-subagent --cwd "/abs/path/to/repo" --clear-sessions
```

### Dry-Run Route Inspection
```bash
claude-subagent --cwd "/abs/path/to/repo" --title "Check route" --dry-run -p "hello"
```

## 3. Complete Flag & Environment Reference

### Management Subcommands
- `claude-subagent setup [--discover] [--json] [--backend crouter|native] [--provider <p>] [--model <m>] [--delegation-mode proactive|ask-first|explicit-only] [--permission-mode <mode>] [--usage-policy <text>] [--scenario name=provider:model] [--note <text>]`
- `claude-subagent config show | path | reset`

### Execution Flags
| Flag | Description |
|---|---|
| `-p, --prompt <text\|file\|->` | Inline prompt, file path, or `-` to read from stdin |
| `--cwd <dir>` | Target working directory (default: current directory) |
| `-n, --name, --title <text>` | Human-readable title shown in `--list` and passed to Claude `-n` |
| `-l, --list` | List numbered sessions (`#1`, `#2`, ...) for `--cwd` |
| `-r, --resume <num\|uuid>` | Resume session by number (`1`, `#1`) or raw UUID |
| `-c, --continue-session` | Continue the most recent session in `--cwd` |
| `--delete-session <num\|uuid>` | Remove one recorded session entry |
| `--clear-sessions` | Clear all recorded session entries for `--cwd` |
| `-w, --worktree [name]` | Isolate execution in a git worktree (optional name) |
| `--agent <name>` | Invoke a Claude Code agent from `~/.claude/agents/` or `.claude/agents/` |
| `--effort <level>` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `--allowed-tools <csv\|preset>` | Tool whitelist (`coding`, `readonly`, or comma-separated tool names) |
| `--disallowed-tools <csv>` | Deny specific tools (`--disallowedTools`) |
| `--add-dir <dir>` | Grant tool access to additional directory (repeatable) |
| `--append-system-prompt <text>` | Append extra instructions to default system prompt |
| `--strict-mcp` | Skip external MCP servers (`--strict-mcp-config`) while keeping hooks/agents |
| `--bare` | Minimal mode skipping hooks, plugins, and settings |
| `--permission-mode <mode>` | Permission mode override (default from config or `bypassPermissions`) |
| `--provider <name>` | One-shot provider override (`claude` for native Anthropic; other providers via `crouter --once`) |
| `--model <id>` | One-shot model override |
| `--allow-unconfigured` | One-off bypass of first-run setup gate |
| `--budget <usd>` | Pass `--max-budget-usd` spend ceiling |
| `--timeout <sec>` | Process-group timeout in seconds (default: `300`) |
| `--max-output-chars <n>` | Safety ceiling on delivery report length (default: `15000`) |
| `--raw-json` | Print raw Claude JSON payload |
| `--dry-run` | Print resolved route & command as JSON without invoking Claude |
| `-V, --version` | Print `claude-subagent` version |

### Environment Variables
- `CLAUDE_SUBAGENT_CONFIG`: Override config file path (default: `$XDG_CONFIG_HOME/claude-subagent/config.json` or `~/.config/claude-subagent/config.json`).
- `CLAUDE_SUBAGENT_STATE_DIR`: Override session registry dir (default: `$XDG_STATE_HOME/claude-subagent/sessions` or `~/.local/state/claude-subagent/sessions`).
- `CLAUDE_SUBAGENT_CLAUDE`: Explicit path to the `claude` binary.
- `CLAUDE_SUBAGENT_CROUTER`: Explicit path to the `crouter` binary.
- `CLAUDE_SUBAGENT_NO_CROUTER=1`: Disable `crouter` auto-detection and always run `claude` directly.
