# 开发指南

本文面向后续开发者：如何搭建环境、理解代码组织、扩展功能、运行测试与调试。

## 开发环境

| 项目 | 要求 |
|---|---|
| Python | >= 3.14（`pyproject.toml`） |
| 包管理器 | 推荐 `uv`（项目带 `uv.lock`） |
| 测试框架 | pytest（`asyncio_mode = auto`，见 `pyproject.toml [tool.pytest.ini_options]`） |

```bash
uv sync --extra dev      # 安装含测试依赖
uv run pytest            # 运行全部测试
```

## 代码组织方式

- 入口 → 装配 → 界面 → 会话/流程 → 能力 → 基础，共六层（见 [project-structure.md](project-structure.md)）。
- **数据模型集中在 `models.py`**：先看它就能了解全系统的"词汇表"。
- **事件驱动**：TurnRunner → TUI 通过 `TurnEvent` 通信；Provider → TurnRunner 通过 `StreamEvent` 通信。
- **协议无关**：会话层只存抽象消息，Provider 负责序列化差异。

## 如何增加一个新功能

### 新增内置工具

1. 在 `lancher_code/tools/builtin/` 新建文件，实现 `Tool` 协议：
   - `definition`：`ToolDefinition`（名称、描述、参数 JSON Schema、分类、并发安全、可见模式）
   - `execute(arguments, context)`：返回 `ToolExecutionResult`（用 `build_tool_success` / `build_tool_error`）
2. 在 `tools/builtin/__init__.py` 导出类
3. 在 `tools/__init__.py` 的 `create_default_tool_registry()` 中注册
4. 如需要专属权限标签，加入 `_BUILTIN_LABELS`（`tools/__init__.py`）
5. 在 `tests/tools/` 添加测试

### 新增模型协议（Provider）

1. `providers/` 新建实现类，继承 `BaseChatProvider`，实现 `stream_chat()`
2. `models.py` 的 `ProviderProtocol` Literal 加协议名
3. `providers/factory.py` 按协议分发
4. `config_system/loader.py` 的 `SUPPORTED_PROTOCOLS` 登记
5. 用 `httpx.MockTransport` 写流式测试（参考 `tests/providers/`）

### 新增 MCP Server 支持类型

- 在 `mcp/config.py` 的 `_parse_server` 扩展类型分支，`mcp/connection.py` 的 `_connect_transport` 增加对应 transport
- 注意 `settings_service._validate_mcp` 与设置面板的类型列表也要同步

### 新增斜杠命令

1. `slash_commands.py` 的 `create_default_slash_command_registry()` 注册 `SlashCommandDefinition`
2. `tui_views/chat.py` 的 `_execute_slash_command()` 实现处理逻辑
3. 如需参数补全，提供 `argument_completer`
4. `tests/test_slash_commands.py` 与 TUI 测试补充覆盖

### 新增配置项

1. `models.py` 对应 dataclass 加字段
2. `config_system/loader.py` 的 `_load_*` 加读取与校验（沿用 `_read_positive_int` 等辅助）
3. `config_system/writer.py` 的 `serialize_config` 加写回
4. 需要界面编辑时，扩展 `settings_service.py` 与 `tui_views/settings.py`
5. 更新 [configuration.md](configuration.md) 配置表

## 设计模式速查

| 模式 | 位置 | 说明 |
|---|---|---|
| 策略 + 工厂 | `providers/` | `create_provider` 按协议选实现 |
| 注册表 | `tools/core/registry.py`、`slash_commands.py` | 注册 + 查询 + 模式过滤 |
| 协议（Protocol） | `tools/core/base.py`、`providers/base.py` | 定义"工具/供应商必须长什么样" |
| 异步生成器 + 队列 | `turn_runner.py` | 后台任务产事件，消费者逐条消费 |
| 门面 | `ChatTUI` / `ConfigBootstrapTUI` | 包装 Textual App |
| 状态机 | `SessionController.set_work_phase` / `set_permission_policy` | 阶段与策略独立，旧模式入口仅供兼容 |
| 原子写 | `session_store.save`、`settings_service._atomic_write_many` | 临时文件 + `os.replace` |

## 运行测试

```bash
uv run pytest                    # 全部
uv run pytest tests/tools/       # 工具系统
uv run pytest tests/mcp/         # MCP（含真实 stdio 测试服务器）
uv run pytest tests/test_tui_flow.py -k streaming   # 按关键字过滤
```

注意：

- `pytest-asyncio` 为 `auto` 模式，`async def test_*` 自动以 asyncio 运行。
- Provider 测试通过注入 `httpx.MockTransport` 模拟流式响应（fixture 见 `tests/conftest.py`）。
- TUI 测试直接驱动 `LanCherTextualApp`（`tests/test_tui_*.py`），不依赖真实终端。

## 调试

| 手段 | 说明 |
|---|---|
| 日志 | `~/.lancher/logs/lancher-error.log`（ERROR 级，滚动 5MB×5）；错误均带 `event=<事件名>` 前缀，如 `event=turn_failed_unexpected` |
| 临时开 DEBUG | `logging_system.py` 中 `logger.setLevel(logging.ERROR)` 是硬编码级别；如需要 DEBUG 输出，可临时改低级别（注意不要提交） |
| 直接测模块 | 大多数核心函数是纯函数或可注入依赖，可用 pytest + monkeypatch 单测 |
| 敏感信息 | 日志自动脱敏（`register_sensitive_values` + `RedactingFormatter`），调试时不要手动打印 api_key |

## 常见开发规范

- 中文注释、中文用户可见文案（`user_message`）。
- 所有错误信息通过异常层次给出：`errors.py` 的 `LanCherError` 子类，带 `user_message`。
- 工具返回**结构化错误**而不是抛异常：`ToolExecutionResult(is_error=True, error_code=..., error_message=...)`。
- 不要向日志写入密钥：新加的敏感值记得 `register_sensitive_values`。
- 新增依赖时更新 `pyproject.toml`，并用 `uv lock` 刷新 `uv.lock`。
- 修改 TUI 时注意窄终端布局（`-narrow` class）与键盘可用性（参考现有测试）。
- 提交前跑一遍 `uv run pytest`，保证全量通过（README 声称"全量测试当前通过"）。

## 打包

PyInstaller 打包流程见 [pyinstaller.md](pyinstaller.md)：

```bash
uv sync --extra build
uv run pyinstaller --clean --noconfirm lancher.spec
```

## 文档维护

- 本文档目录 `docs/` 已被 `.gitignore` 忽略（"docs/"，当前不纳入版本控制）。如需入库，需调整 `.gitignore`。
- 修改行为（配置项、命令、事件、文件格式）时，同步更新 `docs/` 下对应文档与 [glossary.md](glossary.md)。
