# 术语表

## 通用术语

| 名称 | 含义 |
|---|---|
| Turn（回合） | 一次用户输入到模型给出最终回答的完整过程（可含多次工具循环） |
| Transcript | 协议无关的对话消息列表（发给模型的内容），区别于界面消息列表 |
| System prompt | 给模型的固定系统提示（角色、行为准则等） |
| Dynamic reminder | 随会话状态变化的 `<system-reminder>` 提醒（如 Plan Mode 状态） |
| Tool Loop（工具循环） | 模型请求 → 调用工具 → 结果回传 → 再次请求的循环，上限 `tool_loop_limit` |

## 阶段与权限

| 名称 | 含义 |
|---|---|
| `WorkPhase` | 工作阶段：`discuss`（讨论）/ `plan`（计划）/ `execute`（执行），限定工具范围 |
| `PermissionPolicy` | 权限策略：`default`（逐次确认）/ `acceptEdits`（自动编辑）/ `bypass`（跳过询问），不能突破阶段边界 |
| `RuntimeMode` | 旧接口兼容类型；新状态分别保存工作阶段和权限策略 |
| Plan Mode | 计划阶段：只读探索 + 专用计划写入，权限策略保持不变 |
| `PermissionRule` | 权限规则：`{match, result, match_kind}`，match 形如 `RunCommand(git *)` |
| Invocation | 一次工具请求的应用身份与状态；保留模型的调用 ID，和真实进程生命周期分开 |
| Process | 所属 Session 托管的真实进程，以应用 UUID 标识；PID 仅作诊断信息 |
| SessionRuntime | 绑定原对话状态、写入者与后台资源；界面切换后仍能保存原会话事件 |
| ResourceClaim / Lease | 工具的资源需求及实际授予租约；invocation资源在调用结束释放，process资源由真实进程保留到退出 |
| generation | Session 执行代次；停止后拒绝旧审批、旧执行请求和迟到回调 |
| 输出游标 | 累计 UTF-8 解码字符位置；模型与界面各自续读，不消费另一观察者的数据 |
| 收件箱 | 后台完成事件在 Session 的投影；下次正常请求才交给模型，不自动开轮 |
| `match_kind` | `exact` 精确匹配、`glob` 显式通配、`legacy` 旧规则兼容；新授权默认精确匹配 |
| Rule scope | 规则作用域：`session`（随 Session 持久化）/ `project`（`./.lancher/permissions.yaml`）/ `user`（`~/.lancher/permissions.yaml`） |
| PermissionResolution | 用户对权限请求的决议：`allow_once` / `allow_session` / `allow_project` / `deny` |
| Human-in-the-loop | 人在回路：阶段允许且规则与权限策略未放行时，由界面中的权限提示请用户决定 |

## 工具

| 名称 | 含义 |
|---|---|
| `ToolDefinition` | 暴露给模型的工具定义（名称/描述/JSON Schema/分类/工具可用性） |
| `ToolContext` | 工具执行上下文（cwd、阶段、权限策略、项目根、超时、取消令牌、文件状态缓存） |
| `ToolExecutionResult` | 工具执行结果（content、is_error、error_code 等） |
| `FileStateCache` | 文件读写状态缓存，用于"先读后写"守卫 |
| 路径沙箱 | 文件类工具只能访问项目根内路径（解析符号链接后判定） |
| Deferred tool（延迟工具） | MCP 工具默认不随请求暴露，需 `tool_search` 加载 |
| `discovered_tool_names` | `tool_search` 返回的已发现工具名，下一轮请求携带其完整定义 |

## 会话与存储

| 名称 | 含义 |
|---|---|
| JSONL | 每行一个 JSON 对象的文本格式，Session 事件使用（`.lancher/sessions/<UUID>/events.jsonl`） |
| 会话格式版本 | 新 `EVENT_FORMAT_VERSION = 1`，使用 UUID 事件日志；不读取、不迁移旧命名会话 v1–v4 |
| `PlanSnapshot` | 绑定当前会话的计划正文、内容摘要、来源消息与就绪标记；执行确认的来源 |
| `PendingInput` | 工作中投递的输入：`follow_up` 排到下一轮或 `steer` 补充当前任务；恢复会话后均暂停 |
| `ContextUsageAnchor` | 可靠输入 usage 与请求发出前的匹配快照边界，用于只估新增内容 |
| `MessageUsage` | 提供方用量快照，`None` 未知、`0` 真实零，保存部分字段与最终确认状态 |
| `RequestUsageRecord` | 一次实际模型请求的身份、归属、用途、用量快照和结束状态 |
| `RunUsageSummary` | 同一聚合口径的已上报用量小计，附请求完整度与异常数量 |
| `TokenEstimate` | 当前请求的估算数字、来源与内容分类，不用于填补消耗账本 |
| `ContextBudget` | 模型窗口内的实际输出、输入和工具结果等动态额度 |
| Tool result offload（结果卸载） | 大工具原文落盘到 Session `blobs/`，请求投影只留有预算的预览与读取路径 |
| Context compaction（压缩） | 把旧轮次摘要为候选，验证有效且变小后替换模型上下文，原始事件仍保留 |
| `automatic_threshold` | 从实际输入额度继续预留整理余量得到的动态阈值 |

## 模型与协议

| 名称 | 含义 |
|---|---|
| 模型引用 | 稳定的 `provider_id/model_id`；名称修改不改变引用 |
| 本次对话模型 | 当前会话实际使用的模型，切换后用于下一次请求 |
| 新对话默认模型 | 新建对话时选用的模型，修改它不会切换本次对话 |
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
| 项目级数据 | `./.lancher/`（权限、MCP、sessions；每个 Session 有独立 workspace 与 blobs） |
| legacy 配置路径 | `cwd/lancher.yaml`（`get_legacy_config_path`，预留，当前未启用） |

## 代码结构

| 名称 | 含义 |
|---|---|
| `app.py run_app()` | 应用装配入口 |
| `ConfigBootstrapTUI` | 连接供应商、添加模型、确认并开始的三步首次配置界面 |
| `TurnEvent` | TurnRunner → TUI 的事件（`user_message_created`、`tool_result_received` 等） |
| `SlashCommand` | 斜杠命令（`/discuss`、`/plan`、`/do`、`/model`、`/session`、`/compact`、`/settings`、`/exit`） |
