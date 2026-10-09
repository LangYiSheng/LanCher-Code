# 模块：权限引擎（Permission Engine）

权限引擎决定工具调用是允许、拒绝还是需要确认。它将工作阶段硬限制、路径边界、危险命令黑名单、分层规则和权限策略组合成一条判定链。

实现位置：`lancher_code/permission_engine.py`；阶段能力公共判定在 `models.py` 的 `tool_available_in_phase()`，由工具注册表、执行器及权限引擎共同使用。

## 公共类型

| 类型 | 值 / 用途 |
|---|---|
| `WorkPhase` | `discuss`、`plan`、`execute` |
| `PermissionPolicy` | `default`、`acceptEdits`、`bypass` |
| `PermissionMatchKind` | `exact`、`glob`、`legacy` |
| `PermissionCheck` | `decision: allow / deny / ask`、原因和可选确认请求 |

`ToolContext` 携带 `work_phase`、`permission_policy`、`cwd`、`project_root`、`plan_file_path`。旧 `mode` / `RuntimeMode` 只用于调用边界兼容；新逻辑使用独立两轴。

新会话默认 `execute + default`。v1–v3 会话的 `default` / `acceptEdits` / `bypass` 映射为执行阶段和对应策略；旧 `plan` 映射为计划阶段，并保留有效的旧 `plan_restore_mode` 策略，否则采用 `default`。

## 核心类

`PermissionStorage` 管理三个作用域：

| 作用域 | 位置 | 生命周期 |
|---|---|---|
| `session` | 内存，由 `SessionController` 订阅变更 | 随命名会话保存和恢复 |
| `project` | `./.lancher/permissions.yaml` | 项目内持久化 |
| `user` | `~/.lancher/permissions.yaml` | 用户级持久化 |

主要方法为 `rules_for_scope()`、`replace_rules()`、`add_session_rule()`、`replace_session_rules()`、`add_project_rule()` 和 `subscribe_session_rules_changed()`。读写规则均保留 `match_kind`。

`PermissionEngine.evaluate(call, tool, context)` 返回判定。`apply_resolution(request, resolution)` 只负责落实 `allow_session` / `allow_project` 的规则；执行器必须先检查取消、任务补充和审批撤销，不能先保存规则再决定是否执行。

## 判定顺序

1. **工作阶段硬限制。** 讨论和计划允许原生只读调查工具、已配置且显式声明 `readOnlyHint=true` 的 MCP；计划额外允许专用 `write_plan_file`。两阶段均禁止通用 Shell 和普通文件写入。未声明只读的 MCP 也禁止。违规返回 `phase_disallowed`，任何允许规则及 `bypass` 均不能放行。
2. **匹配目标与路径边界。** 路径解析后必须位于项目根内，否则返回 `path_outside_project`。Shell 同时保留旧匹配所需的规范化文本和新精确规则所需的原始完整命令。
3. **危险命令黑名单。** Shell 命中 `COMMAND_BLACKLIST_PATTERNS` 时返回 `permission_blacklist_denied`，规则和 `bypass` 无法覆盖。
4. **分层规则。** 按 session → project → user 顺序，采用第一个存在命中规则的作用域；同一作用域最后一条命中规则生效，结果为 allow 或 deny。
5. **权限策略。** `bypass` 允许；读工具允许；非只读外部工具需要确认；`acceptEdits` 允许原生写工具；其他调用需要确认。
6. **人在回路。** 生成 `PermissionRequest`，由聊天内联权限面板收集决议。

MCP 只读能力依赖服务端 `readOnlyHint` 声明，并不等同于本地执行沙箱验证。`write_plan_file` 在 `default` 下需要确认，在 `acceptEdits` / `bypass` 下可直接写入；它只能写预设计划路径，且拒绝空白正文。

`validate_plan_command()` 仅保留兼容入口，始终拒绝通用 Shell。不存在计划阶段的 Shell 前缀白名单放行路径。

## 规则格式与精确授权

```yaml
rules:
  - match: 'Bash(git status --short)'
    match_kind: exact
    result: allow
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

新权限确认默认 `exact`：Shell 授权完整命令，保留命令内部空白及大小写；文件授权精确路径，仍使用现有的正斜杠、小写路径规范化。`*`、`?`、`[` 在精确规则中按普通字符匹配。MCP 精确规则匹配整个可见工具名，仍允许该工具的不同参数组合。

`glob` 只用于明确配置的通配匹配。缺失 `match_kind` 的旧规则按 `legacy` 读取，保留原行为；修改或保存旧规则时不能意外丢失已有的 `exact` 字段。不会从 `git status` 自动生成 `Bash(git *)` 一类宽授权。

标签映射为 `Bash`、`ReadFile`、`WriteFile`、`EditFile`、`Glob`、`Grep`、`WritePlanFile`。MCP 使用 `mcp__<server>__<tool>` 可见名。

## 权限请求与决议

`PermissionRequest` 包含工具、标题、命令或差异预览、建议规则、`match_kind`、`work_phase` 和 `permission_policy`。用户有四种选择：

| outcome | 效果 |
|---|---|
| `allow_once` | 仅本次放行 |
| `allow_session` | 保存本会话的精确允许规则 |
| `allow_project` | 保存项目级精确允许规则 |
| `deny` | 返回 `permission_user_denied` |

内部另有 `superseded`：用户补充当前任务时撤销等待中的审批，工具返回 `steering_superseded`，不保存允许规则。即使补充随后被删除或改成排队，已撤销的审批也不能再执行。它不是 UI 按钮，不应显示为用户拒绝。

`ToolExecutor.execute_calls(..., should_interrupt=...)` 在每组开始前与审批返回后检查任务补充。已启动的并行组等待结果；后续未启动调用全部记录为跳过。`TurnRunner` 最终发出 `permission_request_closed`，迟到的面板决议不生效。

## 与其他模块的关系

- `tools/core/registry.py`：按阶段过滤普通工具与延迟发现索引。
- `tools/core/executor.py`：每个调用前判定，处理确认、补充撤销和结果配对。
- `app.py`：构造项目和用户权限存储。
- `tui_views/chat.py`、`permission.py`：内联权限面板与决议回调。
- `tools/core/common.py`：路径解析与项目根边界。
- `SessionController`：保存会话规则、阶段、策略和暂停队列；恢复中断任务时补未知工具结果，不自动重执行。

相关测试：`tests/test_permission_engine.py`、`tests/test_work_phase_core.py`、`tests/test_task_interaction.py`、`tests/test_task_safety_regressions.py`、`tests/mcp/test_permission.py`。
