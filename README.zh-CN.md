# claude-subagent

[![Version](https://img.shields.io/badge/version-0.3.0-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](pyproject.toml)

[English](README.md) | 简体中文

**纯 `Skill + CLI` 架构的无头 Claude Code 子代理运行器与通用 Agent Skill——自带首次配置持久化、编号会话与 Git Worktree 隔离。**

不依赖任何第三方路由工具，**直接调用原生 Claude Code 即可使用**；如果搭配 [`crouter`](https://github.com/Luckycat133/crouter) 效果更佳（支持多供应商、本地模型或免费模型统一路由）。

开箱适配 **Antigravity**、**Codex**、**Cursor**、**Windsurf**、**Gemini CLI**、**OpenClaw** 与 **Cline**；**零常驻 MCP 负担**，**零第三方 Python 依赖**（Claude Code 本身已内置原生 `Agent` 子代理，因此 Skill 仅面向外部编排端提供）。

## 核心特性

- **原生级零冗余调用体验**：直接写 `claude-subagent "做 X"`、`claude-subagent -C /repo --readonly "..."` 或 `claude-subagent -w "..."`，无需每次手填 `--title`/`--allowed-tools`/`--strict-mcp`/`--provider`，默认已含极速无头配置（`--strict-mcp-config` + `--no-chrome`，本地启动 ~0.4s）。
- **目标文件夹（Workspace）会话绑定（全 IDE 通用）**：`#1`、`#2`、`-c`、`send`、`ps`、`stop` 严格绑定到指定的目标目录（`-C` / `--cwd` / `--workspace`，经 `realpath` 规范化），不依赖宿主 IDE 环境变量，所有客户端行为一致（`ps --all` 可跨工作区查看）。
- **默认模型稳固 + 按次自由选模**：保留已配置的默认模型，同时支持单次 `-m <model>`（`sonnet`/`opus`/`haiku`/`fable`/`claude-*` 自动走原生 Claude，其余模型自动继承默认供应商）、`--provider <p>` 与 `claude-subagent models` 查看可用模型。
- **原生风格动词 & 实时进度**：提供 `send <id> "消息"`、`fork <id> "消息"`、`ps`、`show <id>`、`stop <id>`、`rm <id>`、`doctor --json` 与 `--json` 结构化信封；通过 `stream-json` 实时向 `stderr` 输出轮次/工具进度并支持僵死 PID 检测。
- **强制首次 Setup 与持久化策略**（`~/.config/claude-subagent/config.json`）：首次使用探测可用模型并确认默认路由与协作策略，后续部分更新自动增量合并。
- **原生 Claude 开箱即用，搭配 `crouter` 体验更佳**：未安装 `crouter` 时直连原生 `claude`；检测到 `crouter` 时始终附带 `--once` 不改写全局 `crouter use`。
- **并发安全 & 干净收尾**：`realpath` 规范化文件锁注册表、worktree 续接自动追踪、脏仓库增量 Git 检测，以及全进程组强杀清理（`SIGINT → SIGTERM → SIGKILL`）。

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
# 健康检查与可用模型列表：
claude-subagent doctor --json
claude-subagent models

# 极简派发、指定目标目录与按次切模：
claude-subagent "修复 parser.py 边界问题并重跑 pytest -q"
claude-subagent -C /path/to/repo --readonly "排查 src/auth/ 的 token 校验遗漏"
claude-subagent -m sonnet "用 sonnet 精审本次并发修复"
claude-subagent -w "重构 src/auth/ 401 错误返回并跑 pytest tests/test_auth_errors.py -q"

# 会话查看、对话续接、分叉与中止：
claude-subagent ps -C /path/to/repo
claude-subagent show 1 -C /path/to/repo
claude-subagent send 1 -C /path/to/repo "补齐缺失 Authorization 头的边界情况并重跑 pytest"
claude-subagent fork 1 -C /path/to/repo "尝试另一种中间件实现思路"
claude-subagent stop 1 -C /path/to/repo
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

## 开源协议

[MIT](LICENSE)
