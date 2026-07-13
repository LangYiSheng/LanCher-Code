from __future__ import annotations

from pathlib import Path

from lancher_code.config_system.paths import get_global_mcp_config_path

MCP_CONFIG_TEMPLATE = '''# LanCher Code MCP Server 配置
# 未启用 MCP 时保持空对象即可。
mcp_servers: {}

# stdio 示例：
# mcp_servers:
#   filesystem:
#     type: stdio
#     command: npx
#     args: ["-y", "@modelcontextprotocol/server-filesystem", "D:/Dev"]
#     env:
#       LOG_LEVEL: info

# Streamable HTTP 示例：
# mcp_servers:
#   internal_api:
#     type: http
#     url: https://mcp.example.com/mcp
#     headers:
#       Authorization: "Bearer ${INTERNAL_MCP_TOKEN}"
'''


def ensure_user_mcp_config(*, home_dir: Path | None = None) -> Path:
    path = get_global_mcp_config_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(MCP_CONFIG_TEMPLATE)
    except FileExistsError:
        pass
    return path
