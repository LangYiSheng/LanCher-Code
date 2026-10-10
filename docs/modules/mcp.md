# 模块：MCP（Model Context Protocol）

MCP 是 LanCher Code 接入外部工具的方式。本轮范围为 Tools，支持 stdio 与 Streamable HTTP；服务器声明的其他能力可显示在状态中，但尚无 Resources、Prompts 或 OAuth 的调用入口。工具返回的资源链接和嵌入文本属于结果内容，可以保留。

## 核心边界

```text
AgentCapabilities（agent/capabilities.py）
  → MCPClientManager：初始化、状态、刷新、重连与关闭
  → MCPServerConnection：传输、握手、工具分页与变更通知
  → MCPToolAdapter → ToolRegistry → ToolExecutor
```

`app.py` 装配依赖，`TurnRunner.capabilities` 提供公共管理入口。TUI 订阅进度并调用门面，不拥有连接任务、工具发现或请求投影逻辑。退出由智能体核心统一收尾。

## 配置与超时

- 全局：`~/.lancher/mcp.yaml`。
- 项目：`./.lancher/mcp.yaml`；同名 Server 整项覆盖全局，不逐字段合并。
- `stdio` 使用 `command`、`args`、`env`；`http` 使用 `url`、`headers`。
- `env`、`headers` 支持 `${VAR}`。保存保留原文，加载启用项时展开；缺少变量只使该项失败。
- 启动与设置共用 `validate_server_config()`。非法项目覆盖单独报告，不回退同名全局项。

每个 Server 可设置有限正数的超时，单位为秒：

| 字段 | 默认值 | 范围 |
|---|---|---|
| `startup_timeout_seconds` | 30 | 连接、握手、完整工具发现，以及手动工具目录刷新 |
| `tool_timeout_seconds` | 60 | 远端工具调用，独立于普通本地工具超时 |
| `close_timeout_seconds` | 5 | 连接关闭，超时后取消连接任务 |

在 `/settings` 保存 MCP 后，界面调用核心立即重新应用配置，关闭旧连接并更新注册表。文件保存成功与运行时应用失败分别报告；直接修改 YAML 后使用 `/mcp reload`。详细格式见 [配置说明](../configuration.md)。

## 后台初始化与动态目录

核心 `start()` 安排后台初始化，Server 并行连接；输入可立即使用内置能力。MCP 完成连接后逐页发现工具，再按 Server 替换整组延迟工具。分页游标重复会明确失败，防止无限循环。单个 Server 的连接或注册问题隔离处理。

远端支持 `tools.listChanged` 时，通知触发后台目录刷新。接收通知的任务只安排刷新，不在接收任务内等待 RPC；同 Server 的刷新串行处理。刷新会更新新增、修改和删除的工具，断开时注销不可用的目录，重连重新发现。

`MCPClientManager.status()` 提供连接类型、状态、工具数量、服务器能力、提示数量及最近错误。主要状态为 `waiting`、`connecting`、`ready`、`refreshing`、`failed`、`disconnected`、`closed`，用于 `/mcp` 管理界面和状态展示。

| 命令 | 作用 |
|---|---|
| `/mcp`、`/mcp list` | 查看服务器与连接状态 |
| `/mcp refresh [服务器名]` | 重读工具目录；省略名称刷新全部 |
| `/mcp reconnect <服务器名>` | 关闭该连接并重新连接、发现工具 |
| `/mcp reload` | 重读全局与项目配置并立即应用 |

管理变更要求智能体空闲；连接失败不会自动重放远端工具操作。

## 延迟加载与搜索

工具可见名为 `mcp__<server>__<tool>`。默认只提供有预算的 Server／工具索引，完整参数 Schema 按需加载。

模型先调用 `tool_search`：完整名称用 `select:<完整工具名>` 精确选择，其他查询按名称、Server 和用途描述排序。每次加载前 8 个候选，更多匹配以 `has_more` 提示，模型可缩小查询；超过 8 个不再整批拒绝。下一次请求附上已发现工具的完整定义。

尚未发现、已删除或断开的工具由执行器拦截；在阶段变化、工具刷新及执行前重新核对当前定义。索引预算只影响展示，不删除真实注册工具，未列出的工具仍可搜索。

## 执行、输出与结果未知

| 项目 | 行为 |
|---|---|
| 阶段 | 明确 `readOnlyHint=true` 的工具允许 discuss／plan／execute；其他工具仅 execute |
| 参数 | 保留远端 JSON Schema，由统一工具入口校验 |
| 权限 | 外部工具身份与可见名用于规则匹配，沿用审批策略 |
| 调度 | 未知远端副作用保守占用项目；只读声明不作为并发安全证明 |
| 文本结果 | 保留文本、资源链接与嵌入文本资源 |
| 结构化结果 | 保存 `structuredContent` 并投影为 JSON；提供 `outputSchema` 时验证 |
| 非文本结果 | 标记未投影的块类型；目前不把图像、音频或二进制内容传给模型 |

输出 Schema 使用离线校验器，支持本地 `$defs`／`$ref`；外部引用不触发网络请求或本地文件读取。远端已经返回响应但结构化输出不符合 Schema 时，返回 `mcp_output_invalid`，与运输失败分开，不自动重放。

外部写操作开始后，本地取消、超时或连接故障不能证明远端失败或回滚。结果保持 `mcp_outcome_unknown`，禁止自动重试，需要先查询远端真实状态。审批或资源排队期间取消则明确未执行；服务器确认返回错误时，记录远端失败。

## 实现与验证

`mcp/config.py` 负责配置，`connection.py` 负责协议连接，`manager.py` 管理目录与生命周期，`adapter.py` 投影工具定义和结果，`template.py` 创建全局配置模板。Server 与工具名称只允许字母、数字、`_`、`-`，非法工具与单项 Schema 问题隔离报告。

`tests/mcp/` 包含真实 stdio 测试服务器、连接和管理测试。目录替换、搜索、阶段与执行身份复核另在工具和智能体测试中验证。
