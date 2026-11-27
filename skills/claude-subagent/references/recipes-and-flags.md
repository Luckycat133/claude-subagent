# Recipes, Setup & Flag Reference (`claude-subagent`)

## 1. Everyday Native-Grade Recipes

```bash
# Minimal spawn (bound to current directory, saved default route & fast headless defaults):
claude-subagent "Fix the off-by-one bug in parser.py and run pytest -q."

# Target a specific workspace from anywhere (`-C` / `--cwd` / `--workspace`):
claude-subagent -C /path/to/repo --readonly "Trace where session cleanup leaks occur."

# Multi-line brief via auto-stdin:
claude-subagent -C /path/to/repo --role implement <<'EOF'
Goal: Return structured 401 JSON errors for expired tokens in src/auth/.
Scope: src/auth/errors.py, src/auth/middleware.py, tests/test_auth_errors.py.
Verification: pytest tests/test_auth_errors.py -q
EOF

# Freely override model per call (saved default stays intact):
claude-subagent models                                      # list default, native aliases & crouter models
claude-subagent -m sonnet "Review complex race condition."  # auto-routes to native claude
claude-subagent -m deepseek/deepseek-r1:free "Analyze bug." # inherits persisted openrouter provider
claude-subagent --provider ollama "Summarize local diff."   # uses ollama's default model

# Worktree isolation (auto-named branch or explicit branch name):
claude-subagent -w "Implement rate limiter middleware and add tests."
claude-subagent -w feat-ratelimit "Implement rate limiter middleware."

# Follow-up, fork, inspect, stop & clean up (bound to target workspace):
claude-subagent send 1 -C /path/to/repo "Cover missing Authorization header and re-run pytest."
claude-subagent fork 1 -C /path/to/repo "Try an alternative middleware design."
claude-subagent ps -C /path/to/repo
claude-subagent ps --all          # list across all tracked workspaces
claude-subagent show 1 -C /path/to/repo --json
claude-subagent stop 1 -C /path/to/repo
claude-subagent rm 1 -C /path/to/repo
```

## 2. Health Check, Discovery & Setup

```bash
# Read-only health check (config, binaries, route, and target workspace sessions):
claude-subagent doctor --json -C /path/to/repo

# Inspect available Claude CLI, optional crouter providers/models & local agents:
claude-subagent setup --discover
claude-subagent setup --discover --json

# Non-interactive setup (Native Claude Code directly):
claude-subagent setup --backend native --provider claude --delegation-mode proactive

# Non-interactive setup (paired with crouter):
claude-subagent setup \
  --backend crouter \
  --provider openrouter \
  --model nvidia/nemotron-3.5-super-vl:free \
  --delegation-mode proactive \
  --usage-policy "Use proactively; remind user when special boundaries apply."

# Partial update (merges onto existing config without wiping other fields):
claude-subagent setup --delegation-mode ask-first

# Inspect or reset persisted configuration:
claude-subagent config show
claude-subagent config path
claude-subagent config reset
```

## 3. Structured JSON, Opt-In Capabilities & Raw Passthrough

```bash
# Machine-readable JSON envelope + schema-validated response:
claude-subagent --readonly --json \
  --json-schema '{"type":"object","properties":{"issues":{"type":"array"}},"required":["issues"]}' \
  "Audit src/auth/ for missing error handling."

# Opt in to global MCP servers, specific MCP config, or Chrome integration:
claude-subagent --with-mcp "Use configured MCP tool X..."
claude-subagent --mcp-config ./mcp.json "Run targeted MCP workflow..."
claude-subagent --chrome "Inspect live browser tab..."

# Raw passthrough to native `claude` flags after `--`:
claude-subagent "Quick check" -- --disable-slash-commands
```

## 4. Complete Command & Flag Reference

### Native-Style & Management Subcommands
- `claude-subagent [run] ["<prompt>"] [flags...] [-- <extra claude args...>]`
- `claude-subagent send <id> ["<message>"] [flags...]` (aliases: `reply`, `followup`, `resume`)
- `claude-subagent fork <id> ["<message>"] [flags...]`
- `claude-subagent ps [--all] [-C|--cwd|--workspace <dir>] [--json]` (alias: `list`)
- `claude-subagent show <id> [-C|--cwd|--workspace <dir>] [--json]`
- `claude-subagent stop <id> [-C|--cwd|--workspace <dir>] [--json]`
- `claude-subagent rm <id> [-C|--cwd|--workspace <dir>] [--json]` (alias: `close`)
- `claude-subagent clear [-C|--cwd|--workspace <dir>] [--json]`
- `claude-subagent sessions [list|show <id>|stop <id>|delete <id>|clear] [--all] [-C|--cwd|--workspace <dir>] [--json]`
- `claude-subagent models [--json]`
- `claude-subagent doctor [-C|--cwd|--workspace <dir>] [--json]`
- `claude-subagent setup [--discover] [--json] [--backend ...] [--provider ...] [-m|--model ...] [--delegation-mode ...] [--permission-mode ...] [--usage-policy ...] [--scenario name=provider:model] [--strict-mcp-default|--with-mcp-default] [--note ...] [--replace]`
- `claude-subagent config show | path | reset`

### Execution Flags
| Flag | Description |
|---|---|
| `prompt` (positional) / `-p, --prompt <text\|file\|->` | Inline prompt, file path, `-`, or automatic piped `stdin` |
| `-C, --cwd, --workspace <dir>` | Target workspace directory (sessions `#1`, `#2` bind to this folder) |
| `-n, --name, --title <text>` | Custom display title (auto-derived from prompt if omitted) |
| `-r, --resume <num\|uuid>` | Resume session by number (`1`, `#1`) or UUID (preserves worktree tracking) |
| `-c, --continue-session` | Continue the most recent session in the target workspace |
| `--fork, --fork-session` | Branch from `-r`/`-c` into a new numbered session (`--fork-session`) |
| `--ephemeral, --no-session-persistence` | Skip saving transcript and registry entry |
| `-w, --worktree [name]` | Isolate execution in a git worktree (auto-promotes `-w "<prompt>"` when no separate prompt is passed) |
| `-R, --readonly` | Read-only shorthand (`--allowed-tools readonly`) |
| `--role <research\|review\|implement>` | Role preset (uses `~/.claude/agents/workflow-*` if installed, else safe tool preset) |
| `--agent <name>` | Invoke a specific Claude Code agent definition |
| `--effort <level>` | Reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `--allowed-tools <csv\|preset>` | Tool whitelist (`coding`, `readonly`, or comma-separated tool names) |
| `--disallowed-tools <csv>` | Deny specific tools (`--disallowedTools`) |
| `--add-dir <dir>` | Grant tool access to additional directory (repeatable) |
| `--append-system-prompt <text>` | Append extra instructions to default system prompt |
| `--json-schema <json\|file>` | Enforce structured JSON output schema (inline JSON or file path) |
| `--strict-mcp` / `--with-mcp` | Skip external MCP servers by default (`--strict-mcp`), or load them (`--with-mcp`) |
| `--mcp-config <file\|json>` | Load specific MCP config(s) (repeatable, implies `--strict-mcp-config`) |
| `--chrome` | Enable Claude in Chrome integration (disabled via `--no-chrome` by default) |
| `--bare` | Minimal mode skipping hooks, plugins, and settings |
| `--permission-mode <mode>` | Permission mode override (default from config or `bypassPermissions`) |
| `--provider <name>` | One-shot provider override (`claude` for native; other providers via `crouter --once`) |
| `-m, --model <id>` | One-shot model override (`sonnet`/`opus`/`haiku`/`fable`/`claude-*` auto-route native; else inherit saved provider) |
| `--fallback-model <csv>` | Fallback model list passed to `--fallback-model` |
| `--allow-unconfigured` | One-off bypass of first-run setup gate |
| `--budget <usd>` | Pass `--max-budget-usd` spend ceiling |
| `--timeout <sec>` | Optional process-group timeout in seconds (default: unlimited) |
| `--max-output-chars <n>` | Optional cap on delivery report length in chars (default: unlimited) |
| `-q, --quiet` | Suppress live progress lines emitted to `stderr` |
| `--json` | Emit structured JSON delivery envelope to `stdout` |
| `--raw-json` | Print raw Claude JSON result payload |
| `--dry-run` | Print resolved route & command as JSON without invoking Claude |
| `-- <args...>` | Raw escape hatch passing remaining flags directly to `claude` |
| `-V, --version` | Print `claude-subagent` version |

### Environment Variables
- `CLAUDE_SUBAGENT_CONFIG`: Override config file path (default: `$XDG_CONFIG_HOME/claude-subagent/config.json` or `~/.config/claude-subagent/config.json`).
- `CLAUDE_SUBAGENT_STATE_DIR`: Override session registry dir (default: `$XDG_STATE_HOME/claude-subagent/sessions` or `~/.local/state/claude-subagent/sessions`).
- `CLAUDE_SUBAGENT_CLAUDE`: Explicit path to the `claude` binary.
- `CLAUDE_SUBAGENT_CROUTER`: Explicit path to the `crouter` binary.
- `CLAUDE_SUBAGENT_NO_CROUTER=1`: Disable `crouter` auto-detection and always run `claude` directly.
