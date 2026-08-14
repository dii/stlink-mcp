"""Entry point. Runs the MCP server on stdio, which is how MCP clients spawn it."""

from .server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
