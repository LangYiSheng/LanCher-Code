# 术语表

## 通用术语

| 名称 | 含义 |
|---|---|
| Turn（回合） | 一次用户输入到模型给出最终回答的完整过程（可含多次工具循环） |
| Transcript | 协议无关的对话消息列表（发给模型的内容），区别于界面消息列表 |
| System prompt | 给模型的固定系统提示（角色、行为准则等） |
| Dynamic reminder | 随会话状态变化的 `<system-reminder>` 提醒（如 Plan Mode 状态） |
| Tool Loop（工具循环） | 模型请求 → 调用工具 → 结果回传 → 再次请求的循环，上限 `tool_loop_limit` |

## 模式与权限

| 名称 | 含义 |
|---|---|
| `RuntimeMode` | 权限模式：`default` / `plan` / `acceptEdits` / `bypass` |
| Plan Mode | 规划模式：只读探索 + 只允许写计划文件（`./.lancher/plan.md`） |
| `PermissionRule` | 权限规则：`{match, result}`，match 形如 `Bash(git *)` |
| Rule scope | 规则作用域：`session`（内存）/ `project`（`./.lancher/permissions.yaml`）/ `user`（`~/.lancher/permissions.yaml`） |
| PermissionResolution | 用户对权限请求的决议：`allow_once` / `allow_session` / `allow_project` / `deny` |
| Human-in-the-loop | 人在回路：规则与模式未放行时弹窗由用户决定 |

## 工具

| 名称 | 含义 |
|---|---|
| `ToolDefinition` | 暴露给模型的工具定义（名称/描述/JSON Schema/分类/模式） |
| `ToolContext` | 工具执行上下文（cwd、模式、项目根、超时、取消令牌、文件状态缓存） |
| `ToolExecutionResult` | 工具执行结果（content、is_error、error_code 等） |
| `FileStateCache` | 文件读写状态缓存，用于"先读后写"守卫 |
| 路径沙箱 | 文件类工具只能访问项目根内路径（解析符号链接后判定） |
| Deferred tool（延迟工具） | MCP 工具默认不随请求暴露，需 `tool_search` 加载 |
| `discovered_tool_names` | `tool_search` 返回的已发现工具名，下一轮请求携带其完整定义 |

## 会话与存储

| 名称 | 含义 |
|---|---|
| JSONL | 每行一个 JSON 对象的文本格式，会话文件使用（`.lancher/session/*.jsonl`） |
| 会话格式版本 | 当前 `SESSION_FORMAT_VERSION = 3`，兼容读取 v1/v2 |
| `ContextUsageAnchor` | 用量锚点：上次请求快照，用于增量 token 估算 |
| Tool result offload（结果卸载） | 大工具结果从请求中移出、落盘到 `.lancher/context/<context_id>/tool-results/`，上下文里只留预览 |
| Context compaction（压缩） | 把旧轮次交给模型生成 `<summary>` 摘要，替换原始消息 |
| `automatic_threshold` | 自动压缩触发阈值 = `context_window - 20000 - 13000` |

## 模型与协议

| 名称 | 含义 |
|---|---|
| `ChatProvider` | 模型供应商抽象接口（`stream_chat()` 返回 `StreamEvent` 流） |
| `StreamEvent` | 统一流事件：`text_delta` / `thinking_delta` / `tool_call_delta` / `message_end` 等 |
| `ToolCallAssembler` | 把流式工具调用分片（名称/参数 JSON）拼接成完整 `ToolCall` |
| thinking | Claude 的扩展思考模式（`budget_tokens`） |
| SSE | Server-Sent Events，流式响应解析格式 |

## MCP

| 名称 | 含义 |
|---|---|
| MCP | Model Context Protocol：标准化的外部工具接入协议 |
| Server config | `mcp.yaml` 中的服务器配置（`stdio` 或 `http` 类型） |
| `MCPToolAdapter` | 远程工具到本地 `Tool` 的适配器，可见名 `mcp__<server>__<tool>` |
| `MCPConfigIssue` | MCP 配置/初始化问题记录（不阻断其他 Server） |

## 配置

| 名称 | 含义 |
|---|---|
| 全局配置 | `~/.lancher/lancher.yaml`（主配置） |
| 项目级数据 | `./.lancher/`（权限、MCP、plan、会话、context） |
| legacy 配置路径 | `cwd/lancher.yaml`（`get_legacy_config_path`，预留，当前未启用） |

## 代码结构

| 名称 | 含义 |
|---|---|
| `app.py run_app()` | 应用装配入口 |
| `ConfigBootstrapTUI` | 首次启动配置引导界面 |
| `TurnEvent` | TurnRunner → TUI 的事件（`user_message_created`、`tool_result_received` 等） |
| `SlashCommand` | 斜杠命令（`/plan`、`/do`、`/mode`、`/session`、`/compact`、`/settings`、`/exit`） |
