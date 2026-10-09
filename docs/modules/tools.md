# 模块：工具系统（Tools）

## 作用

工具系统是模型与外部世界（文件系统、shell、MCP Server）之间的执行层。它负责：

- **注册**：内置工具 + MCP 远程工具的登记
- **调度**：按并发安全分组执行、超时包装、错误归一化
- **权限**：在执行前统一过权限引擎
- **暴露**：把工具定义（名称 / 描述 / JSON Schema）交给模型

实现位置：`lancher_code/tools/`。

## 目录结构

```text
tools/
├── __init__.py        # create_default_tool_registry()：注册 8 个内置工具
├── core/
│   ├── base.py        # Tool 协议、build_tool_success / build_tool_error
│   ├── registry.py    # ToolRegistry
│   ├── executor.py    # ToolExecutor
│   ├── common.py      # 路径沙箱、SKIP_DIRS、输出上限常量
│   └── file_state_cache.py  # FileStateCache
└── builtin/           # 内置工具实现
```

## 核心接口

### `Tool`（协议，`core/base.py`）

```python
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...
    async def execute(self, arguments: dict, context: ToolContext) -> ToolExecutionResult: ...
```

- `ToolDefinition`（`models.py`）：名称、描述、参数 JSON Schema、分类（read/write/command）、并发安全、可见模式等
- `ToolContext`（`models.py`）：cwd、超时、模式、项目根、计划文件路径、取消令牌、文件状态缓存
- `ToolExecutionResult`：`call_id`、`tool_name`、`content`、`is_error`、`metadata`、`summary`、`error_code`、`error_message`

### `ToolRegistry`（`core/registry.py`）

| 方法 | 作用 |
|---|---|
| `register(tool)` | 注册工具（重名抛 ValueError） |
| `get(name)` | 按名取工具（不存在抛 `ToolNotFoundError`） |
| `list_definitions(...)` | 列出定义；支持按模式过滤、是否包含延迟工具、已发现名称 |
| `list_deferred_index()` | 按 Server 分组的延迟工具索引 |
| `search_deferred(query)` | 延迟工具搜索（`tool_search` 工具使用） |

### `ToolExecutor`（`core/executor.py`）

`execute_calls(...)` 接收独立的 `work_phase`、`permission_policy`、`should_interrupt`、计划路径、取消令牌、审批回调与可见工具集合；旧 `mode` 参数仅供兼容：

```text
对每个调用：
  · 已取消 → 抛 CancelledError
  · 不在可见工具集合 → tool_not_found（提示先 tool_search）
  · 注册表中不存在 → tool_not_found
  · 当前阶段不可用 → phase_disallowed（优先于规则与跳过询问）
  · 并发安全 → 加入 safe_batch（最后并行执行）
  · 非并发安全 → 先执行完 safe_batch，再串行执行本调用
对每个调用（_execute_one）：
  · PermissionEngine.evaluate() → deny/ask 处理
  · asyncio.wait_for(tool.execute(...), timeout) → 超时/异常归一化为错误结果
```

## 内置工具一览

| 工具 | 文件 | 分类 | 并发安全 | 可见阶段 | 说明 |
|---|---|---|---|---|---|
| `read_file` | `read_file.py` | read | 是 | 全部 | 按行读取，大文件要求分页（>400 行需 offset/limit），记录文件状态缓存 |
| `write_file` | `write_file.py` | write | 否 | execute | 全量覆盖写；覆盖已有文件前必须完整读过且文件未变（防盲写） |
| `edit_file` | `edit_file.py` | write | 否 | execute | 唯一匹配替换；old_text 必须唯一，0 次/多次匹配都报错 |
| `bash` | `bash.py` | command | 否 | execute | 执行 Windows PowerShell 命令，输出截断 12000 字符，超时 kill |
| `glob` | `glob.py` | read | 是 | 全部 | glob 查找文件，跳过 SKIP_DIRS，结果按修改时间倒序 |
| `grep` | `grep.py` | read | 是 | 全部 | 正则逐行搜索，二进制跳过，单行截断 300 字符 |
| `write_plan_file` | `write_plan_file.py` | write | 否 | **仅 plan** | 只能写配置的计划文件路径 |
| `tool_search` | `tool_search.py` | read | 是 | 全部 | 搜索/加载 MCP 延迟工具，`select:<名称>` 精确加载 |

别名的存在：`RunCommandTool = BashTool`、`ReplaceInFileTool = EditFileTool`、`FindFilesTool = GlobTool`、`SearchCodeTool = GrepTool`（兼容旧名）。

### 关键行为细节

- **read_file 大文件**：单次默认上限 400 行（`DEFAULT_MAX_INLINE_LINES`），超限且未给 `limit` 时返回 `large_file_requires_paging` 错误，提示分页。
- **write_file 防盲写**（`_guard_existing_file_write`）：覆盖已有文件要求 ① 之前用 read_file 读过 ② 是完整读取 ③ mtime 未变化；否则返回 `stale_file_state` / `incomplete_file_read` / `file_changed_since_read`。
- **edit_file 一致性**：基于缓存内容匹配，mtime 变化则拒绝。
- **bash 特例**：退出码非零视为错误（`non_zero_exit`），但 `grep/find/diff/rg/fc/select-string` 与 `git diff` 视为"可能正常非零"（>2 才报错）。讨论与计划一律禁止通用 Shell。
- **补充中断**：执行前及并发组边界检查 `should_interrupt`，尚未启动的调用补齐 `steering_superseded` 结果；已启动的操作等待结束。
- **glob/grep 输出上限**：模型侧 800 条路径 / 400 条匹配，UI 侧 200 条，字符上限 24000。

## 文件状态缓存（`core/file_state_cache.py`）

`FileStateCache` 记录每个文件最近一次读取/写入的状态（路径、mtime、内容、是否完整读取）。write_file / edit_file 的"先读后写"守卫依赖它。缓存由 `ToolExecutor` 持有，**单个进程内跨工具、跨轮次共享**。

## 路径沙箱（`core/common.py`）

- `SKIP_DIRS`：`.git`、`.venv`、`node_modules`、`__pycache__` 等目录在 glob/grep 中跳过
- `resolve_path_in_root()` / `ensure_path_in_root()`：解析符号链接后必须位于项目根内，越界抛 `PathSandboxError`
- `iter_files()`：递归文件遍历（跳过 SKIP_DIRS）

## 如何新增一个内置工具

1. 在 `lancher_code/tools/builtin/` 新建文件，实现 `Tool` 协议（`definition` + `execute`）
2. 在 `builtin/__init__.py` 导出
3. 在 `tools/__init__.py` 的 `create_default_tool_registry()` 中注册
4. 如需权限标签，加入 `_BUILTIN_LABELS`（否则默认使用工具名作为规则名）
5. 补充 `tests/tools/` 下的测试

详见 [development.md](../development.md)。
