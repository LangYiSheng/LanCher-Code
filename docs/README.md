# LanCher Code 项目文档

## 项目简介

LanCher Code 是一个**基于 Python 的终端 AI 编程助手**（类似 Claude Code）。

它不是一个"只会聊天"的对话机器人，而是能在终端里和你一起：

- 读代码、查文件、搜索代码
- 修改文件、执行 shell 命令
- 制定并落盘工作计划（Plan Mode）
- 把危险操作拦截在权限系统里，需要时弹窗请你确认

项目的所有结论均以当前源码为准。对源码中无法确认的内容，文档中会明确标注"无法确认 / 待确认"。

## 核心功能

| 功能 | 说明 |
|---|---|
| 终端多轮对话 | Textual TUI，流式输出，支持思考轨迹、工具调用轨迹展示 |
| 双协议后端 | 支持 OpenAI 兼容协议与 Anthropic Claude 协议 |
| 内置工具 | `read_file`、`write_file`、`edit_file`、`glob`、`grep`、`bash`、`write_plan_file`、`tool_search` |
| ReAct 工具循环 | 模型可多轮调用工具直到给出最终回答（默认上限 50 轮） |
| Plan Mode | `/plan` 进入只读规划模式，唯一允许写入的是计划文件 `./.lancher/plan.md` |
| 五层权限系统 | 危险命令黑名单、路径沙箱、三层规则（会话/项目/用户）、四档权限模式、人在回路确认弹窗 |
| 上下文治理 | Token 估算、大工具结果落盘卸载、自动/紧急上下文压缩 |
| 会话持久化 | 按项目保存/恢复会话（`.lancher/session/*.jsonl`），含会话级权限规则 |
| MCP 扩展 | 支持 stdio / Streamable HTTP 两种 MCP Server，工具延迟加载 |

## 技术栈

| 技术 | 用途 | 版本要求 |
|---|---|---|
| Python | 开发语言 | `>= 3.14`（见 `pyproject.toml`） |
| `textual` | TUI 框架 | `>=6.1.0,<7.0.0` |
| `rich` | 富文本渲染 | `>=14.0.0,<15.0.0` |
| `httpx` | HTTP 客户端（流式请求模型） | `>=0.28.1,<1.0.0` |
| `mcp` | MCP 协议客户端 | `>=1.12.4,<2.0.0` |
| `PyYAML` | 配置文件解析 | `>=6.0.2,<7.0.0` |
| `pytest` / `pytest-asyncio` | 测试（dev 依赖） | `>=8.4.1` / `>=1.0.0` |
| `pyinstaller` | 打包（build 依赖） | `>=6.14.1,<7.0.0` |

## 架构概览

```text
用户
 ↓
Textual TUI（lancher_code/tui_views）
 ↓ 提交输入
TurnRunner（lancher_code/turn_runner.py）── 工具循环
 ├── SessionController（会话状态 / 提示词组装）
 ├── Provider（openai / claude 流式请求）
 ├── ToolExecutor + ToolRegistry（内置工具 + MCP 工具）
 └── PermissionEngine（五层权限判定）
 ↓
外部：模型 API / 本地 shell / 文件系统 / MCP Server
```

详细说明见 [architecture.md](architecture.md)。

## 核心模块

| 模块 | 位置 | 职责 |
|---|---|---|
| 应用装配 | `lancher_code/app.py` | 启动流程：加载配置、创建 Provider、会话、工具、MCP、TUI |
| 会话层 | `lancher_code/session.py` | `SessionController`：消息、transcript、模式切换、用法统计 |
| 工具循环 | `lancher_code/turn_runner.py` | `TurnRunner`：ReAct 循环、事件流、取消、自动压缩 |
| 上下文管理 | `lancher_code/context_management.py` | Token 估算、工具结果卸载、摘要压缩 |
| 权限引擎 | `lancher_code/permission_engine.py` | 五层权限判定、规则存储 |
| 提示词构建 | `lancher_code/prompting.py` | system prompt、Plan Mode 提示、动态提醒 |
| 工具系统 | `lancher_code/tools/` | 工具注册表、执行器、8 个内置工具 |
| 模型供应商 | `lancher_code/providers/` | OpenAI / Claude 流式适配 |
| MCP | `lancher_code/mcp/` | MCP Server 配置、连接、工具适配 |
| TUI | `lancher_code/tui_views/` | 聊天、设置、权限面板、引导界面 |

## 快速开始

```bash
# 1. 安装依赖（推荐 uv）
uv sync

# 2. 启动（任选其一）
uv run lancher
uv run lancher-code
python -m lancher_code
python main.py
```

首次启动会自动进入配置引导界面，填写 `protocol / model / base_url / api_key` 后保存到 `~/.lancher/lancher.yaml`。

详细步骤见 [getting-started.md](getting-started.md)。

## 文档导航

按顺序阅读即可快速上手：

1. [快速开始 getting-started.md](getting-started.md) — 安装、启动、第一次运行
2. [项目结构 project-structure.md](project-structure.md) — 目录与文件职责
3. [架构 architecture.md](architecture.md) — 整体架构、组件关系、数据流
4. [配置 configuration.md](configuration.md) — 所有配置项与规则文件格式
5. [交互与命令 cli-and-interaction.md](cli-and-interaction.md) — 斜杠命令、快捷键、权限确认
6. [模块文档 modules/](modules/) — 各核心模块深入说明
7. [运行流程 workflows/](workflows/) — 启动、一轮对话、会话生命周期等流程
8. [开发指南 development.md](development.md) — 如何继续开发
9. [故障排查 troubleshooting.md](troubleshooting.md) — 常见问题
10. [术语表 glossary.md](glossary.md) — 专有名词
11. [PyInstaller 打包 pyinstaller.md](pyinstaller.md) — Windows 可执行文件打包

## 相关文件速查

| 文件 | 作用 |
|---|---|
| `main.py` | 仓库根入口，仅调用 `lancher_code.cli:main` |
| `pyproject.toml` | 项目元数据、依赖、命令入口、pytest 配置 |
| `lancher.spec` | PyInstaller 打包配置 |
| `lancher.example.yaml` | 配置文件结构示例（无真实密钥） |
| `lancher.yaml` | 仓库根目录的本地运行配置（已被 `.gitignore` 忽略，含真实密钥，不要提交） |
