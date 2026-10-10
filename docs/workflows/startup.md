# 流程：程序启动

启动入口链：`main.py` / `__main__.py` / console script → `lancher_code/cli.py:main` → `lancher_code/app.py:run_app`。

## 时序图

```mermaid
sequenceDiagram
    participant U as 用户
    participant CLI as cli.main
    participant LOG as logging_system
    participant APP as app.run_app
    participant BS as ConfigBootstrapTUI
    participant CFG as config
    participant PF as create_provider
    participant USAGE as RunUsageTracker
    participant SS as SessionController
    participant REG as tool_registry
    participant MCP as MCPClientManager
    participant RUN as TurnRunner
    participant CAP as AgentCapabilities
    participant TUI as ChatTUI

    U->>CLI: uv run lancher
    CLI->>LOG: configure_logging()（写 ~/.lancher/logs/lancher-error.log）
    CLI->>APP: asyncio.run(run_app())
    APP->>CFG: resolve_config_bootstrap_state()
    alt 全局配置不存在（首次启动）
        APP->>BS: ConfigBootstrapTUI.run()
        BS-->>APP: 用户填写并保存 → ~/.lancher/lancher.yaml + mcp.yaml 模板
    else 用户取消引导
        APP-->>CLI: 返回 0，退出
    end
    APP->>CFG: load_config() 校验配置（非法则打印错误、返回 1）
    APP->>USAGE: 创建本次启动的请求用量账本
    APP->>PF: create_provider(active_config, usage_observer)
    APP->>LOG: register_sensitive_values(api_key)
    APP->>SS: SessionController(provider, cwd, plan 路径, 初始阶段, 权限策略, 权限存储)
    APP->>REG: create_default_tool_registry()（基础内置工具）
    APP->>MCP: load_mcp_config(cwd) + MCPClientManager（并注册 env/headers 敏感值）
    APP->>SS: PermissionEngine / SettingsService / ToolExecutor
    APP->>RUN: TurnRunner(provider, session, registry, executor, 循环上限...)
    RUN->>CAP: 创建能力门面、技能服务与工具，绑定项目约定和技能上下文
    APP->>RUN: configure_capabilities(mcp_manager, registry)
    APP->>TUI: ChatTUI(turn_runner, settings_service, ...)
    APP->>RUN: start_capabilities()
    CAP->>MCP: 后台 initialize(registry)，逐页发现工具
    TUI-->>APP: tui.run() 进入事件循环
    APP->>RUN: finally: turn_runner.shutdown()（能力与进程统一收尾）
    APP->>SS: finally: session.close()
    APP->>MCP: finally: 幂等关闭初始 manager
    opt TUI 正常返回
        APP->>USAGE: 获取最终用量快照
        APP-->>U: 普通终端打印告别、恢复命令与本次用量
    end
```

（`REG` 表示工具注册表；`PermissionEngine / SettingsService / ToolExecutor / TurnRunner` 的创建统一归到装配步骤，详见 [../modules](../modules/) 各模块文档。）

## 启动步骤详解

| 步骤 | 代码位置 | 说明 |
|---|---|---|
| 1. 参数解析 | `cli.py build_arg_parser()` | 当前无任何参数 |
| 2. 日志初始化 | `cli.py configure_logging()` | ERROR 级、滚动文件（5MB × 5）、敏感值脱敏；目录不可写回退 stderr |
| 3. 引导判定 | `config/bootstrap.py` | `needs_setup = ~/.lancher/lancher.yaml 不存在` |
| 4. 首次引导 | `tui/bootstrap.py` | 填写协议/模型/Base URL/API Key/超时/thinking；保存并创建 MCP 模板 |
| 5. 加载配置 | `config/loader.py load_config()` | YAML 解析 + 完整校验；失败打印 `[错误]` 并退出码 1 |
| 6. 创建 Provider | `providers/factory.py` | 按 `protocol` 返回 OpenAI/Claude 实现 |
| 7. 敏感值注册 | `logging_system.register_sensitive_values()` | api_key 与 MCP env/headers 值，日志脱敏 |
| 8. 创建会话控制器 | `sessions/controller.py SessionController` | 绑定 cwd、初始阶段与策略、权限存储；首条用户消息才创建 UUID Session 与独立 workspace |
| 9. 创建工具集 | `tools/__init__.py` | 注册 文件工具、run_command、process_list/read/wait/write/stop/background、glob/grep、计划与发现工具 |
| 10. 创建 MCP | `mcp/manager.py` | 加载全局+项目配置，由核心后台初始化 |
| 11. 创建执行链 | `tools/core/executor.py` + `permissions/engine.py` | ToolExecutor 持有注册表与权限引擎 |
| 12. 创建 TurnRunner | `agent/runner.py`、`agent/capabilities.py` | 注入依赖，装配 Skills 工具与项目约定、绑定 MCP 门面；配置循环上限与未知工具熔断 |
| 13. 启动核心能力与 TUI | `app.py`、`tui/app.py` | 核心开始后台 MCP 连接；TUI 显示进度，输入立即可使用内置能力 |
| 14. 退出清理与小结 | `app.py finally` | 等待 Runner、Session 写入者和 MCP 连接收尾；正常返回后打印恢复命令与本次启动用量，清理失败明确报告并返回 1 |

本次启动账本在应用装配时创建。Provider 工厂和切换模型后的新 Provider 都使用同一个 observer，Session 恢复不导入历史消耗。退出确认与统计口径详见 [结束工作与恢复对话](app-exit.md)。

## 启动失败的常见退出

| 退出码 | 场景 | 表现 |
|---|---|---|
| `0` | 引导被取消 | 直接退出，不写配置 |
| `1` | 配置非法（YAML 错误 / 缺必填项 / protocol 不支持） | 终端打印 `[错误] <原因>`（红色），退出码 1 |
| `1` | 其他未捕获异常 | 写入日志（`event=application_uncaught_exception`），退出码 1 |
| `130` | 启动阶段 Ctrl+C | `KeyboardInterrupt`，退出码 130 |

## 首次启动的产物

```text
~/.lancher/lancher.yaml      ← 引导界面写入的主配置
~/.lancher/mcp.yaml          ← 自动生成的 MCP 模板（ensure_user_mcp_config）
~/.lancher/logs/lancher-error.log  ← 日志（启动时即创建）
```
