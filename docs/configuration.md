# 配置

LanCher Code 的配置来源有：**YAML 配置文件**、**环境变量**（通过 `${VAR}` 展开引用）、以及**首次引导界面**（写入配置文件）。当前版本**没有 CLI 参数**（见 [cli-and-interaction.md](cli-and-interaction.md) 中的说明）。

## 配置文件位置

| 文件 | 用途 | 生成方式 |
|---|---|---|
| `~/.lancher/lancher.yaml` | 全局主配置（供应商目录 / 默认模型 / UI / Runtime） | 首次启动引导界面或设置页写入，也可手动创建 |
| `~/.lancher/permissions.yaml` | 用户级权限规则 | 首次产生用户级规则时自动写入 |
| `~/.lancher/mcp.yaml` | 全局 MCP Server 配置 | 引导界面保存时自动生成模板（`mcp/template.py`） |
| `./.lancher/permissions.yaml` | 项目级权限规则（`./` 为当前工作目录） | 首次产生项目级规则时自动写入 |
| `./.lancher/mcp.yaml` | 项目级 MCP Server 配置 | 手动创建 |
| `./.lancher/sessions/<UUID>/workspace/plan.md` | 当前 Session 计划文件 | 首条消息建立工作目录，计划工具写入 |

路径常量定义在 `lancher_code/config_system/paths.py`。

> **旧配置路径说明**：`config_system/paths.py` 保留了 `get_legacy_config_path()`（返回 `cwd/lancher.yaml`），`ConfigBootstrapState` 也会记录该路径是否存在，但当前 `app.py` 的加载流程**只读取全局配置**，未使用旧路径。该字段为预留设计。

## 主配置 `lancher.yaml`

顶层结构为 `providers`、`default_model`、`ui`、`runtime`。解析与校验逻辑在 `lancher_code/config_system/loader.py`，模型继承解析在 `lancher_code/model_catalog.py`。

### 供应商目录与默认模型

`providers` 是按稳定 ID 索引的供应商对象，每个供应商的 `models` 也是按稳定 ID 索引的对象。供应商名称、API 模型名、模型显示名称都可以修改，ID 不随改名变化。`default_model` 使用 `供应商ID/模型ID`，用于新会话启动。

```yaml
providers:
  deepseek:
    name: DeepSeek
    protocol: openai
    base_url: https://api.deepseek.com/v1
    api_key: ${DEEPSEEK_API_KEY}
    models:
      chat:
        model_name: deepseek-chat
        display_name: 日常编程
      reasoner:
        model_name: deepseek-reasoner
default_model: deepseek/chat
```

供应商与模型 ID 只能含中文、字母、数字、下划线、短横线。配置必须包含可解析的默认模型；可以保留暂时没有模型的供应商。相同 API 模型名可存在于不同供应商下，通过完整引用区分。

### `providers.<供应商ID>`

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `name` | str | - | 是 | 自定义供应商显示名称，如 DeepSeek |
| `protocol` | str | - | 是 | `openai` 或 `claude`；界面将后者显示为 Anthropic |
| `base_url` | str | - | 是 | 公共 API 基址，支持环境变量；有效值去掉末尾 `/` |
| `api_key` | str | - | 是 | 公共密钥，支持 `${ENV}`；示例采用环境变量引用 |
| `timeout_seconds` | float | `60.0` | 否 | 公共请求超时，必须为有限正数 |
| `models` | object | `{}` | 否 | 该供应商下的模型目录 |

### `providers.<供应商ID>.models.<模型ID>`

| 配置项 | 类型 | 缺省行为 | 作用 |
|---|---|---|---|
| `model_name` | str | 必填 | 请求 API 使用的模型名，支持环境变量 |
| `display_name` | str | 空字符串 | 有值时用于界面，否则显示 `model_name (供应商名称)` |
| `protocol` | str / null | 继承供应商 | 覆盖为 `openai` 或 `claude` |
| `base_url` | str / null | 继承供应商 | 单独覆盖 API 基址，支持环境变量 |
| `api_key` | str / null | 继承供应商 | 单独覆盖 API 密钥，支持环境变量 |
| `timeout_seconds` | float / null | 继承供应商 | 单独覆盖请求超时 |
| `context_window` | int / null | 按有效协议取 `128000` / `200000` | 上下文窗口，必须为正整数 |
| `thinking.enabled` | bool | `false` | 启用 Anthropic thinking，仅 `claude` 协议生效 |
| `thinking.budget_tokens` | int | 请求阶段默认 `2048` | thinking 预算，必须为正整数 |

继承按字段独立解析：修改模型的 Base URL 不会取消其密钥或协议继承。省略字段或写 `null` 表示继承，连接字段的空字符串不是继承标记。上下文窗口和 thinking 是模型设置，不属于供应商公共字段。

### 旧配置兼容与备份

旧顶层 `provider` 仍可加载，内存中映射为 `legacy/default`，供应商显示名称为“原有供应商”。首次保存改写为目录格式前，保留原文件为同目录 `lancher.yaml.bak`；已有备份不会被覆盖。只读取不会迁移文件。`provider` 和 `providers` 同时存在会报错。

### `ui`

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `ui.show_timestamps` | bool | `false` | 否 | 是否显示消息时间戳（当前界面实现中尚未使用该开关的渲染逻辑） |
| `ui.show_thinking_status` | bool | `true` | 否 | 是否显示思考轨迹折叠区 |
| `ui.theme` | str | `dark` | 否 | `dark` / `light`，统一应用于聊天与设置 |
| `ui.busy_enter_action` | str | `follow_up` | 否 | `follow_up` 排到下一轮、`steer` 补充当前任务、`draft` 保留草稿 |

### `runtime`

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `runtime.tool_loop_limit` | int | `50` | 否 | 单轮对话最大工具循环次数 |
| `runtime.unknown_tool_streak_limit` | int | `3` | 否 | 连续请求未知工具达到该次数即停止本轮 |
| `runtime.work_phase` | str | `execute` | 否 | 初始工作阶段：`discuss` / `plan` / `execute` |
| `runtime.permission_policy` | str | `default` | 否 | 权限策略：`default` / `acceptEdits` / `bypass`，不改变阶段 |

旧 `runtime.permission_mode` 仍可读取：`plan` 映射到计划阶段与标准权限，其他值映射到执行阶段与同名权限。新字段优先，保存时写入新字段。工具记录与思考显示开关独立；关闭思考显示不会隐藏工具活动。

### 环境变量展开

- 供应商与模型的 `base_url`、`api_key`，以及模型 `model_name` 在生成有效连接时使用 `os.path.expandvars` 展开（`${NAME}` 或 `$NAME` 形式）。配置目录保留变量原文；保存设置不会把密钥变量替换成环境变量值，也不会把继承值写成模型覆盖。
- 未定义的环境变量仍原样保留；已定义但展开后为空的有效连接值会报错。没有 `OPENAI_API_KEY` 等隐式覆盖，须在 YAML 中显式引用。
- MCP 的 `env` / `headers` 值：使用 `mcp/config.py` 的正则 `\${NAME}` 展开，**缺失的环境变量会导致该 Server 配置校验失败**（仅该 Server 被跳过，见 `mcp/config.py` `_expand_map`）。

## 权限规则文件

`permissions.yaml` 顶层为 `rules` 数组，每项 `{match, result, match_kind}`。`match_kind` 可为 `exact`、`glob`、`legacy`；旧文件缺少此字段时按原规则解释。新的命令授权默认 `exact`，完整命令精确匹配，不把 `*` 等字符当通配符：

```yaml
rules:
  - match: "Bash(git *)"
    result: allow
  - match: "WriteFile(.env)"
    result: deny
```

三层规则优先级：**session > project > user**（session 规则随当前 UUID Session 自动持久化，project / user 规则写入 YAML）。

阶段硬限制先于三层规则；讨论和计划阶段不开放通用 Shell 和源码修改，MCP 必须明确声明只读。当前 Session 的 `workspace/` 在所有阶段支持已批准的内置文件工具读写，计划路径由 Session 管理，不再使用 `runtime.plan_file_path`。此路径批准不构成操作系统沙箱。

### 匹配格式

| 工具 | 规则写法 | 匹配对象 |
|---|---|---|
| `bash` | `Bash(<命令>)` | 规范化后的命令文本（小写、折叠空白），支持 glob |
| `read_file` | `ReadFile(<路径>)` | 项目相对路径（正斜杠、小写） |
| `write_file` | `WriteFile(<路径>)` | 同上 |
| `edit_file` | `EditFile(<路径>)` | 同上 |
| `glob` | `Glob(<模式>)` | glob 模式本身（小写） |
| `grep` | `Grep(<路径>)` | 搜索范围路径 |
| `write_plan_file` | `WritePlanFile(<路径>)` | 计划文件相对路径 |
| MCP 工具 | `mcp__<server>__<tool>`（裸名称或 glob） | 工具可见名，如 `mcp__github__create_issue`、`mcp__github__*` |

规则书写示例见仓库根 `README.md` 与 `tests/test_permission_engine.py`、`tests/mcp/test_permission.py`。

## MCP 配置

`mcp.yaml` 顶层为 `mcp_servers` 对象：

```yaml
mcp_servers:
  filesystem:
    type: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "D:/Dev"]
    env:
      LOG_LEVEL: info
  internal_api:
    type: http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: "Bearer ${INTERNAL_MCP_TOKEN}"
```

| 配置项 | 说明 |
|---|---|
| `type` | `stdio` 或 `http`，必填 |
| `enabled` | bool，默认 `true`；`false` 时该 Server 被跳过 |
| `command` / `args` / `env` | stdio 类型必填 `command` |
| `url` / `headers` | http 类型必填 `url`（必须是合法 HTTP(S) URL） |
| 名称 | 只能包含字母、数字、`_`、`-` |

合并规则：**项目配置按名称覆盖全局配置**（`{**user, **project}`），同名项目项整体替换。任何校验失败只产生 `MCPConfigIssue`，不影响其他 Server。

## 设置面板（/settings）

运行时可以通过 `/settings` 打开 Textual 设置面板（`tui_views/settings.py` + `settings_service.py`），四个标签页：

- **模型设置**：增删改供应商及其多个模型，选择新会话默认模型；模型连接字段可独立继承或覆盖。API Key 留空保留原值，勾选“继承供应商”才会清除模型自己的密钥覆盖。模型高级选项包含上下文窗口和 thinking。
- **MCP 服务器**：全局 / 项目两层，增删改 Server
- **项目权限 / 全局权限**：增删改、上下移规则

“应用条目”只修改设置草稿，底部“保存”才写入文件。删除默认模型或其供应商前须先指定另一个默认模型；删除供应商会确认其下模型列表。

保存逻辑（`SettingsService.save()`）：先校验整个目录与其余配置，备份旧格式，再以临时文件和逐文件原子替换方式写盘，最后更新权限规则。模型目录保存后立即可用；修改默认值不覆盖当前会话已选模型。当前模型仍存在时继续使用它，并更新其连接配置；已删除时回退新默认模型并提示。MCP 的修改仍需重启生效。

聊天中使用 `/model` 展开模型候选项，Tab 或 Enter 填入后再次 Enter 切换，也可输入 `/model 供应商ID/模型ID`。仅切换当前会话，保留历史和权限，不修改新对话默认模型；首条消息之后的模型选择随 Session 自动持久化。`/settings default-model 供应商ID/模型ID` 单独修改默认值。详见 [cli-and-interaction.md](cli-and-interaction.md)。

## 日志

| 项目 | 值 | 依据 |
|---|---|---|
| 日志级别 | ERROR（仅记录错误） | `logging_system.py` `logger.setLevel(logging.ERROR)` |
| 默认路径 | `~/.lancher/logs/lancher-error.log` | `config_system/paths.py` |
| 滚动策略 | 单文件最大 5 MB，保留 5 个备份 | `DEFAULT_MAX_BYTES` / `DEFAULT_BACKUP_COUNT` |
| 脱敏 | 注册的敏感值替换为 `[REDACTED]`；Authorization / Bearer / api_key / token 等模式自动脱敏 | `register_sensitive_values()` + `RedactingFormatter` |
| 降级 | 日志目录不可写时回退到 stderr | `configure_logging()` |

程序启动和模型目录更新时会注册所有供应商及模型的密钥原文与展开值；模型切换时再次注册有效密钥。MCP 配置中的 env/headers 值也注册为敏感值。
