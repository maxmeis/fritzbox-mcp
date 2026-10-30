"""MCP stdio entry point for local FRITZ!Box administration."""

from __future__ import annotations

import argparse
import inspect
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .router import RouterClient

_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)


def create_server(router: RouterClient | None = None, ui: Any = None) -> FastMCP:
    client = router or RouterClient()
    browser_ui: Any = None

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            try:
                close_ui = getattr(browser_ui, "close", None)
                if callable(close_ui):
                    result = close_ui()
                    if inspect.isawaitable(result):
                        await result
            finally:
                close_router = getattr(client, "close", None)
                if callable(close_router):
                    close_router()

    mcp = FastMCP("FRITZ!Box", lifespan=lifespan)

    @mcp.tool(annotations=_READ)
    def capabilities() -> dict[str, Any]:
        """Show capabilities advertised by this router and known interface limits."""
        return client.capabilities()

    @mcp.tool(annotations=_READ)
    def discover_actions(
        service: str | None = None, action: str | None = None
    ) -> dict[str, Any]:
        """List router TR-064 services/actions or exact argument schema for an action."""
        return client.schema(service, action)

    @mcp.tool(annotations=_WRITE)
    def call_action(
        service: str, action: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call any router-advertised TR-064 action, including configuration writes. Validate inputs against the router's schema first. Sensitive result fields are redacted."""
        return client.action(service, action, arguments)

    @mcp.tool(annotations=_READ)
    def router_info() -> dict[str, Any]:
        """Get router model, firmware, and status information."""
        return client.router_info()

    @mcp.tool(annotations=_READ)
    def list_devices(offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """List one bounded page of known network devices."""
        return client.list_devices(offset, limit)

    @mcp.tool(annotations=_READ)
    def get_device(mac: str) -> dict[str, Any]:
        """Get a host entry by MAC address."""
        return client.get_device(mac)

    @mcp.tool(annotations=_WRITE)
    def rename_device(mac: str, name: str) -> dict[str, Any]:
        """Change a device hostname by MAC address. This differs from its friendly display name."""
        return client.rename_device(mac, name)

    @mcp.tool(annotations=_WRITE)
    def set_friendly_name(mac: str, name: str) -> dict[str, Any]:
        """Change a device's friendly display name by MAC address, if advertised."""
        return client.set_friendly_name(mac, name)

    @mcp.tool(annotations=_READ)
    def get_device_blocking(ip: str) -> dict[str, Any]:
        """Read the WAN disallow flag for a device IPv4 address. This is not a DNS blocklist."""
        return client.get_device_blocking(ip)

    @mcp.tool(annotations=_WRITE)
    def set_device_blocking(ip: str, blocked: bool) -> dict[str, Any]:
        """Disallow or allow a device's WAN access by IPv4 address. Filter profiles may still restrict access."""
        return client.set_device_blocking(ip, blocked)

    @mcp.tool(annotations=_READ)
    def wifi_info(service: str = "WLANConfiguration1") -> dict[str, Any]:
        """Read Wi-Fi band information using an advertised WLANConfiguration service."""
        return client.wifi_info(service)

    @mcp.tool(annotations=_WRITE)
    def set_wifi_enabled(service: str, enabled: bool) -> dict[str, Any]:
        """Enable or disable one Wi-Fi band. Disabling it can disconnect this client."""
        return client.set_wifi_enabled(service, enabled)

    @mcp.tool(annotations=_READ)
    def get_lan_dns() -> dict[str, Any]:
        """Read LAN DNS server information using the router's advertised action."""
        return client.get_lan_dns()

    @mcp.tool(annotations=_READ)
    def get_wan_dns(service: str | None = None) -> dict[str, Any]:
        """Read WAN upstream DNS servers, if advertised. This does not change DNS filters."""
        return client.get_wan_dns(service)

    @mcp.tool(annotations=_READ)
    def get_2fa_info() -> dict[str, Any]:
        """Check whether TR-064 second factor authentication is enabled."""
        return client.get_2fa_info()

    @mcp.tool(annotations=_READ)
    def get_2fa_state() -> dict[str, Any]:
        """Read the current TR-064 second factor authentication state."""
        return client.get_2fa_state()

    @mcp.tool(annotations=_WRITE)
    def start_2fa() -> dict[str, Any]:
        """Start TR-064 second factor authentication. The returned token is redacted for safety."""
        return client.start_2fa()

    @mcp.tool(annotations=_WRITE)
    def stop_2fa() -> dict[str, Any]:
        """Stop the current TR-064 second factor authentication process."""
        return client.stop_2fa()

    from .ui import register_ui_tools

    browser_ui = register_ui_tools(mcp, ui=ui)
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Local FRITZ!Box MCP server")
    parser.parse_args()
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
