"""Console entry point for the Browser MCP stdio server."""

from browser_mcp.config import AppSettings
from browser_mcp.logging_config import configure_logging
from browser_mcp.mcp.server import create_server


def main() -> None:
    """Load process configuration and run the MCP server over stdio."""
    settings = AppSettings.from_env()
    configure_logging(settings.log_level)
    create_server(settings).run(transport="stdio")
