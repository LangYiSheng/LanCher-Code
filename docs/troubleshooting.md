# 故障排查

> 本文只收录**能从当前源码合理推断**的问题与排查路径。真实错误日志在 `~/.lancher/logs/lancher-error.log`。

## 安装与启动

### 模块导入失败 / 找不到 `lancher_code`

| 现象 | 原因与处理 |
|---|---|
| `ModuleNotFoundError: No module named 'lancher_code'` | 当前环境未安装包。执行 `uv sync`，或确认在仓库根目录运行 `python main.py`（源码目录在 `PYTHONPATH` 上） |
| `Python 3.14 required` 类报错 | 版本低于 3.14。升级 Python 或换环境（`pyproject.toml` 硬性要求） |

### 启动即退出 / 打印 `[错误] ...`

配置非法（`ConfigError`）时程序以退出码 1 退出。常见原因：

| 错误信息特征 | 原因 |
|---|---|
| `provider 配置缺失或格式不正确` | `provider` 节缺失或不是对象 |
| `xxx 是必填字符串` | `protocol / model / base_url / api_key` 缺一不可（见 `config/loader.py`） |
| `protocol 必须是以下值之一: openai, claude` | 协议名拼写错误 |
| `配置文件不是合法的 YAML` | YAML 语法错误（缩进/引号），检查 `~/.lancher/lancher.yaml` |
| `runtime.tool_loop_limit 必须是正整数` | 配置了 0 或负数 |
| `配置文件不存在` | `load_config` 直接调用时路径缺失（正常启动前会先走引导） |

修复后重新启动。若不确定字段，对照 `lancher.example.yaml`。

### 首次启动没有出现引导界面

`needs_setup = ~/.lancher/lancher.yaml 不存在`。如果该文件存在（哪怕是空的），就不会进引导。检查：

```powershell
Test-Path $HOME\.lancher\lancher.yaml
```

### 引导界面保存失败

- API Key 为空 → `api_key 是必填字符串`
- 超时/thinking budget 填了非正数 → 对应校验错误
- 保存成功但界面无响应 → 检查 `$HOME\.lancher` 目录是否可写（日志文件创建失败会回退 stderr，但配置写入失败会直接报错）

## 配置相关

### 环境变量没有生效

- Provider 字段（model/base_url/api_key）用 `os.path.expandvars`，**未定义的变量原样保留**，不会报错——若配置里还是 `${XXX}` 字面量，说明环境变量没设置。
- MCP 的 `env` / `headers` 用严格展开，**缺失变量会使该 Server 校验失败**（只影响该 Server，横幅会显示 issue）。

### 权限规则不生效

| 现象 | 检查点 |
|---|---|
| 规则总是命中不了 | 匹配格式是否正确：`RunCommand(git *)` / `WriteFile(.env)` / `mcp__server__tool`；路径是否为项目相对路径（正斜杠、小写）；glob 用 `* ? [` |
| 项目规则不生效 | 检查 `format_version: 2`、`result: deny`、匹配方式及实际 cwd；任意匹配的拒绝规则都生效 |
| 完全访问下仍被拒绝 | 正常：只读阶段、极危险命令和显式 `deny` 仍生效 |
| 项目外文件要求确认 | 项目访问模式对外部读写审批；可选择本次或会话目录授权，完全访问自动允许。讨论／计划不能普通写入 |

### 修改配置后不生效

- `runtime.work_phase` / `runtime.execution_access` 控制启动默认值；运行中可用 `/discuss`、`/plan`、`/do` 和 `/access` 分别修改。忙碌时先完成或停止任务
- 模型目录与权限规则保存后立即生效，MCP 保存后由核心立即重新应用；直接修改 YAML 或应用失败时使用 `/mcp reload`。只修改默认模型不会替换当前会话所选模型；需要立即改用另一模型时执行 `/model`。

## 运行问题

### 模型无响应 / 一直 "Waiting for model response"

排查顺序：

1. **网络与地址**：`base_url` 是否可达、协议是否匹配（openai/claude 的 URL 与 Header 不同，见 [modules/providers.md](modules/providers.md)）
2. **认证**：401/403 → `ProviderAuthError`，检查 `api_key`
3. **超时**：请求超时看 `provider.timeout_seconds`（默认 60）
4. **日志**：`~/.lancher/logs/lancher-error.log` 中 `event=provider_*` 相关记录
5. 上下文过长：模型返回上下文超长错误 → 自动紧急压缩；若压缩失败且接近窗口上限，会话可能报 `ContextCompactionError`

### 还在思考就结束 / 正文只输出了一部分

先查看消息时间线中的警告或错误，再结合本次请求的用量账本检查 `stop_reason` 与实际发送的 `max_output_tokens`。现在这些字段随请求保存；旧日志没记录的停止原因不能仅根据界面现象补成事实。

| 现象或停止原因 | 当前处理与检查点 |
|---|---|
| `length` / `max_tokens` / `max_output_tokens`，或已上报输出达到实际额度 | 显示额度耗尽警告，保留思考与部分正文并自动续接；不会直接标成完成 |
| 提示“已自动续接 3 次，但模型仍达到输出额度” | 本轮尚未完成，已有内容保留。检查模型 `max_output_tokens` 与端点支持上限；可调整 `runtime.output_continuation_limit` 或缩小单次任务 |
| 静默 EOF / 未收到完整响应 | 请求记为 `incomplete`，不自动重试，不执行未提交的工具，消息区保留明确错误 |
| 网络异常 / 供应商错误 | 请求记为 `failed`，保留已有内容并明确提示；先检查连接与日志，再决定后续操作 |
| 用户停止 | 显示持久“已停止”提示，已有正文继续保留 |

普通请求缺省单次总输出额度为 `32768` token，摘要为 `16384`；小窗口按比例适配。思考、正文和工具参数共用这份额度，因此模型还在思考时也可能达到上限。`context_window` 是整个请求容量，不能代替独立的模型输出额度设置；显式 `max_output_tokens` 还须符合供应商的限制。当前额度耗尽默认最多连续自动续接 `3` 次，每次续接前重走输入预算检查，接近输入阈值时自动压缩。

被截断的工具批次整体不执行。自动续接会让模型依据未执行反馈重新生成完整调用，不会把半段 JSON 补上括号后直接执行，也不会自动重放网络断流期间可能已经开始的操作。详细边界见 [思考协议、工具截断与会话恢复](workflows/thinking-and-tool-recovery.md)。

警告、错误与停止在消息区统一使用持久提示，级别独立保存。收起思考区不会隐藏这些提示，恢复会话后仍可查看；部分正文保留，同一原因与文本不重复显示，错误内容也支持 Markdown。

### 关闭思考显示，是否关闭了模型推理

`ui.show_thinking_status` 只改变界面显示，不改变请求、模型能力或保存的思考协议。不同模型可以使用固定推理策略、默认推理或端点支持的可调策略，不能把界面开关理解成所有模型都支持的“深度思考开关”。

聊天用 `/effort <档位>` 切换当前对话的强度，默认 `auto`，从下一条消息开始沿用；消息正文单独发送，设置页不再提供强度选项。在供应商设置中“发现模型”可读取实际能力，设置页会注明信息获取时间。支持自适应推理的声明触发 adaptive，未知字段沿用服务端默认；没有型号、归属或域名推断。未声明档位范围时，显式档位由端点校验。需要 Responses 的 OpenAI 端点可在高级连接选项中明确选择。旧 `thinking.enabled`、`thinking.budget_tokens` 和模型 `reasoning_effort` 读取时忽略、保存时移除；界面的思考显示开关仍只影响显示。详见 [模型发现](workflows/model-discovery.md)及[配置](configuration.md#自动推理与-effort)。

### 工具执行失败（结构化错误）

| error_code | 含义与处理 |
|---|---|
| `tool_not_found` | 工具未注册或 MCP 工具未加载。MCP 工具需先 `tool_search` |
| `phase_disallowed` | 当前阶段不允许该操作（如讨论／计划中的普通写入或无法确认只读的 Shell）；会话目录授权与完全访问均无法突破只读阶段 |
| `invalid_access_target` / `permission_target_changed` | 路径参数无效，或审批等待期间实际目标已改变；检查路径并重新请求执行 |
| `stale_file_state` / `incomplete_file_read` / `file_changed_since_read` | 防盲写守卫：先 `read_file`（完整读取），文件被外部改过就重读 |
| `large_file_requires_paging` | read_file 大文件需要 `offset` + `limit` |
| `match_not_found` / `match_not_unique` | edit_file 的 `old_text` 找不到或匹配多次，提供更精确原文 |
| `permission_user_denied` / `permission_blacklist_denied` / `phase_disallowed` | 权限拒绝。检查审批决议或工作阶段；黑名单与阶段限制不能靠允许规则绕过 |
| `runtime_limit` | 进程达到本次设置的 `max_runtime_ms`，已执行停止；增大运行期限前先确认是否卡住 |
| `output_limit` | 输出达到磁盘额度，已停止进程，已保存日志保留；检查是否无限打印 |
| `process_error` | 启动或控制失败，检查所属 Session、工作目录、系统后端与错误正文 |
| `non_zero_exit` | 命令退出码非零；结合输出判断原因，不根据命令名称推断成功 |

### 命令启动失败或后台任务不往下走

Windows 后端从系统目录使用 PowerShell，通过 Job Object 托管；ConPTY 要求系统支持对应 API。POSIX 后端使用 `/bin/sh`。先检查错误正文，不以后台工具返回句柄作为命令最终成功的证据。

如果连 `python --version` 都返回「执行范围已停止，不能启动新进程」，不要把它当成需要额外批准的提示。早期工具运行时重构版本存在真实 TUI 立即调度与轮次初始化的竞态：事件日志里的 `turn.started` / `invocation.queued` 会带空 `turn_id`，首轮结束后后续命令被同一个已停止范围拦截。更新到修复版后退出并重新启动应用即可使用原 Session，日志和对话无需删除；停止范围只存在于旧进程的内存中。实现说明见 [轮次先绑定再启动](workflows/tool-execution.md#为什么轮次要先绑定再启动)。

默认未知命令只在本次启动调用期间项目独占，返回后台任务后释放临时锁；先启动服务器、再运行 HTTP 请求或文件工具不需要额外 profile。后台命令仍可能自己写文件，这个行为不构成沙箱。若配置了 `lifetime=process` 的持续独占资源，后续冲突调用会显示「等待资源」，表示审批已通过、工具尚未启动。展开详情查看真实阻塞进程及所属 Session，在那个会话用 `/tasks show <完整进程UUID>` 查看或停止它；也可重新审视已知脚本的资源范围。`process_read/list/stop` 管理入口不会被目标进程的项目锁挡住。

等待超时不停止进程，真实运行期限才停止。重启后看到 `lost` 表示应用没有重新接管旧进程；它不会自动重跑，日志仍可阅读。停止后队列保持暂停，需要用户明确继续。

### 连续"未知工具"导致本轮停止

模型连续 `unknown_tool_streak_limit`（默认 3）次请求未加载工具，本轮被熔断。这通常是模型需要 MCP 工具但没先 `tool_search`——可在提示词层面引导，或调大配置 `runtime.unknown_tool_streak_limit`。

### 会话相关

| 现象 | 原因与处理 |
|---|---|
| `/session save` 或 `--force` 报参数错误 | 首条消息已自动保存；使用 `new/list/resume/rename/archive/remove` 与完整 UUID |
| `/session resume` 报会话正在使用 | 同一 Session 只有一个写入者；先退出另一进程或在那里切换到新对话 |
| 恢复会话报"该会话不属于当前项目" | 会话文件是项目绑定的，在保存它的那个目录下恢复 |
| 会话事件损坏或序号不连续 | 先保留 `.lancher/sessions/<UUID>/` 备份；中间完整记录不能自动修复，确认不再需要后用 `/session remove <UUID>` 删除 |

## MCP 相关

| 现象 | 检查点 |
|---|---|
| 横幅显示 `MCP：初始化完成 · 成功 n/m · x 个失败` | 查看 `~/.lancher/logs/lancher-error.log` 中 `event=mcp_server_initialization_failed` 与 `MCPConfigIssue` |
| Server 配置校验失败 | `type` 必须是 `stdio`/`http`；stdio 要 `command`，http 要合法 `url`；`env`/`headers` 中的 `${VAR}` 环境变量必须存在 |
| 工具调用报"尚未加载" | 需先 `tool_search` 加载（延迟加载设计） |
| 工具名非法/冲突 | 名称只能含字母数字 `_` `-`；不同 Server 同名工具会冲突并跳过 |
| 初始化一直转圈 | 单 Server 连接超时默认 10 秒；确认命令/URL 可用 |

## 日志相关

| 现象 | 处理 |
|---|---|
| 找不到日志文件 | 日志目录不可写时回退 stderr（`event=logging_initialization_failed fallback=stderr`） |
| 日志里看到 `[REDACTED]` | 正常脱敏行为，不是 bug |
| 想查更细日志 | 当前仅 ERROR 级；可临时改 `logging_system.py` 的级别调试，勿提交 |

## 常见误区

- **`lancher.yaml` 与 `lancher.example.yaml` 的区别**：前者是本地真实配置（含密钥，被 gitignore），后者是示例（无密钥）。
- **`docs/` 被 gitignore**：仓库当前约定不把 `docs/` 纳入版本控制（见 `.gitignore`）。
- **CLI 没有参数**：`--help` 只有程序名与描述；任何参数都会被解析但无效果。
- **旧格式需要重新配置**：主配置只读 `~/.lancher/lancher.yaml`。旧 `provider` / `permission_mode` 字段、缺少 `match_kind` 的权限规则均明确报错并保留原文件，不自动迁移。会话格式只接受版本 3；列表会单独报告旧格式或损坏文件，使用 `/session new` 建立新会话。
