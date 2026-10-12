```text
    __                ________                 ______          __
   / /   ____ _____  / ____/ /_  ___  _____   / ____/___  ____/ /__
  / /   / __ `/ __ \/ /   / __ \/ _ \/ ___/  / /   / __ \/ __  / _ \
 / /___/ /_/ / / / / /___/ / / /  __/ /     / /___/ /_/ / /_/ /  __/
/_____/\__,_/_/ /_/\____/_/ /_/\___/_/      \____/\____/\__,_/\___/
```

> 基于 Python 的 AI 编程助手，由独立 Agent 核心、终端界面和无界面命令组成。可以读代码、查文件、改文件、跑命令、写计划，也可通过 Python SDK 接入测试脚本或其他前端。

## 当前能力

- 独立 `lancher_code.sdk`：显式配置、事件快照、最终结果、权限处理和统一资源生命周期；核心安装不依赖 Textual / Rich。
- `lancher run` 无界面任务，支持指定工作目录、模型、恢复会话、标准输入、JSON / JSONL 输出与超时。
- 深浅主题的原生极简终端对话，支持流式输出、折叠工具记录和独立的思考显示。
- 支持配置多个自定义供应商，每个供应商可添加多个模型；兼容 `OpenAI` 与 `Anthropic` 两类协议。
- 支持供应商公共连接参数、模型逐字段覆盖、默认主模型，以及聊天中通过 `/model` 切换。
- 内置文件读取、写入、编辑、删除、搜索与计划工具，以及 `run_command` 和六个 `process_*` 进程管理工具。
- Skills 支持项目与用户 `.lancher/skills` 目录、`$技能名` 显式指定和模型按需加载；正文跨轮保留，成功压缩后按引用重载。
- 自动读取项目根 `AGENTS.md`；MCP Tools 支持后台初始化、分页发现、动态目录刷新、重连和独立超时。
- 核心保存固定请求前缀，新增技能与主机状态通过尾部事件提供；MCP 常规工具定义跨用户轮保留。“系统设置”提供默认关闭的原生 MCP 工具追加实验项，仅用于支持端点。
- 按资源调度并行工具；每段对话可同时托管多个 Pipe / PTY 进程，支持后台、增量日志、输入和进程树停止。
- 支持 ReAct 式多轮工具循环、工具轨迹展示、Token 用量展示。
- 讨论和计划强制只读；执行阶段可选择项目访问或完全访问，阶段切换保留执行访问选择，计划支持确认正文后开始执行。
- 工作时可继续输入，将消息排到下一轮或补充当前任务；停止后保留草稿并暂停队列。
- 首条消息自动建立 UUID Session，增量保存 JSONL；每段对话有独立计划、临时文件和产物目录。
- 内置访问权限系统：
  - 只读、项目访问、完全访问三种有效模式
  - Shell AST 分析、真实路径范围与已识别极危险操作拦截
  - 用户级 / 项目级 / 会话级额外拒绝规则
  - 外部访问的本次批准或本会话目录授权
  - 非阻塞内联审批与执行前目标、版本和取消状态复核

## 安装与启动

推荐使用 `uv`：

```bash
uv sync
uv run lancher-code
# 同一入口也可使用 uv run lancher
```

仓库开发默认安装 TUI 依赖。只安装核心可用 `pip install .`；需要终端界面用 `pip install ".[tui]"`。

也可以直接运行：

```bash
python -m lancher_code
```

无界面运行使用显式配置，不进入首次引导：

```bash
lancher run "阅读项目并总结入口职责" --config ./my-model.yaml --cwd ./my-project --format json --timeout 120
```

需要批准的操作默认拒绝，仍遵守配置中的阶段和访问范围。Python SDK、输出契约和评测接入口见 [Agent SDK 与无界面运行](docs/agent-sdk.md)。

## 配置文件

终端界面读取全局配置；`lancher run` 通过 `--config` 明确指定配置，SDK 接受显式 `AgentConfig`。终端界面的默认文件：

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

配置采用 `providers` 目录和 `default_model: 供应商ID/模型ID`，详见 [配置说明](docs/configuration.md)。开发版只接受当前格式；旧单 `provider` 和 `runtime.permission_mode` 会明确提示重新配置，原文件保留，不自动迁移或生成备份。当前格式的环境变量引用和继承关系会保留。

### 运行时配置

`runtime.work_phase` 支持 `discuss`、`plan`、`execute`，默认执行。讨论和计划强制只读；计划阶段仅 `write_plan_file` 能保存当前会话计划，普通文件工具不能写草稿或源码。

`runtime.execution_access` 默认 `project`：项目内读写、递归删除、命令和脚本自动执行，检测到项目外读取、写入或工作目录时审批。设为 `full` 后任意目录自动执行；已识别极危险操作和显式拒绝规则仍生效。运行中用 `/access project|full` 切换；返回执行时恢复之前的选择。

外部审批可仅允许本次，或批准本会话明确列出的目录、读写范围及是否包含子目录。授权随会话保存，新会话清空。普通项目内 Git 回滚和目录删除不再固定弹窗。专用删除工具仍检查完整清单、版本和取消状态。

这些是应用层检测式权限，未增加操作系统沙箱。项目内脚本的动态外部访问可能无法预先识别；MCP 只读信息来自服务器声明。详见 [权限与访问范围](docs/modules/permission-engine.md)。

`ui.theme` 默认为 `dark`，可设为 `light`。`ui.busy_enter_action` 默认为 `follow_up`（下一轮），也可设为 `steer`（补充当前任务）或 `draft`（保留草稿）。旧 `permission_policy` / `permission_mode` 和旧策略值明确拒绝，原文件保留，不自动兼容。

`runtime.experimental_mcp_tool_append` 默认 `false`，在 `/settings` 的“系统设置”中修改；只接受布尔值。常规模式把发现的 MCP 工具放入 `tools` 数组。实验模式使用 OpenAI Responses 的 `additional_tools` 或 Claude 的原生工具追加 beta，只有支持端点才适合开启。保存前由核心校验空闲，应用后下次请求重建前缀；不支持时关闭实验项。当前验证为离线请求与传输测试，尚未实测模型 API 兼容性或缓存命中率，详见 [请求前缀与缓存边界](docs/modules/prompting.md)。

### 权限规则文件

永久权限文件仅支持额外拒绝规则，必须声明 `format_version: 2`。项目规则存于 `./.lancher/permissions.yaml`，用户规则存于 `~/.lancher/permissions.yaml`，匹配结果只能是 `deny`。旧允许规则不兼容，原文件保留并提示重新配置。会话目录授权只在当前会话保存。

## 交互命令

- `/discuss [任务]`、`/plan [任务]`、`/do [任务]`
  分别切到讨论、计划、执行；有参数则切换后提交，无参数只切阶段。`Shift+Tab` 循环阶段。
- `/access [project|full]`
  选择执行访问范围；讨论和计划始终只读，返回执行阶段恢复选择。
- `/model [供应商ID/模型ID]`
- `/effort [档位]`：切换当前对话的推理强度，从下一条消息开始沿用；`auto` 恢复自动，档位按当前模型能力补全。
  打开模型选择器，或直接切换当前会话的主模型；保留对话历史，不修改全局默认值。
- `/settings`
  管理供应商、模型、MCP、权限、外观与输入。逐条保存；本次模型与新对话默认独立。MCP 保存后由核心立即重新应用，失败可用 `/mcp reload` 重试。
- `/skills [list|show|reload|enable|disable|unload] [技能名或ID]`
  查看与管理当前会话的技能；任务中用 `$技能名` 或 `$project/技能名` 显式加载，模型也可按描述选择。
- `/mcp [list|refresh|reconnect|reload] [服务器名]`
  查看连接状态、刷新工具目录、重新连接或重读配置。
- `/session <new|list|resume|rename|archive|remove> [UUID] [标题]`
  管理项目对话；首条消息自动保存，标题可包含空格并允许重复。新建、恢复按独立 UUID 切换；归档和删除需要确认，当前会话请先 `new`。恢复后待发送消息全部暂停。
- `/tasks [list|show|read|stop|background] [进程UUID]`
  打开当前会话任务列表与详情，查看增量输出、发送输入、转后台或停止单个进程；工作中也能使用。
- `/session stop`
  停止当前轮次及本会话全部托管进程。输入区 Esc 或工作中 Ctrl+C 只停止本轮，明确转交会话的后台进程继续运行。
- `/exit`
  退出当前会话。

## 权限确认

项目访问模式检测到外部目录访问时，输入区展示待审批操作、真实目标、工作目录、用途及文件差异。可仅允许本次、拒绝本次，或在次级入口批准明确显示的本会话目录范围；范围注明只读或读写、是否包含子目录，不生成永久允许规则。非只读 MCP 在项目访问模式仅支持本次批准，完全访问模式自动允许。

审批期间仍可编辑草稿或补充消息。补充会撤销未执行操作的旧审批；过期按钮无效。审批焦点下 `Esc` 拒绝当前审批，`Ctrl+C` 停止本轮并暂停队列；会话后台仍保留。拒绝后模型会收到结构化错误结果，再尝试调整策略。

完整键盘操作与消息生效规则见 [交互说明](docs/cli-and-interaction.md)。

## `.lancher` 目录说明

- `~/.lancher`
  存放全局配置、用户级权限规则、MCP 配置与用户 Skills。
- `./.lancher`
  存放项目权限规则、MCP 配置、项目 Skills 和按 UUID 分离的 Session 记录及工作文件。

当前默认文件：

- 全局配置：`~/.lancher/lancher.yaml`
- 用户级权限规则：`~/.lancher/permissions.yaml`
- 项目级权限规则：`./.lancher/permissions.yaml`
- 用户／项目技能：`~/.lancher/skills/<名称>/SKILL.md`、`./.lancher/skills/<名称>/SKILL.md`
- 会话事件：`./.lancher/sessions/<UUID>/events.jsonl`
- 会话工作目录：`./.lancher/sessions/<UUID>/workspace/`（含 `plan.md`、`tmp/`、`artifacts/`）
- 进程记录与输出：`./.lancher/sessions/<UUID>/processes/<进程UUID>/`（应用管理区）

Session ID 是 32 位小写 UUID hex。列表显示短 ID，命令补全填入完整 ID；命令不解析短 ID。事件、列表缓存与 checkpoint 的格式版本为 `3`，只读取当前格式。旧版本与旧 `.lancher/session/` 原样保留，不迁移、不自动删除；列表单独报告不支持或损坏的会话，其他会话仍可使用。启动和查询列表不会创建会话，模型调用失败的对话也会保留。压缩活动在聊天中可展开查看前后估算和压缩率，恢复会话后保留结果，未结束活动显示为已中断。详见 [Session 生命周期](docs/workflows/session-lifecycle.md) 与 [上下文压缩活动](docs/workflows/context-compaction.md)。

后台进程可跨轮次和对话切换，应用退出时统一清理；重启恢复记录与日志，不自动重跑旧命令。未知命令在执行阶段不因分析不完整本身要求审批，已识别的外部范围仍按访问模式判定；只读阶段必须明确识别为读取。资源调度在本次启动调用期间保守独占项目，返回后台任务后可继续请求服务器或修改文件；这不表示后台命令没有真实文件副作用，也无法保证检测脚本动态访问外部目录。`execution.limits` 配置额度，`execution.command_profiles` 为已知命令明确声明持续资源与本机 TCP 就绪检查；资源默认保留到进程退出，也可明确设为仅调用期间。界面区分待批准与等待资源，说明实际阻塞任务。完整设计、取消竞态、Windows Job Object、ConPTY、中文输出分页与存储失败处理的解释见 [工具执行](docs/workflows/tool-execution.md)。

## 当前状态

- Provider、会话层、工具系统、AgentService、TUI 和无界面命令使用同一核心运行链。
- 只读、项目访问、完全访问三种有效权限已落地，覆盖文件、Shell 和 MCP；已识别极危险操作与显式拒绝始终生效。
- 核心代码按 `agent/`、`context/`、`sessions/`、`config/` 等领域组织；目录职责见 [项目结构](docs/project-structure.md)，验证方式见 [开发说明](docs/development.md)。

Skills 的格式、精简示例、引用资料与压缩生命周期见 [Skills 与项目约定](docs/modules/skills.md)。项目同名技能优先，可用 `project/<名称>`、`user/<名称>` 精确指定。技能与项目约定的加载、会话状态及 MCP 生命周期由 `agent/` 核心管理，TUI 只展示状态并调用公共入口；技能不会扩大阶段或执行权限。MCP 本轮只提供 Tools，详见 [MCP 模块](docs/modules/mcp.md)。
