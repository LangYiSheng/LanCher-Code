# 项目结构

## 核心目录树

以下为项目核心结构（省略 `.git`、`.idea`、`__pycache__`、`.venv`、`build`、`dist`、`.pytest_cache` 等无关/生成目录）：

```text
lancher-code/
├── main.py                        # 仓库根入口，仅调用 lancher_code.cli:main
├── pyproject.toml                 # 项目元数据、依赖、命令入口、pytest 配置
├── uv.lock                        # uv 锁定依赖版本
├── lancher.spec                   # PyInstaller 打包配置
├── lancher.example.yaml           # 配置文件结构示例（无真实密钥）
├── lancher.yaml                   # 本地运行配置（gitignore，含真实密钥，勿提交）
├── AGENTS.md                      # 面向 AI 编码助手的项目说明
├── assets/
│   └── lancher_code.ico           # Windows 可执行文件图标
├── lancher_code/                  # 主包
│   ├── __init__.py                # 包版本号 __version__ = "0.1.0"
│   ├── __main__.py                # python -m lancher_code 入口
│   ├── cli.py                     # CLI 入口 main()：日志初始化 + asyncio.run(run_app)
│   ├── app.py                     # 应用装配：启动流程核心（详见 workflows/startup.md）
│   ├── config.py                  # 配置系统对外的统一 re-export
│   ├── errors.py                  # 异常层次（ConfigError / ProviderError / ...）
│   ├── logging_system.py          # 日志系统：ERROR 级滚动文件日志 + 敏感信息脱敏
│   ├── models.py                  # 全项目数据模型（dataclass + Literal 类型）
│   ├── session.py                 # SessionController：会话状态与 transcript 管理
│   ├── sessions/                  # 路径、事件仓库、状态编解码与服务
│   ├── execution/                 # 资源调度、调用与进程状态、输出和平台后端
│   ├── turn_runner.py             # TurnRunner：ReAct 工具循环与事件流
│   ├── context_management.py      # 上下文治理：Token 估算、结果卸载、摘要压缩
│   ├── prompting.py               # 提示词构建（system prompt / Plan Mode / 动态提醒）
│   ├── permission_engine.py       # PermissionEngine + PermissionStorage：五层权限
│   ├── settings_service.py        # SettingsService：设置页数据读写与校验
│   ├── slash_commands.py          # 斜杠命令注册、解析、补全
│   ├── tool_call_parser.py        # ToolCallAssembler：流式工具调用分片拼接
│   ├── tui.py                     # TUI 视图对外 re-export
│   ├── config_system/             # 配置系统
│   │   ├── paths.py               # 所有配置文件路径常量与函数
│   │   ├── bootstrap.py           # 首次引导状态判定
│   │   ├── loader.py              # YAML 加载与校验（load_config）
│   │   └── writer.py              # 配置序列化与写回
│   ├── providers/                 # 模型供应商
│   │   ├── base.py                # BaseChatProvider：SSE 解析、错误分类、用量统计
│   │   ├── claude.py              # ClaudeProvider（/v1/messages 流式）
│   │   ├── openai.py              # OpenAIProvider（/chat/completions 流式）
│   │   └── factory.py             # create_provider：按 protocol 选择实现
│   ├── tools/                     # 工具系统
│   │   ├── __init__.py            # create_default_tool_registry()：注册 14 个内置工具
│   │   ├── core/
│   │   │   ├── base.py            # Tool 协议 + 成功/失败结果构造
│   │   │   ├── registry.py        # ToolRegistry：注册、列出、延迟工具搜索
│   │   │   ├── executor.py        # ToolExecutor：权限判定 + 并发/超时执行
│   │   │   ├── validation.py      # 标准 JSON Schema 参数校验与离线引用
│   │   │   ├── common.py          # 路径沙箱工具与 SKIP_DIRS 列表
│   │   │   └── file_state_cache.py# FileStateCache：读/写状态缓存（防盲写）
│   │   └── builtin/               # 内置工具实现
│   │       ├── read_file.py       # read_file
│   │       ├── write_file.py      # write_file
│   │       ├── edit_file.py       # edit_file
│   │       ├── command.py         # run_command：启动托管进程
│   │       ├── process.py         # 六个进程读取与控制工具
│   │       ├── glob.py            # glob
│   │       ├── grep.py            # grep
│   │       ├── write_plan_file.py # write_plan_file（仅 plan 模式）
│   │       └── tool_search.py     # tool_search（MCP 延迟工具搜索加载）
│   ├── mcp/                       # MCP（Model Context Protocol）客户端
│   │   ├── config.py              # MCP Server 配置加载与校验
│   │   ├── connection.py          # stdio / Streamable HTTP 连接管理
│   │   ├── manager.py             # MCPClientManager：并发初始化、工具注册、进度事件
│   │   ├── adapter.py             # MCPToolAdapter：远程工具 → 本地 Tool
│   │   └── template.py            # 全局 mcp.yaml 模板生成
│   └── tui_views/                 # Textual 界面
│       ├── bootstrap.py           # 首次配置引导界面
│       ├── chat.py                # 主聊天界面 LanCherTextualApp / ChatTUI
│       ├── composer.py            # 输入框与斜杠命令补全菜单
│       ├── message.py             # 消息气泡、思考轨迹、顶部横幅
│       ├── permission.py          # 内联权限确认面板
│       ├── tasks.py               # Session 任务列表、增量输出与控制
│       └── settings.py            # 设置面板（模型 / MCP / 权限规则）
├── tests/                         # 测试（pytest，asyncio_mode = auto）
│   ├── conftest.py                # 共享 fixture（provider 配置、mock http client）
│   ├── test_app_startup.py        # 启动流程（引导/装配）
│   ├── test_config.py             # 配置加载与校验
│   ├── test_config_bootstrap.py   # 引导界面
│   ├── test_context_management.py # 上下文治理
│   ├── test_logging_system.py     # 日志与脱敏
│   ├── test_permission_engine.py  # 权限引擎
│   ├── test_prompting.py          # 提示词构建
│   ├── test_session.py            # 会话层
│   ├── test_session_repository.py # Session事件仓库、锁与路径校验
│   ├── test_session_lifecycle.py  # 首条消息、恢复与会话隔离
│   ├── test_settings.py           # 设置服务/界面
│   ├── test_slash_commands.py     # 斜杠命令
│   ├── test_tool_call_parser.py   # 工具调用解析
│   ├── test_tui_flow.py           # TUI 主流程
│   ├── test_tui_permissions.py    # TUI 权限面板
│   ├── test_tui_phase3.py         # TUI Plan Mode / 模式切换
│   ├── test_turn_runner.py        # 工具循环
│   ├── test_phase3_mode_and_tools.py
│   ├── providers/                 # 供应商测试（mock httpx）
│   ├── mcp/                       # MCP 配置/适配/管理器测试 + stdio 测试服务器
│   └── tools/                     # 各内置工具与执行器测试
├── docs/                          # 项目文档（本目录）
└── .lancher/                      # 项目级运行时数据（gitignore）
    ├── permissions.yaml           # 项目级权限规则
    ├── mcp.yaml                   # 项目级 MCP Server 配置
    └── sessions/                  # 首条消息自动创建 UUID Session
        └── <uuid>/                # events.jsonl、meta.json、checkpoint.json
            ├── processes/         # 进程元信息、输出日志与索引
            ├── blobs/             # 会话内部工具结果等内容
            └── workspace/         # plan.md、tmp/、artifacts/，所有阶段可写
```

## 分层说明

| 层 | 目录 | 说明 |
|---|---|---|
| 入口层 | `main.py`、`lancher_code/cli.py`、`__main__.py` | 解析参数、初始化日志、启动事件循环 |
| 装配层 | `lancher_code/app.py` | 组装所有核心对象，编排启动顺序 |
| 界面层 | `lancher_code/tui_views/`、`tui.py` | Textual 界面，只消费事件、不直接接触网络 |
| 会话/流程层 | `session.py`、`turn_runner.py`、`context_management.py`、`prompting.py` | 对话状态、工具循环、上下文治理、提示词 |
| 能力层 | `tools/`、`providers/`、`mcp/` | 工具执行、模型请求、MCP 扩展 |
| 基础层 | `models.py`、`errors.py`、`logging_system.py`、`config_system/` | 数据模型、异常、日志、配置 |

## 重要文件的调用关系

### `main.py`

- **负责**：仓库根入口
- **被谁调用**：`python main.py`、PyInstaller 打包入口
- **会调用谁**：`lancher_code.cli.main`

### `lancher_code/cli.py`

- **负责**：进程级入口。构建 argparse 解析器（当前**没有定义任何参数**，仅保留 `prog="lancher"` 描述）、初始化日志、`asyncio.run(run_app())`、统一异常处理与退出码
- **被谁调用**：`main.py`、`__main__.py`、console script（`lancher` / `lancher-code`）
- **会调用谁**：`app.run_app`、`logging_system.configure_logging / close_logging`

### `lancher_code/app.py`

- **负责**：装配整个应用（`run_app()`），详见 [workflows/startup.md](workflows/startup.md)
- **被谁调用**：`cli.main`
- **会调用谁**：`config_system`（读配置）、`providers.factory.create_provider`、`session.SessionController`、`tools.create_default_tool_registry`、`mcp.MCPClientManager`、`permission_engine`、`settings_service`、`tools.core.executor.ToolExecutor`、`turn_runner.TurnRunner`、`tui_views.bootstrap.ConfigBootstrapTUI`、`tui_views.chat.ChatTUI`

### `lancher_code/turn_runner.py`

- **负责**：一次用户输入的完整工具循环
- **被谁调用**：`app.py`（构造）、`tui_views/chat.py`（`run_user_turn()` 消费事件流）
- **会调用谁**：`session.SessionController`、`providers.base.ChatProvider`、`tools.core.executor.ToolExecutor`、`tools.core.registry.ToolRegistry`、`tool_call_parser.ToolCallAssembler`、`context_management`

### `lancher_code/session.py`

- **负责**：会话状态、协议无关 transcript、模式切换、会话保存/恢复
- **被谁调用**：`app.py`、`turn_runner.py`、`tui_views/chat.py`
- **会调用谁**：`prompting`（组装请求）、`context_management`（估算/压缩）、`sessions`（事件持久化）

### `lancher_code/permission_engine.py`

- **负责**：工具调用的权限判定与规则存储
- **被谁调用**：`tools/core/executor.py`（每次工具执行前）
- **会调用谁**：`tools/core/common.py`（路径沙箱）

### `lancher_code/tools/core/executor.py`

- **负责**：执行一批工具调用（权限复核、资源调度、托管进程和结果排序）
- **被谁调用**：`turn_runner.py`
- **会调用谁**：`registry.ToolRegistry`、`permission_engine.PermissionEngine`、具体工具实现
