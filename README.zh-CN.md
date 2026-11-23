# claude-subagent

[![Version](https://img.shields.io/badge/version-0.1.1-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)

[English](README.md) | 简体中文

**纯 `Skill + CLI` 架构的无头 Claude Code 子代理运行器与通用 Agent Skill——自带首次配置持久化、编号会话与 Git Worktree 隔离。**

不依赖任何第三方路由工具，**直接调用原生 Claude Code 即可使用**；如果搭配 [`crouter`](https://github.com/Luckycat133/crouter) 效果更佳（支持多供应商、本地模型或免费模型统一路由）。

开箱适配 **Antigravity**、**Codex**、**Cursor**、**Windsurf**、**Gemini CLI**、**OpenClaw**、**Cline** 及 **Claude Code** 自身；**零常驻 MCP 负担**，**零第三方 Python 依赖**。

## 核心特性

- **强制首次 Setup 与持久化策略**（`~/.config/claude-subagent/config.json`）：首次使用前自动探测本机可用模型（原生 `claude` 及可选 `crouter` 供应商），引导向用户确认默认子代理模型与协作/使用策略并持久化保存——后续所有 Agent 统一遵守，不再出现未确认就擅自切换模型的问题。
- **原生 Claude 开箱即用，搭配 `crouter` 体验更佳**：未安装 `crouter` 时直连原生 `claude`；检测到 `crouter` 时自动支持按供应商/场景路由，且始终附带 `--once` 不改写全局 `crouter use`。
- **默认免打扰全权限 + 最小作用域约束**：无头 `-p` 模式默认 `bypassPermissions`，不会在执行中途卡住等待人工点批准（守住子代理省主 Agent token 的核心价值），安全边界改由 `--allowed-tools readonly|coding`、`-w <分支>` worktree 隔离与 `--timeout`/`--budget` 保障。
- **团队规划 + 异常处理最佳实践 Skill**：`skills/claude-subagent/` 教主 Agent 何时主动拉起最小有效团队（2–4 人）、如何写无冗余的目标导向提示词、以及面对代码审查不通过、桌面抢占、超时、写冲突等异常如何闭环处理。
- **并发安全 & 干净收尾**：文件锁原子写编号会话（`#1`、`#2`、`-r 1`、`-c`）、脏仓库本轮增量 Git 变更检测、独立进程组超时清理（`SIGINT → SIGTERM → SIGKILL`）。

## 安装方式

### 1. 安装通用 Agent Skill

```bash
npx skills add Luckycat133/claude-subagent
```

或克隆仓库一键安装 CLI + Skill + 预设子代理角色：

```bash
git clone https://github.com/Luckycat133/claude-subagent.git
cd claude-subagent
./install.sh --all
```

### 2. 安装 CLI

```bash
uv tool install git+https://github.com/Luckycat133/claude-subagent.git
# 或
pip install git+https://github.com/Luckycat133/claude-subagent.git
```

## 快速上手

### 第一步：首次探测并持久化配置（只需一次）

```bash
# 查看当前可用的 Claude CLI 与可选 crouter 模型列表：
claude-subagent setup --discover

# 终端交互式向导：
claude-subagent setup

# 或非交互式直接写入（例如 Agent 征得用户确认后执行）：
# 方案 A：直接使用原生 Claude Code（无需安装 crouter）
claude-subagent setup --backend native --provider claude --delegation-mode proactive

# 方案 B：搭配 crouter
claude-subagent setup \
  --backend crouter \
  --provider openrouter \
  --model nvidia/nemotron-3.5-super-vl:free \
  --delegation-mode proactive \
  --usage-policy "默认主动派工；遇到特殊场景先提醒用户。"
```

### 第二步：派发、续接与独立审查

```bash
# 预览实际路由与命令（不真实调用模型）：
claude-subagent --cwd /path/to/repo --title "Dry run" --dry-run -p "Hello"

# 在独立 git worktree 中派发编码子代理：
claude-subagent \
  --cwd /path/to/repo \
  --title "Auth 401 error refactor" \
  --allowed-tools coding \
  --effort medium \
  -w "refactor-auth" \
  -p - <<'EOF'
Goal: Return structured 401 JSON errors for expired tokens in src/auth/.
Scope: src/auth/errors.py, src/auth/middleware.py, tests/test_auth_errors.py.
Verification: pytest tests/test_auth_errors.py -q
EOF

# 查看会话并用编号续接：
claude-subagent --cwd /path/to/repo --list
claude-subagent --cwd /path/to/repo -r 1 -p "补齐缺失 Authorization 头的边界情况并重跑 pytest"
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 开源协议

[MIT](LICENSE)
