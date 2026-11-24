#!/usr/bin/env python3
"""
Headless Claude Code Subagent Runner (`claude-subagent`).

Turns Claude Code (native or paired with `crouter`) into a persistent, stateful,
worktree-isolated worker CLI for any orchestrating AI agent or developer workflow.

Highlights:
  * First-run discovery & persistent setup (~/.config/claude-subagent/config.json):
    records confirmed default model/provider, delegation/usage policy, and optional
    per-scenario overrides so agents never silently pick an unconfirmed model.
  * Works out of the box with native `claude`; pairs seamlessly with `crouter`.
  * Zero permanent MCP overhead (Skill + CLI architecture, Python 3.9+ stdlib only).
  * File-locked atomic numbered sessions (#1, #2, ...) per workspace.
  * Worktree-aware & dirty-tree-delta Git change reporting.
  * Process-group timeout teardown (SIGINT -> SIGTERM -> SIGKILL) to prevent orphans.
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
        "*(Or pass explicit `--provider`/`--model`, or `--allow-unconfigured` for a one-off bypass.)*",
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
            "usagePolicy": (usage_policy or "").strip(),
            "scenarioOverrides": scenario_overrides or {},
        },
    }


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
    cfg_perm = policies.get("permissionMode") or "bypassPermissions"

    if explicit_override:
        backend = "native" if cli_provider == "claude" else ("crouter" if find_crouter() else "native")
        return backend, cli_provider, cli_model, cfg_perm, "cli-override"

    if config is None:
        backend = "crouter" if find_crouter() else "native"
        return backend, None, None, cfg_perm, "unconfigured-bypass"

    default_route = config.get("defaultRoute") or {}
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
# Session Registry Management (Locked & Atomic)
# ---------------------------------------------------------------------------

def get_registry_path(cwd: str) -> str:
    state_dir = get_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    dir_hash = hashlib.md5(os.path.abspath(cwd).encode("utf-8")).hexdigest()[:12]
    return os.path.join(state_dir, f"registry_{dir_hash}.json")


def _read_registry_unlocked(reg_path: str, cwd: str) -> dict:
    if os.path.exists(reg_path):
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("cwd", os.path.abspath(cwd))
                    data.setdefault("sessions", [])
                    return data
        except Exception:
            pass
    return {"cwd": os.path.abspath(cwd), "sessions": []}


def load_registry(cwd: str) -> dict:
    reg_path = get_registry_path(cwd)
    try:
        with _file_lock(reg_path + ".lock", exclusive=False):
            return _read_registry_unlocked(reg_path, cwd)
    except Exception:
        return _read_registry_unlocked(reg_path, cwd)


def save_registry(cwd: str, data: dict):
    reg_path = get_registry_path(cwd)
    try:
        with _file_lock(reg_path + ".lock", exclusive=True):
            _write_json_atomic(reg_path, data)
    except Exception:
        _write_json_atomic(reg_path, data)


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


def get_sessions_text(cwd: str) -> str:
    """Return human/agent-readable string listing numbered sessions."""
    reg = load_registry(cwd)
    sessions = reg.get("sessions", [])
    if not sessions:
        return f"No subagent sessions recorded for: {os.path.abspath(cwd)}"

    out = [f"### Subagent Sessions for `{os.path.abspath(cwd)}`:"]
    for s in sessions:
        sid = s.get("id")
        title = s.get("title", "Untitled")
        updated = s.get("updated_at", s.get("created_at", "N/A"))
        turns = s.get("turns", 1)
        model = s.get("model", "default")
        wt = s.get("worktree_path")
        wt_suffix = f" | Worktree: `{wt}`" if wt else ""
        out.append(f"- **#{sid}** [{updated}] {title} (Turns: {turns} | Model: `{model}`{wt_suffix})")
    return "\n".join(out)


def list_sessions(cwd: str):
    print(get_sessions_text(cwd))


def clear_sessions(cwd: str) -> int:
    """Remove all recorded numbered sessions for `cwd`, returning removed count."""
    reg_path = get_registry_path(cwd)
    with _file_lock(reg_path + ".lock", exclusive=True):
        reg = _read_registry_unlocked(reg_path, cwd)
        count = len(reg.get("sessions", []))
        reg["sessions"] = []
        _write_json_atomic(reg_path, reg)
        return count


def delete_session(cwd: str, ref: str) -> bool:
    """Delete a single numbered session (or UUID) from the registry for `cwd`."""
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
            _write_json_atomic(reg_path, reg)
        return removed


def resolve_session(cwd: str, ref: str) -> tuple:
    """Resolve user reference (int, '#int', 'latest', or UUID) to (session_id, numeric_id, title)."""
    reg = load_registry(cwd)
    sessions = reg.get("sessions", [])
    if not sessions:
        if ref and len(str(ref)) > 20:
            return ref, None, "External Session"
        return None, None, None

    if ref in (None, "latest", "AUTO"):
        last = sessions[-1]
        return last["session_id"], last["id"], last.get("title", "")

    clean_ref = str(ref).lstrip("#").strip()
    if clean_ref.isdigit():
        num_id = int(clean_ref)
        for s in sessions:
            if s.get("id") == num_id:
                return s["session_id"], s["id"], s.get("title", "")

    for s in sessions:
        if s.get("session_id") == ref:
            return s["session_id"], s["id"], s.get("title", "")

    if len(str(ref)) > 20:
        return ref, None, "Custom UUID"

    return None, None, None


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
                    if custom_title and custom_title.strip():
                        s["title"] = make_title(prompt, custom_title)
                    if worktree_path:
                        s["worktree_path"] = worktree_path
                    _write_json_atomic(reg_path, reg)
                    return s["id"], s.get("title", "")

        for s in sessions:
            if s.get("session_id") == session_id:
                s["turns"] = s.get("turns", 1) + 1
                s["updated_at"] = now_str
                s["model"] = model
                if custom_title and custom_title.strip():
                    s["title"] = make_title(prompt, custom_title)
                if worktree_path:
                    s["worktree_path"] = worktree_path
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
        }
        if worktree_path:
            new_entry["worktree_path"] = worktree_path
        sessions.append(new_entry)
        _write_json_atomic(reg_path, reg)
        return new_id, title


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
        unstaged = subprocess.run(
            ["git", "diff", "--shortstat"],
            cwd=target_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if unstaged.returncode != 0:
            return "", []
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

        post_snap = snapshot_git_state(target_dir) or {}
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
# Telemetry and Output Parsing
# ---------------------------------------------------------------------------

def clean_output(raw_text: str):
    """Strip ANSI escapes and locate/parse the JSON payload in Claude output."""
    cleaned = ANSI_ESCAPE.sub("", raw_text or "")

    for line in reversed(cleaned.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

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


def get_reports_dir() -> str:
    """Resolve directory for saving full uncut reports when inline output hits safety ceiling."""
    state_dir = get_state_dir()
    return os.path.join(os.path.dirname(os.path.abspath(state_dir)), "reports")


def save_full_report(text: str, session_num=None) -> str:
    """Save full uncut Markdown report to disk and return its path (or '' on error)."""
    try:
        reports_dir = get_reports_dir()
        os.makedirs(reports_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = f"s{session_num}" if session_num is not None else "run"
        fname = f"report_{tag}_{ts}_{os.getpid()}.md"
        path = os.path.join(reports_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
        return path
    except Exception:
        return ""


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
                    msg = entry.get("message")
                    if isinstance(msg, dict):
                        if msg.get("model"):
                            model = msg.get("model")
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
                                    for key in ("command", "file_path", "path", "pattern"):
                                        if isinstance(tinp, dict) and tinp.get(key):
                                            hint = str(tinp[key]).splitlines()[0][:120]
                                            break
                                    last_activity = f"Tool `{tname}`" + (f": {hint}" if hint else "")
                                    break
                                if btype == "text" and block.get("text", "").strip():
                                    txt_line = [l.strip() for l in block["text"].splitlines() if l.strip()][-1]
                                    last_activity = txt_line[:200]
                                    break
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

    model_l = (actual_model or "").lower()
    prov_l = (provider or "").lower()
    is_free_or_local = (
        model_l.endswith(":free")
        or prov_l in ("ollama", "lmstudio", "local")
        or "ollama" in model_l
        or "-dflash" in model_l
    )
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
    max_lines: int = 250,
    max_chars: int = 15000,
    session_num=None,
) -> str:
    """Clean and preserve full substantive Markdown delivery; archive uncut report if safety ceiling is hit."""
    cleaned = ANSI_ESCAPE.sub("", text or "").strip()
    filtered_lines = []
    for line in cleaned.splitlines():
        if line.startswith("[claude-code:") or line.startswith("auth:") or line.startswith("crouter:"):
            continue
        filtered_lines.append(line)

    if not filtered_lines:
        return "Subagent completed the task with no text output."

    joined = "\n".join(filtered_lines)
    if len(filtered_lines) > max_lines or len(joined) > max_chars:
        saved_path = save_full_report(joined, session_num=session_num)
        truncated = "\n".join(filtered_lines[:max_lines])
        if len(truncated) > max_chars:
            truncated = truncated[:max_chars].rstrip()
        saved_note = f" Full uncut report saved to: `{saved_path}`" if saved_path else ""
        return (
            truncated
            + f"\n\n... (Delivery report capped at {max_lines} lines / {max_chars} chars for context safety.{saved_note})"
        )

    return joined


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


def run_subprocess_safely(cmd, cwd, timeout=None):
    """Launch cmd in its own process group; strip TMUX so tmux hooks never fire for headless runs."""
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    effective_timeout = timeout if (timeout is not None and timeout > 0) else None
    popen_kwargs = dict(
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=effective_timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                _signal_process_group(proc, signal.SIGKILL)
                stdout, stderr = proc.communicate()
        timeout_note = f"\n[claude-subagent] Timed out after {timeout}s; terminated subprocess group."
        return -1, stdout, (stderr or "") + timeout_note
    except KeyboardInterrupt:
        _signal_process_group(proc, signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_process_group(proc, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        raise


# ---------------------------------------------------------------------------
# Command Construction (Testable Pure Helper)
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
        "json",
        "--permission-mode",
        permission_mode or "bypassPermissions",
    ]

    if bare:
        claude_args.append("--bare")

    if strict_mcp:
        claude_args.append("--strict-mcp-config")

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

    if resume_uuid:
        claude_args.extend(["--resume", resume_uuid])
    elif continue_session:
        claude_args.append("--continue")
    elif session_id:
        claude_args.extend(["--session-id", session_id])

    if str(worktree).lower() in ("true", "1") or worktree is True:
        claude_args.append("--worktree")
    elif isinstance(worktree, str) and worktree.strip():
        claude_args.extend(["--worktree", worktree.strip()])

    if budget is not None and str(budget).strip():
        claude_args.extend(["--max-budget-usd", str(budget).strip()])

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
    allowed_tools: str = None,
    disallowed_tools: str = None,
    agent: str = None,
    effort: str = None,
    title: str = None,
    add_dirs: list = None,
    append_system_prompt: str = None,
    permission_mode: str = None,
    allow_unconfigured: bool = False,
    max_output_chars: int = 15000,
    raw_json: bool = False,
    dry_run: bool = False,
) -> tuple:
    """Execute Claude subagent and return (exit_code: int, output_text: str)."""
    target_cwd = os.path.abspath(cwd or os.getcwd())
    if not os.path.isdir(target_cwd):
        return 1, f"Error: Target directory does not exist or is not a directory: {target_cwd}"

    prompt = resolve_prompt_input(prompt)
    if not prompt or not prompt.strip():
        return 1, "Error: Prompt is empty."

    config = load_config()
    try:
        eff_backend, eff_provider, eff_model, cfg_perm, route_source = resolve_effective_route(
            config=config,
            cli_provider=provider,
            cli_model=model,
            agent=agent,
            allow_unconfigured=allow_unconfigured,
        )
    except RuntimeError as e:
        if str(e) == "SETUP_REQUIRED":
            return EXIT_SETUP_REQUIRED, format_setup_required_message(discover_environment())
        return 1, f"Error: {e}"
    except ValueError as e:
        return 1, f"Error: {e}"

    eff_permission_mode = permission_mode or cfg_perm or "bypassPermissions"

    target_session_uuid = None
    target_numeric_id = None
    target_title = None

    if resume:
        target_session_uuid, target_numeric_id, target_title = resolve_session(target_cwd, resume)
        if not target_session_uuid:
            sessions_hint = get_sessions_text(target_cwd)
            return 1, f"Error: Could not find session '{resume}' in {target_cwd}.\n\n{sessions_hint}"
    elif continue_session:
        target_session_uuid, target_numeric_id, target_title = resolve_session(target_cwd, "latest")

    # Pre-assign a deterministic UUID for new sessions so interrupted/timed-out runs stay resumable
    pre_session_uuid = None if (target_session_uuid or continue_session) else str(uuid.uuid4())

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
            strict_mcp=strict_mcp,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            agent=agent,
            effort=effort,
            title=title,
            add_dirs=add_dirs,
            append_system_prompt=append_system_prompt,
            permission_mode=eff_permission_mode,
        )
    except ValueError as e:
        return 1, f"Error: {e}"

    if dry_run:
        return 0, json.dumps(
            {
                "cwd": target_cwd,
                "routeSource": route_source,
                "backend": eff_backend,
                "provider": eff_provider,
                "model": eff_model,
                "permissionMode": eff_permission_mode,
                "sessionId": target_session_uuid or pre_session_uuid,
                "command": cmd,
                "shell": " ".join(shlex.quote(x) for x in cmd),
            },
            indent=2,
            ensure_ascii=False,
        )

    pre_worktrees = list_git_worktrees(target_cwd) if worktree else []
    base_head = get_git_head(target_cwd)
    pre_git_snap = None if worktree else snapshot_git_state(target_cwd)

    started_at = time.monotonic()
    try:
        returncode, stdout, stderr = run_subprocess_safely(cmd, target_cwd, timeout)
    except FileNotFoundError as e:
        return 1, f"Error launching Claude subagent: {e}"
    elapsed_sec = time.monotonic() - started_at

    payload = clean_output(stdout)

    worktree_path, worktree_branch = detect_worktree_info(target_cwd, worktree, pre_worktrees, payload)
    git_inspect_dir = worktree_path or target_cwd
    git_summary, git_files = get_git_changes(
        git_inspect_dir,
        base_head=base_head,
        pre_snapshot=None if worktree_path else pre_git_snap,
    )

    if raw_json:
        if payload:
            return (0 if returncode == 0 else 1), json.dumps(payload, indent=2, ensure_ascii=False)
        return (0 if returncode == 0 else 1), stdout

    success = returncode == 0 and payload is not None and not payload.get("is_error")
    status_label = "SUCCESS" if success else "FAILED"

    candidate_uuid = (payload.get("session_id") if payload else None) or target_session_uuid or pre_session_uuid
    recovered = {}
    if payload is None and candidate_uuid:
        recovered = recover_session_telemetry(git_inspect_dir, candidate_uuid)
        if not recovered and git_inspect_dir != target_cwd:
            recovered = recover_session_telemetry(target_cwd, candidate_uuid)

    cfg_default_model = ((config or {}).get("defaultRoute") or {}).get("model") if not model else None
    model_fallback = recovered.get("model") or eff_model or cfg_default_model or eff_provider or "default"
    actual_model = extract_actual_model(payload, model_fallback)
    result_text = payload.get("result", "") if payload else (stdout.strip() or stderr.strip())

    should_record = bool(
        (payload and payload.get("session_id"))
        or target_session_uuid
        or recovered.get("exists")
    )
    session_uuid = candidate_uuid if should_record else None
    numeric_id, resolved_title = record_session(
        target_cwd,
        session_uuid,
        prompt,
        actual_model,
        existing_id=target_numeric_id,
        custom_title=title,
        worktree_path=worktree_path,
    )

    lines = []
    display_title = resolved_title or target_title or ""
    session_label = f"#{numeric_id} (\"{display_title}\")" if numeric_id else (f"\"{display_title}\"" if display_title else "")
    session_str = f" Session {session_label} |" if session_label else ""
    metrics_str = format_metrics(
        payload,
        elapsed_sec,
        actual_model=actual_model,
        provider=eff_provider,
        recovered_turns=recovered.get("num_turns"),
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
        if recovered.get("last_activity"):
            lines.append(f"**Last Activity Before Interruption:** {recovered['last_activity']}")
        if numeric_id:
            lines.append(
                f"**Resume:** `claude-subagent --cwd {shlex.quote(target_cwd)} -r {numeric_id} -p \"Continue from where you stopped.\"`"
            )
        lines.append("**Error (Actionable Details):**")
        if payload and payload.get("is_error") and payload.get("result"):
            error_blob = str(payload.get("result"))
            if stderr and stderr.strip():
                error_blob += "\n" + stderr.strip()
        else:
            error_blob = stderr or stdout
        lines.append(truncate_error(error_blob))
    else:
        lines.append("**Subagent Delivery & Review Report:**")
        lines.append(format_delivery_report(result_text, max_chars=max_output_chars, session_num=numeric_id))

    return (0 if success else 1), "\n".join(lines)


# ---------------------------------------------------------------------------
# Setup & Config Subcommand Handlers
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
    parser.add_argument("--model", default=None, help="Default model ID/alias")
    parser.add_argument("--delegation-mode", choices=DELEGATION_MODES, default="proactive", help="Default team delegation posture")
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default="bypassPermissions", help="Default Claude permission mode")
    parser.add_argument("--usage-policy", default=None, help="Free-form delegation/usage rules or reminders saved for agents")
    parser.add_argument(
        "--scenario", action="append", default=None,
        help="Optional per-agent/scenario route override as name=provider:model (repeatable)"
    )
    parser.add_argument("--note", default=None, help="Optional human note recorded in config")

    args = parser.parse_args(argv)
    env_info = discover_environment()

    if args.discover:
        if args.as_json:
            print(json.dumps(env_info, indent=2, ensure_ascii=False))
        else:
            print(format_discovery_report(env_info))
        return 0

    has_write_flags = any(
        [
            args.backend,
            args.provider,
            args.model,
            args.usage_policy,
            args.scenario,
        ]
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

    existing = load_config() or {}
    existing_scenarios = ((existing.get("policies") or {}).get("scenarioOverrides")) or {}
    try:
        scenarios = _parse_scenario_args(args.scenario, existing=existing_scenarios)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    resolved_backend = args.backend
    if not resolved_backend:
        if args.provider == "claude":
            resolved_backend = "native"
        elif args.provider:
            resolved_backend = "crouter"
        else:
            resolved_backend = "crouter" if env_info["crouter"]["installed"] else "native"

    cfg = build_config_payload(
        backend=resolved_backend,
        provider=args.provider,
        model=args.model,
        delegation_mode=args.delegation_mode,
        usage_policy=args.usage_policy,
        scenario_overrides=scenarios,
        permission_mode=args.permission_mode,
        note=args.note,
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


# ---------------------------------------------------------------------------
# Main CLI Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-subagent",
        description=(
            "Headless Claude Code subagent runner with persistent first-run setup, "
            "numbered sessions, worktree isolation, and delta diff reporting.\n\n"
            "Management commands:\n"
            "  claude-subagent setup [--discover | --backend ... --provider ... --model ...]\n"
            "  claude-subagent config show|path|reset"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-p", "--prompt", default=None, help="Task prompt, file path, or '-' to read from stdin"
    )
    parser.add_argument(
        "-l", "--list", "--list-sessions", dest="list_sessions", action="store_true",
        help="List all numbered subagent sessions in the target directory"
    )
    parser.add_argument(
        "--delete-session", default=None,
        help="Remove a session entry by number (e.g. 1 or #1) or UUID from the target directory"
    )
    parser.add_argument(
        "--clear-sessions", action="store_true",
        help="Clear all recorded session entries for the target directory"
    )
    parser.add_argument(
        "--provider", default=None,
        help="One-shot provider override (bypasses persisted default; use 'claude' for native Anthropic)"
    )
    parser.add_argument(
        "--model", default=None, help="One-shot model override (passed with crouter --once when crouter is active)"
    )
    parser.add_argument(
        "--allow-unconfigured", action="store_true",
        help="Bypass the first-run setup gate for a one-off invocation"
    )
    parser.add_argument(
        "--cwd", default=os.getcwd(), help="Target working directory"
    )
    parser.add_argument(
        "-r", "--resume", default=None,
        help="Resume session by number (e.g. -r 1 or -r #2), or UUID"
    )
    parser.add_argument(
        "-c", "--continue-session", action="store_true",
        help="Continue the most recent session in this directory"
    )
    parser.add_argument(
        "-w", "--worktree", nargs="?", const=True, default=None,
        help="Create or use a git worktree for isolated execution (optional branch/worktree name)"
    )
    parser.add_argument(
        "-n", "--name", "--title", dest="title", default=None,
        help="Custom display title for this session (shown in --list and Claude -n)"
    )
    parser.add_argument(
        "--agent", default=None,
        help="Invoke a specific Claude Code agent definition (from ~/.claude/agents/ or .claude/agents/)"
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
        "--max-output-chars", type=int, default=15000,
        help="Safety cap on delivery report length in chars (default: 15000)"
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
        "--bare", action="store_true",
        help="Minimal mode: pass native --bare (skips hooks, plugins, and settings)"
    )
    parser.add_argument(
        "--strict-mcp", "--strict-mcp-config", dest="strict_mcp", action="store_true",
        help="Skip external MCP servers via native --strict-mcp-config without disabling hooks/agents"
    )
    parser.add_argument(
        "--raw-json", action="store_true", help="Print raw JSON output instead of formatted delivery report"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print resolved route & command as JSON without invoking Claude"
    )
    return parser


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    if raw_argv and raw_argv[0] == "setup":
        sys.exit(handle_setup(raw_argv[1:]))
    if raw_argv and raw_argv[0] == "config":
        sys.exit(handle_config(raw_argv[1:]))

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    if args.clear_sessions:
        removed = clear_sessions(args.cwd)
        print(f"Cleared {removed} session(s) for: {os.path.abspath(args.cwd)}")
        sys.exit(0)

    if args.delete_session:
        ok = delete_session(args.cwd, args.delete_session)
        if ok:
            print(f"Deleted session '{args.delete_session}' for: {os.path.abspath(args.cwd)}")
            sys.exit(0)
        print(f"Session '{args.delete_session}' not found in: {os.path.abspath(args.cwd)}", file=sys.stderr)
        sys.exit(1)

    if args.list_sessions:
        list_sessions(args.cwd)
        sys.exit(0)

    if not args.prompt:
        parser.print_help()
        sys.exit(1)

    exit_code, output = execute_subagent(
        prompt=args.prompt,
        cwd=args.cwd,
        resume=args.resume,
        continue_session=args.continue_session,
        worktree=args.worktree,
        model=args.model,
        provider=args.provider,
        budget=args.budget,
        timeout=args.timeout,
        bare=args.bare,
        strict_mcp=args.strict_mcp,
        allowed_tools=args.allowed_tools or args.tools,
        disallowed_tools=args.disallowed_tools,
        agent=args.agent,
        effort=args.effort,
        title=args.title,
        add_dirs=args.add_dir,
        append_system_prompt=args.append_system_prompt,
        permission_mode=args.permission_mode,
        allow_unconfigured=args.allow_unconfigured,
        max_output_chars=args.max_output_chars,
        raw_json=args.raw_json,
        dry_run=args.dry_run,
    )

    print(output)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
