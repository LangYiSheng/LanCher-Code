# 模块：MCP（Model Context Protocol）

## 作用

MCP 是 LanCher Code 接入外部工具的标准方式。通过 MCP，你可以把任何实现了 MCP 协议的服务（文件系统、GitHub、数据库等）挂进对话，模型就能调用它的远程工具。

实现位置：`lancher_code/mcp/`。

## 目录结构

```text
mcp/
├── config.py      # MCPServerConfig 加载与校验（stdio / http）
├── connection.py  # MCPServerConnection：连接生命周期（后台任务）
├── manager.py     # MCPClientManager：并发初始化、注册工具、进度事件
├── adapter.py     # MCPToolAdapter：远程工具 → 本地 Tool
└── template.py    # 全局 mcp.yaml 模板生成
```

## 配置

- 全局：`~/.lancher/mcp.yaml`
- 项目：`./.lancher/mcp.yaml`（同名 Server 完整覆盖全局）
- 支持两种类型：
  - `stdio`：`command` + `args` + `env`（用本机进程启动，如 `npx -y @modelcontextprotocol/server-filesystem`）
  - `http`：`url` + `headers`（Streamable HTTP）
- `env` / `headers` 值支持 `${VAR}` 环境变量展开，缺失环境变量会使该 Server 校验失败（只跳过该 Server）
- 格式与校验见 [configuration.md](../configuration.md) 的"MCP 配置"一节

## 初始化流程（`MCPClientManager.initialize`）

```text
load_mcp_config(cwd) 合并全局+项目配置
→ MCPClientManager(configs, issues, timeout_seconds)
→ 应用启动（ChatTUI on_mount）触发 initialize(registry)
→ 所有 Server 并行 _discover_safely：
     连接（stdio 起进程 / http 建客户端）
     → initialize 握手 → list_tools 列出工具
→ 每个远程工具注册为 MCPToolAdapter：
     名称：mcp__<server>__<tool>
     should_defer=True（延迟加载，不随默认工具集暴露）
→ 失败不拖垮其他 Server；问题记录为 MCPConfigIssue 并展示
→ 全部完成后发 progress "complete"
```

初始化进度通过 `MCPInitializationProgress` 回调推送给横幅（`BannerWidget`）与状态栏；初始化期间输入框禁用。

## 延迟加载机制

- 注册的 MCP 工具默认**不包含在模型请求的工具列表**中（`should_defer=True`），只以 `<deferred_tools>` 索引（Server 名 + 工具名列表）出现在 system 提示里。
- 模型需要时先调用 `tool_search`（关键词或 `select:<完整工具名>`）搜索加载；返回的 `discovered_tool_names` 会在下一轮模型请求中带上完整参数定义。
- 直接调用未加载工具会被 `ToolExecutor` 拦截：`tool_not_found`，提示"请先调用 tool_search"。

## 工具适配（`MCPToolAdapter`）

| 属性 | 值 |
|---|---|
| 可见名 | `mcp__<server>__<tool>` |
| 分类 | 远程标注 `readOnlyHint` → read，否则 command |
| 并发安全 | 只读工具并发安全 |
| 权限 | `source="external"`，规则键为可见名；模式矩阵：default=ask、plan 非只读=deny、bypass=allow |

调用通过 `MCPServerConnection.call_tool()` 转发；返回内容只保留 `TextContent`，其他块类型标记 `[已忽略非文本 MCP 内容: <类型>]`。

## 连接生命周期（`MCPServerConnection`）

- 每个 Server 一个后台任务（`asyncio.Task`，名 `mcp-<server>`）
- 状态机：`waiting → connecting → registering → ready / failed`
- 退出时 `MCPClientManager.close()` 统一关闭（默认 5 秒超时，超时则取消任务）

## 与其他模块的关系

- ← `app.py`：`load_mcp_config(cwd)` + 构造 manager；`tui.configure_mcp(manager, registry)`
- → `tools/core/registry.py`：注册延迟工具与 Server 元数据
- → `tui_views/message.py`（横幅 MCP 状态）、`tui_views/chat.py`（初始化门控）
- → `permission_engine.py`：外部工具权限判定（`source="external"`）
- → `settings_service.py` / `tui_views/settings.py`：MCP 配置编辑

## 注意事项

- Server 名只能含字母、数字、`_`、`-`；远程工具名同样受限（`TOOL_NAME_PATTERN`），非法名称只记录 issue 不注册。
- 同名工具注册冲突（不同 Server 同名工具）会记录 `duplicate_tool` issue 并跳过。
- 测试目录 `tests/mcp/` 含一个真实 stdio 测试服务器（`stdio_test_server.py`）与全套 mock 测试。
