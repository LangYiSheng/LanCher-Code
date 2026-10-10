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

退出确认、实际收尾和终端小结有各自职责：`tui_views/exit_flow.py` 用单调时钟决定停止与退出意图，界面持有异步操作任务，`app.py` 统一等待清理，`run_summary.py` 只展示最终结果。`run_usage.py` 的本次启动账本观察实际 Provider 请求，切换 Session 或模型时要继续注入同一个 observer，不能改成累计恢复的历史消息。完整场景和统计口径见 [结束工作与恢复对话](workflows/app-exit.md)。

## 如何增加一个新功能

### 新增内置工具

1. 在 `lancher_code/tools/builtin/` 新建文件，实现 `Tool` 协议：
   - `definition`：`ToolDefinition`（名称、描述、参数 JSON Schema、分类、权限元信息、可见阶段）
   - `execute(arguments, context)`：返回 `ToolExecutionResult`（用 `build_tool_success` / `build_tool_error`）
   - `resource_claims(arguments, context)`：根据可信实现声明读写资源；没有声明时默认项目独占
2. 在 `tools/builtin/__init__.py` 导出类
3. 在 `tools/__init__.py` 的 `create_default_tool_registry()` 中注册
4. 如需要专属权限标签，加入 `_BUILTIN_LABELS`（`tools/__init__.py`）
5. 在 `tests/tools/` 添加测试

参数 Schema 由执行入口统一验证，不要再实现一套部分校验器。保持 Schema 本身有效，局部与内嵌引用可用；未知外部引用会被离线解析器拒绝。普通工具和远端写操作分别说明提交边界与取消后结果，详见 [工具执行](workflows/tool-execution.md)。

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
| 退出意图状态机 | `tui_views/exit_flow.py` | 停止期间锁存请求，空闲双按在 3 秒内确认；时钟可注入 |
| 请求用量账本 | `run_usage.py` | 按请求 UUID 累计本次启动用量，同一流的 usage 快照替换而非累加 |
| 持久化 | `sessions.repository`、`settings_service._atomic_write_many` | Session 追加事件 + 文件锁；摘要与配置采用临时文件 + `os.replace` |

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
- `tests/test_exit_flow.py` 用注入时钟检查确认窗口，不靠睡眠；`tests/test_run_summary.py` 检查本次访问的恢复目标、完整 UUID、纯文本标题、窄终端和缺失统计。
- 修改退出流程时，状态机测试不能替代真实应用入口：需要确认停止与关闭等待完成后才打印小结，并覆盖清理失败。模型用量测试应同时检查跨 Session/模型、手动/自动压缩、重复 usage 帧和中断流。
- Windows 上将测试临时目录放到系统 Temp 下的具名目录，例如 `--basetemp="$env:TEMP/lancher-exit-tests"`；配合 `PYTHONDONTWRITEBYTECODE=1` 和 `-p no:cacheprovider`，避免在项目根留下验证产物。

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

- `docs/` 已纳入版本控制，行为变更与对应文档一起提交。
- 修改行为（配置项、命令、事件、文件格式）时，同步更新 `docs/` 下对应文档与 [glossary.md](glossary.md)。


## 给工具声明资源

新工具可实现 `resource_claims(arguments, context)`，返回 `ResourceClaim`；声明由工具代码提供，不能信任模型自己提供的安全标签。相同文件的共享读可以并行，独占写与它冲突；目录声明需要考虑 recursive。多个资源一次取得，调用返回后释放 `lifetime=invocation` 部分，`lifetime=process` 部分由真实进程保留到退出。没有真实长进程的工具仍在调用结束释放全部资源。

没有资源声明时默认在本次调用期间项目独占；未知Shell返回后台句柄后释放调用期锁，未声明MCP在本次调用结束释放。profile资源默认 `process`，应按真实持续占用明确配置；允许默认后台服务与后续请求共存，不等于后台命令没有文件副作用。MCP 的 readOnlyHint 只决定阶段与权限能力，不能证明它与其他操作没有资源冲突。新增后端要实现现有 ProcessBackend，不在 Tool.execute 里另藏不受Session管理的长进程。

先用 [工具执行](workflows/tool-execution.md) 的场景确认归属、取消和提交边界，再写测试：取消发生在资源排队、审批通过后、系统创建期间和文件提交边界，都应有明确结果。事件写入失败时阻止新副作用，停止现存进程仍要完成。
