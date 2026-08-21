# 交互与命令

## CLI 参数

当前版本的 CLI 解析器（`lancher_code/cli.py` 的 `build_arg_parser()`）**没有定义任何命令行参数**，只设置了程序名 `lancher` 和描述。也就是说：

```bash
uv run lancher            # 直接启动
uv run lancher --help     # 仅显示程序名与描述，无其他选项
```

**任何参数都会被解析但没有任何效果**（`parser.parse_args(argv)` 未绑定 action）。如需增加参数，参见 [development.md](development.md) 的"如何增加命令"。

## 退出码

| 退出码 | 含义 | 来源 |
|---|---|---|
| `0` | 正常退出（/exit、Ctrl+C 空闲时、引导取消） | `cli.py` |
| `130` | 顶层 `KeyboardInterrupt` | `cli.py` |
| `1` | 未捕获异常 / 配置非法 | `cli.py` / `app.py` |

## 斜杠命令

命令注册与解析在 `lancher_code/slash_commands.py`，执行逻辑在 `lancher_code/tui_views/chat.py` 的 `_execute_slash_command()`。

| 命令 | 用法 | 作用 |
|---|---|---|
| `/plan` | `/plan [任务]` | 进入 Plan Mode；带参数时把参数作为本轮用户请求提交 |
| `/do` | `/do` | 退出 Plan Mode，恢复到进入 plan 前的最近一个非 plan 模式 |
| `/mode` | `/mode <default\|plan\|acceptEdits\|bypass>` | 直接切换权限模式 |
| `/session` | `/session <list\|save\|remove\|rename\|resume> [名称]` | 管理项目会话（见下） |
| `/compact` | `/compact` | 手动压缩当前会话上下文（不创建显示消息） |
| `/settings` | `/settings` | 打开设置面板 |
| `/exit` | `/exit` | 退出当前会话（程序） |

### `/session` 子命令

| 子命令 | 说明 |
|---|---|
| `list` | 列出当前项目已保存会话（含时间、消息数、会话权限条数） |
| `save <名称>` | 保存并绑定当前会话（名称仅限中文、字母、数字、`_`、`-`） |
| `remove <名称>` | 删除指定会话（不能删除当前正在使用的会话） |
| `rename <旧> <新>` | 重命名会话 |
| `resume <名称> [--force]` | 恢复会话；若当前对话有未保存改动，需要 `--force` 强制丢弃 |

会话文件存放在 `./.lancher/session/<名称>.jsonl`，恢复时会同时恢复会话级权限规则。详见 [workflows/session-lifecycle.md](workflows/session-lifecycle.md)。

### 模式可见性

- `/plan` 在非 plan 模式下可见；`/do` 仅在 plan 模式下可见（`visible_modes`）。
- 所有命令在任何模式都可执行（`executable_modes` 为全部四种模式）。

## 输入框快捷键

绑定定义在 `lancher_code/tui_views/composer.py` 与 `chat.py`：

| 按键 | 作用 |
|---|---|
| `Enter` | 发送消息 / 确认斜杠补全 |
| `Shift+Enter` | 插入换行 |
| `Tab` | 接受斜杠命令/参数补全 |
| `Shift+Tab` | 循环切换权限模式（default → plan → acceptEdits → bypass） |
| `↑` / `↓` | 在补全菜单、权限面板选项中移动 |
| `Esc` | 关闭补全菜单 / 拒绝权限请求 |
| `Ctrl+C` | 模型回复中：取消当前回合；空闲：退出程序 |

## 权限模式

| 模式 | 提示符 | 语义 |
|---|---|---|
| `default` | `>` | 读工具自动放行；文件写入与命令执行需要确认 |
| `plan` | `#` | 与 default 权限语义相同，但受 Plan Mode 提示词与工具集限制（只读 + 计划文件） |
| `acceptEdits` | `+` | 读工具与文件写工具自动放行；命令执行需要确认 |
| `bypass` | `!` | 默认全部放行，但显式 `deny` 规则与危险命令黑名单仍生效 |

## 权限确认弹窗

当规则与模式都没有明确放行时，TUI 会挂起工具循环并弹出**内联权限面板**（`tui_views/permission.py`）：

- **命令执行**（`bash`）：
  1. 仅允许执行本次命令
  2. 在本次会话中放行（写入会话规则）
  3. 在当前项目中放行（写入 `./.lancher/permissions.yaml`）
  4. 拒绝执行
- **文件编辑**（`write_file` / `edit_file` / `write_plan_file`）：
  1. 仅允许本次编辑
  2. 拒绝本次编辑
- **MCP 工具**（external_tool）：
  1. 仅允许本次调用
  2. 在本次会话中放行
  3. 在当前项目中放行
  4. 拒绝调用

用户拒绝后工具循环**不会中断**：模型会收到结构化的错误结果（`error_code=permission_user_denied`），自行调整策略继续。

## 界面布局（主聊天界面）

```text
┌────────────────────────────────────────────┐
│ LanCher Code  cwd: ...       MCP 0/0 · 上下文 5% │  ← 横幅
│   （ASCII Logo + MCP 服务面板）                 │
│                                             │
│ YOU   用户消息                                 │  ← 消息区（垂直滚动）
│ LANCHER  模型回复                             │
│   ▼ 思考轨迹 (n)                             │
│     ● tool_call(args)                       │
│     ✓ tool_result                           │
│                                             │
│ /plan 继续补充或修改计划           [命令提示]    │  ← 斜杠补全菜单
│ ─────────────────────────────────────────── │
│ > 发送一条消息                                │  ← 输入框
│ [状态左: 模型名/模式] [状态中: Ready] [状态右: Tokens] │  ← 状态栏
└────────────────────────────────────────────┘
```

- 模式切换时提示符颜色变化：default 蓝、plan 黄、acceptEdits 绿、bypass 橙。
- 思考轨迹默认折叠（`▶ 思考轨迹 (n)`），点击标题可展开。
- 消息错误/取消时以 `ERROR` / `CANCELLED` 标记并红色显示。
