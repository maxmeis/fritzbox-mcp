"""Local FRITZ!Box MCP server."""

from .router import RouterClient, RouterError

__all__ = ["RouterClient", "RouterError"]
