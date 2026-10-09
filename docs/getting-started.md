# 快速开始

本文说明如何从零开始安装、配置并运行 LanCher Code。

## 环境要求

| 项目 | 要求 | 依据 |
|---|---|---|
| Python | **>= 3.14** | `pyproject.toml` 中 `requires-python = ">=3.14"` |
| 操作系统 | 主要面向 **Windows**（终端命令执行依赖 Windows PowerShell） | `tools/builtin/bash.py` 硬编码 `C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe`；提示词中系统标签为 "Windows PowerShell"。其他平台未做适配，**非 Windows 上 `bash` 工具会因找不到 PowerShell 而失败（无法确认其他平台兼容性）** |
| 终端 | 支持 Textual 的现代终端（Windows Terminal 等） | Textual 框架要求 |

> 注意：Python 3.14 是当前项目的硬性版本下限，低于 3.14 的环境无法安装依赖。

## 1. 安装依赖

推荐使用 [`uv`](https://docs.astral.sh/uv/)（项目使用 `uv.lock` 锁定依赖）：

```bash
uv sync
```

这会在 `.venv` 中创建虚拟环境并安装 `pyproject.toml` 中声明的全部依赖。

如果还要运行测试或打包，分别需要额外依赖组：

```bash
uv sync --extra dev     # 测试依赖（pytest 等）
uv sync --extra build   # 打包依赖（pyinstaller）
```

不使用 uv 也可以（等效于）：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## 2. 启动程序

有四种等价启动方式，入口都是 `lancher_code/cli.py` 的 `main()`：

```bash
uv run lancher          # 安装后注册的命令（推荐）
uv run lancher-code     # 同一入口的别名命令
python -m lancher_code  # 模块方式（依赖 __main__.py）
python main.py          # 仓库根入口（依赖 main.py）
```

四种方式没有行为差异，都会：

1. 初始化日志系统（`~/.lancher/logs/lancher-error.log`）
2. 运行 `lancher_code.app.run_app()`
3. 以退出码结束：`0` 正常退出 / `130` 用户按 Ctrl+C / `1` 发生未捕获异常

## 3. 第一次运行：配置引导

首次启动时，如果 `~/.lancher/lancher.yaml` 不存在，程序会进入 **Textual 引导界面**（`ConfigBootstrapTUI`，见 `lancher_code/tui_views/bootstrap.py`）。

需要填写：

- **供应商名称**：例如 DeepSeek 或自己的网关名称
- **提供商协议**：`OpenAI` 或 `Anthropic`（配置中的值是 `claude`）
- **API 模型名称**：例如 `gpt-4.1-mini` 或 `claude-sonnet`，可另填仅用于界面的显示名称
- **Base URL**：切换协议时会自动填入对应官方地址，可改为你自己的网关地址
- **API Key**：模型供应商的密钥（输入时隐藏显示）
- 高级选项（可折叠展开）：请求超时秒数（默认 60）、Anthropic thinking 开关与 `budget_tokens`

点击「保存并启动」后，配置写入 `~/.lancher/lancher.yaml`，同时自动创建 `~/.lancher/mcp.yaml` 模板文件。之后正常进入聊天界面。

> 取消引导（「取消」按钮）则直接退出，不写入任何配置。

## 4. 配置文件

程序实际读取的全局配置文件是：

```text
~/.lancher/lancher.yaml
```

结构参考仓库中的 `lancher.example.yaml`（该文件不含真实密钥）：

```yaml
providers:
  openai:
    name: OpenAI
    protocol: openai         # openai | claude
    base_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}
    timeout_seconds: 60
    models:
      mini:
        model_name: gpt-4.1-mini
        context_window: 128000
default_model: openai/mini
ui:
  show_timestamps: false
  show_thinking_status: true
runtime:
  tool_loop_limit: 50
  unknown_tool_streak_limit: 3
  plan_file_path: ./.lancher/plan.md
  permission_mode: default
```

完整配置项说明见 [configuration.md](configuration.md)。用 `/settings` 增加供应商及模型，用 `/model` 在当前对话中切换。旧单 `provider` 配置无需手动迁移，首次保存会备份后升级。

> 仓库根目录的 `lancher.yaml` 是本地运行配置（已被 `.gitignore` 忽略），里面可能包含真实 API Key，**不要**把它当作示例或提交到版本库。

## 5. 最简单的使用示例

启动并完成配置后：

```text
你: 这个项目的入口文件是哪个？
LanCher: （先调用 glob / read_file 工具，再给出答案）
```

输入 `Enter` 发送消息，`Shift+Enter` 换行。

常用命令示例：

```text
/plan 帮我梳理这个项目的启动流程    ← 进入规划模式并提交任务
/mode acceptEdits                 ← 切换到允许编辑模式
/exit                             ← 退出程序
```

## 6. 如何判断程序运行正常

- 聊天界面正常渲染，输入消息后模型开始流式回复（状态栏显示 `Busy`）
- 状态栏左侧显示当前模型名与协议，右侧显示 Token 用量（`Tokens In / Out`）
- 底部横幅显示 `上下文 xx%`，MCP 初始化完成后显示 `MCP：已就绪 · n/n Server · m 个工具`
- 出现问题时：
  - 界面内消息会标记为 `ERROR`，附错误原因
  - 详细日志写入 `~/.lancher/logs/lancher-error.log`
  - 配置文件非法时程序以退出码 `1` 退出并在终端打印 `[错误] ...` 提示

## 7. 如何停止程序

| 场景 | 操作 |
|---|---|
| 模型正在回复 | 按 `Ctrl+C`：先取消当前回合（不会立即退出） |
| 空闲状态 | 按 `Ctrl+C` 或输入 `/exit`：退出程序 |
| 权限确认弹窗 | `Esc` 表示拒绝本次请求，返回对话 |

## 8. 常见启动方式差异小结

| 启动方式 | 区别 |
|---|---|
| `uv run lancher` / `uv run lancher-code` | 通过 `pyproject.toml` 注册的 console script，与 `uv sync` 环境绑定 |
| `python -m lancher_code` | 不依赖 console script，但需要当前环境已安装 `lancher-code` 包（或源码目录在 `PYTHONPATH` 中） |
| `python main.py` | 直接在源码根目录运行，最简单 |
| `dist/lancher/lancher.exe` | PyInstaller 打包产物，分发时使用，见 [pyinstaller.md](pyinstaller.md) |
