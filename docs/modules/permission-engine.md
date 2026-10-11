# 模块：权限引擎（Permission Engine）

权限引擎决定工具调用是允许、拒绝还是需要确认。它将工作阶段硬限制、路径边界、危险命令黑名单、分层规则和权限策略组合成一条判定链。

判定入口在 `lancher_code/permissions/engine.py`，规则存储、匹配与差异预览分别位于 `storage.py`、`rules.py`、`preview.py`；路径检查复用 `filesystem/access.py`。阶段能力公共判定在 `contracts/tools.py` 的 `tool_available_in_phase()`，由工具注册表、执行器及权限引擎共同使用。

## 公共类型

| 类型 | 值 / 用途 |
|---|---|
| `WorkPhase` | `discuss`、`plan`、`execute` |
| `PermissionPolicy` | `default`、`acceptEdits`、`bypass` |
| `PermissionMatchKind` | `exact`、`glob` |
| `PermissionCheck` | `decision: allow / deny / ask`、原因和可选确认请求 |

`ToolContext` 携带 `work_phase`、`permission_policy`、`cwd`、`project_root`、当前 `session_workspace` 和 `plan_file_path`。所有调用都必须使用独立两轴，不接受 `mode` / `RuntimeMode`。

新会话默认 `execute + default`。Session 使用 UUID 事件格式 2；旧格式保留原文件，拒绝恢复并提示新建。

## 核心类

`PermissionStorage` 管理三个作用域：

| 作用域 | 位置 | 生命周期 |
|---|---|---|
| `session` | 内存，由 `SessionController` 订阅变更 | 随 UUID Session 自动持久化和恢复 |
| `project` | `./.lancher/permissions.yaml` | 项目内持久化 |
| `user` | `~/.lancher/permissions.yaml` | 用户级持久化 |

主要方法为 `rules_for_scope()`、`replace_rules()`、`add_session_rule()`、`replace_session_rules()`、`add_project_rule()` 和 `subscribe_session_rules_changed()`。读写规则均保留 `match_kind`，以及可选的 `command_cwd` / `command_shell` 上下文。

`PermissionEngine.evaluate(call, tool, context)` 返回判定。`apply_resolution(request, resolution)` 只负责落实 `allow_session` / `allow_project` 的规则；执行器保留审批选择，成功执行后才保存。目标变更、失败、取消或补充撤销不会留下新允许规则。

## 判定顺序

1. **工作阶段硬限制。** 讨论和计划允许原生只读调查工具、已配置且显式声明 `readOnlyHint=true` 的 MCP；当前 Session 的 `workspace/` 在所有阶段允许文件读写，计划额外允许专用 `write_plan_file`。两阶段均禁止通用 Shell 和普通源码写入。未声明只读的 MCP 也禁止。违规返回 `phase_disallowed`，任何允许规则及 `bypass` 均不能放行。
2. **匹配目标与路径边界。** 路径解析后必须位于项目根内，否则返回 `path_outside_project`。Shell AST 检查独立 cwd、正文中能确定的文件目标、目录变化和重定向；动态或未识别行为需要确认，不能证明全部子进程访问都在项目内。
3. **系统高风险操作。** 按 AST 命令名称及参数识别磁盘格式化、关机、提权与部分破坏性 Git 操作，返回 `permission_blacklist_denied`。普通删除和重定向不再因关键字被永久拒绝，引号里的 format 或 > 不是对应操作。
4. **分层规则。** 按 session → project → user 顺序，采用第一个存在命中规则的作用域；同一作用域最后一条命中规则生效，结果为 allow 或 deny。
5. **权限策略。** `bypass` 允许已完成静态分析的普通调用；读工具允许；非只读外部工具需要确认；`acceptEdits` 允许原生写工具（含普通单文件删除）；目录删除、批量 Shell 删除、动态或无法完整分析的命令始终仅本次确认。
6. **人在回路。** 生成 `PermissionRequest`，由聊天内联权限面板收集决议。

当前 Session 的 `workspace/` 路径批准不覆盖其他会话目录或日志、快照、摘要、blobs 等控制内容；内置文件工具继续检查真实路径、读取状态与显式拒绝规则。会话控制路径拒绝符号链接与 Windows junction，现有普通控制文件拒绝多个硬链接；`write_file`、`edit_file` 和 `write_plan_file` 也拒绝写入现有的多硬链接普通文件，返回 `hardlinked_file_protected`，`bypass` 不能覆盖。`write_plan_file` 写入当前会话的 `workspace/plan.md`，拒绝空白正文。

路径批准属于内置工具的应用层校验，**不是操作系统沙箱**。MCP 只读能力依赖服务端 `readOnlyHint` 声明，Shell 与外部 MCP 不会因 Session 目录存在而得到系统级隔离。

讨论与计划阶段始终拒绝通用 Shell，不提供 Shell 前缀白名单入口。

## 规则格式与精确授权

```yaml
rules:
  - match: 'RunCommand(git status --short)'
    match_kind: exact
    result: allow
    command_cwd: 'D:/projects/demo'
    command_shell: powershell
  - match: 'WriteFile(src/[draft].py)'
    match_kind: exact
    result: allow
  - match: 'WriteFile(.env)'
    match_kind: exact
    result: deny
  - match: 'mcp__github__get_issue'
    match_kind: exact
    result: allow
  - match: 'ReadFile(docs/*)'
    match_kind: glob
    result: allow
```

新权限确认默认 `exact`：Shell 授权完整命令，保留命令内部空白及大小写，同时保存绝对 `command_cwd` 和实际 `command_shell`。更换 cwd 或解释器不复用批准，Session 与项目规则读写均保留上下文。没有上下文的旧 Shell exact allow 记录保留但不再自动放行，需要重新批准；旧拒绝规则继续有效。文件授权精确路径，仍使用现有路径规范化；DeleteFile 授权删除链接本体的项目相对路径。`*`、`?`、`[` 在精确规则中按普通字符匹配。MCP 精确规则匹配整个可见工具名，仍允许该工具的不同参数组合。

人工配置的 Shell glob 规则必须逐个 AST 子命令匹配，不以整段前缀放行后续命令；PowerShell 常见别名仅规范化命令名称，参数原文不改写。命中作用域继续遵守 session → project → user。静态分析不完整时即使命中允许规则仍本次确认，且不生成永久规则。

`glob` 只用于明确配置的通配匹配。缺失 `match_kind` 或填写 `legacy` 的规则会被拒绝，提示重新配置；读取失败不改写原文件。不会从 `git status` 自动生成 `RunCommand(git *)` 一类宽授权。

标签映射为 `RunCommand`、`ReadFile`、`WriteFile`、`EditFile`、`DeleteFile`、`Glob`、`Grep`、`WritePlanFile`。MCP 使用 `mcp__<server>__<tool>` 可见名。

## 权限请求与决议

`PermissionRequest` 包含工具、标题、命令或差异预览、建议规则、`match_kind`、`work_phase` 和 `permission_policy`。用户有四种选择：

| outcome | 效果 |
|---|---|
| `allow_once` | 仅本次放行 |
| `allow_session` | 保存本会话的精确允许规则 |
| `allow_project` | 保存项目级精确允许规则 |
| `deny` | 返回 `permission_user_denied` |

内部另有 `superseded`：用户补充当前任务时撤销等待中的审批，工具返回 `steering_superseded`，不保存允许规则。即使补充随后被删除或改成排队，已撤销的审批也不能再执行。它不是 UI 按钮，不应显示为用户拒绝。

`process_write` 和 `process_background` 只提供本次允许与拒绝。向交互 Shell 输入内容可能执行任意新操作，不能把一次输入批准保存成整个 Session 或项目的永久放行；显式拒绝规则和命令黑名单仍生效。启动 `run_command` 的确认展示 cwd、归属、Pipe / PTY、有限等待和运行期限，让用户看清要启动的实际工作。

JSON Schema 校验在权限询问和资源申请之前。审批不占资源锁；取得资源和运行通知后重新检查取消令牌、generation、阶段、规则与真实目标，执行成功后落实保存选择。停止后的迟到批准不会保存允许规则或开始执行。审批签名包含命令、cwd、Shell、路径分析及删除清单版本。AST 解析和清单扫描使用工作线程，不阻塞 TUI 响应停止。

`ToolExecutor.execute_calls(..., should_interrupt=...)` 在每组开始前与审批返回后检查任务补充。已启动的并行组等待结果；后续未启动调用全部记录为跳过。`TurnRunner` 最终发出 `permission_request_closed`，迟到的面板决议不生效。

## 与其他模块的关系

- `tools/core/registry.py`：提供阶段过滤的搜索接口；请求通过核心保留已发布定义，实际执行继续受阶段校验。
- `tools/core/executor.py`：每个调用前判定，处理确认、补充撤销和结果配对。
- `app.py`：构造项目和用户权限存储。
- `tui/app.py`、`permission.py`：内联权限面板与决议回调。
- `tools/core/common.py`：路径解析与项目根边界。
- `SessionController`：保存会话规则、阶段、策略和暂停队列；恢复中断任务时补未知工具结果，不自动重执行。

相关测试：`tests/permissions/test_permission_engine.py`、`tests/agent/test_work_phase_core.py`、`tests/agent/test_task_interaction.py`、`tests/agent/test_task_safety_regressions.py`、`tests/mcp/test_permission.py`。
