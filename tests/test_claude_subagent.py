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
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="claude_subagent_test_")
        self.orig_state_dir = os.environ.get("CLAUDE_SUBAGENT_STATE_DIR")
        self.orig_config_path = os.environ.get("CLAUDE_SUBAGENT_CONFIG")
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
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_fake_exec(self, name: str, body: str = "#!/bin/sh\nexit 0\n") -> str:
        path = os.path.join(self.tmp_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_version_matches_semver(self):
        self.assertEqual(__version__, "0.1.1")

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


if __name__ == "__main__":
    unittest.main()
