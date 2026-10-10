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
| `xxx 是必填字符串` | `protocol / model / base_url / api_key` 缺一不可（见 `config_system/loader.py`） |
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
| 项目规则不生效 | 检查文件在 `./.lancher/permissions.yaml`（不是 `~/.lancher/`）；注意**同一作用域内最后一条命中规则生效**，后面的规则会覆盖前面的 |
| bypass 策略下仍被拒绝 | 正常：工作阶段、危险命令黑名单、路径限制与显式 `deny` 规则依然生效 |
| 文件写入总被要求确认 | 当前 Session workspace 已批准；其他项目文件在执行阶段按 `default` 询问写入，可选择规则或 `acceptEdits`。讨论／计划阶段源码只读 |

### 修改配置后不生效

- `runtime.work_phase` / `runtime.permission_policy` 控制启动默认值；运行中可用 `/discuss`、`/plan`、`/do` 和 `/permissions` 分别修改。忙碌时先完成或停止任务
- 模型目录与权限规则保存后立即生效，MCP 配置仍需重启。只修改默认模型不会替换当前会话所选模型；需要立即改用另一模型时执行 `/model`。

## 运行问题

### 模型无响应 / 一直 "Waiting for model response"

排查顺序：

1. **网络与地址**：`base_url` 是否可达、协议是否匹配（openai/claude 的 URL 与 Header 不同，见 [modules/providers.md](modules/providers.md)）
2. **认证**：401/403 → `ProviderAuthError`，检查 `api_key`
3. **超时**：请求超时看 `provider.timeout_seconds`（默认 60）
4. **日志**：`~/.lancher/logs/lancher-error.log` 中 `event=provider_*` 相关记录
5. 上下文过长：模型返回上下文超长错误 → 自动紧急压缩；若压缩失败且接近窗口上限，会话可能报 `ContextCompactionError`

### 工具执行失败（结构化错误）

| error_code | 含义与处理 |
|---|---|
| `tool_not_found` | 工具未注册或 MCP 工具未加载。MCP 工具需先 `tool_search` |
| `phase_disallowed` / `mode_disallowed` | 当前阶段不允许该工具（如讨论／计划中的普通写入和 Shell）；规则与 bypass 无法放开 |
| `path_outside_project` | 路径越出项目根（路径沙箱）。确认路径在 cwd 内 |
| `stale_file_state` / `incomplete_file_read` / `file_changed_since_read` | 防盲写守卫：先 `read_file`（完整读取），文件被外部改过就重读 |
| `large_file_requires_paging` | read_file 大文件需要 `offset` + `limit` |
| `match_not_found` / `match_not_unique` | edit_file 的 `old_text` 找不到或匹配多次，提供更精确原文 |
| `permission_user_denied` / `permission_blacklist_denied` / `permission_mode_denied` | 权限拒绝。调整模式、加规则或换命令 |
| `runtime_limit` | 进程达到本次设置的 `max_runtime_ms`，已执行停止；增大运行期限前先确认是否卡住 |
| `output_limit` | 输出达到磁盘额度，已停止进程，已保存日志保留；检查是否无限打印 |
| `process_error` | 启动或控制失败，检查所属 Session、工作目录、系统后端与错误正文 |
| `non_zero_exit` | 命令退出码非零；结合输出判断原因，不根据命令名称推断成功 |

### 命令启动失败或后台任务不往下走

Windows 后端从系统目录使用 PowerShell，通过 Job Object 托管；ConPTY 要求系统支持对应 API。POSIX 后端使用 `/bin/sh`。先检查错误正文，不以后台工具返回句柄作为命令最终成功的证据。

未知命令长期持有项目独占资源时，其他文件工具会排队，这是保守调度的结果。停止进程，或为确实已知的开发脚本配置合适的 `execution.command_profiles`。`process_read/list/stop` 管理入口不会被目标进程的项目锁挡住。

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
- **旧配置路径未启用**：`cwd/lancher.yaml`（legacy path）在 `ConfigBootstrapState` 中被记录，但当前加载流程只读全局配置。
