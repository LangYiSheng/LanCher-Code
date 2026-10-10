# 模块：会话层

`SessionController` 管理当前对话的界面消息、协议无关 transcript、阶段、审批策略、计划、队列、模型引用和上下文治理状态。持久化职责由 `lancher_code/sessions/` 承担，对话身份使用稳定 UUID，标题可改名并允许重复。

## SessionController

启动时 Controller 表示空白草稿，`session_id`、`session_title` 和 `paths` 均为 `None`。首条用户消息创建 UUID、独立目录和创建事件，再开始模型请求；查看列表或切换阶段不创建会话。

| 属性 / 方法 | 作用 |
|---|---|
| `state` / `transcript` | 界面状态与发给模型的协议无关上下文 |
| `session_id` / `session_title` / `paths` | 当前 UUID、显示标题和 `SessionPaths` |
| `work_phase` / `permission_policy` | 独立的工作阶段与审批策略 |
| `plan_snapshot` / `set_plan_snapshot(...)` | 当前会话的计划正文、摘要、来源和就绪状态 |
| `plan_file_path` | 当前 Session 的 `workspace/plan.md`；草稿尚无路径 |
| `pending_inputs` / `update_pending_inputs(...)` | 保存待处理消息与暂停状态 |
| `create_user_message(text)` | 首条消息建立会话，再创建用户消息和动态提醒 |
| `create_assistant_message()` / `append_message_content(...)` | 助手消息与流式文本 |
| `append_trace_*()` | 思考、文本、通知、工具调用及结果记录 |
| `append_assistant_tool_calls()` / `append_tool_results()` | 写入模型 transcript |
| `complete_message()` / `fail_message()` / `cancel_message()` | 消息终止与持久化 |
| `build_request(...)` / `estimate_request_tokens(...)` | 组装请求与上下文估算 |
| `offload_large_tool_results()` / `compact_context()` | 工具结果卸载与上下文压缩 |
| `list_sessions()` | 只读列出项目会话，返回 `SessionInfo` |
| `new_session()` | 刷新旧会话并重置草稿；有后台资源时保留原Runtime及写入者 |
| `resume_session(session_id, resolved_model=...)` | 恢复指定 UUID，返回恢复的会话权限条数 |
| `rename_session(session_id, title)` | 只修改标题 |
| `archive_session(session_id)` / `remove_session(session_id)` | 归档或删除非当前会话 |
| `read_session_model_ref(session_id)` | 只读取得目标会话模型引用 |
| `flush()` / `close()` | 刷新事件；退出时写快照并释放锁 |

旧 `active_session_name`、`save_session`、`auto_save`、`list_saved_sessions` 和切换 `force` 接口已移除。

## 存储分层

| 模块 | 职责 |
|---|---|
| `sessions/paths.py` | 规范 UUID hex、独立目录、符号链接与 junction 校验 |
| `sessions/repository.py` | `ProjectSessionRepository`、JSONL 事件、摘要与快照、文件锁、归档与删除 |
| `sessions/codec.py` | 将日志投影为会话状态，编解码消息、上下文与权限规则 |
| `sessions/service.py` | 协调会话创建、恢复、增量持久化与写入者切换 |

`events.jsonl` 使用新事件版本 `1`，是持久化事实来源；`meta.json` 和 `checkpoint.json` 是可重建摘要及状态快照。旧 `.lancher/session/*.jsonl` v1–v4 文件不读取、不迁移。具体布局和生命周期见 [session-lifecycle.md](../workflows/session-lifecycle.md)。

## 模型上下文与恢复

界面消息和模型 transcript 分开维护。失败、取消与中断可显示在界面，恢复时缺少工具结果的调用会补齐未知结果，工具不会自动重放。待处理输入恢复为暂停。上下文压缩记录新的上下文投影，同时保留原始事件历史。

动态提醒在请求组装时使用当前阶段、权限、计划和 Session 工作目录，避免恢复后的旧提醒继续生效。计划确认绑定当前 Session ID 和计划摘要；恢复对话不会回滚源码文件。

新建和恢复由 `TurnRunner` 协调有效模型。会话只保存稳定模型引用，不保存连接密钥；恢复找不到原模型时按当前可用默认模型处理并显示提示。模型切换清除旧模型的上下文用量锚点，保留对话与卸载引用。

当前 `workspace/` 在全部阶段允许内置文件工具读写，日志和控制文件不在此批准范围；源码、Shell 与外部 MCP 继续遵守阶段和权限判定。这不构成操作系统沙箱。


## 执行投影与后台写入

SessionState.execution保存调用、进程和完成收件箱的投影。后台事件通过ExecutionRuntime绑定的原SessionService写入，切换界面不改变归属，也不创建第二个写入者。启动事实先提交再创建进程；退出后记录状态并生成完成通知。

离开对话后，只在仍有后台或已提交控制操作时持有原写入者。最后一个进程和控制操作结束后保存快照并释放非当前 Session；以后从磁盘恢复。当前界面所属 Session 始终保留写入者。

下一条用户消息最多附带20条结构化完成摘要，先保存协议消息再确认收件箱，避免通知丢失；它不是新指令，也不会自动唤醒模型。应用重启把未结束进程标为lost、调用标为interrupted，不接管旧PID或重复命令。详见 [工具执行](../workflows/tool-execution.md)。
