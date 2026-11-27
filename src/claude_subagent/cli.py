#!/usr/bin/env python3
"""
Headless Claude Code Subagent Runner (`claude-subagent`).

Native-grade subagent CLI for Codex, Antigravity, Cursor, Gemini CLI, and terminal
workflows. Zero external dependencies (Python 3.9+ stdlib only).

Highlights:
  * Minimal native-like interface: `claude-subagent "task"`, `send <id> "msg"`,
    `fork <id> "msg"`, `ps`, `show <id>`, `stop <id>`, `rm <id>`, `models`, `doctor`.
  * Target-workspace session binding: binds `#1`, `#2`, `-c`, `send`, and `ps`
    strictly to the specified target workspace (`-C` / `--cwd` / `--workspace`),
    normalized via `realpath` so every IDE works identically without host adapters.
  * Flexible model selection with preserved default: keeps configured default
    (`openrouter` / `nvidia/nemotron-3.5-super-vl:free`) while supporting one-shot
    `-m <model>` (auto-routing native `sonnet`/`opus`/`haiku`/`fable`/`claude-*`
    or inheriting `defaultRoute.provider`), `--provider <p>`, and `models`.
  * Fast headless defaults (`--strict-mcp-config`, `--no-chrome`, `bypassPermissions`)
    and live `stream-json` `stderr` progress with crash-safe telemetry recovery.
"""

import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

from . import __version__

EXIT_SETUP_REQUIRED = 2

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TAG_STRIP = re.compile(r"<[^>]+>")

TOOL_PRESETS = {
    "coding": "Bash,Edit,Read,Write,Grep,Glob",
    "readonly": "Read,Grep,Glob",
}

PERMISSION_MODES = [
    "bypassPermissions",
    "acceptEdits",
    "default",
    "plan",
    "dontAsk",
    "auto",
]

DELEGATION_MODES = ["proactive", "ask-first", "explicit-only"]

NATIVE_CLAUDE_MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable")


# ---------------------------------------------------------------------------
# Environment, Config & Binary Discovery
# ---------------------------------------------------------------------------

def get_config_path() -> str:
    """Resolve persistent user config path (env override or XDG config dir)."""
    override = os.environ.get("CLAUDE_SUBAGENT_CONFIG", "").strip()
    if override:
        return os.path.expanduser(override)
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = os.path.expanduser(xdg_config) if xdg_config else os.path.expanduser("~/.config")
    return os.path.join(base, "claude-subagent", "config.json")


def get_state_dir() -> str:
    """Resolve portable session state directory (env override or XDG state dir)."""
    override = os.environ.get("CLAUDE_SUBAGENT_STATE_DIR", "").strip()
    if override:
        return os.path.expanduser(override)
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = os.path.expanduser(xdg_state) if xdg_state else os.path.expanduser("~/.local/state")
    return os.path.join(base, "claude-subagent", "sessions")


def find_crouter() -> str:
    """Locate optional `crouter` executable unless disabled via CLAUDE_SUBAGENT_NO_CROUTER=1."""
    if os.environ.get("CLAUDE_SUBAGENT_NO_CROUTER", "").strip().lower() in ("1", "true", "yes"):
        return ""
    override = os.environ.get("CLAUDE_SUBAGENT_CROUTER", "").strip()
    if override:
        expanded = os.path.expanduser(override)
        return expanded if (os.path.exists(expanded) and os.access(expanded, os.X_OK)) else ""
    found = shutil.which("crouter")
    if found:
        return found
    local_bin = os.path.expanduser("~/.local/bin/crouter")
    if os.path.exists(local_bin) and os.access(local_bin, os.X_OK):
        return local_bin
    return ""


def find_claude() -> str:
    """Locate the `claude` CLI executable."""
    override = os.environ.get("CLAUDE_SUBAGENT_CLAUDE", "").strip()
    if override:
        return os.path.expanduser(override)
    found = shutil.which("claude")
    if found:
        return found
    local_bin = os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(local_bin) and os.access(local_bin, os.X_OK):
        return local_bin
    return "claude"


# ---------------------------------------------------------------------------
# Atomic File I/O & Locking
# ---------------------------------------------------------------------------

@contextmanager
def _file_lock(lock_path: str, exclusive: bool = True):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(lock_path, "a+", encoding="utf-8") as lock_f:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_f.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Persistent Configuration & Environment Discovery
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load persistent configuration dict, or return None if not yet configured."""
    cfg_path = get_config_path()
    if not os.path.isfile(cfg_path):
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("configured"):
            return data
    except Exception:
        pass
    return None


def save_config(data: dict) -> str:
    """Atomically save persistent configuration to config.json and return its path."""
    cfg_path = get_config_path()
    with _file_lock(cfg_path + ".lock", exclusive=True):
        _write_json_atomic(cfg_path, data)
    return cfg_path


def reset_config() -> bool:
    """Delete persistent config.json if present."""
    cfg_path = get_config_path()
    if os.path.exists(cfg_path):
        os.unlink(cfg_path)
        return True
    return False


def _parse_crouter_list(output: str) -> list:
    """Parse tabular output of `crouter list` into structured provider dicts."""
    providers = []
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("PROVIDER ") or stripped[0].isdigit():
            continue
        parts = stripped.split()
        if len(parts) >= 4:
            providers.append(
                {
                    "provider": parts[0],
                    "status": parts[1],
                    "auth": parts[2],
                    "defaultModel": parts[3],
                    "endpoint": parts[4] if len(parts) >= 5 else "",
                }
            )
    return providers


def discover_environment(crouter_path: str = None, claude_path: str = None) -> dict:
    """Read-only inspection of available Claude CLI, crouter providers/models, agents, and config."""
    resolved_claude = find_claude() if claude_path is None else claude_path
    resolved_crouter = find_crouter() if crouter_path is None else crouter_path

    claude_version = None
    claude_available = False
    try:
        proc = subprocess.run(
            [resolved_claude, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            claude_available = True
            claude_version = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "unknown"
    except Exception:
        pass

    crouter_info = {"installed": False, "path": None, "activeSelection": None, "providers": []}
    if resolved_crouter and os.path.exists(resolved_crouter) and os.access(resolved_crouter, os.X_OK):
        crouter_info["installed"] = True
        crouter_info["path"] = resolved_crouter
        try:
            u_proc = subprocess.run(
                [resolved_crouter, "use"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if u_proc.returncode == 0 and u_proc.stdout.strip():
                crouter_info["activeSelection"] = u_proc.stdout.strip().splitlines()[0]
        except Exception:
            pass
        try:
            l_proc = subprocess.run(
                [resolved_crouter, "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if l_proc.returncode == 0:
                crouter_info["providers"] = _parse_crouter_list(l_proc.stdout)
        except Exception:
            pass

    agents = []
    agents_dir = os.path.expanduser("~/.claude/agents")
    if os.path.isdir(agents_dir):
        for fname in sorted(os.listdir(agents_dir)):
            if fname.endswith(".md"):
                agents.append(fname[:-3])

    cfg = load_config()
    return {
        "version": __version__,
        "configPath": get_config_path(),
        "configured": cfg is not None,
        "config": cfg,
        "claude": {
            "available": claude_available,
            "path": resolved_claude,
            "version": claude_version,
        },
        "crouter": crouter_info,
        "agents": agents,
    }


def format_discovery_report(env_info: dict) -> str:
    """Format human/agent-readable discovery summary."""
    lines = ["### claude-subagent Environment Discovery"]
    lines.append(f"- **Config Path**: `{env_info['configPath']}` (configured: `{env_info['configured']}`)")
    cl = env_info["claude"]
    ver_suffix = f", {cl['version']}" if cl.get("version") else ""
    lines.append(
        f"- **Claude Code CLI**: {'available' if cl['available'] else 'NOT FOUND'} "
        f"(`{cl['path']}`{ver_suffix})"
    )
    cr = env_info["crouter"]
    if cr["installed"]:
        lines.append(
            f"- **crouter**: installed (`{cr['path']}` | current `crouter use`: `{cr.get('activeSelection') or 'unset'}`)"
        )
        if cr["providers"]:
            lines.append("- **Configured crouter Providers & Default Models**:")
            for p in cr["providers"]:
                lines.append(
                    f"  - `{p['provider']}` ({p['status']}, auth={p['auth']}) → `{p['defaultModel']}`"
                )
    else:
        lines.append("- **crouter**: not installed (optional — native `claude` works directly)")

    if env_info["agents"]:
        lines.append(f"- **Local Claude Agents (`~/.claude/agents/`)**: {', '.join(f'`{a}`' for a in env_info['agents'])}")
    else:
        lines.append("- **Local Claude Agents**: none installed")

    return "\n".join(lines)


def format_setup_required_message(env_info: dict) -> str:
    """Clear actionable message emitted when first-run setup has not been completed."""
    lines = [
        "### [claude-subagent: SETUP_REQUIRED]",
        "No persistent subagent configuration found yet. Before running subagents, ask the user which model/route "
        "and delegation/usage policy to save as the persistent default (subsequent runs will automatically use it "
        "unless explicitly overridden).",
        "",
        format_discovery_report(env_info),
        "",
        "**How to persist the user's choice:**",
        "```bash",
        "# Example 1 (native Claude Code directly, no crouter required):",
        "claude-subagent setup --backend native --provider claude --delegation-mode proactive",
        "",
        "# Example 2 (paired with crouter):",
        "claude-subagent setup \\",
        "  --backend crouter \\",
        "  --provider openrouter \\",
        "  --model <model-id> \\",
        "  --delegation-mode proactive \\",
        "  --usage-policy \"<optional user rules or reminders>\"",
        "```",
        "",
        "*(Or pass explicit `--provider`/`-m`, or `--allow-unconfigured` for a one-off bypass.)*",
    ]
    return "\n".join(lines)


def build_config_payload(
    backend: str,
    provider: str = None,
    model: str = None,
    delegation_mode: str = "proactive",
    usage_policy: str = None,
    scenario_overrides: dict = None,
    permission_mode: str = "bypassPermissions",
    note: str = None,
    strict_mcp: bool = True,
) -> dict:
    """Create normalized schemaVersion=1 config dict."""
    norm_backend = (backend or "native").strip().lower()
    if norm_backend not in ("crouter", "native"):
        raise ValueError(f"Unsupported backend: {backend}")

    norm_provider = (provider or ("claude" if norm_backend == "native" else "")).strip() or None
    norm_model = (model or "").strip() or None

    return {
        "schemaVersion": 1,
        "configured": True,
        "configuredAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "defaultRoute": {
            "backend": norm_backend,
            "provider": norm_provider,
            "model": norm_model,
            "note": (note or "").strip(),
        },
        "policies": {
            "delegationMode": delegation_mode or "proactive",
            "permissionMode": permission_mode or "bypassPermissions",
            "strictMcp": bool(strict_mcp),
            "usagePolicy": (usage_policy or "").strip(),
            "scenarioOverrides": scenario_overrides or {},
        },
    }


def _is_native_claude_model(model: str) -> bool:
    norm = (model or "").strip().lower()
    if not norm:
        return False
    return norm in NATIVE_CLAUDE_MODEL_ALIASES or norm.startswith("claude-")


def resolve_effective_route(
    config: dict,
    cli_provider: str = None,
    cli_model: str = None,
    agent: str = None,
    allow_unconfigured: bool = False,
) -> tuple:
    """
    Resolve effective (backend, provider, model, permission_mode, route_source) from CLI flags + persisted config.
    Raises RuntimeError("SETUP_REQUIRED") if unconfigured and no explicit override/bypass is given.
    """
    explicit_override = bool(cli_provider or cli_model)

    if config is None and not explicit_override and not allow_unconfigured:
        raise RuntimeError("SETUP_REQUIRED")

    policies = (config or {}).get("policies") or {}
    default_route = (config or {}).get("defaultRoute") or {}
    cfg_perm = policies.get("permissionMode") or "bypassPermissions"

    if explicit_override:
        if cli_provider:
            backend = "native" if cli_provider == "claude" else ("crouter" if find_crouter() else "native")
            return backend, cli_provider, cli_model, cfg_perm, "cli-override"

        # -m/--model without --provider: auto-route native Claude aliases/models, else inherit default provider
        if _is_native_claude_model(cli_model):
            return "native", "claude", cli_model, cfg_perm, "cli-override"

        inherited_provider = default_route.get("provider")
        inherited_backend = default_route.get("backend")
        if inherited_backend:
            backend = inherited_backend
        else:
            backend = "native" if inherited_provider == "claude" else ("crouter" if find_crouter() else "native")
        return backend, inherited_provider, cli_model, cfg_perm, "cli-override"

    if config is None:
        backend = "crouter" if find_crouter() else "native"
        return backend, None, None, cfg_perm, "unconfigured-bypass"

    scenario_overrides = policies.get("scenarioOverrides") or {}

    if agent and agent in scenario_overrides and isinstance(scenario_overrides[agent], dict):
        sc = scenario_overrides[agent]
        sc_backend = sc.get("backend") or default_route.get("backend") or ("crouter" if find_crouter() else "native")
        return (
            sc_backend,
            sc.get("provider", default_route.get("provider")),
            sc.get("model", default_route.get("model")),
            cfg_perm,
            f"config-scenario({agent})",
        )

    return (
        default_route.get("backend", "native"),
        default_route.get("provider"),
        default_route.get("model"),
        cfg_perm,
        "config-default",
    )


# ---------------------------------------------------------------------------
# Target-Workspace Session Registry Management (Locked, Atomic, Realpath-Canonical)
# ---------------------------------------------------------------------------

def get_registry_path(cwd: str) -> str:
    state_dir = get_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    real_cwd = os.path.realpath(cwd)
    real_hash = hashlib.md5(real_cwd.encode("utf-8")).hexdigest()[:12]
    real_path = os.path.join(state_dir, f"registry_{real_hash}.json")
    if os.path.exists(real_path):
        return real_path

    abs_cwd = os.path.abspath(cwd)
    if abs_cwd != real_cwd:
        legacy_hash = hashlib.md5(abs_cwd.encode("utf-8")).hexdigest()[:12]
        legacy_path = os.path.join(state_dir, f"registry_{legacy_hash}.json")
        if os.path.exists(legacy_path):
            return legacy_path

    return real_path


def _read_registry_unlocked(reg_path: str, cwd: str) -> dict:
    if os.path.exists(reg_path):
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("cwd", os.path.realpath(cwd))
                    data.setdefault("sessions", [])
                    return data
        except Exception:
            pass
    return {"cwd": os.path.realpath(cwd), "sessions": []}


def _stamp_registry_meta(reg: dict):
    reg["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_registry(cwd: str) -> dict:
    reg_path = get_registry_path(cwd)
    try:
        with _file_lock(reg_path + ".lock", exclusive=False):
            return _read_registry_unlocked(reg_path, cwd)
    except Exception:
        return _read_registry_unlocked(reg_path, cwd)


def save_registry(cwd: str, data: dict):
    reg_path = get_registry_path(cwd)
    _stamp_registry_meta(data)
    try:
        with _file_lock(reg_path + ".lock", exclusive=True):
            _write_json_atomic(reg_path, data)
    except Exception:
        _write_json_atomic(reg_path, data)


def _is_pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def make_title(prompt: str, custom_title: str = None) -> str:
    """Extract a clean, human-readable title from custom_title or prompt text."""
    if custom_title and custom_title.strip():
        cleaned = re.sub(r"\s+", " ", custom_title.strip())
        return cleaned[:60] if len(cleaned) <= 60 else cleaned[:57].rstrip() + "..."

    lines = [l.strip() for l in (prompt or "").splitlines() if l.strip()]
    first_line = lines[0] if lines else "Untitled Task"
    first_line = re.sub(r"^(Goal:|Task:|Prompt:|Instructions?:|#+)\s*", "", first_line, flags=re.I).strip()
    first_line = TAG_STRIP.sub("", first_line).strip()
    first_line = re.sub(r"[`*~]+", "", first_line).strip()
    first_line = re.sub(r"\s+", " ", first_line).strip()
    if not first_line:
        first_line = "Task"
    if len(first_line) > 50:
        return first_line[:47].rstrip() + "..."
    return first_line


def _enrich_session_entry(s: dict, workspace_cwd: str = None) -> dict:
    entry = dict(s)
    raw_status = entry.get("status") or "completed"
    pid = entry.get("pid")
    if raw_status == "running":
        if pid and not _is_pid_alive(pid):
            entry["effective_status"] = "interrupted"
            entry["stale_pid"] = True
        else:
            entry["effective_status"] = "running"
            entry["stale_pid"] = False
    else:
        entry["effective_status"] = raw_status
        entry["stale_pid"] = False
    if workspace_cwd:
        entry["cwd"] = workspace_cwd
    return entry


def get_sessions_data(cwd: str) -> list:
    """Return session dicts enriched with effective live/stale-PID status for target workspace."""
    reg = load_registry(cwd)
    ws_cwd = reg.get("cwd") or os.path.realpath(cwd)
    return [_enrich_session_entry(s, workspace_cwd=ws_cwd) for s in reg.get("sessions", [])]


def list_all_sessions() -> list:
    """List sessions across all tracked target workspaces."""
    state_dir = get_state_dir()
    if not os.path.isdir(state_dir):
        return []
    results = []
    for fname in sorted(os.listdir(state_dir)):
        if not (fname.startswith("registry_") and fname.endswith(".json")):
            continue
        fpath = os.path.join(state_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            continue
        ws_cwd = data.get("cwd") or ""
        for s in data["sessions"]:
            item = _enrich_session_entry(s, workspace_cwd=ws_cwd)
            item["registryPath"] = fpath
            results.append(item)
    return results


def _format_sessions_lines(sessions: list, header: str, show_cwd: bool = False) -> str:
    out = [header]
    for s in sessions:
        sid = s.get("id")
        title = s.get("title", "Untitled")
        updated = s.get("updated_at", s.get("created_at", "N/A"))
        turns = s.get("turns", 1)
        model = s.get("model", "default")
        status = s.get("effective_status", "completed")
        if s.get("stale_pid"):
            status_label = "interrupted (stale pid)"
        elif status == "running" and s.get("pid"):
            status_label = f"running pid={s['pid']}"
        else:
            status_label = status
        wt = s.get("worktree_path")
        wt_suffix = f" | Worktree: `{wt}`" if wt else ""
        cwd_prefix = f"[`{s.get('cwd')}`] " if (show_cwd and s.get("cwd")) else ""
        out.append(
            f"- {cwd_prefix}**#{sid}** [{updated}] {title} ({status_label} | Turns: {turns} | Model: `{model}`{wt_suffix})"
        )
    return "\n".join(out)


def get_sessions_text(cwd: str, show_all: bool = False) -> str:
    """Return human/agent-readable string listing numbered sessions."""
    real_cwd = os.path.realpath(cwd)
    if show_all:
        sessions = list_all_sessions()
        if not sessions:
            return "No subagent sessions recorded across any workspace."
        return _format_sessions_lines(
            sessions,
            "### All Subagent Sessions Across Workspaces:",
            show_cwd=True,
        )

    sessions = get_sessions_data(real_cwd)
    if not sessions:
        return f"No subagent sessions recorded for workspace: {real_cwd}"
    return _format_sessions_lines(
        sessions,
        f"### Subagent Sessions for `{real_cwd}`:",
        show_cwd=False,
    )


def list_sessions(cwd: str, show_all: bool = False):
    print(get_sessions_text(cwd, show_all=show_all))


def clear_sessions(cwd: str) -> int:
    """Remove all recorded numbered sessions for target workspace `cwd`, returning removed count."""
    reg_path = get_registry_path(cwd)
    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        count = len(reg.get("sessions", []))
        reg["sessions"] = []
        _stamp_registry_meta(reg)
        _write_json_atomic(reg_path, reg)
        return count


def delete_session(cwd: str, ref: str) -> bool:
    """Delete a single numbered session (or UUID) from the registry for target workspace `cwd`."""
    reg_path = get_registry_path(cwd)
    clean_ref = str(ref).lstrip("#").strip()
    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        sessions = reg.get("sessions", [])
        kept = []
        removed = False
        for s in sessions:
            match_num = clean_ref.isdigit() and s.get("id") == int(clean_ref)
            match_uuid = s.get("session_id") == ref
            if match_num or match_uuid:
                removed = True
            else:
                kept.append(s)
        if removed:
            reg["sessions"] = kept
            _stamp_registry_meta(reg)
            _write_json_atomic(reg_path, reg)
        return removed


def resolve_session_entry(cwd: str, ref: str):
    """Resolve user reference (int, '#int', 'latest', or UUID) to full session dict or None."""
    reg = load_registry(cwd)
    sessions = reg.get("sessions", [])
    if not sessions:
        if ref and len(str(ref)) > 20:
            return {"session_id": ref, "id": None, "title": "External Session"}
        return None

    if ref in (None, "latest", "AUTO"):
        return dict(sessions[-1])

    clean_ref = str(ref).lstrip("#").strip()
    if clean_ref.isdigit():
        num_id = int(clean_ref)
        for s in sessions:
            if s.get("id") == num_id:
                return dict(s)

    for s in sessions:
        if s.get("session_id") == ref:
            return dict(s)

    if len(str(ref)) > 20:
        return {"session_id": ref, "id": None, "title": "Custom UUID"}

    return None


def resolve_session(cwd: str, ref: str) -> tuple:
    """Resolve user reference (int, '#int', 'latest', or UUID) to (session_id, numeric_id, title)."""
    entry = resolve_session_entry(cwd, ref)
    if not entry:
        return None, None, None
    return entry["session_id"], entry.get("id"), entry.get("title", "")


def register_running_session(
    cwd: str,
    session_id: str,
    prompt: str,
    model: str,
    existing_id: int = None,
    custom_title: str = None,
    worktree_path: str = None,
    pid: int = None,
) -> tuple:
    """Atomically pre-register or mark an active session as `running` before execution."""
    if not session_id or session_id == "N/A":
        return None, make_title(prompt, custom_title) if custom_title else "N/A"

    reg_path = get_registry_path(cwd)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        sessions = reg.setdefault("sessions", [])

        target = None
        if existing_id is not None:
            target = next((s for s in sessions if s.get("id") == existing_id), None)
        if target is None:
            target = next((s for s in sessions if s.get("session_id") == session_id), None)

        if target is not None:
            target["session_id"] = session_id
            target["turns"] = target.get("turns", 1) + 1
            target["updated_at"] = now_str
            target["model"] = model
            target["status"] = "running"
            target["pid"] = pid
            if custom_title and custom_title.strip():
                target["title"] = make_title(prompt, custom_title)
            if worktree_path:
                target["worktree_path"] = worktree_path
            _stamp_registry_meta(reg)
            _write_json_atomic(reg_path, reg)
            return target["id"], target.get("title", "")

        max_id = max([s.get("id", 0) for s in sessions], default=0)
        new_id = max_id + 1
        title = make_title(prompt, custom_title)

        new_entry = {
            "id": new_id,
            "session_id": session_id,
            "title": title,
            "created_at": now_str,
            "updated_at": now_str,
            "model": model,
            "turns": 1,
            "status": "running",
            "pid": pid,
        }
        if worktree_path:
            new_entry["worktree_path"] = worktree_path
        sessions.append(new_entry)
        _stamp_registry_meta(reg)
        _write_json_atomic(reg_path, reg)
        return new_id, title


def update_session_pid(cwd: str, numeric_id: int, pid: int):
    """Attach live subprocess PID to an active running session entry."""
    if numeric_id is None or not pid:
        return
    reg_path = get_registry_path(cwd)
    try:
        with _file_lock(reg_path + ".lock", exclusive=True):
            reg = _read_registry_unlocked(reg_path, cwd)
            for s in reg.get("sessions", []):
                if s.get("id") == numeric_id:
                    s["pid"] = pid
                    _write_json_atomic(reg_path, reg)
                    return
    except Exception:
        pass


def finalize_session(
    cwd: str,
    session_id: str,
    prompt: str,
    model: str,
    status: str = "completed",
    existing_id: int = None,
    custom_title: str = None,
    worktree_path: str = None,
    last_activity: str = None,
    turns_override: int = None,
) -> tuple:
    """Finalize a pre-registered (or unrecorded) session entry with final status and metadata."""
    if not session_id or session_id == "N/A":
        return None, make_title(prompt, custom_title) if custom_title else "N/A"

    reg_path = get_registry_path(cwd)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        sessions = reg.setdefault("sessions", [])

        target = None
        if existing_id is not None:
            target = next((s for s in sessions if s.get("id") == existing_id), None)
        if target is None:
            target = next((s for s in sessions if s.get("session_id") == session_id), None)

        if target is not None:
            target["session_id"] = session_id
            target["updated_at"] = now_str
            target["model"] = model
            if target.get("status") == "stopped" and status in ("failed", "interrupted"):
                target["status"] = "stopped"
            else:
                target["status"] = status
            target["pid"] = None
            if turns_override is not None:
                target["turns"] = turns_override
            if custom_title and custom_title.strip():
                target["title"] = make_title(prompt, custom_title)
            if worktree_path:
                target["worktree_path"] = worktree_path
            if last_activity:
                target["last_activity"] = last_activity[:240]
            _stamp_registry_meta(reg)
            _write_json_atomic(reg_path, reg)
            return target["id"], target.get("title", "")

        max_id = max([s.get("id", 0) for s in sessions], default=0)
        new_id = max_id + 1
        title = make_title(prompt, custom_title)
        new_entry = {
            "id": new_id,
            "session_id": session_id,
            "title": title,
            "created_at": now_str,
            "updated_at": now_str,
            "model": model,
            "turns": turns_override if turns_override is not None else 1,
            "status": status,
            "pid": None,
        }
        if worktree_path:
            new_entry["worktree_path"] = worktree_path
        if last_activity:
            new_entry["last_activity"] = last_activity[:240]
        sessions.append(new_entry)
        _stamp_registry_meta(reg)
        _write_json_atomic(reg_path, reg)
        return new_id, title


def record_session(
    cwd: str,
    session_id: str,
    prompt: str,
    model: str,
    existing_id: int = None,
    custom_title: str = None,
    worktree_path: str = None,
) -> tuple:
    """Atomically record or update a session in the registry, returning (numeric_id, title)."""
    if not session_id or session_id == "N/A":
        return None, make_title(prompt, custom_title) if custom_title else "N/A"

    reg_path = get_registry_path(cwd)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        sessions = reg.setdefault("sessions", [])

        if existing_id is not None:
            for s in sessions:
                if s.get("id") == existing_id:
                    s["session_id"] = session_id
                    s["turns"] = s.get("turns", 1) + 1
                    s["updated_at"] = now_str
                    s["model"] = model
                    s["status"] = "completed"
                    s["pid"] = None
                    if custom_title and custom_title.strip():
                        s["title"] = make_title(prompt, custom_title)
                    if worktree_path:
                        s["worktree_path"] = worktree_path
                    _stamp_registry_meta(reg)
                    _write_json_atomic(reg_path, reg)
                    return s["id"], s.get("title", "")

        for s in sessions:
            if s.get("session_id") == session_id:
                s["turns"] = s.get("turns", 1) + 1
                s["updated_at"] = now_str
                s["model"] = model
                s["status"] = "completed"
                s["pid"] = None
                if custom_title and custom_title.strip():
                    s["title"] = make_title(prompt, custom_title)
                if worktree_path:
                    s["worktree_path"] = worktree_path
                _stamp_registry_meta(reg)
                _write_json_atomic(reg_path, reg)
                return s["id"], s.get("title", "")

        max_id = max([s.get("id", 0) for s in sessions], default=0)
        new_id = max_id + 1
        title = make_title(prompt, custom_title)

        new_entry = {
            "id": new_id,
            "session_id": session_id,
            "title": title,
            "created_at": now_str,
            "updated_at": now_str,
            "model": model,
            "turns": 1,
            "status": "completed",
            "pid": None,
        }
        if worktree_path:
            new_entry["worktree_path"] = worktree_path
        sessions.append(new_entry)
        _stamp_registry_meta(reg)
        _write_json_atomic(reg_path, reg)
        return new_id, title


def stop_session(cwd: str, ref: str) -> dict:
    """Stop a running subagent session by terminating its process group, preserving session entry."""
    entry = resolve_session_entry(cwd, ref)
    if not entry or entry.get("id") is None:
        return {"ok": False, "found": False, "error": f"Session '{ref}' not found in {os.path.realpath(cwd)}"}

    reg_path = get_registry_path(cwd)
    pid = entry.get("pid")
    was_running = entry.get("status") == "running" and _is_pid_alive(pid)
    if was_running:
        _terminate_pid_group(pid, grace_sec=2.0)

    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        for s in reg.get("sessions", []):
            if s.get("id") == entry.get("id"):
                if was_running or s.get("status") == "running":
                    s["status"] = "stopped"
                    s["last_activity"] = "Stopped via `claude-subagent stop`"
                s["pid"] = None
                s["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                _stamp_registry_meta(reg)
                _write_json_atomic(reg_path, reg)
                return {
                    "ok": True,
                    "found": True,
                    "id": s["id"],
                    "wasRunning": was_running,
                    "status": s["status"],
                }

    return {"ok": False, "found": False, "error": f"Session '{ref}' not found"}


# ---------------------------------------------------------------------------
# Git & Worktree Inspection
# ---------------------------------------------------------------------------

def get_git_head(target_dir: str):
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def list_git_worktrees(target_dir: str) -> list:
    """Return list of dicts: [{'path': str, 'head': str, 'branch': str}]."""
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=target_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return []
        entries = []
        current = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                if current.get("path"):
                    entries.append(current)
                current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = os.path.abspath(line[len("worktree "):].strip())
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD "):].strip()
            elif line.startswith("branch "):
                ref = line[len("branch "):].strip()
                current["branch"] = ref.replace("refs/heads/", "", 1)
        if current.get("path"):
            entries.append(current)
        return entries
    except Exception:
        return []


def detect_worktree_info(target_cwd: str, worktree_arg, pre_worktrees: list, payload: dict = None) -> tuple:
    """Locate the active worktree (path, branch) created/used by `claude --worktree`."""
    if not worktree_arg:
        return None, None

    post_worktrees = list_git_worktrees(target_cwd)
    pre_paths = {w["path"] for w in pre_worktrees}

    if isinstance(payload, dict):
        for key in ("worktreePath", "worktree_path", "worktree"):
            val = payload.get(key)
            if isinstance(val, str) and os.path.isdir(val):
                abs_val = os.path.abspath(val)
                branch = next((w.get("branch") for w in post_worktrees if w["path"] == abs_val), None)
                return abs_val, branch

    added = [w for w in post_worktrees if w["path"] not in pre_paths]
    if len(added) == 1:
        return added[0]["path"], added[0].get("branch")

    if isinstance(worktree_arg, str) and worktree_arg.strip().lower() not in ("true", "1"):
        name = worktree_arg.strip()
        candidates = added or post_worktrees
        for w in candidates:
            base = os.path.basename(w["path"])
            branch = w.get("branch") or ""
            if base == name or branch == name or branch.endswith(f"/{name}") or branch == f"worktree-{name}":
                return w["path"], branch

    if added:
        return added[-1]["path"], added[-1].get("branch")

    return None, None


def _parse_shortstat(output: str) -> tuple:
    m = re.search(
        r"(\d+)\s+file[s]?\s+changed"
        r"(?:,\s*(\d+)\s+insertion[s]?\(\+\))?"
        r"(?:,\s*(\d+)\s+deletion[s]?\(\-\))?",
        output or "",
    )
    if not m:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def snapshot_git_state(target_dir: str):
    """Capture pre-run dirty file signatures {path: (status, mtime_ns, size)} to isolate run deltas."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        snap = {}
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            status_code = line[:2].strip() or "M"
            path = line[3:].strip() if len(line) > 3 else line[2:].strip()
            abs_p = os.path.join(target_dir, path)
            try:
                st = os.stat(abs_p)
                sig = (status_code, st.st_mtime_ns, st.st_size)
            except OSError:
                sig = (status_code, None, None)
            snap[path] = sig
        return snap
    except Exception:
        return None


def get_git_changes(target_dir: str, base_head: str = None, pre_snapshot: dict = None):
    """Return compact diff summary (run-delta aware when pre_snapshot given) and per-file status list."""
    try:
        post_snap = snapshot_git_state(target_dir)
        if post_snap is None:
            return "", []
        file_items = [(sig[0], path) for path, sig in post_snap.items()]

        commit_note = ""
        current_head = get_git_head(target_dir)
        if base_head and current_head and current_head != base_head:
            rev_count = subprocess.run(
                ["git", "rev-list", "--count", f"{base_head}..HEAD"],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            c_diff = subprocess.run(
                ["git", "diff", "--shortstat", f"{base_head}..HEAD"],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            c_files, c_add, c_del = _parse_shortstat(c_diff.stdout)
            n_commits = rev_count.stdout.strip() or "1"
            commit_note = f"{n_commits} commit(s) ({c_files} file(s), +{c_add}/-{c_del})"

        if pre_snapshot is not None and len(pre_snapshot) > 0:
            delta_items = []
            for path, sig in post_snap.items():
                if path not in pre_snapshot or pre_snapshot[path] != sig:
                    delta_items.append((sig[0], path))
            for path in pre_snapshot:
                if path not in post_snap:
                    delta_items.append(("R/clean", path))

            parts = []
            if delta_items:
                parts.append(
                    f"{len(delta_items)} file(s) touched this run ({len(pre_snapshot)} pre-existing dirty)"
                )
            if commit_note:
                parts.append(commit_note)
            if not parts:
                return "", []
            return "; ".join(parts), delta_items

        added = 0
        deleted = 0
        if file_items:
            unstaged = subprocess.run(
                ["git", "diff", "--shortstat"],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            staged = subprocess.run(
                ["git", "diff", "--cached", "--shortstat"],
                cwd=target_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            _, u_add, u_del = _parse_shortstat(unstaged.stdout)
            _, s_add, s_del = _parse_shortstat(staged.stdout)
            added = u_add + s_add
            deleted = u_del + s_del

        parts = []
        if file_items:
            parts.append(f"{len(file_items)} working-tree file(s) changed, +{added}/-{deleted}")
        if commit_note:
            parts.append(commit_note)

        if not parts:
            return "", []

        return "; ".join(parts), file_items
    except Exception:
        return "", []


# ---------------------------------------------------------------------------
# Telemetry, Stream-JSON Parsing & Output Formatting
# ---------------------------------------------------------------------------

def parse_stream_event(line: str):
    """Parse a single NDJSON line from `claude --output-format stream-json`."""
    stripped = ANSI_ESCAPE.sub("", line or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_activity_from_assistant_message(msg: dict) -> tuple:
    """Return (model_or_none, activity_or_none, text_or_none) from a Claude assistant message."""
    if not isinstance(msg, dict):
        return None, None, None
    model = msg.get("model") or None
    activity = None
    last_text = None
    content = msg.get("content")
    if isinstance(content, list):
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tname = block.get("name", "tool")
                tinp = block.get("input") or {}
                hint = ""
                for key in ("command", "file_path", "path", "pattern", "query"):
                    if isinstance(tinp, dict) and tinp.get(key):
                        hint = str(tinp[key]).splitlines()[0][:120]
                        break
                activity = f"Tool `{tname}`" + (f": {hint}" if hint else "")
                break
            if btype == "text" and block.get("text", "").strip():
                txt_lines = [l.strip() for l in block["text"].splitlines() if l.strip()]
                if txt_lines:
                    last_text = block["text"].strip()
                    activity = txt_lines[-1][:200]
                break
    return model, activity, last_text


def clean_output(raw_text: str):
    """Strip ANSI escapes and locate/parse the final result JSON payload (supports stream-json & json)."""
    cleaned = ANSI_ESCAPE.sub("", raw_text or "")
    parsed_dicts = []

    for line in cleaned.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    parsed_dicts.append(obj)
            except json.JSONDecodeError:
                continue

    if parsed_dicts:
        for obj in reversed(parsed_dicts):
            if obj.get("type") == "result":
                return obj
        for obj in reversed(parsed_dicts):
            if "result" in obj or "session_id" in obj:
                return obj
        return parsed_dicts[-1]

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


def _encode_claude_project_dir(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]", "-", os.path.abspath(cwd))


def recover_session_telemetry(cwd: str, session_uuid: str, claude_projects_dir: str = None) -> dict:
    """Inspect Claude Code's native session JSONL on timeout/crash to recover model, turns, and last activity."""
    if not session_uuid:
        return {}
    projects_root = claude_projects_dir or os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_root):
        return {}

    candidates = []
    for path_variant in (os.path.abspath(cwd), os.path.realpath(cwd)):
        encoded = _encode_claude_project_dir(path_variant)
        cand = os.path.join(projects_root, encoded, f"{session_uuid}.jsonl")
        if cand not in candidates:
            candidates.append(cand)

    transcript_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not transcript_path:
        return {}

    model = None
    num_turns = 0
    last_activity = None

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue

                etype = entry.get("type")
                if etype == "cost-state":
                    mu = entry.get("modelUsage")
                    if isinstance(mu, dict) and mu:
                        model = list(mu.keys())[0]
                elif etype == "assistant":
                    num_turns += 1
                    msg_model, msg_act, _ = _extract_activity_from_assistant_message(entry.get("message"))
                    if msg_model:
                        model = msg_model
                    if msg_act:
                        last_activity = msg_act
    except Exception:
        return {"exists": True, "transcript_path": transcript_path}

    return {
        "exists": True,
        "transcript_path": transcript_path,
        "model": model,
        "num_turns": num_turns,
        "last_activity": last_activity,
    }


def extract_actual_model(payload: dict, fallback_model: str) -> str:
    """Extract canonical model name actually executed from Claude Code payload."""
    if payload and isinstance(payload, dict):
        model_usage = payload.get("modelUsage")
        if model_usage and isinstance(model_usage, dict) and len(model_usage) > 0:
            return list(model_usage.keys())[0]
        if payload.get("model"):
            return payload.get("model")
    return fallback_model or "default"


def is_free_or_local_model(actual_model: str = None, provider: str = None) -> bool:
    model_l = (actual_model or "").lower()
    prov_l = (provider or "").lower()
    return (
        model_l.endswith(":free")
        or prov_l in ("ollama", "lmstudio", "local")
        or "ollama" in model_l
        or "-dflash" in model_l
    )


def format_metrics(
    payload: dict,
    elapsed_sec: float,
    actual_model: str = None,
    provider: str = None,
    recovered_turns: int = None,
) -> str:
    """Build concise telemetry snippet: duration, turns, optional cost (omitting fake USD on :free/local)."""
    parts = []
    duration_ms = payload.get("duration_ms") if isinstance(payload, dict) else None
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        parts.append(f"{duration_ms / 1000.0:.1f}s")
    elif elapsed_sec is not None and elapsed_sec >= 0:
        parts.append(f"{elapsed_sec:.1f}s")

    num_turns = payload.get("num_turns") if isinstance(payload, dict) else None
    if not (isinstance(num_turns, int) and num_turns > 0) and isinstance(recovered_turns, int) and recovered_turns > 0:
        num_turns = recovered_turns
    if isinstance(num_turns, int) and num_turns > 0:
        parts.append(f"{num_turns} turn{'s' if num_turns != 1 else ''}")

    is_free_or_local = is_free_or_local_model(actual_model=actual_model, provider=provider)
    cost = payload.get("total_cost_usd") if isinstance(payload, dict) else None
    if is_free_or_local:
        parts.append("free")
    elif isinstance(cost, (int, float)) and cost > 0:
        parts.append(f"est. ${cost:.4f}")

    return " · ".join(parts)


def truncate_error(text: str, max_lines: int = 12) -> str:
    """Return last N actionable lines of failure."""
    cleaned = ANSI_ESCAPE.sub("", text or "")
    lines = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip() and not line.startswith("[claude-code:") and not line.startswith("auth:")
    ]
    if not lines:
        return "(no error output recorded)"
    return "\n".join("  " + line for line in lines[-max_lines:])


def format_delivery_report(
    text: str,
    max_lines: int = None,
    max_chars: int = None,
) -> str:
    """Clean and return the full Markdown delivery report (no truncation unless explicitly requested)."""
    cleaned = ANSI_ESCAPE.sub("", text or "").strip()
    filtered_lines = []
    for line in cleaned.splitlines():
        if line.startswith("[claude-code:") or line.startswith("auth:") or line.startswith("crouter:"):
            continue
        filtered_lines.append(line)

    if not filtered_lines:
        return "Subagent completed the task with no text output."

    joined = "\n".join(filtered_lines)
    over_lines = bool(max_lines and max_lines > 0 and len(filtered_lines) > max_lines)
    over_chars = bool(max_chars and max_chars > 0 and len(joined) > max_chars)
    if over_lines or over_chars:
        truncated = "\n".join(filtered_lines[:max_lines]) if over_lines else joined
        if max_chars and max_chars > 0 and len(truncated) > max_chars:
            truncated = truncated[:max_chars].rstrip()
        return truncated + "\n\n... (Delivery report capped per explicit --max-output-chars limit)"

    return joined


def _signal_pid_group(pid: int, sig: int):
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass
    try:
        os.kill(pid, sig)
    except Exception:
        pass


def _signal_process_group(proc: subprocess.Popen, sig: int):
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except Exception:
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except Exception:
        pass


def _pgid_alive(pgid: int) -> bool:
    if not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _terminate_pid_group(pid: int, grace_sec: float = 2.0):
    """Best-effort SIGINT -> SIGTERM -> SIGKILL teardown for an externally stopped session PID."""
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        _signal_pid_group(pid, sig)
        deadline = time.monotonic() + grace_sec
        while time.monotonic() < deadline:
            if not _pgid_alive(pid) and not _is_pid_alive(pid):
                return
            time.sleep(0.05)


def _terminate_process_group(proc: subprocess.Popen, threads: list, grace_sec: float = 3.0):
    """Escalate SIGINT -> SIGTERM -> SIGKILL across the full process group until all members exit."""
    pgid = proc.pid
    for sig, wait_sec in ((signal.SIGINT, grace_sec), (signal.SIGTERM, grace_sec), (signal.SIGKILL, None)):
        _signal_process_group(proc, sig)
        if wait_sec is None:
            try:
                proc.wait()
            except Exception:
                pass
            for t in threads:
                t.join(timeout=2)
            return

        deadline = time.monotonic() + wait_sec
        try:
            proc.wait(timeout=wait_sec)
        except subprocess.TimeoutExpired:
            continue

        while time.monotonic() < deadline:
            for t in threads:
                t.join(timeout=0.05)
            pipes_closed = not any(t.is_alive() for t in threads)
            if pipes_closed and not _pgid_alive(pgid):
                return
            time.sleep(0.05)


def run_subprocess_streaming(cmd, cwd, timeout=None, on_line=None, on_start=None):
    """Launch cmd in its own process group, streaming stdout lines while avoiding pipe deadlocks."""
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    effective_timeout = timeout if (timeout is not None and timeout > 0) else None
    popen_kwargs = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:
            pass

    stdout_chunks = []
    stderr_chunks = []

    def _read_stdout():
        try:
            for line in iter(proc.stdout.readline, ""):
                stdout_chunks.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _read_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_chunks.append(line)
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()
    threads = [t_out, t_err]

    timed_out = False
    try:
        proc.wait(timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc, threads, grace_sec=3.0)
    except KeyboardInterrupt:
        _terminate_process_group(proc, threads, grace_sec=2.0)
        raise

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout_text = "".join(stdout_chunks)
    stderr_text = "".join(stderr_chunks)
    if timed_out:
        timeout_note = f"\n[claude-subagent] Timed out after {timeout}s; terminated subprocess group."
        return -1, stdout_text, stderr_text + timeout_note
    return proc.returncode, stdout_text, stderr_text


def run_subprocess_safely(cmd, cwd, timeout=None):
    """Launch cmd in its own process group; strip TMUX so tmux hooks never fire for headless runs."""
    return run_subprocess_streaming(cmd, cwd, timeout=timeout)


# ---------------------------------------------------------------------------
# Command Construction & Role/Prompt Helpers
# ---------------------------------------------------------------------------

def resolve_prompt_input(prompt: str) -> str:
    """Resolve '-' (stdin), an existing file path, or an inline prompt string."""
    if prompt == "-":
        return sys.stdin.read()
    if prompt and "\n" not in prompt and len(prompt) < 512:
        try:
            if os.path.isfile(prompt):
                with open(prompt, "r", encoding="utf-8") as f:
                    return f.read()
        except OSError:
            pass
    return prompt


def resolve_json_schema_input(schema: str) -> str:
    """Resolve inline JSON schema string or a path to a JSON schema file."""
    if not schema:
        return schema
    if "\n" not in schema and len(schema) < 512:
        try:
            if os.path.isfile(schema):
                with open(schema, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except OSError:
            pass
    return schema


def _resolve_role_defaults(role: str = None, agent: str = None, allowed_tools: str = None, readonly: bool = False) -> tuple:
    """Resolve `--readonly` and `--role research|review|implement` into (agent, allowed_tools)."""
    eff_agent = agent
    eff_tools = allowed_tools
    if readonly and not eff_tools:
        eff_tools = "readonly"

    if role:
        norm_role = role.strip().lower()
        role_map = {
            "research": ("workflow-researcher", "readonly"),
            "review": ("workflow-reviewer", "readonly"),
            "implement": ("workflow-implementer", None),
        }
        if norm_role in role_map:
            agent_name, fallback_tools = role_map[norm_role]
            agent_file = os.path.expanduser(f"~/.claude/agents/{agent_name}.md")
            if not eff_agent and os.path.isfile(agent_file):
                eff_agent = agent_name
            elif not eff_tools and fallback_tools:
                eff_tools = fallback_tools

    return eff_agent, eff_tools


def build_command(
    prompt: str,
    backend: str = None,
    provider: str = None,
    model: str = None,
    resume_uuid: str = None,
    continue_session: bool = False,
    session_id: str = None,
    worktree=None,
    budget=None,
    bare: bool = False,
    strict_mcp: bool = False,
    allowed_tools: str = None,
    disallowed_tools: str = None,
    agent: str = None,
    effort: str = None,
    title: str = None,
    add_dirs: list = None,
    append_system_prompt: str = None,
    permission_mode: str = "bypassPermissions",
    crouter_path: str = None,
    claude_path: str = None,
    output_format: str = "stream-json",
    chrome: bool = False,
    mcp_configs: list = None,
    fork_session: bool = False,
    fallback_model: str = None,
    json_schema: str = None,
    no_session_persistence: bool = False,
    extra_claude_args: list = None,
) -> list:
    """Build exact crouter/claude command line without mutating saved crouter defaults."""
    resolved_crouter = find_crouter() if crouter_path is None else crouter_path
    crouter_usable = bool(
        resolved_crouter and os.path.exists(resolved_crouter) and os.access(resolved_crouter, os.X_OK)
    )
    norm_backend = (backend or "").strip().lower()
    norm_provider = (provider or "").strip()

    claude_args = [
        "-p",
        prompt,
        "--output-format",
        output_format or "stream-json",
    ]
    if (output_format or "stream-json") == "stream-json":
        claude_args.append("--verbose")

    claude_args.extend(["--permission-mode", permission_mode or "bypassPermissions"])
    claude_args.append("--chrome" if chrome else "--no-chrome")

    if bare:
        claude_args.append("--bare")

    if strict_mcp:
        claude_args.append("--strict-mcp-config")

    if mcp_configs:
        for cfg in mcp_configs:
            if cfg and str(cfg).strip():
                claude_args.extend(["--mcp-config", str(cfg).strip()])

    if allowed_tools:
        resolved_tools = TOOL_PRESETS.get(allowed_tools.strip().lower(), allowed_tools.strip())
        claude_args.extend(["--tools", resolved_tools])

    if disallowed_tools:
        claude_args.extend(["--disallowedTools", disallowed_tools.strip()])

    if agent:
        claude_args.extend(["--agent", agent.strip()])

    if effort:
        claude_args.extend(["--effort", effort.strip()])

    if title and title.strip():
        claude_args.extend(["-n", title.strip()])

    if add_dirs:
        for d in add_dirs:
            if d and d.strip():
                claude_args.extend(["--add-dir", os.path.abspath(d.strip())])

    if append_system_prompt:
        claude_args.extend(["--append-system-prompt", append_system_prompt])

    if fallback_model and str(fallback_model).strip():
        claude_args.extend(["--fallback-model", str(fallback_model).strip()])

    if json_schema and str(json_schema).strip():
        claude_args.extend(["--json-schema", str(json_schema).strip()])

    if no_session_persistence:
        claude_args.append("--no-session-persistence")

    if resume_uuid:
        claude_args.extend(["--resume", resume_uuid])
        if fork_session:
            claude_args.append("--fork-session")
    elif continue_session:
        claude_args.append("--continue")
        if fork_session:
            claude_args.append("--fork-session")
    elif session_id and not no_session_persistence:
        claude_args.extend(["--session-id", session_id])

    if str(worktree).lower() in ("true", "1") or worktree is True:
        claude_args.append("--worktree")
    elif isinstance(worktree, str) and worktree.strip():
        claude_args.extend(["--worktree", worktree.strip()])

    if budget is not None and str(budget).strip():
        claude_args.extend(["--max-budget-usd", str(budget).strip()])

    if extra_claude_args:
        claude_args.extend(extra_claude_args)

    want_native = norm_backend == "native" or norm_provider == "claude"
    use_crouter = crouter_usable and not want_native

    if norm_backend == "crouter" and not crouter_usable:
        raise ValueError(
            "Configured backend is 'crouter', but `crouter` was not found in PATH (or CLAUDE_SUBAGENT_CROUTER). "
            "Install crouter or re-run `claude-subagent setup --backend native --provider claude`."
        )

    if use_crouter:
        if norm_provider in ("", "default", "auto", "native"):
            cmd = [resolved_crouter, "run"]
            if model:
                cmd.extend(["--once", "--model", model])
            cmd.append("--")
            return cmd + claude_args

        cmd = [resolved_crouter, norm_provider, "--once"]
        if model:
            cmd.extend(["--model", model])
        cmd.append("--")
        return cmd + claude_args

    # Direct Claude CLI execution
    if norm_provider not in ("", "default", "auto", "native", "claude"):
        raise ValueError(
            f"--provider '{norm_provider}' requires `crouter` in PATH (or CLAUDE_SUBAGENT_CROUTER). "
            "Omit --provider or pass --provider claude to run native Claude Code directly."
        )
    resolved_claude = find_claude() if claude_path is None else claude_path
    cmd = [resolved_claude]
    if model:
        claude_args.extend(["--model", model])
    return cmd + claude_args


# ---------------------------------------------------------------------------
# Core Subagent Execution
# ---------------------------------------------------------------------------

def _emit_stderr(msg: str, quiet: bool = False):
    if quiet:
        return
    try:
        sys.stderr.write(msg.rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def execute_subagent(
    prompt: str,
    cwd: str = None,
    resume: str = None,
    continue_session: bool = False,
    worktree: str = None,
    model: str = None,
    provider: str = None,
    budget: float = None,
    timeout: int = None,
    bare: bool = False,
    strict_mcp: bool = False,
    with_mcp: bool = False,
    mcp_configs: list = None,
    chrome: bool = False,
    allowed_tools: str = None,
    disallowed_tools: str = None,
    agent: str = None,
    role: str = None,
    readonly: bool = False,
    effort: str = None,
    title: str = None,
    add_dirs: list = None,
    append_system_prompt: str = None,
    permission_mode: str = None,
    allow_unconfigured: bool = False,
    max_output_chars: int = None,
    raw_json: bool = False,
    json_output: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    fork_session: bool = False,
    fallback_model: str = None,
    json_schema: str = None,
    ephemeral: bool = False,
    extra_claude_args: list = None,
) -> tuple:
    """Execute Claude subagent and return (exit_code: int, output_text: str)."""
    target_cwd = os.path.realpath(cwd or os.getcwd())
    if not os.path.isdir(target_cwd):
        msg = f"Error: Target directory does not exist or is not a directory: {target_cwd}"
        if json_output:
            return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": msg}, indent=2, ensure_ascii=False)
        return 1, msg

    prompt = resolve_prompt_input(prompt)
    if not prompt or not prompt.strip():
        msg = "Error: Prompt is empty."
        if json_output:
            return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": msg}, indent=2, ensure_ascii=False)
        return 1, msg

    eff_agent, eff_allowed_tools = _resolve_role_defaults(
        role=role, agent=agent, allowed_tools=allowed_tools, readonly=readonly
    )

    config = load_config()
    try:
        eff_backend, eff_provider, eff_model, cfg_perm, route_source = resolve_effective_route(
            config=config,
            cli_provider=provider,
            cli_model=model,
            agent=eff_agent,
            allow_unconfigured=allow_unconfigured,
        )
    except RuntimeError as e:
        if str(e) == "SETUP_REQUIRED":
            env_info = discover_environment()
            if json_output:
                return EXIT_SETUP_REQUIRED, json.dumps(
                    {
                        "ok": False,
                        "status": "setup_required",
                        "exitCode": EXIT_SETUP_REQUIRED,
                        "discovery": env_info,
                        "error": "SETUP_REQUIRED: Run `claude-subagent setup --discover` and persist a default route.",
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            return EXIT_SETUP_REQUIRED, format_setup_required_message(env_info)
        return 1, f"Error: {e}"
    except ValueError as e:
        if json_output:
            return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": str(e)}, indent=2, ensure_ascii=False)
        return 1, f"Error: {e}"

    eff_permission_mode = permission_mode or cfg_perm or "bypassPermissions"
    policies = (config or {}).get("policies") or {}

    if with_mcp:
        eff_strict_mcp = False
    elif strict_mcp:
        eff_strict_mcp = True
    elif mcp_configs:
        eff_strict_mcp = True
    else:
        eff_strict_mcp = bool(policies.get("strictMcp", True))

    resolved_schema = resolve_json_schema_input(json_schema) if json_schema else None

    target_session_uuid = None
    target_numeric_id = None
    target_title = None
    prior_worktree_path = None

    if resume:
        entry = resolve_session_entry(target_cwd, resume)
        if not entry:
            sessions_hint = get_sessions_text(target_cwd)
            err = f"Error: Could not find session '{resume}' in {target_cwd}.\n\n{sessions_hint}"
            if json_output:
                return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": err}, indent=2, ensure_ascii=False)
            return 1, err
        target_session_uuid = entry["session_id"]
        target_numeric_id = entry.get("id")
        target_title = entry.get("title")
        prior_worktree_path = entry.get("worktree_path")
    elif continue_session:
        entry = resolve_session_entry(target_cwd, "latest")
        if entry:
            target_session_uuid = entry["session_id"]
            target_numeric_id = entry.get("id")
            target_title = entry.get("title")
            prior_worktree_path = entry.get("worktree_path")

    if fork_session:
        target_numeric_id = None

    is_new_session = not (target_session_uuid or continue_session)
    pre_session_uuid = None if (not is_new_session or ephemeral) else str(uuid.uuid4())
    output_format = "json" if raw_json else "stream-json"

    try:
        cmd = build_command(
            prompt=prompt,
            backend=eff_backend,
            provider=eff_provider,
            model=eff_model,
            resume_uuid=target_session_uuid,
            continue_session=continue_session,
            session_id=pre_session_uuid,
            worktree=worktree,
            budget=budget,
            bare=bare,
            strict_mcp=eff_strict_mcp,
            allowed_tools=eff_allowed_tools,
            disallowed_tools=disallowed_tools,
            agent=eff_agent,
            effort=effort,
            title=title,
            add_dirs=add_dirs,
            append_system_prompt=append_system_prompt,
            permission_mode=eff_permission_mode,
            output_format=output_format,
            chrome=chrome,
            mcp_configs=mcp_configs,
            fork_session=fork_session,
            fallback_model=fallback_model,
            json_schema=resolved_schema,
            no_session_persistence=ephemeral,
            extra_claude_args=extra_claude_args,
        )
    except ValueError as e:
        if json_output:
            return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": str(e)}, indent=2, ensure_ascii=False)
        return 1, f"Error: {e}"

    if dry_run:
        return 0, json.dumps(
            {
                "cwd": target_cwd,
                "routeSource": route_source,
                "backend": eff_backend,
                "provider": eff_provider,
                "model": eff_model,
                "agent": eff_agent,
                "allowedTools": eff_allowed_tools,
                "permissionMode": eff_permission_mode,
                "strictMcp": eff_strict_mcp,
                "chrome": chrome,
                "ephemeral": ephemeral,
                "forkSession": fork_session,
                "sessionId": target_session_uuid or pre_session_uuid,
                "command": cmd,
                "shell": " ".join(shlex.quote(x) for x in cmd),
            },
            indent=2,
            ensure_ascii=False,
        )

    base_head = get_git_head(target_cwd)
    is_git_repo = base_head is not None
    pre_worktrees = list_git_worktrees(target_cwd) if (is_git_repo and worktree) else []
    active_prior_wt = prior_worktree_path if (prior_worktree_path and os.path.isdir(prior_worktree_path)) else None
    git_snap_dir = active_prior_wt or target_cwd
    wt_base_head = get_git_head(active_prior_wt) if active_prior_wt else base_head
    pre_git_snap = None if (not is_git_repo or worktree) else snapshot_git_state(git_snap_dir)

    cfg_default_model = (config or {}).get("defaultRoute", {}).get("model") if not model else None
    initial_model_label = eff_model or cfg_default_model or eff_provider or "default"

    pre_reg_id = None
    pre_reg_title = make_title(prompt, title) if (title or not target_title) else target_title
    provisional_uuid = pre_session_uuid or target_session_uuid

    if not ephemeral and provisional_uuid and not fork_session:
        pre_reg_id, pre_reg_title = register_running_session(
            target_cwd,
            provisional_uuid,
            prompt,
            initial_model_label,
            existing_id=target_numeric_id,
            custom_title=title,
            worktree_path=active_prior_wt,
        )

    live_state = {
        "session_id": provisional_uuid,
        "model": None,
        "turns": 0,
        "last_activity": None,
        "last_text": None,
        "last_reported_activity": None,
    }
    started_at = time.monotonic()

    sess_tag = f"#{pre_reg_id}" if pre_reg_id else ("fork" if fork_session else "ephemeral")
    route_desc = f"{eff_provider or eff_backend}/{initial_model_label}"
    _emit_stderr(
        f"[claude-subagent] Session {sess_tag} (\"{pre_reg_title}\") started | route={route_desc} | cwd={target_cwd}",
        quiet=quiet,
    )

    def _on_stream_line(line: str):
        ev = parse_stream_event(line)
        if not ev:
            return
        if ev.get("session_id") and not live_state["session_id"]:
            live_state["session_id"] = ev.get("session_id")
        etype = ev.get("type")
        if etype == "system" and ev.get("subtype") == "init":
            if ev.get("model"):
                live_state["model"] = ev.get("model")
        elif etype == "assistant":
            live_state["turns"] += 1
            msg_model, msg_act, msg_txt = _extract_activity_from_assistant_message(ev.get("message"))
            if msg_model:
                live_state["model"] = msg_model
            if msg_txt:
                live_state["last_text"] = msg_txt
            if msg_act:
                live_state["last_activity"] = msg_act
                if msg_act != live_state["last_reported_activity"]:
                    live_state["last_reported_activity"] = msg_act
                    dt = time.monotonic() - started_at
                    _emit_stderr(
                        f"[claude-subagent {sess_tag} +{dt:.1f}s · turn {live_state['turns']}] {msg_act}",
                        quiet=quiet,
                    )
        elif etype == "result":
            if ev.get("session_id"):
                live_state["session_id"] = ev.get("session_id")

    def _on_start(pid: int):
        if pre_reg_id is not None:
            update_session_pid(target_cwd, pre_reg_id, pid)

    finalized = False
    try:
        try:
            returncode, stdout, stderr = run_subprocess_streaming(
                cmd,
                target_cwd,
                timeout=timeout,
                on_line=_on_stream_line if output_format == "stream-json" else None,
                on_start=_on_start,
            )
        except FileNotFoundError as e:
            if pre_reg_id is not None and provisional_uuid:
                finalize_session(
                    target_cwd,
                    provisional_uuid,
                    prompt,
                    initial_model_label,
                    status="failed",
                    existing_id=pre_reg_id,
                    custom_title=title,
                    worktree_path=active_prior_wt,
                    last_activity=f"Launch error: {e}",
                )
                finalized = True
            err = f"Error launching Claude subagent: {e}"
            if json_output:
                return 1, json.dumps({"ok": False, "status": "error", "exitCode": 1, "error": err}, indent=2, ensure_ascii=False)
            return 1, err
        elapsed_sec = time.monotonic() - started_at

        payload = clean_output(stdout)

        new_wt_path, new_wt_branch = detect_worktree_info(target_cwd, worktree, pre_worktrees, payload) if is_git_repo else (None, None)
        worktree_path = new_wt_path or active_prior_wt
        worktree_branch = new_wt_branch
        if worktree_path and not worktree_branch and is_git_repo:
            wt_entries = list_git_worktrees(target_cwd)
            real_wt = os.path.realpath(worktree_path)
            worktree_branch = next(
                (w.get("branch") for w in wt_entries if os.path.realpath(w["path"]) == real_wt),
                None,
            )

        git_inspect_dir = worktree_path or target_cwd
        if is_git_repo:
            git_summary, git_files = get_git_changes(
                git_inspect_dir,
                base_head=wt_base_head if (active_prior_wt and not new_wt_path) else base_head,
                pre_snapshot=None if new_wt_path else pre_git_snap,
            )
        else:
            git_summary, git_files = "", []

        success = returncode == 0 and payload is not None and not payload.get("is_error")
        status_label = "SUCCESS" if success else "FAILED"
        final_status = "completed" if success else ("interrupted" if returncode == -1 else "failed")

        candidate_uuid = (
            (payload.get("session_id") if payload else None)
            or live_state.get("session_id")
            or target_session_uuid
            or pre_session_uuid
        )
        recovered = {}
        if payload is None and candidate_uuid:
            recovered = recover_session_telemetry(git_inspect_dir, candidate_uuid)
            if not recovered and git_inspect_dir != target_cwd:
                recovered = recover_session_telemetry(target_cwd, candidate_uuid)

        model_fallback = (
            live_state.get("model")
            or recovered.get("model")
            or eff_model
            or cfg_default_model
            or eff_provider
            or "default"
        )
        actual_model = extract_actual_model(payload, model_fallback)
        last_activity = live_state.get("last_activity") or recovered.get("last_activity")
        effective_turns = live_state.get("turns") or recovered.get("num_turns")

        if payload:
            result_text = payload.get("result", "")
            if not result_text and live_state.get("last_text"):
                result_text = live_state["last_text"]
        else:
            result_text = live_state.get("last_text") or stdout.strip() or stderr.strip()

        should_record = not ephemeral and bool(
            (payload and payload.get("session_id"))
            or live_state.get("session_id")
            or target_session_uuid
            or recovered.get("exists")
            or pre_reg_id is not None
        )
        session_uuid = candidate_uuid if should_record else None
        if should_record and session_uuid:
            numeric_id, resolved_title = finalize_session(
                target_cwd,
                session_uuid,
                prompt,
                actual_model,
                status=final_status,
                existing_id=pre_reg_id if pre_reg_id is not None else target_numeric_id,
                custom_title=title,
                worktree_path=worktree_path,
                last_activity=last_activity,
            )
            finalized = True
            persisted = resolve_session_entry(target_cwd, str(numeric_id))
            if persisted and persisted.get("status") == "stopped":
                final_status = "stopped"
        else:
            numeric_id, resolved_title = None, make_title(prompt, title) if title else (target_title or "Ephemeral")

        _emit_stderr(
            f"[claude-subagent] Session {f'#{numeric_id}' if numeric_id else 'ephemeral'} finished: "
            f"{final_status} ({elapsed_sec:.1f}s)",
            quiet=quiet,
        )

        if raw_json:
            if payload:
                return (0 if returncode == 0 else 1), json.dumps(payload, indent=2, ensure_ascii=False)
            return (0 if returncode == 0 else 1), stdout

        if payload and payload.get("is_error") and payload.get("result"):
            error_blob = str(payload.get("result"))
            if stderr and stderr.strip():
                error_blob += "\n" + stderr.strip()
        else:
            error_blob = stderr or stdout

        if json_output:
            duration_ms = payload.get("duration_ms") if isinstance(payload, dict) else None
            num_turns = (payload.get("num_turns") if isinstance(payload, dict) else None) or effective_turns
            cost = payload.get("total_cost_usd") if isinstance(payload, dict) else None
            envelope = {
                "ok": success,
                "status": "success" if success else final_status,
                "exitCode": 0 if success else 1,
                "session": {
                    "id": numeric_id,
                    "uuid": session_uuid,
                    "title": resolved_title or target_title or "",
                    "cwd": target_cwd,
                    "worktreePath": worktree_path,
                    "worktreeBranch": worktree_branch,
                },
                "route": {
                    "source": route_source,
                    "backend": eff_backend,
                    "provider": eff_provider,
                    "model": actual_model,
                    "strictMcp": eff_strict_mcp,
                    "chrome": chrome,
                },
                "metrics": {
                    "elapsedSec": round(elapsed_sec, 2),
                    "durationMs": duration_ms,
                    "turns": num_turns,
                    "costUsd": None if is_free_or_local_model(actual_model, eff_provider) else cost,
                    "isFreeOrLocal": is_free_or_local_model(actual_model, eff_provider),
                },
                "git": {
                    "summary": git_summary,
                    "files": [{"status": sc, "path": p} for sc, p in git_files],
                },
                "report": format_delivery_report(result_text, max_chars=max_output_chars) if success else "",
                "lastActivity": last_activity,
                "error": None if success else truncate_error(error_blob),
            }
            return (0 if success else 1), json.dumps(envelope, indent=2, ensure_ascii=False)

        lines = []
        display_title = resolved_title or target_title or ""
        session_label = f"#{numeric_id} (\"{display_title}\")" if numeric_id else (f"\"{display_title}\"" if display_title else "")
        session_str = f" Session {session_label} |" if session_label else ""
        metrics_str = format_metrics(
            payload,
            elapsed_sec,
            actual_model=actual_model,
            provider=eff_provider,
            recovered_turns=effective_turns,
        )
        metrics_suffix = f" | {metrics_str}" if metrics_str else ""
        lines.append(f"### [Claude Subagent: {status_label}]{session_str} Model: `{actual_model}`{metrics_suffix}")

        if worktree_path:
            branch_part = f" (branch: `{worktree_branch}`)" if worktree_branch else ""
            lines.append(f"**Worktree:** `{worktree_path}`{branch_part}")

        if git_summary:
            lines.append(f"**Git Changes:** {git_summary}")
            if git_files:
                for status_code, path in git_files[:15]:
                    lines.append(f"  * `{status_code}` {path}")
                if len(git_files) > 15:
                    lines.append(f"  * ... and {len(git_files) - 15} more files")
            lines.append("")

        if not success:
            if last_activity:
                lines.append(f"**Last Activity Before Interruption:** {last_activity}")
            if numeric_id:
                cwd_hint = f"-C {shlex.quote(target_cwd)} " if target_cwd != os.path.realpath(os.getcwd()) else ""
                lines.append(
                    f"**Resume:** `claude-subagent send {numeric_id} {cwd_hint}\"Continue from where you stopped.\"`"
                )
            lines.append("**Error (Actionable Details):**")
            lines.append(truncate_error(error_blob))
        else:
            lines.append("**Subagent Delivery & Review Report:**")
            lines.append(format_delivery_report(result_text, max_chars=max_output_chars))

        return (0 if success else 1), "\n".join(lines)
    finally:
        if not finalized and pre_reg_id is not None and provisional_uuid:
            try:
                finalize_session(
                    target_cwd,
                    provisional_uuid,
                    prompt,
                    live_state.get("model") or initial_model_label,
                    status="interrupted",
                    existing_id=pre_reg_id,
                    custom_title=title,
                    worktree_path=active_prior_wt,
                    last_activity=live_state.get("last_activity"),
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Setup, Config, Models, Doctor, Sessions & Native-Verb Handlers
# ---------------------------------------------------------------------------

def _parse_scenario_args(items: list, existing: dict = None) -> dict:
    """Parse repeatable `--scenario name=provider:model` (or `name=provider`) entries."""
    out = dict(existing or {})
    for raw in items or []:
        if "=" not in raw:
            raise ValueError(f"Invalid --scenario '{raw}'. Expected format: name=provider:model")
        name, rhs = raw.split("=", 1)
        name = name.strip()
        rhs = rhs.strip()
        if not name or not rhs:
            raise ValueError(f"Invalid --scenario '{raw}'. Expected format: name=provider:model")
        if ":" in rhs:
            prov, mod = rhs.split(":", 1)
            prov, mod = prov.strip(), mod.strip()
        else:
            prov, mod = rhs, None
        out[name] = {
            "backend": "native" if prov == "claude" else "crouter",
            "provider": prov,
            "model": mod,
        }
    return out


def handle_setup(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-subagent setup",
        description="Discover available models/providers and persist default subagent route & policies.",
    )
    parser.add_argument("--discover", action="store_true", help="Inspect available Claude CLI / crouter models without writing config")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output discovery or saved config as JSON")
    parser.add_argument("--backend", choices=["crouter", "native"], default=None, help="Execution backend (crouter or native)")
    parser.add_argument("--provider", default=None, help="Default provider (e.g. openrouter, ollama, claude)")
    parser.add_argument("-m", "--model", default=None, help="Default model ID/alias")
    parser.add_argument("--delegation-mode", choices=DELEGATION_MODES, default=None, help="Default team delegation posture")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default=None, help="Default Claude permission mode")
    parser.add_argument("--usage-policy", default=None, help="Free-form delegation/usage rules or reminders saved for agents")
    parser.add_argument(
        "--scenario", action="append", default=None,
        help="Optional per-agent/scenario route override as name=provider:model (repeatable)"
    )
    parser.add_argument("--note", default=None, help="Optional human note recorded in config")
    parser.add_argument("--strict-mcp-default", dest="strict_mcp_default", action="store_true", default=None, help="Default to --strict-mcp-config for fast headless starts (default: true)")
    parser.add_argument("--with-mcp-default", dest="strict_mcp_default", action="store_false", help="Default to loading global MCP servers on headless runs")
    parser.add_argument("--replace", action="store_true", help="Replace existing configuration instead of merging partial flags")

    args = parser.parse_args(argv)
    env_info = discover_environment()

    if args.discover:
        if args.as_json:
            print(json.dumps(env_info, indent=2, ensure_ascii=False))
        else:
            print(format_discovery_report(env_info))
        return 0

    has_write_flags = any(
        x is not None
        for x in (
            args.backend,
            args.provider,
            args.model,
            args.delegation_mode,
            args.permission_mode,
            args.usage_policy,
            args.scenario,
            args.note,
            args.strict_mcp_default,
        )
    )

    if not has_write_flags:
        if not sys.stdin.isatty():
            if args.as_json:
                print(json.dumps(env_info, indent=2, ensure_ascii=False))
            else:
                print(format_setup_required_message(env_info))
            return EXIT_SETUP_REQUIRED

        print(format_discovery_report(env_info))
        print("\n--- Interactive First-Run Setup (saved as persistent default) ---")
        default_backend = "crouter" if env_info["crouter"]["installed"] else "native"
        backend = input(f"Backend [crouter/native] ({default_backend}): ").strip() or default_backend
        default_prov = "claude" if backend == "native" else (env_info["crouter"].get("activeSelection") or "openrouter")
        provider = input(f"Default provider ({default_prov}): ").strip() or default_prov
        model = input("Default model ID (leave blank for provider default): ").strip() or None
        delegation_mode = input("Delegation mode [proactive/ask-first/explicit-only] (proactive): ").strip() or "proactive"
        usage_policy = input("Optional usage policy / reminder note (blank for none): ").strip() or ""
        cfg = build_config_payload(
            backend=backend,
            provider=provider,
            model=model,
            delegation_mode=delegation_mode,
            usage_policy=usage_policy,
        )
        path = save_config(cfg)
        print(f"\nSaved persistent configuration to: {path}")
        return 0

    existing = {} if args.replace else (load_config() or {})
    existing_route = existing.get("defaultRoute") or {}
    existing_pol = existing.get("policies") or {}
    existing_scenarios = existing_pol.get("scenarioOverrides") or {}
    try:
        scenarios = _parse_scenario_args(args.scenario, existing=existing_scenarios)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    eff_provider = args.provider if args.provider is not None else existing_route.get("provider")
    eff_model = args.model if args.model is not None else existing_route.get("model")
    eff_note = args.note if args.note is not None else existing_route.get("note")

    resolved_backend = args.backend
    if not resolved_backend:
        if args.provider == "claude":
            resolved_backend = "native"
        elif args.provider:
            resolved_backend = "crouter"
        elif existing_route.get("backend"):
            resolved_backend = existing_route.get("backend")
        else:
            resolved_backend = "crouter" if env_info["crouter"]["installed"] else "native"

    eff_delegation = args.delegation_mode if args.delegation_mode is not None else existing_pol.get("delegationMode", "proactive")
    eff_permission = args.permission_mode if args.permission_mode is not None else existing_pol.get("permissionMode", "bypassPermissions")
    eff_usage_policy = args.usage_policy if args.usage_policy is not None else existing_pol.get("usagePolicy", "")
    eff_strict_mcp = args.strict_mcp_default if args.strict_mcp_default is not None else existing_pol.get("strictMcp", True)

    cfg = build_config_payload(
        backend=resolved_backend,
        provider=eff_provider,
        model=eff_model,
        delegation_mode=eff_delegation,
        usage_policy=eff_usage_policy,
        scenario_overrides=scenarios,
        permission_mode=eff_permission,
        note=eff_note,
        strict_mcp=eff_strict_mcp,
    )
    path = save_config(cfg)
    if args.as_json:
        print(json.dumps({"configPath": path, "config": cfg}, indent=2, ensure_ascii=False))
    else:
        print(f"Saved persistent configuration to: {path}")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def handle_config(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-subagent config",
        description="Inspect or reset persistent claude-subagent configuration.",
    )
    parser.add_argument("action", choices=["show", "path", "reset"], help="Config action")
    args = parser.parse_args(argv)

    if args.action == "path":
        print(get_config_path())
        return 0

    if args.action == "reset":
        removed = reset_config()
        print("Reset persistent configuration." if removed else "No configuration file existed.")
        return 0

    cfg = load_config()
    if cfg is None:
        print(f"Not configured yet (expected path: {get_config_path()}). Run `claude-subagent setup --discover`.")
        return EXIT_SETUP_REQUIRED
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def handle_models(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-subagent models",
        description="List saved default route, native Claude aliases, and configured crouter provider models.",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args(argv)

    env_info = discover_environment()
    cfg = env_info.get("config") or {}
    default_route = cfg.get("defaultRoute")
    payload = {
        "configured": env_info["configured"],
        "defaultRoute": default_route,
        "nativeAliases": list(NATIVE_CLAUDE_MODEL_ALIASES),
        "claudeAvailable": env_info["claude"]["available"],
        "crouter": env_info["crouter"],
        "agents": env_info["agents"],
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    lines = ["### Available Subagent Models & Routes"]
    if default_route:
        lines.append(
            f"- **Saved Default**: `{default_route.get('backend')}` / "
            f"`{default_route.get('provider') or 'auto'}` / `{default_route.get('model') or 'provider-default'}`"
        )
    else:
        lines.append("- **Saved Default**: not configured yet (`claude-subagent setup --discover`)")

    lines.append(f"- **Native Claude Aliases** (`-m sonnet|opus|haiku|fable` auto-routes to native `claude`): {', '.join(f'`{a}`' for a in NATIVE_CLAUDE_MODEL_ALIASES)}")

    cr = env_info["crouter"]
    if cr["installed"] and cr["providers"]:
        lines.append("- **Configured `crouter` Providers** (`--provider <p> [-m <model>]`):")
        for p in cr["providers"]:
            lines.append(f"  - `{p['provider']}` ({p['status']}, auth={p['auth']}) → `{p['defaultModel']}`")
    else:
        lines.append("- **crouter**: not installed or no providers configured")

    lines.append("")
    lines.append("Usage: `claude-subagent -m <model> \"<task>\"` or `claude-subagent --provider <p> -m <model> \"<task>\"`")
    print("\n".join(lines))
    return 0


def handle_doctor(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-subagent doctor",
        description="Read-only health check for claude-subagent config, binaries, routes, and workspace sessions.",
    )
    parser.add_argument("-C", "--cwd", "--workspace", dest="cwd", default=os.getcwd(), help="Target workspace directory")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args(argv)

    target_cwd = os.path.realpath(args.cwd or os.getcwd())
    env_info = discover_environment()
    cfg = env_info.get("config")
    sessions = get_sessions_data(target_cwd) if os.path.isdir(target_cwd) else []

    running_count = sum(1 for s in sessions if s.get("effective_status") == "running")
    interrupted_count = sum(1 for s in sessions if s.get("effective_status") == "interrupted")

    policies = (cfg or {}).get("policies") or {}
    default_route = (cfg or {}).get("defaultRoute") or {}
    report = {
        "ok": bool(env_info["claude"]["available"] and env_info["configured"]),
        "version": __version__,
        "cwd": target_cwd,
        "configPath": env_info["configPath"],
        "configured": env_info["configured"],
        "effectiveDefaultRoute": default_route if cfg else None,
        "policies": {
            "delegationMode": policies.get("delegationMode") if cfg else None,
            "permissionMode": policies.get("permissionMode") if cfg else None,
            "strictMcpDefault": policies.get("strictMcp", True) if cfg else True,
            "usagePolicy": policies.get("usagePolicy") if cfg else None,
            "scenarioOverrides": policies.get("scenarioOverrides", {}) if cfg else {},
        },
        "claude": env_info["claude"],
        "crouter": env_info["crouter"],
        "agents": env_info["agents"],
        "sessionsSummary": {
            "registryPath": get_registry_path(target_cwd),
            "total": len(sessions),
            "running": running_count,
            "interrupted": interrupted_count,
        },
        "nextStep": None if env_info["configured"] else "Run `claude-subagent setup --discover` and persist a default route.",
    }

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    lines = [
        f"### claude-subagent Doctor (`v{__version__}`)",
        format_discovery_report(env_info),
    ]
    if cfg:
        lines.append(
            f"- **Default Route**: `{default_route.get('backend')}` / "
            f"`{default_route.get('provider') or 'auto'}` / `{default_route.get('model') or 'provider-default'}`"
        )
        lines.append(
            f"- **Headless Defaults**: `strictMcp={policies.get('strictMcp', True)}`, "
            f"`permissionMode={policies.get('permissionMode', 'bypassPermissions')}`, "
            f"`delegationMode={policies.get('delegationMode', 'proactive')}`"
        )
        if policies.get("usagePolicy"):
            lines.append(f"- **Usage Policy**: {policies['usagePolicy']}")
    else:
        lines.append("- **Next Step**: Run `claude-subagent setup --discover` and persist a default route.")

    lines.append(
        f"- **Workspace Sessions** (`{target_cwd}`): {len(sessions)} total "
        f"(running: {running_count}, interrupted: {interrupted_count})"
    )
    print("\n".join(lines))
    return 0


def handle_sessions(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-subagent sessions",
        description="Inspect, show details, stop, delete, or clear workspace-bound subagent sessions.",
    )
    parser.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "show", "stop", "delete", "clear"],
        help="Session action (default: list)"
    )
    parser.add_argument("target", nargs="?", default=None, help="Session #id or UUID for show/stop/delete")
    parser.add_argument("-C", "--cwd", "--workspace", dest="cwd", default=os.getcwd(), help="Target workspace directory")
    parser.add_argument("--all", dest="show_all", action="store_true", help="List sessions across all tracked workspaces")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output machine-readable JSON")
    args = parser.parse_args(argv)

    target_cwd = os.path.realpath(args.cwd or os.getcwd())

    if args.action == "list":
        sessions = list_all_sessions() if args.show_all else get_sessions_data(target_cwd)
        if args.as_json:
            print(
                json.dumps(
                    {
                        "cwd": None if args.show_all else target_cwd,
                        "all": args.show_all,
                        "registryPath": None if args.show_all else get_registry_path(target_cwd),
                        "sessions": sessions,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        list_sessions(target_cwd, show_all=args.show_all)
        return 0

    if args.action == "show":
        if not args.target:
            msg = "Error: `sessions show` requires a session ID (e.g. `1`, `#1`, or `latest`)."
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
            else:
                print(msg, file=sys.stderr)
            return 1
        entry = resolve_session_entry(target_cwd, args.target)
        if not entry:
            msg = f"Session '{args.target}' not found in workspace: {target_cwd}"
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
            else:
                print(msg, file=sys.stderr)
            return 1

        enriched = next(
            (s for s in get_sessions_data(target_cwd) if s.get("id") == entry.get("id")),
            _enrich_session_entry(entry, workspace_cwd=target_cwd),
        )
        wt = enriched.get("worktree_path")
        rec = {}
        if wt and os.path.isdir(wt):
            rec = recover_session_telemetry(wt, enriched.get("session_id"))
        if not rec:
            rec = recover_session_telemetry(target_cwd, enriched.get("session_id"))
        enriched["transcript"] = rec

        if args.as_json:
            print(json.dumps({"ok": True, "cwd": target_cwd, "session": enriched}, indent=2, ensure_ascii=False))
            return 0

        lines = [
            f"### Session #{enriched.get('id')} — {enriched.get('title')}",
            f"- **Workspace**: `{target_cwd}`",
            f"- **UUID**: `{enriched.get('session_id')}`",
            f"- **Status**: `{enriched.get('effective_status', enriched.get('status', 'completed'))}`",
            f"- **Model**: `{enriched.get('model')}` | **Turns**: {enriched.get('turns')}",
            f"- **Updated**: {enriched.get('updated_at')}",
        ]
        if wt:
            lines.append(f"- **Worktree**: `{wt}`")
        act = enriched.get("last_activity") or rec.get("last_activity")
        if act:
            lines.append(f"- **Last Activity**: {act}")
        if rec.get("transcript_path"):
            lines.append(f"- **Transcript**: `{rec['transcript_path']}`")
        print("\n".join(lines))
        return 0

    if args.action == "stop":
        if not args.target:
            msg = "Error: `stop` requires a session ID (e.g. `1` or `latest`)."
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
            else:
                print(msg, file=sys.stderr)
            return 1
        res = stop_session(target_cwd, args.target)
        if args.as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("ok") else 1
        if res.get("ok"):
            verb = "Stopped running" if res.get("wasRunning") else "Session already idle"
            print(f"{verb}: #{res['id']} (status: {res['status']})")
            return 0
        print(res.get("error"), file=sys.stderr)
        return 1

    if args.action == "delete":
        if not args.target:
            msg = "Error: `sessions delete` requires a session ID."
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}, indent=2, ensure_ascii=False))
            else:
                print(msg, file=sys.stderr)
            return 1
        entry = resolve_session_entry(target_cwd, args.target)
        if entry and entry.get("status") == "running" and _is_pid_alive(entry.get("pid")):
            _terminate_pid_group(entry["pid"], grace_sec=2.0)
        ok = delete_session(target_cwd, args.target)
        if args.as_json:
            print(json.dumps({"ok": ok, "cwd": target_cwd, "target": args.target}, indent=2, ensure_ascii=False))
            return 0 if ok else 1
        if ok:
            print(f"Deleted session '{args.target}' for: {target_cwd}")
            return 0
        print(f"Session '{args.target}' not found in: {target_cwd}", file=sys.stderr)
        return 1

    removed = clear_sessions(target_cwd)
    if args.as_json:
        print(json.dumps({"ok": True, "cwd": target_cwd, "removed": removed}, indent=2, ensure_ascii=False))
    else:
        print(f"Cleared {removed} session(s) for: {target_cwd}")
    return 0


def _handle_target_verb(action: str, argv: list) -> int:
    """Handle top-level target verbs like `show <id>`, `stop <id>`, `rm <id>` regardless of flag order."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-C", "--cwd", "--workspace", dest="cwd", default=os.getcwd())
    parser.add_argument("--json", dest="as_json", action="store_true")
    parser.add_argument("positionals", nargs="*")
    ns, _ = parser.parse_known_args(argv)

    cmd_argv = [action]
    if ns.positionals:
        cmd_argv.append(ns.positionals[0])
    cmd_argv.extend(["--cwd", ns.cwd])
    if ns.as_json:
        cmd_argv.append("--json")
    return handle_sessions(cmd_argv)


# ---------------------------------------------------------------------------
# Main CLI Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-subagent",
        description=(
            "Native-grade headless Claude Code subagent runner with target-workspace session binding, "
            "flexible model selection, fast headless defaults, live progress streaming, and git worktree isolation.\n\n"
            "Quick commands:\n"
            "  claude-subagent \"<task>\"                        Spawn in current dir (or pass -C <dir>)\n"
            "  claude-subagent -C <dir> \"<task>\"               Spawn bound to target workspace <dir>\n"
            "  claude-subagent -m <model> \"<task>\"             One-shot model override (keeps default otherwise)\n"
            "  claude-subagent -w \"<task>\"                     Spawn in an isolated git worktree\n"
            "  claude-subagent --readonly \"<task>\"             Spawn a read-only scout/reviewer\n"
            "  claude-subagent send <id> \"<message>\"           Continue session #id in target workspace\n"
            "  claude-subagent fork <id> \"<message>\"           Branch session #id into a new #id\n"
            "  claude-subagent ps [--all]                      List sessions in workspace (or across all)\n"
            "  claude-subagent show|stop|rm <id>               Inspect, stop, or remove session #id\n"
            "  claude-subagent models [--json]                 List saved default, native aliases & crouter models\n"
            "  claude-subagent doctor [--json]                 Check config, binaries & workspace sessions\n"
            "  claude-subagent setup | config | sessions"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "positionals", nargs="*",
        help="Task prompt (positional shorthand for -p/--prompt)"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-p", "--prompt", default=None, help="Task prompt, file path, or '-' to read from stdin"
    )
    parser.add_argument(
        "-l", "--list", "--list-sessions", dest="list_sessions", action="store_true",
        help="List numbered subagent sessions for the target workspace"
    )
    parser.add_argument(
        "--delete-session", default=None,
        help="Remove a session entry by number (e.g. 1 or #1) or UUID"
    )
    parser.add_argument(
        "--clear-sessions", action="store_true",
        help="Clear all recorded session entries for the target workspace"
    )
    parser.add_argument(
        "--provider", default=None,
        help="One-shot provider override (bypasses persisted default; use 'claude' for native Anthropic)"
    )
    parser.add_argument(
        "-m", "--model", default=None,
        help="One-shot model override (auto-routes native sonnet/opus/haiku/fable/claude-*, else inherits default provider)"
    )
    parser.add_argument(
        "--fallback-model", default=None,
        help="Comma-separated fallback model(s) passed to Claude --fallback-model"
    )
    parser.add_argument(
        "--allow-unconfigured", action="store_true",
        help="Bypass the first-run setup gate for a one-off invocation"
    )
    parser.add_argument(
        "-C", "--cwd", "--workspace", dest="cwd", default=os.getcwd(),
        help="Target workspace directory (sessions #1, #2 bind to this folder)"
    )
    parser.add_argument(
        "-r", "--resume", default=None,
        help="Resume session by number (e.g. -r 1 or -r #2), or UUID"
    )
    parser.add_argument(
        "-c", "--continue-session", action="store_true",
        help="Continue the most recent session in the target workspace"
    )
    parser.add_argument(
        "--fork", "--fork-session", dest="fork_session", action="store_true",
        help="When used with -r, -c, or fork verb, branch into a new numbered session (--fork-session)"
    )
    parser.add_argument(
        "--ephemeral", "--no-session-persistence", dest="ephemeral", action="store_true",
        help="Skip saving session transcript and registry entry (--no-session-persistence)"
    )
    parser.add_argument(
        "-w", "--worktree", nargs="?", const=True, default=None,
        help="Create or use a git worktree for isolated execution (optional branch/worktree name)"
    )
    parser.add_argument(
        "-n", "--name", "--title", dest="title", default=None,
        help="Custom display title for this session (auto-derived from prompt if omitted)"
    )
    parser.add_argument(
        "--agent", default=None,
        help="Invoke a specific Claude Code agent definition (from ~/.claude/agents/ or .claude/agents/)"
    )
    parser.add_argument(
        "--role", choices=["research", "review", "implement"], default=None,
        help="Quick role preset (maps to workflow-researcher/reviewer/implementer or readonly fallback)"
    )
    parser.add_argument(
        "-R", "--readonly", action="store_true",
        help="Read-only shorthand (equivalent to --allowed-tools readonly)"
    )
    parser.add_argument(
        "--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort level for this session"
    )
    parser.add_argument(
        "--permission-mode", default=None, choices=PERMISSION_MODES,
        help="Claude Code permission mode override (default from config or bypassPermissions)"
    )
    parser.add_argument(
        "--budget", default=None, help="Max budget in USD (e.g. 0.50)"
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="Optional command timeout in seconds (default: none / unlimited; pass 0 to disable)"
    )
    parser.add_argument(
        "--max-output-chars", type=int, default=None,
        help="Optional cap on delivery report length in chars (default: unlimited)"
    )
    parser.add_argument(
        "--allowed-tools", default=None,
        help="Optional comma-separated tool whitelist or preset ('coding', 'readonly')"
    )
    parser.add_argument(
        "--tools", default=None, help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--disallowed-tools", default=None,
        help="Optional comma-separated tools to deny (passed to --disallowedTools)"
    )
    parser.add_argument(
        "--add-dir", action="append", default=None,
        help="Additional directory to grant tool access to (repeatable)"
    )
    parser.add_argument(
        "--append-system-prompt", default=None,
        help="Append instructions to the default Claude Code system prompt"
    )
    parser.add_argument(
        "--json-schema", default=None,
        help="JSON Schema string or path to a schema file for structured output (--json-schema)"
    )
    parser.add_argument(
        "--bare", action="store_true",
        help="Minimal mode: pass native --bare (skips hooks, plugins, and settings)"
    )
    parser.add_argument(
        "--strict-mcp", "--strict-mcp-config", dest="strict_mcp", action="store_true",
        help="Skip external MCP servers via native --strict-mcp-config (enabled by default)"
    )
    parser.add_argument(
        "--with-mcp", action="store_true",
        help="Load global/project MCP servers instead of skipping them via --strict-mcp-config"
    )
    parser.add_argument(
        "--mcp-config", action="append", default=None,
        help="Load specific MCP config JSON file or string (repeatable, implies --strict-mcp-config)"
    )
    parser.add_argument(
        "--chrome", action="store_true",
        help="Enable Claude in Chrome integration (disabled via --no-chrome by default)"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress live progress lines emitted to stderr"
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit structured machine-readable JSON delivery envelope to stdout"
    )
    parser.add_argument(
        "--raw-json", action="store_true", help="Print raw Claude JSON result payload instead of formatted report"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print resolved route & command as JSON without invoking Claude"
    )
    return parser


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    verb = raw_argv[0] if raw_argv else None
    if verb == "setup":
        sys.exit(handle_setup(raw_argv[1:]))
    if verb == "config":
        sys.exit(handle_config(raw_argv[1:]))
    if verb == "models":
        sys.exit(handle_models(raw_argv[1:]))
    if verb == "doctor":
        sys.exit(handle_doctor(raw_argv[1:]))
    if verb == "sessions":
        sys.exit(handle_sessions(raw_argv[1:]))
    if verb in ("list", "ps"):
        sys.exit(handle_sessions(["list"] + raw_argv[1:]))
    if verb == "show":
        sys.exit(_handle_target_verb("show", raw_argv[1:]))
    if verb == "stop":
        sys.exit(_handle_target_verb("stop", raw_argv[1:]))
    if verb in ("rm", "close"):
        sys.exit(_handle_target_verb("delete", raw_argv[1:]))
    if verb == "clear":
        sys.exit(handle_sessions(["clear"] + raw_argv[1:]))

    sub_verb = None
    preset_resume = None
    if verb in ("run", "send", "reply", "followup", "resume", "fork"):
        sub_verb = verb
        raw_argv = raw_argv[1:]
        if sub_verb in ("send", "reply", "followup", "resume", "fork") and raw_argv and not raw_argv[0].startswith("-") and raw_argv[0] != "--":
            preset_resume = raw_argv[0]
            raw_argv = raw_argv[1:]

    extra_claude_args = None
    if "--" in raw_argv:
        sep_idx = raw_argv.index("--")
        cli_argv = raw_argv[:sep_idx]
        extra_claude_args = raw_argv[sep_idx + 1:]
    else:
        cli_argv = raw_argv

    parser = build_parser()
    args = parser.parse_args(cli_argv)

    if args.clear_sessions:
        removed = clear_sessions(args.cwd)
        if args.json_output:
            print(json.dumps({"ok": True, "cwd": os.path.realpath(args.cwd), "removed": removed}, indent=2, ensure_ascii=False))
        else:
            print(f"Cleared {removed} session(s) for: {os.path.realpath(args.cwd)}")
        sys.exit(0)

    if args.delete_session:
        ok = delete_session(args.cwd, args.delete_session)
        if args.json_output:
            print(json.dumps({"ok": ok, "cwd": os.path.realpath(args.cwd), "target": args.delete_session}, indent=2, ensure_ascii=False))
            sys.exit(0 if ok else 1)
        if ok:
            print(f"Deleted session '{args.delete_session}' for: {os.path.realpath(args.cwd)}")
            sys.exit(0)
        print(f"Session '{args.delete_session}' not found in: {os.path.realpath(args.cwd)}", file=sys.stderr)
        sys.exit(1)

    if args.list_sessions:
        if args.json_output:
            sys.exit(handle_sessions(["list", "--cwd", args.cwd, "--json"]))
        list_sessions(args.cwd)
        sys.exit(0)

    pos = list(args.positionals or [])
    if sub_verb in ("send", "reply", "followup", "resume", "fork") and not preset_resume and not args.resume and pos:
        preset_resume = pos.pop(0)

    eff_resume = args.resume or preset_resume
    eff_fork = args.fork_session or (sub_verb == "fork")

    if sub_verb in ("send", "reply", "followup", "resume", "fork") and not eff_resume and not args.continue_session:
        print(f"Error: `{sub_verb}` requires a session ID (e.g. `claude-subagent {sub_verb} 1 \"...\"`).", file=sys.stderr)
        sys.exit(1)

    eff_prompt = args.prompt or (" ".join(pos) if pos else None)
    eff_worktree = args.worktree

    if not eff_prompt and not sys.stdin.isatty():
        stdin_data = sys.stdin.read()
        if stdin_data and stdin_data.strip():
            eff_prompt = stdin_data

    # Smart `-w "<prompt>"` promotion when no separate prompt was passed
    if not eff_prompt and isinstance(eff_worktree, str) and (" " in eff_worktree or "\n" in eff_worktree or len(eff_worktree) > 40):
        eff_prompt = eff_worktree
        eff_worktree = True

    if not eff_prompt:
        if sub_verb in ("send", "reply", "followup", "resume", "fork"):
            print(f"Error: Missing message for `{sub_verb}`.", file=sys.stderr)
            sys.exit(1)
        parser.print_help()
        sys.exit(1)

    exit_code, output = execute_subagent(
        prompt=eff_prompt,
        cwd=args.cwd,
        resume=eff_resume,
        continue_session=args.continue_session,
        worktree=eff_worktree,
        model=args.model,
        provider=args.provider,
        budget=args.budget,
        timeout=args.timeout,
        bare=args.bare,
        strict_mcp=args.strict_mcp,
        with_mcp=args.with_mcp,
        mcp_configs=args.mcp_config,
        chrome=args.chrome,
        allowed_tools=args.allowed_tools or args.tools,
        disallowed_tools=args.disallowed_tools,
        agent=args.agent,
        role=args.role,
        readonly=args.readonly,
        effort=args.effort,
        title=args.title,
        add_dirs=args.add_dir,
        append_system_prompt=args.append_system_prompt,
        permission_mode=args.permission_mode,
        allow_unconfigured=args.allow_unconfigured,
        max_output_chars=args.max_output_chars,
        raw_json=args.raw_json,
        json_output=args.json_output,
        dry_run=args.dry_run,
        quiet=args.quiet,
        fork_session=eff_fork,
        fallback_model=args.fallback_model,
        json_schema=args.json_schema,
        ephemeral=args.ephemeral,
        extra_claude_args=extra_claude_args,
    )

    print(output)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
