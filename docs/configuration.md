# 配置

LanCher Code 的配置来源有：**YAML 配置文件**、**环境变量**（通过 `${VAR}` 展开引用）、以及**首次引导界面**（写入配置文件）。当前版本**没有 CLI 参数**（见 [cli-and-interaction.md](cli-and-interaction.md) 中的说明）。

## 配置文件位置

| 文件 | 用途 | 生成方式 |
|---|---|---|
| `~/.lancher/lancher.yaml` | 全局主配置（Provider / UI / Runtime） | 首次启动引导界面写入，或手动创建 |
| `~/.lancher/permissions.yaml` | 用户级权限规则 | 首次产生用户级规则时自动写入 |
| `~/.lancher/mcp.yaml` | 全局 MCP Server 配置 | 引导界面保存时自动生成模板（`mcp/template.py`） |
| `./.lancher/permissions.yaml` | 项目级权限规则（`./` 为当前工作目录） | 首次产生项目级规则时自动写入 |
| `./.lancher/mcp.yaml` | 项目级 MCP Server 配置 | 手动创建 |
| `./.lancher/plan.md` | Plan Mode 计划文件（默认路径，可配置） | `write_plan_file` 工具写入 |

路径常量定义在 `lancher_code/config_system/paths.py`。

> **旧配置路径说明**：`config_system/paths.py` 保留了 `get_legacy_config_path()`（返回 `cwd/lancher.yaml`），`ConfigBootstrapState` 也会记录该路径是否存在，但当前 `app.py` 的加载流程**只读取全局配置**，未使用旧路径。该字段为预留设计。

## 主配置 `lancher.yaml`

顶层结构为 `provider`、`ui`、`runtime` 三节。解析与校验逻辑在 `lancher_code/config_system/loader.py`。

### `provider`（必填节）

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `provider.protocol` | str | - | 是 | `openai` 或 `claude`，二选一 |
| `provider.model` | str | - | 是 | 模型名称，支持 `${ENV}` 展开 |
| `provider.base_url` | str | - | 是 | API 地址，支持 `${ENV}` 展开，加载时去掉末尾 `/` |
| `provider.api_key` | str | - | 是 | API 密钥，支持 `${ENV}` 展开。**只写变量名，不要写真实密钥** |
| `provider.timeout_seconds` | float | `60.0` | 否 | 单次请求超时秒数，必须为正数 |
| `provider.context_window` | int | `128000`（openai）/ `200000`（claude） | 否 | 上下文窗口大小，用于压缩阈值估算 |
| `provider.thinking.enabled` | bool | `false` | 否 | 是否启用 Claude thinking（仅 claude 生效） |
| `provider.thinking.budget_tokens` | int | 无（未设置时请求阶段默认 2048） | 否 | thinking 预算 token，必须为正整数 |

### `ui`

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `ui.show_timestamps` | bool | `false` | 否 | 是否显示消息时间戳（当前界面实现中尚未使用该开关的渲染逻辑） |
| `ui.show_thinking_status` | bool | `true` | 否 | 是否显示思考轨迹折叠区 |

### `runtime`

| 配置项 | 类型 | 默认值 | 必填 | 作用 |
|---|---|---|---|---|
| `runtime.tool_loop_limit` | int | `50` | 否 | 单轮对话最大工具循环次数 |
| `runtime.unknown_tool_streak_limit` | int | `3` | 否 | 连续请求未知工具达到该次数即停止本轮 |
| `runtime.plan_file_path` | str | `./.lancher/plan.md` | 否 | Plan Mode 计划文件路径（相对路径基于启动时 cwd 解析） |
| `runtime.permission_mode` | str | `default` | 否 | 启动时的权限模式：`default` / `plan` / `acceptEdits` / `bypass` |

### 环境变量展开

- Provider 字段：`model`、`base_url`、`api_key` 使用 `os.path.expandvars` 展开（`${NAME}` 或 `$NAME` 形式），未定义的环境变量会**原样保留**（例如 `${TEST_OPENAI_KEY}` 未被替换时不报错，见 `tests/test_config.py`）。
- MCP 的 `env` / `headers` 值：使用 `mcp/config.py` 的正则 `\${NAME}` 展开，**缺失的环境变量会导致该 Server 配置校验失败**（仅该 Server 被跳过，见 `mcp/config.py` `_expand_map`）。

## 权限规则文件

`permissions.yaml` 顶层为 `rules` 数组，每项 `{match, result}`：

```yaml
rules:
  - match: "Bash(git *)"
    result: allow
  - match: "WriteFile(.env)"
    result: deny
```

三层规则优先级：**session > project > user**（session 层仅内存，不落盘；project / user 层落盘）。

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

- **模型设置**：协议、模型名、Base URL、API Key（留空不覆盖原值）、超时、thinking
- **MCP 服务器**：全局 / 项目两层，增删改 Server
- **项目权限 / 全局权限**：增删改、上下移规则

保存逻辑（`SettingsService.save()`）：先做**全量校验**，再以"临时文件 + 原子替换"方式批量写盘，最后才热切换内存中的权限规则。模型与 MCP 的修改提示**重启后生效**。

## 日志

| 项目 | 值 | 依据 |
|---|---|---|
| 日志级别 | ERROR（仅记录错误） | `logging_system.py` `logger.setLevel(logging.ERROR)` |
| 默认路径 | `~/.lancher/logs/lancher-error.log` | `config_system/paths.py` |
| 滚动策略 | 单文件最大 5 MB，保留 5 个备份 | `DEFAULT_MAX_BYTES` / `DEFAULT_BACKUP_COUNT` |
| 脱敏 | 注册的敏感值替换为 `[REDACTED]`；Authorization / Bearer / api_key / token 等模式自动脱敏 | `register_sensitive_values()` + `RedactingFormatter` |
| 降级 | 日志目录不可写时回退到 stderr | `configure_logging()` |

程序启动时会把 `api_key` 及 MCP 配置中的 env/headers 值注册为敏感值，避免泄漏到日志。
