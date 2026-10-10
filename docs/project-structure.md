# 项目结构

代码按领域组织。根包只保留启动装配、异常和日志；类型与逻辑放在所属领域，公共契约放在 `contracts/`。目录名说明职责，`__init__.py` 不承担旧模块路径的转导出。

## 核心目录树

以下省略缓存、虚拟环境、构建产物与大部分测试文件：

```text
lancher-code/
├── main.py                      # 仓库和打包入口，调用 lancher_code.cli:main
├── pyproject.toml               # 依赖、测试配置、lancher / lancher-code 双 CLI 入口
├── uv.lock
├── lancher.spec                 # PyInstaller 配置
├── lancher.example.yaml         # 当前主配置示例
├── assets/
├── lancher_code/
│   ├── __init__.py              # 包版本
│   ├── __main__.py              # python -m lancher_code
│   ├── cli.py                  # 参数解析、日志和退出码
│   ├── app.py                  # 配置、服务、运行时与 TUI 装配
│   ├── errors.py
│   ├── logging_system.py
│   ├── contracts/              # 跨领域契约，不承载业务流程
│   │   ├── control.py          # 工作阶段、权限策略、取消令牌
│   │   ├── messages.py         # ChatRequest、ContentBlock、StreamEvent
│   │   └── tools.py            # 工具定义、调用、结果、阶段能力
│   ├── agent/                  # 单轮任务编排与智能体能力核心
│   │   ├── runner.py           # TurnRunner 公开入口与任务循环
│   │   ├── capabilities.py     # Skills、项目约定与 MCP 生命周期的公共门面
│   │   ├── skill_context.py    # 技能激活、跨轮状态和核心请求内容
│   │   ├── instructions.py     # 项目根 AGENTS.md 常驻只读加载
│   │   ├── skills/             # 技能发现、格式、快照和限定资源读取
│   │   ├── inputs.py           # 忙时输入与队列
│   │   ├── selection.py        # ModelSelection：当前模型与 Provider 切换
│   │   ├── streaming.py        # 完整模型响应收集
│   │   ├── tool_batch.py       # 工具批次与待处理调用收尾
│   │   └── events.py           # TurnEvent
│   ├── context/                # 模型可见上下文
│   │   ├── models.py           # 上下文状态与结果类型
│   │   ├── tokens.py           # 估算、指纹、输入 usage 校准
│   │   ├── budget.py           # 输入、输出、工具结果与近期历史预算
│   │   ├── request.py          # 请求组装与发送副本
│   │   ├── prefix.py           # 固定前缀、主机尾部事件与工具基线
│   │   ├── projection.py       # 跨来源工具历史投影
│   │   ├── offload.py          # 大工具结果落盘与引用
│   │   ├── compaction.py       # 压缩候选与请求编排
│   │   ├── summary.py          # 摘要提示与结构验证
│   │   ├── recovery.py         # 压缩后的恢复提示
│   │   ├── prompts.py          # 系统、环境、阶段与工具索引提示
│   │   └── prompt_models.py    # PromptContext / PromptPayload
│   ├── sessions/               # 会话状态与持久化
│   │   ├── controller.py       # SessionController
│   │   ├── models.py           # 消息展示、轨迹、计划、队列等会话类型
│   │   ├── messages.py         # 消息与轨迹更新
│   │   ├── compaction.py       # 会话压缩活动与状态协调
│   │   ├── recovery.py         # 未完成消息与执行记录恢复
│   │   ├── paths.py            # UUID 目录与路径边界
│   │   ├── repository.py       # 会话创建、列表、读取、归档、删除
│   │   ├── event_log.py        # 事件读写、版本验证与写入者生命周期
│   │   ├── locking.py          # Windows / POSIX 会话独占锁
│   │   ├── cache.py            # 列表摘要缓存
│   │   ├── storage.py          # 格式常量、存储结果与错误
│   │   ├── codec.py            # 当前状态编解码
│   │   ├── projection.py       # 事件重放
│   │   └── service.py          # 生命周期与增量持久化协调
│   ├── config/                 # 应用配置与设置
│   │   ├── models.py           # AppConfig、RuntimeConfig、UIConfig
│   │   ├── paths.py
│   │   ├── bootstrap.py        # 首次引导状态
│   │   ├── loader.py           # 当前 YAML 格式校验
│   │   ├── writer.py           # 序列化与单文件原子写入
│   │   └── settings.py         # 按领域保存设置
│   ├── providers/              # 模型目录与协议适配
│   │   ├── models.py           # 供应商、模型、有效连接配置
│   │   ├── catalog.py          # 显式模型引用与逐字段继承解析
│   │   ├── base.py             # Provider 契约、SSE、错误与 usage
│   │   ├── factory.py
│   │   ├── openai.py
│   │   └── claude.py
│   ├── permissions/            # 审批与规则
│   │   ├── models.py
│   │   ├── engine.py           # 阶段、边界、规则和策略判定
│   │   ├── storage.py          # 用户、项目、会话规则存储
│   │   ├── rules.py            # 目标匹配与黑名单
│   │   └── preview.py          # 审批展示内容
│   ├── filesystem/access.py    # 公共路径访问与写入边界
│   ├── tools/                  # 本地与远端工具执行入口
│   │   ├── __init__.py         # 默认内置工具注册工厂
│   │   ├── context.py          # ToolContext
│   │   ├── parser.py           # 流式工具调用拼接与整批参数检查
│   │   ├── core/              # 注册、执行、Schema 校验、文件状态缓存
│   │   └── builtin/           # 文件、搜索、计划、命令与进程工具
│   ├── execution/              # 资源调度、进程、输出与平台后端
│   ├── usage/                  # 实际请求消耗
│   │   ├── models.py
│   │   ├── ledger.py
│   │   └── tracking.py
│   ├── mcp/                    # 连接、工具适配、配置校验与模板
│   └── tui/                    # Textual 界面
│       ├── app.py              # 主应用装配与生命周期
│       ├── chat/               # 布局、补全、HUD、消息与命令控制
│       ├── settings/           # 设置页与模型、MCP、权限、外观编辑器
│       ├── commands.py         # 斜杠命令定义、解析、补全
│       ├── bootstrap.py        # 首次引导
│       ├── composer.py         # 输入框
│       ├── message.py          # 消息容器
│       ├── timeline.py         # 正文、思考与工具顺序呈现
│       ├── permission.py       # 内联审批
│       ├── tasks.py            # 进程任务列表与详情
│       ├── capabilities.py     # Skills / MCP 状态、详情与管理命令转发
│       ├── compaction.py       # 压缩活动
│       ├── usage.py            # 用量显示
│       └── exit_summary.py     # 退出后的终端小结
├── tests/                      # 按领域与行为覆盖
└── docs/                       # 架构、配置、模块与流程说明
```

## 依赖与职责

- `app.py` 负责装配；TUI 消费 `TurnEvent` 并调用公开服务，不直接调用模型网络接口。
- `agent/` 编排一轮任务并管理 Skills、项目约定和 MCP 能力；前端只消费 `TurnRunner.capabilities` 的状态与公共操作。`sessions/` 持有对话事实，`context/` 构造模型可见副本并管理容量。正式请求持久化主机尾部事件，HUD 预览只使用副本，不提交前缀变化。
- `config/models.py` 持有应用配置组合；供应商定义属于 `providers/models.py`。`providers/catalog.py` 只接收供应商映射与显式引用，不导入 `AppConfig`，避免配置加载与目录解析的循环依赖。
- `contracts/` 只承载真正跨领域的消息、工具、控制契约。会话、权限、用量等类型由自己的领域维护，不再集中到根层 `models.py`。
- `permissions/` 判定是否执行，`tools/` 组织工具调用，`execution/` 负责资源与进程；公共路径访问规则集中在 `filesystem/access.py`。
- `usage/` 保存真实请求消耗，`context/` 管理下一次请求大小，两者分别建模；界面的格式化逻辑位于 `tui/usage.py`。

测试按相同领域放在 `tests/agent`、`config`、`context`、`sessions`、`permissions`、`usage`、`tui` 等目录；跨领域链路放在 `tests/integration`。根级只保留共享 fixture 和测试供应商辅助函数，测试替身也必须明确标记响应完成。

## 运行数据

```text
~/.lancher/
├── lancher.yaml                 # 唯一主配置
├── permissions.yaml             # 用户权限规则
├── mcp.yaml                     # 全局 MCP
├── skills/<名称>/SKILL.md         # 用户技能，可包含 references / scripts 等资源
└── logs/

<项目>/.lancher/
├── permissions.yaml             # 项目权限规则
├── mcp.yaml                     # 项目 MCP 覆盖
├── skills/<名称>/SKILL.md         # 项目技能，同名优先
└── sessions/<UUID>/
    ├── events.jsonl             # 版本 2，持久化事实来源
    ├── meta.json                # 版本 2，列表缓存
    ├── checkpoint.json          # 版本 2，投影快照
    ├── processes/               # 进程元信息与输出
    ├── blobs/                   # 大工具结果
    └── workspace/               # plan.md、tmp/、artifacts/
```

主配置不读取项目根的 `lancher.yaml`。旧配置和旧会话格式会明确报错，原文件保留，不迁移、不自动删除；可用会话与列表中的格式问题分别返回。公开启动入口 `lancher`、`lancher-code`、`python -m lancher_code` 和仓库 `main.py` 保留。

项目根 `AGENTS.md` 位于 `.lancher` 外，由智能体核心在请求构造时加载。技能格式与正文的压缩生命周期见 [Skills 与项目约定](modules/skills.md)。

详细运行链路见 [架构](architecture.md)、[启动流程](workflows/startup.md)、[会话生命周期](workflows/session-lifecycle.md) 与 [工具执行](workflows/tool-execution.md)。
