from mcp.server.fastmcp import FastMCP

server = FastMCP("LanCher MCP test")


@server.tool(annotations={"readOnlyHint": True})
def echo(value: str) -> str:
    """返回输入文本。"""
    return value


if __name__ == "__main__":
    server.run(transport="stdio")
