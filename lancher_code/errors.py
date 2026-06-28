from __future__ import annotations


class LanCherError(Exception):
    """项目级基础异常。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


class ConfigError(LanCherError):
    """配置加载或校验失败。"""


class ProviderError(LanCherError):
    """模型供应商调用失败。"""


class ProviderAuthError(ProviderError):
    """认证失败。"""


class ProviderRequestError(ProviderError):
    """请求发送失败，例如网络异常或超时。"""


class ProviderResponseError(ProviderError):
    """后端返回了无效或错误响应。"""


class StreamProtocolError(ProviderError):
    """流式协议解析失败。"""


class ToolCallParseError(LanCherError):
    """工具调用分片拼接或 JSON 解析失败。"""


class ToolNotFoundError(LanCherError):
    """未找到指定工具。"""


class ToolExecutionError(LanCherError):
    """工具执行过程中发生不可恢复错误。"""
