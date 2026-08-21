# 模块：权限引擎（Permission Engine）

## 作用

权限引擎是 LanCher Code 的安全核心。它决定**每个工具调用是否可以执行**，并把"危险命令黑名单、路径沙箱、三层规则、四档模式、人在回路确认"整合成一条判定链。

实现位置：`lancher_code/permission_engine.py`。

## 在系统中的位置

```text
ToolExecutor.execute_calls()
  对每个工具调用
    → PermissionEngine.evaluate(call, tool, context)
        → 返回 PermissionCheck（allow / deny / ask）
    → deny → 返回结构化错误给模型
    → ask → 生成 PermissionRequest → TUI 弹窗 → PermissionResolution → apply_resolution()
```

## 核心类

### `PermissionStorage`

规则存储，维护三个作用域：

| 作用域 | 存储位置 | 生命周期 |
|---|---|---|
| `session` | 内存（`SessionController` 订阅变更回调） | 会话内；随命名会话保存/恢复 |
| `project` | `./.lancher/permissions.yaml` | 落盘 |
| `user` | `~/.lancher/permissions.yaml` | 落盘 |

主要方法：`rules_for_scope()`、`replace_rules()`、`add_session_rule()`、`replace_session_rules()`、`add_project_rule()`、`subscribe_session_rules_changed()`。

### `PermissionEngine`

| 方法 | 作用 |
|---|---|
| `evaluate(call, tool, context)` | 五层判定，返回 `PermissionCheck` |
| `apply_resolution(request, resolution)` | 把用户决议写入规则（allow_session / allow_project） |

## 判定链（五层）

```text
① 构造匹配目标（_build_match_target）
   · bash → 规范化命令文本（小写、折叠空白）
   · 文件工具 → 项目相对路径（正斜杠、小写）
   · glob → 模式本身；grep → 搜索路径
   · MCP 外部工具 → 可见名 mcp__<server>__<tool>（精确匹配）
   路径非法（越界）→ 直接 deny（path_outside_project）

② bash 危险命令黑名单（COMMAND_BLACKLIST_PATTERNS）
   remove-item/del/rm、shutdown、format/diskpart/cipher、runas/sudo、
   git reset --hard / clean -fdx / checkout --、重定向符号 >> > 等
   → 命中即 deny（permission_blacklist_denied），bypass 模式也生效

③ Plan Mode 命令校验（validate_plan_command）
   仅允许白名单前缀（ls/dir/pwd/cat/rg/git status/git diff/版本查询等）
   禁止重定向、管道、&&、set-content、npm/pip/uv 等
   → 违规即 deny（plan_mode_command_rejected）

④ 规则引擎（_match_rules）
   按 session > project > user 顺序，同一作用域内**最后一条**命中规则生效
   格式 ToolLabel(value)，支持 glob（* ? [）
   → allow / deny（permission_rule_allow / permission_rule_deny）

⑤ 权限模式（_mode_decision）
   bypass          → allow（黑名单与规则仍优先）
   读工具(read)    → allow
   plan 模式外部工具 → deny
   acceptEdits + 写工具（非 bash）→ allow
   其余            → ask（进入人在回路）

⑥ 人在回路
   生成 PermissionRequest → TUI InlinePermissionPanel
   用户选择 allow_once / allow_session / allow_project / deny
```

## 匹配规则格式

```yaml
rules:
  - match: "Bash(git *)"       # 命令 glob
    result: allow
  - match: "WriteFile(.env)"   # 项目相对路径
    result: deny
  - match: "mcp__github__*"    # MCP 工具名 glob
    result: allow
```

规则标签 → 工具名映射（`TOOL_LABELS`）：`Bash`、`ReadFile`、`WriteFile`、`EditFile`、`Glob`、`Grep`、`WritePlanFile`。MCP 工具使用 `mcp__<server>__<tool>` 可见名（见 `tests/mcp/test_permission.py`）。

## 权限请求与决议

`PermissionRequest` 字段（`models.py`）包含请求标题、详情、命令/文件预览、建议规则等。`PermissionResolution.outcome` 取值：

| outcome | 效果 |
|---|---|
| `allow_once` | 仅本次放行 |
| `allow_session` | 写入 session 规则（内存） |
| `allow_project` | 写入 project 规则（落盘 `./.lancher/permissions.yaml`） |
| `deny` | 拒绝，工具返回 `permission_user_denied` 错误 |

建议规则（`_suggest_rule`）：命令含空格时生成 `Bash(首个单词 *)` 形式的 glob 规则。

## 输入与输出

| 方向 | 说明 |
|---|---|
| 输入 | `ToolCall`、`ToolDefinition`、`ToolContext`（mode / cwd / project_root / plan_file_path） |
| 输出 | `PermissionCheck`（decision + reason_code + 可选 PermissionRequest） |

## 与其他模块的关系

- ← `tools/core/executor.py`：每个工具调用前调用 `evaluate()`
- ← `app.py`：构造 `PermissionStorage`（项目/用户规则路径）
- ← `tui_views/chat.py`、`permission.py`：弹窗与决议回调
- → `tools/core/common.py`：路径沙箱（`resolve_path_in_root` / `ensure_path_in_root`）
- ↔ `SessionController`：会话规则变更订阅（用于自动保存）

## 注意事项

- 同一作用域内多条规则命中时取**最后一条**（列表顺序即优先级）。
- `bypass` 模式不能绕过：黑名单、Plan 校验、显式 `deny` 规则。
- 路径沙箱基于 `path.resolve()`（解析符号链接）后判断是否位于项目根内，防目录穿越与符号链接逃逸（见 `tests/tools/test_path_sandbox.py`）。
