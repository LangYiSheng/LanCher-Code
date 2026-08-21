# 模块：TUI 界面

## 作用

TUI 层基于 [Textual](https://textual.textualize.io/) 构建，是用户唯一直接交互的界面。它**不直接调用模型或执行工具**，而是：

- 消费 `TurnRunner` 的事件流更新界面
- 渲染斜杠命令补全、权限确认弹窗、设置面板
- 管理 MCP 初始化门控与进度展示

实现位置：`lancher_code/tui_views/`。

## 目录结构

| 文件 | 内容 |
|---|---|
| `chat.py` | 主界面 `LanCherTextualApp` + 门面 `ChatTUI` |
| `composer.py` | 输入框 `ComposerTextArea`、斜杠补全菜单、消息事件定义 |
| `message.py` | `MessageWidget`（消息气泡）、`ThinkingTraceWidget`（思考轨迹）、`BannerWidget`（顶部横幅） |
| `permission.py` | `InlinePermissionPanel`（内联权限确认面板） |
| `settings.py` | `SettingsScreen`（设置面板，4 个标签页） |
| `bootstrap.py` | `ConfigBootstrapApp` / `ConfigBootstrapTUI`（首次配置引导） |
| `__init__.py` | 对外 re-export |

## 主界面（`chat.py`）

### 组件布局

```text
BannerWidget（横幅：cwd、MCP 状态、上下文用量）
VerticalScroll #chat-view（消息列表）
SlashCommandMenu（斜杠补全菜单）
ComposerTextArea（输入框）+ 提示符（模式字形）
CommandHintBar（命令提示）
状态栏：status-left（模型/模式） | status-center（Ready/Busy） | status-right（Token 用量）
```

### 关键流程

```text
ComposerSubmitted（Enter）
→ handle_input_submitted：
    · 解析斜杠命令（/plan /do /mode /session /compact /settings /exit）
    · 非命令 → TurnRunner.run_user_turn(text)（@work 后台任务）
→ _consume_turn_event：逐个消费 TurnEvent
    · user_message_created / assistant_message_started → 挂载 MessageWidget
    · 其他事件 → _sync_message_widget 增量更新
    · permission_request_created → 挂载 InlinePermissionPanel，await 用户决议
    · CONTEXT_REFRESH_EVENTS → 刷新上下文用量估算
→ 结束后恢复输入框、自动保存、滚动到底
```

### 消息渲染（`message.py`）

- `MessageWidget`：按角色着色（YOU 绿 / LANCHER 蓝 / SYSTEM 灰），错误/取消红色标记
- `ThinkingTraceWidget`：可折叠的思考轨迹（thinking / tool_call / tool_result / text / notice）
- `BannerWidget`：顶部横幅，含 ASCII Logo、MCP 初始化进度（spinner）、上下文用量百分比；首条消息后进入紧凑模式

### 权限面板（`permission.py`）

- `InlinePermissionPanel` 内联在输入框区域，挂起输入
- 选项由 `_option_specs()` 按请求类型生成（命令 4 项 / 文件编辑 2 项 / MCP 4 项）
- 键盘：`↑↓` 或 `Tab/Shift+Tab` 移动，`Enter` 确认，`Esc` 拒绝
- 决议经 `PermissionResolution` 返回给 `TurnRunner.resolve_permission_request()`

### 设置面板（`settings.py`）

- 四个标签页：模型设置 / MCP 服务器 / 项目权限 / 全局权限（左右键或点击切换）
- 数据来自 `SettingsService.load()`（快照）；保存走 `SettingsService.save()`（原子写盘 + 热切换权限）
- 有未保存修改时按 Esc/取消会弹出"放弃修改"确认
- 模型与 MCP 修改保存后提示重启生效

### 首次引导（`bootstrap.py`）

- 仅在 `~/.lancher/lancher.yaml` 不存在时出现
- 字段：protocol / model / base_url / api_key / 超时 / thinking
- 保存时调用 `write_config_data()` 写全局配置，并生成全局 `mcp.yaml` 模板
- 窄终端（宽度 < 48）自动切换按钮竖排布局

## 关键事件（Message 子类）

`composer.py` 定义了 TUI 内部消息：`ComposerSubmitted`、`SlashMenuNavigateRequested`、`SlashMenuAcceptRequested`、`SlashMenuDismissRequested`、`SlashCompletionChosen`、`PermissionModeCycleRequested`。`permission.py` 定义了 `PermissionOptionChosen` 与 `InlinePermissionPanel.Resolved`。

## 输入与输出

| 方向 | 说明 |
|---|---|
| 输入 | 键盘事件、`TurnEvent` 流、`PermissionResolution`、`MCPInitializationProgress` |
| 输出 | 用户文本、斜杠命令、权限决议、模式切换请求 |

## 与其他模块的关系

- → `turn_runner.py`：`run_user_turn()` / `resolve_permission_request()` / `cancel_active_turn()` / `set_mode()`
- → `session.py`：读写状态、会话管理（/session）
- → `settings_service.py`：设置数据
- → `mcp/manager.py`：初始化与进度回调
- → `slash_commands.py`：命令注册与补全

## 注意事项

- `ChatTUI` 是薄门面：构造 `LanCherTextualApp`，`run()` 调 `app.run_async()`；`configure_settings` / `configure_mcp` 是 app.py 装配时注入的后门方法。
- `Ctrl+C` 在回复中只取消当前回合，不退出；空闲时才退出（`action_request_quit`）。
- 输入框在 MCP 初始化完成前禁用，占位提示 "正在初始化 MCP，请稍候…"。
- TUI 测试（`tests/test_tui_*.py`）通过 Textual 的异步测试机制直接驱动 `LanCherTextualApp`。
