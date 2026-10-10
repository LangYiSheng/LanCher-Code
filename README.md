```text
    __                ________                 ______          __
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/
```

> 基于 Python 的终端 AI 编程助手。目标不是只会聊天，而是真能在终端里和你一起读代码、查文件、改文件、跑命令、写计划，并且把危险操作拦在权限系统里。

## 当前能力

- 深浅主题的原生极简终端对话，支持流式输出、折叠工具记录和独立的思考显示。
- 支持配置多个自定义供应商，每个供应商可添加多个模型；兼容 `OpenAI` 与 `Anthropic` 两类协议。
- 支持供应商公共连接参数、模型逐字段覆盖、默认主模型，以及聊天中通过 `/model` 切换。
- 内置文件、搜索、计划工具，以及 `run_command` 和六个 `process_*` 进程管理工具。
- 按资源调度并行工具；每段对话可同时托管多个 Pipe / PTY 进程，支持后台、增量日志、输入和进程树停止。
- 支持 ReAct 式多轮工具循环、工具轨迹展示、Token 用量展示。
- 讨论、计划、执行三个工作阶段与审批策略独立；计划支持确认正文后开始执行。
- 工作时可继续输入，将消息排到下一轮或补充当前任务；停止后保留草稿并暂停队列。
- 首条消息自动建立 UUID Session，增量保存 JSONL；每段对话有独立计划、临时文件和产物目录。
- 内置五层权限系统：
  - 危险命令黑名单
  - 内置文件工具的项目路径边界
  - 用户级 / 项目级 / 会话级规则
  - 三种权限策略，以及优先于规则的工作阶段限制
  - 非阻塞的内联审批面板

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

首次启动如果不存在该文件，会自动进入 Textual 引导界面，创建第一个供应商和默认模型，要求填写：

- 供应商名称和 `protocol`（`openai` / `claude`，界面显示 Anthropic）
- API 模型名称 `model_name`，以及可选显示名称 `display_name`
- `base_url`
- `api_key`

仓库中保留了一个结构示例：

```text
lancher.example.yaml
```

之后用 `/settings` 管理供应商及模型，选择新会话的默认主模型。模型默认继承供应商的协议、Base URL、API Key、超时，也可以逐项覆盖。显示名称不影响 API 调用；未填写时显示 `model_name (供应商名称)`。

配置采用 `providers` 目录和 `default_model: 供应商ID/模型ID`，详见 [配置说明](docs/configuration.md)。原有单 `provider` 配置仍能读取；第一次保存新格式前自动备份为 `lancher.yaml.bak`。环境变量引用和继承关系会保留。

### 运行时配置

`runtime.work_phase` 支持 `discuss`、`plan`、`execute`，默认执行。讨论与计划允许内置读取、查找、搜索和明确声明只读的 MCP 工具；当前 Session 的 `workspace/` 在所有阶段允许文件写入，计划工具写入该会话的 `workspace/plan.md`。普通源码修改与通用 Shell 仍遵守阶段限制，跳过询问不能突破这些限制。路径批准属于内置工具权限判定，不是操作系统沙箱；MCP 只读信息来自服务器声明。

`runtime.permission_policy` 独立控制审批，支持三种策略：

- `default`
  读工具自动放行；文件写入和命令执行需要确认。
- `acceptEdits`
  读工具和文件写工具自动放行；命令执行需要确认。
- `bypass`
  跳过常规询问，但阶段限制、显式 `deny` 规则与危险命令黑名单仍然生效。

`ui.theme` 默认为 `dark`，可设为 `light`。`ui.busy_enter_action` 默认为 `follow_up`（下一轮），也可设为 `steer`（补充当前任务）或 `draft`（保留草稿）。旧 `permission_mode=plan` 会迁移为计划＋标准权限，其余旧值迁移为执行＋原策略；完整新字段优先。

### 权限规则文件

LanCher Code 现在区分三层权限规则：

- 会话级：随当前 Session 自动持久化和恢复
- 项目级：`./.lancher/permissions.yaml`
- 用户级：`~/.lancher/permissions.yaml`

优先级：

```text
session > project > user
```

规则格式：

```yaml
rules:
  - match: "RunCommand(git *)"
    match_kind: glob
    result: allow
  - match: "WriteFile(.env)"
    result: deny
```

说明：

- `RunCommand(...)` 匹配规范化后的命令文本。
- `ReadFile/WriteFile/EditFile(...)` 匹配项目相对路径。
- `Glob(...)` 匹配 glob 模式本身。
- `Grep(...)` 匹配搜索范围路径。
- 新增命令授权默认使用 `match_kind: exact` 精确匹配整条命令；旧规则保持原有通配语义，可在设置中区分。

## 交互命令

- `/discuss [任务]`、`/plan [任务]`、`/do [任务]`
  分别切到讨论、计划、执行；有参数则切换后提交，无参数只切阶段。`Shift+Tab` 循环阶段。
- `/permissions [default|acceptEdits|bypass]`
  打开或切换本次审批策略，不改变阶段。
- `/model [供应商ID/模型ID]`
  打开模型选择器，或直接切换当前会话的主模型；保留对话历史，不修改全局默认值。
- `/settings`
  管理供应商、模型、MCP、权限、外观与输入。逐条保存；本次模型与新对话默认独立。MCP 修改持续标记待重启。
- `/session <new|list|resume|rename|archive|remove> [UUID] [标题]`
  管理项目对话；首条消息自动保存，标题可包含空格并允许重复。新建、恢复按独立 UUID 切换；归档和删除需要确认，当前会话请先 `new`。恢复后待发送消息全部暂停。
- `/tasks [list|show|read|stop|background] [进程UUID]`
  打开当前会话任务列表与详情，查看增量输出、发送输入、转后台或停止单个进程；工作中也能使用。
- `/session stop`
  停止当前轮次及本会话全部托管进程。输入区 Esc 或工作中 Ctrl+C 只停止本轮，明确转交会话的后台进程继续运行。
- `/exit`
  退出当前会话。

## 权限确认

当规则和策略都没有明确放行时，输入区展示待审批操作、目标、工作目录、用途及文件差异。主要动作是仅允许本次和拒绝本次；命令的会话／项目授权位于次级入口，并显示实际匹配范围。

审批期间仍可编辑草稿或补充消息。补充会撤销未执行操作的旧审批；过期按钮无效。审批焦点下 `Esc` 拒绝当前审批，`Ctrl+C` 停止本轮并暂停队列；会话后台仍保留。拒绝后模型会收到结构化错误结果，再尝试调整策略。

完整键盘操作与消息生效规则见 [交互说明](docs/cli-and-interaction.md)。

## `.lancher` 目录说明

- `~/.lancher`
  存放全局配置、用户级权限规则，以及后续全局能力。
- `./.lancher`
  存放当前项目权限规则和按 UUID 分离的 Session 记录及工作文件。

当前默认文件：

- 全局配置：`~/.lancher/lancher.yaml`
- 用户级权限规则：`~/.lancher/permissions.yaml`
- 项目级权限规则：`./.lancher/permissions.yaml`
- 会话事件：`./.lancher/sessions/<UUID>/events.jsonl`
- 会话工作目录：`./.lancher/sessions/<UUID>/workspace/`（含 `plan.md`、`tmp/`、`artifacts/`）
- 进程记录与输出：`./.lancher/sessions/<UUID>/processes/<进程UUID>/`（应用管理区）

Session ID 是 32 位小写 UUID hex。列表显示短 ID，命令补全填入完整 ID；命令不解析短 ID。事件格式版本为 `1`，不兼容旧命名会话 v1–v4；旧 `.lancher/session/` 原样保留，不读取、不迁移。启动和查询列表不会创建会话，模型调用失败的对话也会保留。压缩活动在聊天中可展开查看前后估算和压缩率，恢复会话后保留结果，未结束活动显示为已中断。详见 [Session 生命周期](docs/workflows/session-lifecycle.md) 与 [上下文压缩活动](docs/workflows/context-compaction.md)。

后台进程可跨轮次和对话切换，应用退出时统一清理；重启恢复记录与日志，不自动重跑旧命令。未知命令只在本次启动调用期间保守独占项目，返回后台任务后可继续请求服务器或修改文件；这不表示后台命令没有真实文件副作用。`execution.limits` 配置额度，`execution.command_profiles` 为已知命令明确声明持续资源与本机 TCP 就绪检查；资源默认保留到进程退出，也可明确设为仅调用期间。界面区分待批准与等待资源，说明实际阻塞任务。完整设计、取消竞态、Windows Job Object、ConPTY、中文输出分页与存储失败处理的解释见 [工具执行](docs/workflows/tool-execution.md)。

## 当前状态

- Provider、会话层、工具系统、TurnRunner、TUI 已全部打通。
- 五层权限系统已落地，并覆盖命令执行与文件操作。
- 全量测试当前通过。
