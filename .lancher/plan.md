# 简化输入框提示内容

## 当前提示

| 模式 | 提示内容 |
|------|----------|
| Plan | `Plan Mode：继续补充或修改计划；输入 /do 返回正常模式，Ctrl+C 取消当前请求` |
| 普通 | `发送一条消息... Enter 发送，Shift+Enter 换行，/plan 进入计划模式，Ctrl+C 取消/退出，/exit 退出` |

## 简化方案

| 模式 | 提示内容 |
|------|----------|
| Plan | `输入计划内容... /do 执行，Ctrl+C 取消` |
| 普通 | `输入消息... Enter 发送，/help 查看命令` |

## 修改位置

文件：`lancher_code/tui.py`，第 579-584 行，`_refresh_composer_placeholder` 方法

```python
def _refresh_composer_placeholder(self) -> None:
    composer = self.query_one("#composer-input", ComposerTextArea)
    if self._session_controller.runtime_mode == "plan":
        composer.placeholder = "输入计划内容... /do 执行，Ctrl+C 取消"
        return
    composer.placeholder = "输入消息... Enter 发送，/help 查看命令"
```

## 简化思路

1. 保留核心操作提示（Enter 发送、/do 执行、Ctrl+C 取消）
2. 移除冗余信息（Shift+Enter 换行、/plan、/exit 等可通过 /help 查看）
3. 使用更简洁的中文表述
