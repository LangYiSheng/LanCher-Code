```text
    __                ________                 ______          __
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/
```

> 基于 Python 的终端 AI 编程助手。目标不是只会聊天，而是真能在终端里和你一起读代码、查文件、改文件、跑命令、写计划，并且把危险操作拦在权限系统里。

## 当前能力

- 终端内多轮对话，支持流式输出。
- 支持 `OpenAI` 与 `Claude` 两类协议后端。
- 内置工具：`read_file`、`write_file`、`edit_file`、`glob`、`grep`、`bash`、`write_plan_file`。
- 支持 ReAct 式多轮工具循环、工具轨迹展示、Token 用量展示。
- 支持 `Plan Mode`、`/do` 恢复、`/mode` 模式切换。
- 内置五层权限系统：
  - 危险命令黑名单
  - 项目路径沙箱
  - 用户级 / 项目级 / 会话级规则
  - 四档权限模式
  - 人在回路确认弹窗

## 安装与启动

推荐使用 `uv`：

```bash
uv sync
uv run lancher-code
```

也可以直接运行：

```bash
python -m lancher_code
```

## 配置文件

程序优先读取全局配置：

```text
~/.lancher/lancher.yaml
```

首次启动如果不存在该文件，会自动进入 Textual 引导界面，要求填写：

- `protocol`
- `model`
- `base_url`
- `api_key`

仓库中保留了一个结构示例：

```text
lancher.example.yaml
```

### 运行时配置

`runtime.permission_mode` 支持四档：

- `default`
  读工具自动放行；文件写入和命令执行需要确认。
- `plan`
  权限语义与 `default` 相同，但额外受 Plan Mode prompt 与工具集限制。
- `acceptEdits`
  读工具和文件写工具自动放行；命令执行需要确认。
- `bypass`
  默认全部放行，但显式 `deny` 规则和危险命令黑名单仍然生效。

### 权限规则文件

LanCher Code 现在区分三层权限规则：

- 会话级：仅内存生效，不落盘
- 项目级：`./.lancher/permissions.yaml`
- 用户级：`~/.lancher/permissions.yaml`

优先级：

```text
session > project > user
```

规则格式：

```yaml
rules:
  - match: "Bash(git *)"
    result: allow
  - match: "WriteFile(.env)"
    result: deny
```

说明：

- `Bash(...)` 匹配规范化后的命令文本。
- `ReadFile/WriteFile/EditFile(...)` 匹配项目相对路径。
- `Glob(...)` 匹配 glob 模式本身。
- `Grep(...)` 匹配搜索范围路径。

## 交互命令

- `/plan [任务]`
  进入 Plan Mode；带参数时立即把参数作为本轮用户请求提交。
- `/do`
  退出 Plan Mode，恢复到进入 `plan` 前的最近一个非 `plan` 模式。
- `/mode <default|plan|acceptEdits|bypass>`
  直接切换权限模式。
- `/exit`
  退出当前会话。

## 权限确认

当规则和模式都没有明确放行时，TUI 会弹出确认框：

- 命令执行支持：
  - 允许执行本次命令
  - 本会话永久放行
  - 本项目永久放行
  - 拒绝执行
- 文件编辑支持：
  - 允许本次编辑
  - 拒绝本次编辑

如果用户拒绝，Agent Loop 不会被打断；模型会收到结构化错误结果，再尝试调整策略。

## `.lancher` 目录说明

- `~/.lancher`
  存放全局配置、用户级权限规则，以及后续全局能力。
- `./.lancher`
  存放当前项目私有内容，例如 `plan.md`、项目级权限规则等。

当前默认文件：

- 全局配置：`~/.lancher/lancher.yaml`
- 用户级权限规则：`~/.lancher/permissions.yaml`
- 项目级权限规则：`./.lancher/permissions.yaml`
- Plan 文件：`./.lancher/plan.md`

## 当前状态

- Provider、会话层、工具系统、TurnRunner、TUI 已全部打通。
- 五层权限系统已落地，并覆盖命令执行与文件操作。
- 全量测试当前通过。
