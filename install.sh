#!/usr/bin/env bash
# Install claude-subagent CLI (and optionally the Agent Skill and starter Claude agents).
#
# Usage:
#   ./install.sh                     # Install CLI to ~/.local/bin/claude-subagent
#   ./install.sh --all               # Install CLI + starter ~/.claude/agents/ + auto-detected skill dirs
#   ./install.sh --bin-dir ~/bin     # Custom CLI destination
#   ./install.sh --skill-dir <dir>   # Install skill into a specific skills directory (repeatable)
#   ./install.sh --agents            # Install starter workflow-* agents into ~/.claude/agents/
#   ./install.sh --force             # Overwrite existing starter agent files

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
INSTALL_AGENTS=0
INSTALL_ALL=0
FORCE=0
SKILL_DIRS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir)
      BIN_DIR="$2"; shift 2 ;;
    --skill-dir)
      SKILL_DIRS+=("$2"); shift 2 ;;
    --agents)
      INSTALL_AGENTS=1; shift ;;
    --all)
      INSTALL_ALL=1; INSTALL_AGENTS=1; shift ;;
    --force)
      FORCE=1; shift ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1 ;;
  esac
done

chmod +x "$SCRIPT_DIR/bin/claude-subagent"
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT_DIR/bin/claude-subagent" "$BIN_DIR/claude-subagent"
echo "Installed CLI: $BIN_DIR/claude-subagent -> $SCRIPT_DIR/bin/claude-subagent"

if [ "$INSTALL_ALL" -eq 1 ] && [ ${#SKILL_DIRS[@]} -eq 0 ]; then
  for candidate in \
    "${HOME}/.agents/skills" \
    "${HOME}/.claude/skills" \
    "${HOME}/.gemini/config/skills" \
    "${HOME}/.codex/skills"; do
    if [ -d "$candidate" ]; then
      SKILL_DIRS+=("$candidate")
    fi
  done
fi

for sdir in "${SKILL_DIRS[@]:-}"; do
  [ -n "$sdir" ] || continue
  mkdir -p "$sdir"
  target="$sdir/claude-subagent"
  ln -sfn "$SCRIPT_DIR/skills/claude-subagent" "$target"
  echo "Installed skill: $target -> $SCRIPT_DIR/skills/claude-subagent"
done

if [ "$INSTALL_AGENTS" -eq 1 ]; then
  AGENTS_DST="${HOME}/.claude/agents"
  mkdir -p "$AGENTS_DST"
  for agent_file in "$SCRIPT_DIR"/agents/*.md; do
    base="$(basename "$agent_file")"
    dst="$AGENTS_DST/$base"
    if [ -e "$dst" ] && [ "$FORCE" -ne 1 ]; then
      echo "Kept existing agent: $dst (pass --force to overwrite)"
    else
      cp "$agent_file" "$dst"
      echo "Installed agent: $dst"
    fi
  done
fi

if ! command -v claude-subagent >/dev/null 2>&1; then
  echo ""
  echo "Note: $BIN_DIR is not currently in your PATH. Add this to your shell profile:"
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi
