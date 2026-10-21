"""A real FastMCP server for the live tests. Run with a Python that has fastmcp.

  python live_fastmcp_server.py stdio
  python live_fastmcp_server.py http <port>
"""
import sys

from fastmcp import FastMCP

mcp = FastMCP("live-test")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool
def shout(text: str) -> str:
    """Upper-case the text."""
    return text.upper()


@mcp.tool
def fail(message: str = "boom") -> str:
    """Always raises an error."""
    raise ValueError(message)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[2]))
