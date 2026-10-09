# 流程：会话持久化生命周期

## 概述

LanCher Code 支持**按项目**保存 / 恢复会话。会话文件存放在启动时工作目录下的：

```text
./.lancher/session/<会话名称>.jsonl
```

每个项目（cwd）维护自己的会话列表。实现位置：`lancher_code/session_store.py`（存储）、`lancher_code/session.py`（`SessionController` 的 save / auto_save / resume）。

## 会话文件格式（JSONL，版本 4）

每行一个 JSON 对象，记录类型：

| 类型 | 内容 |
|---|---|
| `metadata` | 格式版本、会话名、项目根、创建/更新时间、消息数、会话权限规则数、上下文治理状态（v3）、可选 `model_ref` |
| `state` | 稳定会话 ID、工作阶段、权限策略、计划快照、待处理消息及计划提示状态 |
| `permissions` | 会话级权限规则列表 |
| `message` | 界面消息（`SessionMessage`，含 usage 与 trace） |
| `transcript` | 协议无关消息（`ConversationMessage`） |

版本兼容：当前写版本 `4`（`SESSION_FORMAT_VERSION`），支持读取 `1 / 2 / 3 / 4`；v1 无 permissions 记录，其余版本必须恰好一条 permissions 记录，v3/v4 带 `context_management` 元数据。旧非计划模式映射为执行阶段与同名权限；旧计划模式保留有效的恢复权限，否则使用标准权限。

v4 保存当前会话的计划正文、摘要与来源消息；项目旧计划文件不会被自动导入为可批准快照。待处理消息尚未进入正式 transcript，恢复时一律暂停，需用户明确继续；恢复本身不会调用模型。

`model_ref` 保存稳定的 `供应商ID/模型ID`，不保存 API Key、Base URL 或解析后的连接快照。恢复时从当前全局目录解析最新连接参数。旧记录没有此字段仍可正常读取，使用默认模型并提示；原引用已删除时也回退默认模型。

写入方式：临时文件逐行写入 → `flush` + `fsync` → `os.replace` 原子替换。

## 生命周期

```text
启动（未绑定会话）
  │
  │ /session save <名称>  （save_session）
  ▼
绑定会话：active_session_name = <名称>
  │
  │ 每次状态变更（消息、模式、权限规则变更）→ _mark_dirty → auto_save()
  │   · 自动保存失败只记日志，不阻断对话
  │
  │ /session resume <名称> [--force]（resume_session）
  ▼
恢复会话：
  · 读取 JSONL → 解码 state / messages / transcript / permissions / context_management
  · 校验：名称一致、项目根一致、消息数与 metadata 一致、权限规则数一致
  · 当前对话有未保存改动且未加 --force → 抛 SessionStoreError
  · TurnRunner 先解析保存的模型引用；不存在则准备默认模型并给出提示
  · 替换会话规则（PermissionStorage.replace_session_rules，notify=False）
  · 替换 state 与 transcript；TUI 重建消息列表
  │
  │ /session rename <旧> <新> / remove <名称>
  ▼
重命名：改写 metadata 后另存新文件并删除旧文件
删除：不能删除当前正在使用的会话；直接 unlink
```

## 关键规则

| 规则 | 说明 |
|---|---|
| 名称限制 | 只能包含中文、字母、数字、`_`、`-`（正则 `^[\w\-\u3400-\u9fff]+$`） |
| 名称冲突 | 保存到已存在名称时报错（当前绑定会话除外，覆盖自身） |
| 删除限制 | 不能删除 `active_session_name` 指向的会话 |
| resume 保护 | 有未保存改动时必须 `--force` |
| 项目隔离 | 恢复时校验 `metadata.project_root` 与当前 cwd 一致，跨项目会话拒绝加载 |
| 权限随行 | 会话级规则随会话保存/恢复（`permission_rule_count` 校验） |
| 模型随行 | `metadata.model_ref` 随会话保存；恢复时使用当前目录，不把连接密钥写入会话 |
| 上下文状态 | v3 恢复 `context_management`（卸载结果引用）；若卸载文件已丢失，只记 warning，不阻断 |

## 自动保存时机

`SessionController` 通过订阅权限存储的 `session_rules_changed` 回调以及所有状态变更方法（`_mark_dirty`）跟踪脏状态：

- 消息创建 / 内容追加 / 状态完成
- 模式切换（`set_runtime_mode` 内直接调用 `auto_save()`）
- 会话级权限规则变更
- 每轮对话结束后（`TurnRunner._run_turn` finally 中 `auto_save()`）

`/model` 切换在已绑定会话中会立即保存引用，写入失败则回滚此次模型切换。切换或恢复模型会清除旧模型的 token 用量锚点和自动压缩失败状态，按当前模型的上下文窗口重新估算；消息历史与文件卸载引用保留。

## 恢复后的界面行为

`tui_views/chat.py` 的 `_restore_session_view()`：

```text
清空消息区 → 按 state.messages 重建 MessageWidget
→ 恢复横幅紧凑状态、模式提示符、占位文本
→ 刷新上下文用量估算与状态栏 → 滚动到底
```

## 与其他模块的关系

- `session_store.ProjectSessionStore`：文件 IO（save / load / list / remove / rename）
- `SessionController`：业务编排（校验、状态合并、脏标记）
- `TurnRunner`：恢复前准备目标模型适配器，并协调会话引用与有效连接配置
- `PermissionStorage`：会话级规则随会话走
- `tui_views/chat.py`：`/session` 命令交互与界面重建
