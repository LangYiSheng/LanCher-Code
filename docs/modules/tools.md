# 模块：工具与托管执行

工具层把模型请求变成可审计的实际操作：先确认身份与参数，检查权限，再取得资源，执行后返回一个结果。长进程由托管层继续管理，启动调用无需一直等待它退出。

第一次阅读建议先看 [工具执行故事与实现细节](../workflows/tool-execution.md)。这页用于查接口和继续开发。

## 从请求到执行

```text
模型 ToolCall
  → ToolExecutor 冻结参数，创建 InvocationInfo，校验 JSON Schema
  → 阶段、可见工具、PermissionEngine
  → 资源队列与权限复核
  → 短工具执行 / ProcessSupervisor 启动
  → 结构化 ToolExecutionResult
```

工具契约定义在 `contracts/tools.py`：`ToolDefinition` 使用 `input_schema` 与 `allowed_phases`（默认仅 execute），`ToolExecutionResult` 使用 `content`、`is_error`、`metadata`，不提供旧 `ok` / `payload` 别名。执行所需的阶段、权限和会话路径由 `tools/context.py` 的 `ToolContext` 提供。

实际完成事件立即更新 TUI；一批结果按请求顺序交给模型。资源相互冲突的同批调用先排序，独立调用可以并行。`is_concurrency_safe` 已移除，未知工具默认在本次调用期间项目独占，不用类别猜副作用；未知Shell返回后台任务后释放该调用期锁。

工具可以实现 `resource_claims(arguments, context)`，返回可信的 `ResourceClaim` 列表；模型参数不能覆盖这个声明。每项资源可设置 `lifetime=invocation/process`：调用期资源随工具返回释放，持续资源由真实进程保留到退出；没有长进程的工具在调用结束释放全部资源。命令profile的资源默认process，未知命令的默认项目锁则明确invocation，允许服务器返回后台身份后继续执行请求和文件工具，不证明后台没有文件副作用。没有声明的 MCP，即使提供 `readOnlyHint`，也在本次调用期间保守使用项目独占；该提示影响工作阶段可见性，不足以证明并发独立。

执行器通过 `on_invocation_state(call, info)` 在状态持久化成功后通知Runner；`waiting` 保存实际冲突、并发额度或FIFO原因与阻塞者。TUI区分已批准的资源等待和未审批的前序等待，`on_call_started` 标记进入执行阶段；开始执行或进入终态清除旧等待信息。运行通知仍可能在真正调用 `execute` 前被取消，Runner按trace绑定的稳定invocation_id查询终态，区分未派发和已进入远端工具的未知结果。

权限检查位于资源申请之前，避免等待用户批准时长期占锁。得到租约后再次校验阶段、规则、审批内容和 generation；文件预览或目标在等待期间变了，不能套用旧批准继续写。

参数使用标准 JSON Schema 校验，支持局部引用、组合和条件；外部引用只在离线注册表解析，不读取网络或外部文件。无效参数或 Schema 在审批、资源占用和实际执行之前失败。

## 文件与查询工具

| 工具 | 实现 | 行为 |
|---|---|---|
| `read_file` | `builtin/read_file.py` | 按行分页读取，记录完整读取与文件版本 |
| `write_file` | `builtin/write_file.py` | 覆盖前验证已完整读过且版本未变；临时文件后原子提交 |
| `edit_file` | `builtin/edit_file.py` | 唯一文本替换；读取版本守卫与原子提交 |
| `glob` | `builtin/glob.py` | 查找文件，默认跳过 `.lancher` 等内部目录 |
| `grep` | `builtin/grep.py` | 正则搜索，有模型和界面输出预算 |
| `write_plan_file` | `builtin/write_plan_file.py` | 计划阶段写本 Session `workspace/plan.md`，更新计划快照 |
| `tool_search` | `builtin/tool_search.py` | 查找并加载延迟 MCP 工具，下一次模型请求才使用 |
| `load_skill` | `builtin/skills.py` | 加载已登记技能，正文经回调交给核心系统投影；工具结果只保留确认 |
| `read_skill_resource` | `builtin/skills.py` | 按已登记技能的相对目录读取 UTF-8 资料或源码，按行分页、不执行脚本 |

当前 Session workspace 文件操作在各阶段自动批准，显式拒绝优先；源码修改仅执行阶段允许。控制记录和多硬链接文件受保护，bypass 无法放行。文件权限是应用层路径约束，不能据此声称 Shell 获得系统沙箱。

技能工具由 `AgentCapabilities` 注册，均为各阶段可用的只读系统工具；只声明精确读取资源，不修改普通文件读取的项目边界。技能指令和脚本仍遵守原有阶段与审批。目录、资源预算和正文生命周期见 [Skills 与项目约定](skills.md)。

FileStateCache 按 Session 分开，恢复旧会话时不会借用另一对话的「已读文件」证据。调度锁覆盖同一应用内的 Session；其他程序仍可能写文件，所以版本检查不省略。

## 命令与进程工具

| 工具 | 主要参数 | 返回 / 作用 |
|---|---|---|
| `run_command` | `command`、`description`、可选 `cwd`、`transport`、`lifetime`、`yield_ms`、`max_runtime_ms` | 很快完成就给退出结果，否则给 `running` 和 `process_id` |
| `process_list` | 无 | 当前 Session 的进程及结束记录 |
| `process_read` | `process_id`、`cursor`、`max_chars` | 输出分页与 `next_cursor`；读取不消费日志 |
| `process_wait` | `process_id`、`timeout_ms` | 有限等待；等不到时进程继续运行 |
| `process_write` | `process_id`、`text` | 精确写 stdin，换行由 text 明确提供 |
| `process_stop` | `process_id` | 停止目标及托管子进程，日志保留 |
| `process_background` | `process_id` | 本轮进程转交 Session 后台 |

启动工具在 `builtin/command.py`，管理工具在 `builtin/process.py`。旧 `bash` 名称与旧命令路径已删除，不额外维持一条兼容执行路径。命令非零退出返回 `non_zero_exit`，不根据命令名字猜测失败是否正常。

进程控制工具只接受所属 Session 的 UUID。列表、日志、等待和停止可以出现在各阶段；启动、输入、转后台遵守执行阶段和权限判断。已知内置的 stop/read/list/background 使用不占普通并发额度的管理通道，资源锁仍有效；wait/write 受普通额度约束。TUI 的实际输入和按钮是用户直接操作，经 runner 的有归属检查的 facade 控制；模型工具调用仍经过权限层。

## 执行层目录

| 文件 | 阅读时关注什么 |
|---|---|
| `execution/contracts.py` | 调用、进程、资源、配置、输出页的数据契约 |
| `execution/scheduler.py` | 冲突 FIFO、原子多资源授予、租约转交、跨 Session 项目调度器 |
| `execution/runtime.py` | Session 写入者绑定、generation、调用事件、后台收件箱与恢复 |
| `execution/processes.py` | 启动取消竞态、停止幂等、进程监控、期限和输出失败止损 |
| `execution/output.py` | UTF-8 增量解码、字符游标、独立日志、稀疏索引与磁盘额度 |
| `execution/backends.py` | 平台统一 Pipe / PTY 接口与 POSIX 进程组 |
| `execution/windows.py` | 挂起创建、Job Object、ConPTY、句柄继承和关闭 |
| `tools/core/executor.py` | 权限、可见性、资源请求、结果排序、取消和基础设施失败 |
| `tools/core/validation.py` | 标准 JSON Schema、离线引用解析与执行前参数校验 |

## 新增工具时需要回答的问题

1. 它读写哪些资源？共享还是独占，是否覆盖子目录？需要只保留到调用结束，还是由真实进程持续持有？不能确认时维持本次调用期间默认项目独占。
2. 它何时形成不可撤销副作用？提交前如何复核参数、权限与版本？
3. 用户取消后能证明它没有执行吗？远端结果未知时要明确返回，不能自动重试。
4. 它会产生真实长进程吗？使用现有 ProcessSupervisor，不在工具里自行藏一个 subprocess。
5. 返回内容是否有预算，必要的审计事件是否在副作用前提交？

将工具注册到 `tools/__init__.py`，配置权限标签并覆盖真实边界测试。详细开发步骤见 [开发指南](../development.md)。
