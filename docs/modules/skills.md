# 模块：Skills 与项目约定

Skills 保存任务流程，MCP 提供外部工具，项目根 `AGENTS.md` 保存常驻约定。技能发现、激活、上下文投影和连接管理都在智能体核心；TUI 只展示状态并调用核心门面。

## 存放与发现

```text
<项目>/.lancher/skills/<名称>/SKILL.md
~/.lancher/skills/<名称>/SKILL.md
```

目录内部使用 `SKILL.md` 的 YAML frontmatter 和 Markdown 正文。名称必须与技能文件夹一致，由小写字母、数字及单个连字符组成，最多 64 字符；`description` 必填，最多 1024 字符。

项目级同名技能优先。稳定 ID 为 `project/<名称>`、`user/<名称>`；被覆盖的用户技能仍可通过完整 ID 指定。首版只扫描这两个目录的直接子文件夹，文件不存在时跳过，坏格式单独记录，不影响其他技能。

## 一个精简使用例

创建 `.lancher/skills/review-change/SKILL.md`：

```markdown
---
name: review-change
description: 审查当前代码变更，定位行为风险并核对相关验证。
---

先阅读当前变更及直接调用方，再检查边界条件。
需要检查清单时读取 references/checklist.md。
结论区分已经验证的事实与仍待验证的行为。
```

可在同一文件夹内添加 `references/checklist.md`。进入聊天后刷新目录，再发出任务：

```text
/skills reload
请用 $review-change 审查当前修改。
```

用户显式指定由核心在任务输入生效时加载。未指定时，模型看到技能名称、描述和来源，可根据任务调用 `load_skill({"skill": "review-change"})`；选择由模型完成，不依靠关键词固定路由。正文进入下一次模型请求的系统上下文，工具结果只保存加载确认与 ID，避免正文重复留在工具历史里。

需要只允许用户主动选择的技能，在 frontmatter 增加：

```yaml
disable-model-invocation: true
```

这类技能仍可用 `$review-change` 或 `$user/review-change` 指定，但不会放入自动推荐目录。会话级禁用则同时阻止显式加载与模型加载。

## 生命周期与管理

| 时机 | 行为 |
|---|---|
| 未加载 | 模型只看到预算内的目录元数据 |
| 加载后 | 正文快照跨用户轮生效，当前周期重复加载去重 |
| 文件被修改 | 当前周期仍使用原快照；卸载后重载或下个周期采用新正文 |
| 压缩成功提交 | 回收激活正文，保留 ID、来源、内容指纹与使用记录 |
| 压缩失败或取消 | 保持原有技能正文和状态 |
| 压缩后仍需该流程 | 核心投影技能引用，提示先用 `load_skill` 重新读取，避免依据摘要猜规则 |
| 会话恢复 | 恢复激活快照、引用和禁用状态；不重跑技能中的脚本或已完成操作 |

技能正文与任务执行记录分开：重载读取指南，已经完成的操作仍依据任务事实继续。新会话拥有独立的激活与禁用状态。

| 命令 | 作用 |
|---|---|
| `/skills`、`/skills list` | 查看技能、来源与本会话状态 |
| `/skills show <名称或ID>` | 阅读技能详情；查看本身不激活技能 |
| `/skills reload` | 重新扫描两个技能目录，不替换当前周期的正文快照 |
| `/skills disable <名称或ID>` | 当前会话禁用并回收正文 |
| `/skills enable <名称或ID>` | 当前会话启用，不自动加载正文 |
| `/skills unload <名称或ID>` | 只回收正文，后续任务仍可再次加载 |

变更操作要求核心处于空闲状态；阶段、权限和模型选择仍由现有核心服务校验。

## 引用资源与预算

`read_skill_resource` 只读取已登记技能目录内的 UTF-8 文本，参数为 `skill`、`relative_path`，可选 `start_line`（从 1 开始）、`line_count`（默认 200，上限 400）。例如模型调用：

```json
{"skill": "project/review-change", "relative_path": "references/checklist.md", "start_line": 1, "line_count": 80}
```

结果包含总行数、当前范围与下一页 `next_line`。单页最多约 12,000 个文本字符，不截断一行；超长行明确报错。入口读取资料或脚本源码，不执行脚本。脚本操作继续使用普通命令工具，经过阶段、权限和资源调度。

只读边界拒绝绝对路径、上级目录、Windows ADS 和跨目录符号链接；`SKILL.md` 及同文件硬链接别名须通过 `load_skill` 加载。该能力不扩大普通 `read_file` 的项目路径范围，也不提供任意用户主目录读取。

技能文件最多 64 KiB，frontmatter 最多 8,000 字符，正文最多 24,000 字符；资源文件最多 1 MiB。核心还按当前模型窗口限制技能正文总量与目录大小，超额明确报错或裁减目录，不静默截断技能指令。

## 项目根 AGENTS.md

核心在组装请求时读取 `<项目>/AGENTS.md`，作为项目常驻约定注入系统上下文，压缩后继续提供。当前只处理项目根文件；最多 32 KiB、UTF-8 编码，跨项目链接与读取问题单独提示。

项目约定和技能指令都不能提升权限，用户当前请求优先。子目录继承、用户级 `AGENTS.md` 和技能安装下载不属于本轮能力。

## 实现边界

| 模块 | 职责 |
|---|---|
| `agent/skills/service.py` | 目录发现、格式校验、限定资源读取与预算 |
| `agent/skills/models.py` | 元数据、诊断、激活正文快照与分页结果 |
| `agent/skill_context.py` | 模型选择加载、显式输入激活、会话状态与请求投影 |
| `agent/instructions.py` | 项目根 `AGENTS.md` 的受限读取 |
| `agent/capabilities.py` | 能力装配、管理入口、MCP 生命周期与上下文提供器 |
| `tools/builtin/skills.py` | 模型工具入口，正文通过回调交给核心 |
| `tui/capabilities.py` | 技能和 MCP 列表、详情、命令转发 |

`SessionController` 持久化上下文状态；成功压缩使用候选状态回收正文，并与摘要一起提交。TUI 不负责扫描技能、解析正文、匹配任务或注入提示词。验证位于 `tests/skills/` 及核心能力、会话压缩和 TUI 的相关测试。
