import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from claude_subagent import __version__  # noqa: E402
from claude_subagent import cli  # noqa: E402


class TestClaudeSubagent(unittest.TestCase):
    _PARENT_ENV_KEYS = (
        "ANTIGRAVITY_CONVERSATION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SUBAGENT_PARENT_ID",
        "CLAUDE_SUBAGENT_AGY_BRAIN_DIR",
        "CLAUDE_SUBAGENT_CLAUDE_PROJECTS_DIR",
        "CODEX_HOME",
    )

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="claude_subagent_test_")
        self.orig_state_dir = os.environ.get("CLAUDE_SUBAGENT_STATE_DIR")
        self.orig_config_path = os.environ.get("CLAUDE_SUBAGENT_CONFIG")
        self.orig_parent_env = {k: os.environ.pop(k, None) for k in self._PARENT_ENV_KEYS}
        os.environ["CLAUDE_SUBAGENT_STATE_DIR"] = os.path.join(self.tmp_dir, "state")
        os.environ["CLAUDE_SUBAGENT_CONFIG"] = os.path.join(self.tmp_dir, "config", "config.json")

    def tearDown(self):
        if self.orig_state_dir is None:
            os.environ.pop("CLAUDE_SUBAGENT_STATE_DIR", None)
        else:
            os.environ["CLAUDE_SUBAGENT_STATE_DIR"] = self.orig_state_dir
        if self.orig_config_path is None:
            os.environ.pop("CLAUDE_SUBAGENT_CONFIG", None)
        else:
            os.environ["CLAUDE_SUBAGENT_CONFIG"] = self.orig_config_path
        for k, v in self.orig_parent_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_fake_exec(self, name: str, body: str = "#!/bin/sh\nexit 0\n") -> str:
        path = os.path.join(self.tmp_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_version_matches_semver(self):
        self.assertEqual(__version__, "0.3.0")
        pyproject = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(pyproject, encoding="utf-8") as f:
            self.assertIn(f'version = "{__version__}"', f.read())

    def test_preassigned_session_id_and_transcript_recovery(self):
        # New session passes --session-id; resumed session uses --resume instead
        cmd_new = cli.build_command(
            prompt="hi", backend="native", session_id="11111111-2222-3333-4444-555555555555",
            crouter_path="", claude_path="claude",
        )
        self.assertIn("--session-id", cmd_new)
        self.assertIn("11111111-2222-3333-4444-555555555555", cmd_new)

        cmd_res = cli.build_command(
            prompt="hi", backend="native", resume_uuid="11111111-2222-3333-4444-555555555555",
            session_id="99999999-2222-3333-4444-555555555555",
            crouter_path="", claude_path="claude",
        )
        self.assertIn("--resume", cmd_res)
        self.assertNotIn("--session-id", cmd_res)

        # Simulate a Claude Code native JSONL written before a timeout/SIGKILL
        fake_projects = os.path.join(self.tmp_dir, "claude_projects")
        ws = os.path.join(self.tmp_dir, "demo_repo")
        os.makedirs(ws)
        enc = cli._encode_claude_project_dir(ws)
        proj_dir = os.path.join(fake_projects, enc)
        os.makedirs(proj_dir)
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        jsonl_path = os.path.join(proj_dir, f"{sid}.jsonl")
        entries = [
            {"type": "assistant", "message": {"model": "nvidia/nemotron-3.5-super-vl:free", "content": [{"type": "text", "text": "Reading files"}]}},
            {"type": "assistant", "message": {"model": "nvidia/nemotron-3.5-super-vl:free", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
            {"type": "cost-state", "modelUsage": {"nvidia/nemotron-3.5-super-vl:free": {"inputTokens": 100}}},
        ]
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        rec = cli.recover_session_telemetry(ws, sid, claude_projects_dir=fake_projects)
        self.assertTrue(rec.get("exists"))
        self.assertEqual(rec.get("model"), "nvidia/nemotron-3.5-super-vl:free")
        self.assertEqual(rec.get("num_turns"), 2)
        self.assertIn("Tool `Bash`: pytest -q", rec.get("last_activity", ""))

    def test_delivery_report_uncut_by_default_and_optional_cap(self):
        long_text = "\n".join(f"Line {i}: details about change {i}" for i in range(400))
        # Default: full uncut output, no truncation
        uncut = cli.format_delivery_report(long_text)
        self.assertIn("Line 0: details about change 0", uncut)
        self.assertIn("Line 399: details about change 399", uncut)
        self.assertNotIn("capped", uncut)

        # Explicit --max-output-chars still works as opt-in
        capped = cli.format_delivery_report(long_text, max_chars=500)
        self.assertIn("Delivery report capped", capped)
        self.assertNotIn("Line 399", capped)

    def test_default_timeout_is_unlimited(self):
        rc, out, _ = cli.run_subprocess_safely(["sh", "-c", 'printf "ok"'], cwd=self.tmp_dir, timeout=None)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "ok")
        rc2, out2, _ = cli.run_subprocess_safely(["sh", "-c", 'printf "ok0"'], cwd=self.tmp_dir, timeout=0)
        self.assertEqual(rc2, 0)
        self.assertEqual(out2, "ok0")

    def test_first_run_setup_gate_and_persistent_config(self):
        # Unconfigured run without explicit provider/model MUST return SETUP_REQUIRED (exit code 2)
        rc, out = cli.execute_subagent(prompt="hello", cwd=self.tmp_dir, dry_run=True)
        self.assertEqual(rc, cli.EXIT_SETUP_REQUIRED)
        self.assertIn("SETUP_REQUIRED", out)

        # Persist setup with crouter + unified default + usagePolicy + video-watcher scenario
        fake_crouter = self._make_fake_exec("crouter")
        orig_crouter_env = os.environ.get("CLAUDE_SUBAGENT_CROUTER")
        os.environ["CLAUDE_SUBAGENT_CROUTER"] = fake_crouter
        try:
            rc_setup = cli.handle_setup(
                [
                    "--backend", "crouter",
                    "--provider", "openrouter",
                    "--model", "nvidia/nemotron-3.5-super-vl:free",
                    "--delegation-mode", "proactive",
                    "--usage-policy", "Unified default; remind user before special cases.",
                    "--scenario", "video-watcher=openrouter:nvidia/nemotron-3.5-super-vl:free",
                ]
            )
            self.assertEqual(rc_setup, 0)

            cfg = cli.load_config()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg["defaultRoute"]["provider"], "openrouter")
            self.assertEqual(cfg["defaultRoute"]["model"], "nvidia/nemotron-3.5-super-vl:free")
            self.assertEqual(
                cfg["policies"]["usagePolicy"],
                "Unified default; remind user before special cases.",
            )
            self.assertIn("video-watcher", cfg["policies"]["scenarioOverrides"])

            # Normal run uses persisted default automatically (never local ollama)
            rc_def, out_def = cli.execute_subagent(prompt="hello", cwd=self.tmp_dir, dry_run=True)
            self.assertEqual(rc_def, 0)
            info_def = json.loads(out_def)
            self.assertEqual(info_def["routeSource"], "config-default")
            self.assertEqual(
                info_def["command"][:6],
                [fake_crouter, "openrouter", "--once", "--model", "nvidia/nemotron-3.5-super-vl:free", "--"],
            )

            # Scenario run (--agent video-watcher) uses scenario override
            rc_sc, out_sc = cli.execute_subagent(
                prompt="watch clip", cwd=self.tmp_dir, agent="video-watcher", dry_run=True
            )
            self.assertEqual(rc_sc, 0)
            info_sc = json.loads(out_sc)
            self.assertEqual(info_sc["routeSource"], "config-scenario(video-watcher)")

            # Reset removes config cleanly
            self.assertTrue(cli.reset_config())
            self.assertIsNone(cli.load_config())
        finally:
            if orig_crouter_env is None:
                os.environ.pop("CLAUDE_SUBAGENT_CROUTER", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CROUTER"] = orig_crouter_env

    def test_crouter_list_parser(self):
        sample = (
            "PROVIDER               STATUS  AUTH        DEFAULT MODEL                  ENDPOINT\n"
            "ollama                 local   none        qwen3.8:27b-dflash             http://127.0.0.1:11435\n"
            "openrouter             ready   env         nvidia/nemotron-3.5-super-vl:free https://openrouter.ai/api\n"
            "\n26 of 36 providers have no credential yet\n"
        )
        parsed = cli._parse_crouter_list(sample)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[1]["provider"], "openrouter")
        self.assertEqual(parsed[1]["defaultModel"], "nvidia/nemotron-3.5-super-vl:free")

    def test_build_command_native_without_crouter(self):
        cmd = cli.build_command(
            prompt="hello",
            backend="native",
            model="sonnet",
            crouter_path="",
            claude_path="/usr/local/bin/claude",
        )
        self.assertEqual(cmd[0], "/usr/local/bin/claude")
        self.assertIn("--model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertNotIn("run", cmd)

        with self.assertRaises(ValueError):
            cli.build_command(
                prompt="hello",
                provider="openrouter",
                crouter_path="",
                claude_path="/usr/local/bin/claude",
            )

    def test_build_command_with_crouter(self):
        fake_crouter = self._make_fake_exec("crouter")

        cmd_default = cli.build_command(prompt="hello", crouter_path=fake_crouter)
        self.assertEqual(cmd_default[:3], [fake_crouter, "run", "--"])

        cmd_model = cli.build_command(prompt="hello", model="qwen3", crouter_path=fake_crouter)
        self.assertEqual(cmd_model[:6], [fake_crouter, "run", "--once", "--model", "qwen3", "--"])

        cmd_prov = cli.build_command(prompt="hello", provider="openrouter", model="free-m", crouter_path=fake_crouter)
        self.assertEqual(cmd_prov[:6], [fake_crouter, "openrouter", "--once", "--model", "free-m", "--"])

        cmd_claude = cli.build_command(
            prompt="hello", backend="native", provider="claude", model="sonnet",
            crouter_path=fake_crouter, claude_path="/usr/local/bin/claude"
        )
        self.assertEqual(cmd_claude[0], "/usr/local/bin/claude")
        self.assertIn("--model", cmd_claude)
        self.assertIn("sonnet", cmd_claude)

    def test_build_command_claude_21_flags(self):
        cmd = cli.build_command(
            prompt="audit",
            backend="native",
            agent="workflow-reviewer",
            effort="high",
            title="Review Auth",
            strict_mcp=True,
            bare=False,
            allowed_tools="coding",
            disallowed_tools="WebFetch",
            add_dirs=[self.tmp_dir],
            append_system_prompt="Stay focused",
            permission_mode="acceptEdits",
            worktree="feat-auth",
            budget="0.50",
            crouter_path="",
            claude_path="claude",
        )
        self.assertIn("--agent", cmd)
        self.assertIn("workflow-reviewer", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("high", cmd)
        self.assertIn("-n", cmd)
        self.assertIn("Review Auth", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--bare", cmd)
        self.assertIn("--tools", cmd)
        self.assertIn("Bash,Edit,Read,Write,Grep,Glob", cmd)
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("WebFetch", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("acceptEdits", cmd)
        self.assertIn("--worktree", cmd)
        self.assertIn("feat-auth", cmd)
        self.assertIn("--max-budget-usd", cmd)
        self.assertIn("0.50", cmd)

    def test_prompt_resolution_long_file_and_stdin(self):
        long_prompt = "a" * 3000
        self.assertEqual(cli.resolve_prompt_input(long_prompt), long_prompt)

        pfile = os.path.join(self.tmp_dir, "brief.txt")
        with open(pfile, "w", encoding="utf-8") as f:
            f.write("from file\nline 2")
        self.assertEqual(cli.resolve_prompt_input(pfile), "from file\nline 2")

        res = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.path.insert(0, {SRC_DIR!r}); from claude_subagent import cli; "
                "assert cli.resolve_prompt_input('-') == 'stdin\\nprompt'",
            ],
            input="stdin\nprompt",
            text=True,
            check=True,
        )
        self.assertEqual(res.returncode, 0)

    def test_session_lifecycle_and_concurrency(self):
        ws = os.path.join(self.tmp_dir, "ws")
        os.makedirs(ws)

        worker_code = (
            f"import sys; sys.path.insert(0, {SRC_DIR!r}); from claude_subagent import cli; "
            "idx = int(sys.argv[2]); "
            "cli.record_session(sys.argv[1], f'uuid-worker-{idx:02d}-000000000000', "
            "f'Task {idx}', 'claude-sonnet', custom_title=f'Worker {idx}')"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", worker_code, ws, str(i)])
            for i in range(8)
        ]
        rcs = [p.wait(timeout=15) for p in procs]
        self.assertTrue(all(rc == 0 for rc in rcs), rcs)

        reg = cli.load_registry(ws)
        self.assertEqual(len(reg["sessions"]), 8)
        self.assertEqual(sorted(s["id"] for s in reg["sessions"]), list(range(1, 9)))

        uuid1, num1, title1 = cli.resolve_session(ws, "#1")
        self.assertEqual(num1, 1)
        self.assertTrue(uuid1.startswith("uuid-worker-"))
        self.assertTrue(title1.startswith("Worker "))

        self.assertTrue(cli.delete_session(ws, "#1"))
        self.assertFalse(cli.delete_session(ws, "#999"))
        self.assertEqual(len(cli.load_registry(ws)["sessions"]), 7)

        cleared = cli.clear_sessions(ws)
        self.assertEqual(cleared, 7)
        self.assertEqual(cli.load_registry(ws)["sessions"], [])

    def test_git_worktree_and_dirty_tree_delta(self):
        repo = os.path.join(self.tmp_dir, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

        with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("v2 dirty\n")
        pre_snap = cli.snapshot_git_state(repo)
        base_head = cli.get_git_head(repo)

        s_empty, f_empty = cli.get_git_changes(repo, base_head=base_head, pre_snapshot=pre_snap)
        self.assertEqual(s_empty, "")
        self.assertEqual(f_empty, [])

        with open(os.path.join(repo, "new.txt"), "w", encoding="utf-8") as f:
            f.write("added\n")
        s_delta, f_delta = cli.get_git_changes(repo, base_head=base_head, pre_snapshot=pre_snap)
        self.assertIn("1 file(s) touched this run (1 pre-existing dirty)", s_delta)
        self.assertEqual(f_delta, [("??", "new.txt")])

        pre_wt = cli.list_git_worktrees(repo)
        wt_dir = os.path.join(repo, ".claude", "worktrees", "wt-demo")
        os.makedirs(os.path.dirname(wt_dir), exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-q", "-b", "worktree-wt-demo", wt_dir], cwd=repo, check=True)

        with open(os.path.join(wt_dir, "a.txt"), "a", encoding="utf-8") as f:
            f.write("wt commit\n")
        subprocess.run(["git", "add", "a.txt"], cwd=wt_dir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "subagent commit"], cwd=wt_dir, check=True)
        with open(os.path.join(wt_dir, "b.txt"), "w", encoding="utf-8") as f:
            f.write("untracked\n")

        det_path, det_branch = cli.detect_worktree_info(repo, "wt-demo", pre_wt)
        self.assertEqual(os.path.realpath(det_path), os.path.realpath(wt_dir))
        self.assertEqual(det_branch, "worktree-wt-demo")

        wt_summary, wt_files = cli.get_git_changes(det_path, base_head=base_head)
        self.assertIn("1 commit(s)", wt_summary)
        self.assertIn("1 working-tree file(s) changed", wt_summary)
        self.assertIn(("??", "b.txt"), wt_files)

    def test_tmux_stripping_and_timeout_kills_process_group(self):
        os.environ["TMUX"] = "/tmp/fake,1,0"
        try:
            rc, out, _ = cli.run_subprocess_safely(
                ["sh", "-c", 'printf "TMUX=%s" "${TMUX:-}"'], cwd=self.tmp_dir, timeout=5
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out, "TMUX=")
        finally:
            os.environ.pop("TMUX", None)

        marker = os.path.join(self.tmp_dir, "child.pid")
        rc, _, err = cli.run_subprocess_safely(
            ["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"],
            cwd=self.tmp_dir,
            timeout=1,
        )
        self.assertEqual(rc, -1)
        self.assertIn("Timed out after 1s", err)
        time.sleep(0.2)
        with open(marker, encoding="utf-8") as f:
            child_pid = int(f.read().strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_metrics_formatting_and_dry_run_cli(self):
        self.assertEqual(
            cli.format_metrics(
                {"duration_ms": 10500, "num_turns": 7, "total_cost_usd": 0.1096},
                10.5,
                actual_model="nvidia/nemotron-3.5-super-vl:free",
            ),
            "10.5s · 7 turns · free",
        )
        self.assertEqual(
            cli.format_metrics(
                {"duration_ms": 4000, "num_turns": 2, "total_cost_usd": 0.025},
                4.0,
                actual_model="claude-sonnet-4-6",
                provider="claude",
            ),
            "4.0s · 2 turns · est. $0.0250",
        )

        env = os.environ.copy()
        env["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        proc = subprocess.run(
            [
                os.path.join(REPO_ROOT, "bin", "claude-subagent"),
                "--allow-unconfigured", "--dry-run", "--title", "Dry", "-p", "hi",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertIn("-n", payload["command"])
        self.assertIn("Dry", payload["command"])

        vproc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "claude-subagent"), "--version"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn(__version__, vproc.stdout)

    def test_skill_frontmatter_and_relative_links(self):
        skill_dir = os.path.join(REPO_ROOT, "skills", "claude-subagent")
        skill_md = os.path.join(skill_dir, "SKILL.md")
        with open(skill_md, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: claude-subagent", text[:400])
        self.assertIn("description:", text[:600])
        found_refs = set()
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = match.group(1)
            if not target.startswith("http"):
                self.assertTrue(
                    os.path.isfile(os.path.join(skill_dir, target)),
                    f"Broken relative link in SKILL.md: {target}",
                )
                found_refs.add(target)
        self.assertIn("references/team-topologies.md", found_refs)
        self.assertIn("references/exception-handling.md", found_refs)
        self.assertIn("references/recipes-and-flags.md", found_refs)

    def test_stream_json_parsing_live_progress_and_json_envelope(self):
        # Clean output handles both stream-json NDJSON and legacy single JSON
        ndjson = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "sid-1", "model": "m1"}),
            json.dumps({"type": "assistant", "message": {"model": "m1", "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200, "num_turns": 2, "session_id": "sid-1", "result": "All good"}),
        ])
        parsed = cli.clean_output(ndjson)
        self.assertEqual(parsed["result"], "All good")
        self.assertEqual(parsed["session_id"], "sid-1")

        # Fake claude emitting stream-json NDJSON
        fake_claude = self._make_fake_exec(
            "claude",
            "#!/bin/sh\n"
            "printf '{\"type\":\"system\",\"subtype\":\"init\",\"session_id\":\"11111111-1111-1111-1111-111111111111\",\"model\":\"demo:free\"}\\n'\n"
            "printf '{\"type\":\"assistant\",\"message\":{\"model\":\"demo:free\",\"content\":[{\"type\":\"tool_use\",\"name\":\"Bash\",\"input\":{\"command\":\"pytest -q\"}}]}}\\n'\n"
            "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"duration_ms\":850,\"num_turns\":2,\"session_id\":\"11111111-1111-1111-1111-111111111111\",\"result\":\"Done cleanly\"}\\n'\n"
        )
        orig_cl = os.environ.get("CLAUDE_SUBAGENT_CLAUDE")
        orig_nocr = os.environ.get("CLAUDE_SUBAGENT_NO_CROUTER")
        os.environ["CLAUDE_SUBAGENT_CLAUDE"] = fake_claude
        os.environ["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        try:
            rc, out = cli.execute_subagent(
                prompt="Run check",
                cwd=self.tmp_dir,
                allow_unconfigured=True,
                json_output=True,
                quiet=True,
                title="Stream Test",
            )
            self.assertEqual(rc, 0)
            env_data = json.loads(out)
            self.assertTrue(env_data["ok"])
            self.assertEqual(env_data["status"], "success")
            self.assertEqual(env_data["session"]["id"], 1)
            self.assertEqual(env_data["session"]["uuid"], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(env_data["route"]["strictMcp"], True)
            self.assertEqual(env_data["route"]["chrome"], False)
            self.assertEqual(env_data["metrics"]["turns"], 2)
            self.assertEqual(env_data["report"], "Done cleanly")
            self.assertIn("Tool `Bash`: pytest -q", env_data["lastActivity"])
        finally:
            if orig_cl is None:
                os.environ.pop("CLAUDE_SUBAGENT_CLAUDE", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CLAUDE"] = orig_cl
            if orig_nocr is None:
                os.environ.pop("CLAUDE_SUBAGENT_NO_CROUTER", None)
            else:
                os.environ["CLAUDE_SUBAGENT_NO_CROUTER"] = orig_nocr

    def test_running_session_stale_pid_and_realpath_canonicalization(self):
        real_ws = os.path.realpath(os.path.join(self.tmp_dir, "real_ws"))
        os.makedirs(real_ws)
        sym_ws = os.path.join(self.tmp_dir, "sym_ws")
        os.symlink(real_ws, sym_ws)

        num_id, _ = cli.register_running_session(
            real_ws, "uuid-stale-000000000001", "Long task", "m1", pid=999999
        )
        self.assertEqual(num_id, 1)
        # Symlink path sees the exact same session
        sessions = cli.get_sessions_data(sym_ws)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["effective_status"], "interrupted")
        self.assertTrue(sessions[0]["stale_pid"])
        self.assertIn("interrupted (stale pid)", cli.get_sessions_text(sym_ws))

        cli.finalize_session(
            sym_ws, "uuid-stale-000000000001", "Long task", "m1", status="completed", existing_id=1
        )
        sessions2 = cli.get_sessions_data(real_ws)
        self.assertEqual(sessions2[0]["effective_status"], "completed")
        self.assertIsNone(sessions2[0]["pid"])
        self.assertEqual(sessions2[0]["turns"], 1)

    def test_model_only_override_and_partial_setup_merge(self):
        fake_crouter = self._make_fake_exec("crouter")
        orig_crouter = os.environ.get("CLAUDE_SUBAGENT_CROUTER")
        os.environ["CLAUDE_SUBAGENT_CROUTER"] = fake_crouter
        try:
            self.assertEqual(
                cli.handle_setup([
                    "--backend", "crouter",
                    "--provider", "openrouter",
                    "--model", "default-free",
                    "--usage-policy", "Keep free",
                ]),
                0,
            )
            # Partial update changes only delegation-mode, preserving provider/model/usagePolicy
            self.assertEqual(cli.handle_setup(["--delegation-mode", "ask-first"]), 0)
            cfg = cli.load_config()
            self.assertEqual(cfg["defaultRoute"]["provider"], "openrouter")
            self.assertEqual(cfg["defaultRoute"]["model"], "default-free")
            self.assertEqual(cfg["policies"]["delegationMode"], "ask-first")
            self.assertEqual(cfg["policies"]["usagePolicy"], "Keep free")
            self.assertTrue(cfg["policies"]["strictMcp"])

            # --model without --provider inherits defaultRoute.provider ('openrouter')
            rc, out = cli.execute_subagent(prompt="hi", cwd=self.tmp_dir, model="alt-model", dry_run=True)
            self.assertEqual(rc, 0)
            info = json.loads(out)
            self.assertEqual(info["provider"], "openrouter")
            self.assertEqual(info["model"], "alt-model")
            self.assertEqual(info["command"][:6], [fake_crouter, "openrouter", "--once", "--model", "alt-model", "--"])
            self.assertTrue(info["strictMcp"])
            self.assertIn("--strict-mcp-config", info["command"])
            self.assertIn("--no-chrome", info["command"])

            # --with-mcp disables --strict-mcp-config; --chrome enables --chrome
            rc2, out2 = cli.execute_subagent(
                prompt="hi", cwd=self.tmp_dir, with_mcp=True, chrome=True, dry_run=True
            )
            self.assertEqual(rc2, 0)
            info2 = json.loads(out2)
            self.assertFalse(info2["strictMcp"])
            self.assertNotIn("--strict-mcp-config", info2["command"])
            self.assertIn("--chrome", info2["command"])
            self.assertNotIn("--no-chrome", info2["command"])
        finally:
            if orig_crouter is None:
                os.environ.pop("CLAUDE_SUBAGENT_CROUTER", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CROUTER"] = orig_crouter

    def test_composable_subcommands_fork_schema_and_passthrough(self):
        schema_file = os.path.join(self.tmp_dir, "schema.json")
        with open(schema_file, "w", encoding="utf-8") as f:
            json.dump({"type": "object", "properties": {"ok": {"type": "boolean"}}}, f)

        env = os.environ.copy()
        env["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        bin_path = os.path.join(REPO_ROOT, "bin", "claude-subagent")

        # doctor --json works even when unconfigured without crashing
        doc_proc = subprocess.run(
            [bin_path, "doctor", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        doc_data = json.loads(doc_proc.stdout)
        self.assertEqual(doc_data["version"], "0.3.0")
        self.assertFalse(doc_data["configured"])
        self.assertEqual(doc_data["sessionsSummary"]["total"], 0)

        # Record a session, check `sessions list --json`, `sessions show 1 --json`, `list --json`
        cli.record_session(self.tmp_dir, "22222222-2222-2222-2222-222222222222", "First", "m1")
        list_proc = subprocess.run(
            [bin_path, "list", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertEqual(len(json.loads(list_proc.stdout)["sessions"]), 1)

        show_proc = subprocess.run(
            [bin_path, "sessions", "show", "1", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertEqual(json.loads(show_proc.stdout)["session"]["session_id"], "22222222-2222-2222-2222-222222222222")

        # `run`, `--fork`, `--fallback-model`, `--json-schema`, `--mcp-config`, `-- <extra>`
        dry_proc = subprocess.run(
            [
                bin_path, "run",
                "--allow-unconfigured", "--cwd", self.tmp_dir,
                "-r", "1", "--fork",
                "--fallback-model", "sonnet",
                "--json-schema", schema_file,
                "--mcp-config", "extra.json",
                "--dry-run", "-p", "branch it",
                "--", "--disable-slash-commands",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        dry_data = json.loads(dry_proc.stdout)
        cmd = dry_data["command"]
        self.assertTrue(dry_data["forkSession"])
        self.assertIn("--fork-session", cmd)
        self.assertIn("--fallback-model", cmd)
        self.assertIn("sonnet", cmd)
        self.assertIn("--json-schema", cmd)
        self.assertIn("--mcp-config", cmd)
        self.assertIn("extra.json", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[-1], "--disable-slash-commands")

    def test_worktree_resume_preserves_and_inspects_worktree(self):
        repo = os.path.join(self.tmp_dir, "wt_repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
        with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as f:
            f.write("v1\n")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        wt_dir = os.path.join(repo, ".claude", "worktrees", "wt-keep")
        os.makedirs(os.path.dirname(wt_dir), exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-q", "-b", "worktree-wt-keep", wt_dir], cwd=repo, check=True)
        with open(os.path.join(wt_dir, "wt_only.txt"), "w", encoding="utf-8") as f:
            f.write("hello\n")

        cli.record_session(
            repo, "33333333-3333-3333-3333-333333333333", "WT task", "demo:free", worktree_path=wt_dir
        )

        fake_claude = self._make_fake_exec(
            "claude",
            "#!/bin/sh\n"
            f"printf 'updated\\n' >> {wt_dir}/wt_only.txt\n"
            "printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"duration_ms\":200,\"num_turns\":1,\"session_id\":\"33333333-3333-3333-3333-333333333333\",\"result\":\"Resumed ok\"}\\n'\n"
        )
        orig_cl = os.environ.get("CLAUDE_SUBAGENT_CLAUDE")
        orig_nocr = os.environ.get("CLAUDE_SUBAGENT_NO_CROUTER")
        os.environ["CLAUDE_SUBAGENT_CLAUDE"] = fake_claude
        os.environ["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        try:
            rc, out = cli.execute_subagent(
                prompt="follow up", cwd=repo, resume="1", allow_unconfigured=True, json_output=True, quiet=True
            )
            self.assertEqual(rc, 0)
            res = json.loads(out)
            self.assertEqual(os.path.realpath(res["session"]["worktreePath"]), os.path.realpath(wt_dir))
            self.assertEqual(res["session"]["worktreeBranch"], "worktree-wt-keep")
            self.assertTrue(any(f["path"] == "wt_only.txt" for f in res["git"]["files"]))
            # Worktree path stays preserved in registry after resume
            entry = cli.resolve_session_entry(repo, "1")
            self.assertEqual(os.path.realpath(entry["worktree_path"]), os.path.realpath(wt_dir))
        finally:
            if orig_cl is None:
                os.environ.pop("CLAUDE_SUBAGENT_CLAUDE", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CLAUDE"] = orig_cl
            if orig_nocr is None:
                os.environ.pop("CLAUDE_SUBAGENT_NO_CROUTER", None)
            else:
                os.environ["CLAUDE_SUBAGENT_NO_CROUTER"] = orig_nocr

    def test_target_workspace_binding_and_cross_workspace_ps(self):
        repo_a = os.path.join(self.tmp_dir, "repo_a")
        repo_b = os.path.join(self.tmp_dir, "repo_b")
        os.makedirs(repo_a)
        os.makedirs(repo_b)

        # Even if IDE env vars differ, sessions bind strictly to target workspace
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "conv-1"
        id_a, _ = cli.record_session(repo_a, "aaaa-0000-0000-0000-000000000001", "Task A", "m1")
        self.assertEqual(id_a, 1)

        os.environ.pop("ANTIGRAVITY_CONVERSATION_ID", None)
        os.environ["CODEX_THREAD_ID"] = "codex-1"
        # Same workspace seen under Codex sees the same #1
        self.assertEqual(len(cli.get_sessions_data(repo_a)), 1)
        # Different target workspace gets its own isolated #1
        id_b, _ = cli.record_session(repo_b, "bbbb-0000-0000-0000-000000000002", "Task B", "m1")
        self.assertEqual(id_b, 1)

        all_sess = cli.list_all_sessions()
        cwds = {os.path.realpath(s["cwd"]) for s in all_sess}
        self.assertEqual(cwds, {os.path.realpath(repo_a), os.path.realpath(repo_b)})

        bin_path = os.path.join(REPO_ROOT, "bin", "claude-subagent")
        env = os.environ.copy()
        env["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        p_all = subprocess.run(
            [bin_path, "ps", "--all", "--json"],
            cwd=self.tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertEqual(len(json.loads(p_all.stdout)["sessions"]), 2)

        # `-C` / `--workspace` alias binds cleanly from outside target dir
        p_b = subprocess.run(
            [bin_path, "ps", "-C", repo_b, "--json"],
            cwd=self.tmp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d_b = json.loads(p_b.stdout)
        self.assertEqual(len(d_b["sessions"]), 1)
        self.assertEqual(d_b["sessions"][0]["session_id"], "bbbb-0000-0000-0000-000000000002")

    def test_free_model_selection_with_preserved_default(self):
        fake_crouter = self._make_fake_exec(
            "crouter",
            "#!/bin/sh\n"
            "if [ \"$1\" = \"list\" ]; then\n"
            "  printf 'PROVIDER STATUS AUTH DEFAULT_MODEL ENDPOINT\\n'\n"
            "  printf 'ollama local none qwen3.8:27b-dflash http://127.0.0.1:11435\\n'\n"
            "  printf 'openrouter ready env nvidia/nemotron-3.5-super-vl:free https://openrouter.ai/api\\n'\n"
            "fi\n"
            "exit 0\n"
        )
        fake_claude = self._make_fake_exec("claude", "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '2.1.316'; fi\nexit 0\n")
        orig_crouter = os.environ.get("CLAUDE_SUBAGENT_CROUTER")
        orig_claude = os.environ.get("CLAUDE_SUBAGENT_CLAUDE")
        os.environ["CLAUDE_SUBAGENT_CROUTER"] = fake_crouter
        os.environ["CLAUDE_SUBAGENT_CLAUDE"] = fake_claude
        try:
            self.assertEqual(
                cli.handle_setup([
                    "--backend", "crouter",
                    "--provider", "openrouter",
                    "--model", "nvidia/nemotron-3.5-super-vl:free",
                ]),
                0,
            )

            # 1. Default preserved
            rc0, out0 = cli.execute_subagent(prompt="hi", cwd=self.tmp_dir, dry_run=True)
            self.assertEqual(rc0, 0)
            d0 = json.loads(out0)
            self.assertEqual(d0["provider"], "openrouter")
            self.assertEqual(d0["model"], "nvidia/nemotron-3.5-super-vl:free")

            # 2. `-m sonnet` and `-m claude-opus-4-6` auto-route to native claude
            for alias in ("sonnet", "claude-opus-4-6"):
                rc_nat, out_nat = cli.execute_subagent(prompt="hi", cwd=self.tmp_dir, model=alias, dry_run=True)
                self.assertEqual(rc_nat, 0)
                d_nat = json.loads(out_nat)
                self.assertEqual(d_nat["backend"], "native")
                self.assertEqual(d_nat["provider"], "claude")
                self.assertEqual(d_nat["model"], alias)
                self.assertEqual(d_nat["command"][0], fake_claude)

            # 3. `-m deepseek/deepseek-r1:free` inherits persisted openrouter provider
            rc_or, out_or = cli.execute_subagent(
                prompt="hi", cwd=self.tmp_dir, model="deepseek/deepseek-r1:free", dry_run=True
            )
            self.assertEqual(rc_or, 0)
            d_or = json.loads(out_or)
            self.assertEqual(d_or["backend"], "crouter")
            self.assertEqual(d_or["provider"], "openrouter")
            self.assertEqual(d_or["model"], "deepseek/deepseek-r1:free")

            # 4. `--provider ollama` routes to ollama
            rc_ol, out_ol = cli.execute_subagent(prompt="hi", cwd=self.tmp_dir, provider="ollama", dry_run=True)
            self.assertEqual(rc_ol, 0)
            d_ol = json.loads(out_ol)
            self.assertEqual(d_ol["provider"], "ollama")
            self.assertEqual(d_ol["command"][:3], [fake_crouter, "ollama", "--once"])

            # 5. `claude-subagent models --json` returns default + nativeAliases + crouter providers
            bin_path = os.path.join(REPO_ROOT, "bin", "claude-subagent")
            m_proc = subprocess.run(
                [bin_path, "models", "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
            )
            m_data = json.loads(m_proc.stdout)
            self.assertEqual(m_data["defaultRoute"]["model"], "nvidia/nemotron-3.5-super-vl:free")
            self.assertIn("sonnet", m_data["nativeAliases"])
            self.assertEqual(len(m_data["crouter"]["providers"]), 2)
        finally:
            if orig_crouter is None:
                os.environ.pop("CLAUDE_SUBAGENT_CROUTER", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CROUTER"] = orig_crouter
            if orig_claude is None:
                os.environ.pop("CLAUDE_SUBAGENT_CLAUDE", None)
            else:
                os.environ["CLAUDE_SUBAGENT_CLAUDE"] = orig_claude

    def test_minimal_cli_syntax_auto_stdin_and_role_presets(self):
        bin_path = os.path.join(REPO_ROOT, "bin", "claude-subagent")
        env = {k: v for k, v in os.environ.items() if k not in self._PARENT_ENV_KEYS}
        env["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        env["CLAUDE_SUBAGENT_CONFIG"] = os.environ["CLAUDE_SUBAGENT_CONFIG"]
        env["CLAUDE_SUBAGENT_STATE_DIR"] = os.environ["CLAUDE_SUBAGENT_STATE_DIR"]

        # 1. Positional prompt + --readonly shorthand
        p1 = subprocess.run(
            [bin_path, "--allow-unconfigured", "--dry-run", "--readonly", "Audit src/"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d1 = json.loads(p1.stdout)
        self.assertEqual(d1["allowedTools"], "readonly")
        self.assertIn("-p", d1["command"])
        self.assertIn("Audit src/", d1["command"])

        # 2. Auto-stdin without `-p -`
        p2 = subprocess.run(
            [bin_path, "--allow-unconfigured", "--dry-run"],
            input="From piped stdin\nLine 2",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d2 = json.loads(p2.stdout)
        self.assertIn("From piped stdin\nLine 2", d2["command"])

        # 3. Smart `-w "Refactor auth"` promotes to prompt + worktree=True
        p3 = subprocess.run(
            [bin_path, "--allow-unconfigured", "--dry-run", "-w", "Refactor auth module"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d3 = json.loads(p3.stdout)
        self.assertIn("Refactor auth module", d3["command"])
        self.assertIn("--worktree", d3["command"])
        wt_idx = d3["command"].index("--worktree")
        self.assertNotEqual(d3["command"][wt_idx + 1] if wt_idx + 1 < len(d3["command"]) else None, "Refactor auth module")

        # 4. Explicit `-w feat-auth "Refactor auth"` keeps branch name
        p4 = subprocess.run(
            [bin_path, "--allow-unconfigured", "--dry-run", "-w", "feat-auth", "Refactor auth module"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d4 = json.loads(p4.stdout)
        wt_idx4 = d4["command"].index("--worktree")
        self.assertEqual(d4["command"][wt_idx4 + 1], "feat-auth")
        self.assertIn("Refactor auth module", d4["command"])

    def test_native_verbs_send_fork_ps_show_stop_rm_clear(self):
        fake_claude = self._make_fake_exec(
            "claude",
            "#!/bin/sh\n"
            "if echo \"$*\" | grep -q -- '--fork-session'; then\n"
            "  printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"duration_ms\":100,\"num_turns\":1,\"session_id\":\"99999999-9999-9999-9999-999999999999\",\"result\":\"Forked ok\"}\\n'\n"
            "else\n"
            "  printf '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"duration_ms\":100,\"num_turns\":1,\"session_id\":\"88888888-8888-8888-8888-888888888888\",\"result\":\"Verb ok\"}\\n'\n"
            "fi\n"
        )
        bin_path = os.path.join(REPO_ROOT, "bin", "claude-subagent")
        env = {k: v for k, v in os.environ.items() if k not in self._PARENT_ENV_KEYS}
        env["CLAUDE_SUBAGENT_CLAUDE"] = fake_claude
        env["CLAUDE_SUBAGENT_NO_CROUTER"] = "1"
        env["CLAUDE_SUBAGENT_CONFIG"] = os.environ["CLAUDE_SUBAGENT_CONFIG"]
        env["CLAUDE_SUBAGENT_STATE_DIR"] = os.environ["CLAUDE_SUBAGENT_STATE_DIR"]
        env["CODEX_THREAD_ID"] = "codex-verb-test"

        # 1. Spawn via minimal positional prompt with -C
        p1 = subprocess.run(
            [bin_path, "--allow-unconfigured", "-C", self.tmp_dir, "--json", "-q", "Initial task"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d1 = json.loads(p1.stdout)
        self.assertEqual(d1["session"]["id"], 1)
        self.assertEqual(os.path.realpath(d1["session"]["cwd"]), os.path.realpath(self.tmp_dir))

        # 2. `send 1 "follow up"` increments turns on #1
        p2 = subprocess.run(
            [bin_path, "send", "1", "--allow-unconfigured", "--cwd", self.tmp_dir, "--json", "-q", "Follow up"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d2 = json.loads(p2.stdout)
        self.assertEqual(d2["session"]["id"], 1)

        # 3. `fork 1 "branch"` creates #2
        p3 = subprocess.run(
            [bin_path, "fork", "1", "--allow-unconfigured", "--cwd", self.tmp_dir, "--json", "-q", "Branch out"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        d3 = json.loads(p3.stdout)
        self.assertEqual(d3["session"]["id"], 2)
        self.assertEqual(d3["session"]["uuid"], "99999999-9999-9999-9999-999999999999")

        # 4. `ps --json` & `show 1 --json`
        ps_proc = subprocess.run(
            [bin_path, "ps", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        ps_data = json.loads(ps_proc.stdout)
        self.assertEqual(len(ps_data["sessions"]), 2)
        self.assertEqual(ps_data["sessions"][0]["turns"], 2)

        show_proc = subprocess.run(
            [bin_path, "show", "2", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertEqual(json.loads(show_proc.stdout)["session"]["id"], 2)

        # 5. `stop <id>` terminates a live running process group and marks it stopped
        os.environ["CODEX_THREAD_ID"] = "codex-verb-test"
        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            stop_id, _ = cli.register_running_session(
                self.tmp_dir, "77777777-7777-7777-7777-777777777777", "Sleeping", "m1", pid=sleeper.pid
            )
            self.assertEqual(stop_id, 3)
            stop_proc = subprocess.run(
                [bin_path, "stop", "3", "--cwd", self.tmp_dir, "--json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
            )
            stop_data = json.loads(stop_proc.stdout)
            self.assertTrue(stop_data["ok"])
            self.assertTrue(stop_data["wasRunning"])
            self.assertEqual(stop_data["status"], "stopped")
            sleeper.wait(timeout=3)
        finally:
            if sleeper.poll() is None:
                sleeper.kill()

        # 6. `rm 3` and `clear`
        rm_proc = subprocess.run(
            [bin_path, "rm", "3", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertTrue(json.loads(rm_proc.stdout)["ok"])
        clr_proc = subprocess.run(
            [bin_path, "clear", "--cwd", self.tmp_dir, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=True,
        )
        self.assertEqual(json.loads(clr_proc.stdout)["removed"], 2)


if __name__ == "__main__":
    unittest.main()
