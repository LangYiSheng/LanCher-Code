# 模块：模型供应商（Providers）

## 作用

供应商配置目录管理自定义厂商及其模型；协议适配层把流式 API 统一成 `ChatProvider.stream_chat(request) -> AsyncIterator[StreamEvent]`。自定义厂商名称（如 DeepSeek）与协议类型分开，一个厂商的不同模型可以使用不同协议。

实现位置：`lancher_code/providers/`。

## 配置与模型解析

- `models.ProviderDefinition` 保存供应商名称、公共协议、Base URL、API Key、超时和模型目录。
- `models.ModelDefinition` 保存 API 模型名、可选显示名、连接覆盖以及上下文窗口/thinking。`None` 表示继承供应商的对应连接字段。
- `AppConfig.providers` 与 `default_model` 是配置源；旧 `AppConfig.provider` 仅兼容默认模型的有效快照，编辑它不会写回目录。
- `model_catalog.resolve_model(config, ref)` 逐字段合并覆盖、展开环境变量，产生独立的 `ProviderConfig`。原配置保留变量原文和缺省字段，`ref` 为稳定的 `供应商ID/模型ID`。
- `model_display_name()` 优先显示模型的 `display_name`，否则显示 `model_name (供应商名称)`；显示名称不发送给 API。

配置文件可读旧单 `provider` 格式，保存时迁移并保留 `.bak`。详细 schema 见 [configuration.md](../configuration.md)。

## 接口与实现

| 文件 | 内容 |
|---|---|
| `base.py` | `ChatProvider` 协议、`BaseChatProvider` 基类（SSE 解析、错误分类、用量统计、消息序列化辅助） |
| `openai.py` | `OpenAIProvider`：`POST {base_url}/chat/completions` |
| `claude.py` | `ClaudeProvider`：`POST {base_url}/messages` |
| `factory.py` | `create_provider(config)`：按 `config.protocol` 返回对应实现 |

## 流式事件（StreamEvent）

`Provider.stream_chat()` 把厂商的流解析为统一的 `StreamEvent`（`models.py`）：

| kind | 含义 |
|---|---|
| `message_start` | 响应开始 |
| `text_delta` | 普通文本增量 |
| `thinking_delta` | 思考内容增量（Claude thinking / OpenAI reasoning） |
| `tool_call_delta` | 工具调用增量（名称 / 参数 JSON 分片） |
| `message_end` | 响应结束，携带 `usage` |
| `error` | 错误（当前各 Provider 直接抛异常，未产出该事件） |

## 请求序列化差异

### OpenAI（`openai.py`）

- URL：`{base_url}/chat/completions`，Header：`Authorization: Bearer <api_key>`
- Payload：`model`、`messages`（system 提示作为独立 system 消息）、`stream: true`、`stream_options: {include_usage: true}`、可选 `tools`（`type: function`）
- 工具调用：`delta.tool_calls[].function.{name, arguments}`，参数为 JSON 字符串分片
- 思考：`delta.reasoning_content` 或 `delta.reasoning`
- 结束标记：SSE 数据 `[DONE]`；用量从 `chunk.usage` 读取（含 `prompt_tokens_details.cached_tokens`）

### Anthropic（内部协议值 `claude`，`claude.py`）

- URL：`{base_url}/messages`，Header：`x-api-key: <api_key>`、`anthropic-version: 2023-06-01`
- Payload：`model`、`system`（字符串拼接）、`max_tokens: 4096`（固定）、`stream: true`、`thinking`（enabled/disabled + budget_tokens）、可选 `tools`（`input_schema`）
- 事件类型：`message_start` / `content_block_start`（tool_use 整体给出 input 时直接产出完整参数）/ `content_block_delta`（text_delta / thinking_delta / input_json_delta）/ `message_delta`（usage）/ `message_stop` / `error`
- 用量合并：`input_tokens + cache_read_input_tokens + cache_creation_input_tokens` 记为 input

## 错误处理（`base.py`）

`raise_for_error_status()` 统一分类：

| 条件 | 异常 |
|---|---|
| HTTP 401 / 403 | `ProviderAuthError` |
| 上下文超长（code 或 message 命中） | `ProviderPromptTooLongError` |
| 其他 4xx/5xx | `ProviderResponseError` |
| 网络异常 / 超时 | `ProviderRequestError`（`map_request_error()`） |
| SSE 数据非合法 JSON | `StreamProtocolError` |

`is_prompt_too_long()` 识别 `context_length_exceeded`、`prompt_too_long` 等错误码与常见文案，供 TurnRunner 触发紧急压缩。

## 输入与输出

| 方向 | 说明 |
|---|---|
| 输入 | `ChatRequest`（model / system / messages / tools / allow_tool_calls / thinking / mode / cancellation_token） |
| 输出 | `StreamEvent` 异步迭代器 |

## 与其他模块的关系

- ← `factory.py` ← `app.py`：启动时解析默认模型后创建
- ← `TurnRunner.configure_models/switch_model/reload_models`：协调模型目录、协议适配器和会话有效配置的切换；正在响应或压缩时禁止切换
- ← `turn_runner.py`：`_stream_request()` 消费流事件
- ← `context_management.py`：摘要压缩请求也走 `stream_chat()`
- ← `session.py`：`_request_thinking()` 只在 claude 协议下启用 thinking

切换保留协议无关 transcript、消息、工具结果和权限；模型、thinking、context_window 一起更新，同时清除旧模型的 token 校准锚点和压缩失败计数。工具历史由当前协议重新序列化，OpenAI 会把同一批工具结果逐条展开为 `tool` 消息。摘要压缩也使用当前主模型，不另设摘要模型。

## 如何新增一个协议

1. 在 `providers/` 新增实现类，继承 `BaseChatProvider`，实现 `stream_chat()`
2. 在 `models.py` 的 `ProviderProtocol` Literal 中增加协议名
3. 在 `factory.py` 中按协议分发
4. 在 `config_system/loader.py` 的 `SUPPORTED_PROTOCOLS` 与 `model_catalog.resolve_model()` 中登记，并更新默认上下文窗口策略
5. 更新设置与引导界面的协议选项，参考 `tests/providers/` 和 `tests/test_model_config.py` 补测试

## 注意事项

- 每个请求都会用 `client_factory()` 新建一个 `httpx.AsyncClient`（默认按 `timeout_seconds` 配置）。
- `max_tokens` 在 Claude 实现中是固定值 4096；thinking 开启时 `budget_tokens` 默认 2048。
- 流式解析基于逐行 SSE（`iter_sse_events`），兼容 `event:` / `data:` 分段与 `:` 注释行。
