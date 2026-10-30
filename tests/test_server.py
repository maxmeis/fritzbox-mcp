import pytest

from fritzbox_mcp.server import create_server


class FakeRouter:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        self.calls.append(("capabilities",))
        return {"device_names": True}

    def schema(self, service=None, action=None):
        self.calls.append(("schema", service, action))
        return {"service": service, "action": action}

    def action(self, service, action, arguments=None):
        self.calls.append(("action", service, action, arguments))
        return {"called": action}


class FakeUI:
    def __init__(self):
        self.calls = []

    async def open(self, path="/"):
        self.calls.append(("open", path))
        return {"path": path}

    async def snapshot(self):
        self.calls.append(("snapshot",))
        return {"text": "local"}

    async def click(self, role, name, exact=True):
        self.calls.append(("click", role, name, exact))
        return {}

    async def fill(self, label, value):
        self.calls.append(("fill", label, value))
        return {}

    async def select(self, label, value):
        self.calls.append(("select", label, value))
        return {}

    async def set_checked(self, label, checked):
        self.calls.append(("set_checked", label, checked))
        return {}

    async def close(self):
        self.calls.append(("close",))
        return "closed"


@pytest.mark.asyncio
async def test_initialize_and_tool_discovery_are_offline_and_do_not_connect():
    router, ui = FakeRouter(), FakeUI()
    server = create_server(router=router, ui=ui)

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "capabilities",
            "discover_actions",
            "call_action",
            "set_device_blocking",
            "ui_open",
            "ui_snapshot",
            "ui_close",
        } <= names
        assert router.calls == []
        assert ui.calls == []

        result = await session.call_tool(
            "call_action",
            {"service": "Hosts1", "action": "GetInfo", "arguments": {}},
        )
        assert not result.isError
        assert router.calls == [("action", "Hosts1", "GetInfo", {})]
        assert ui.calls == []

        result = await session.call_tool("ui_snapshot", {})
        assert not result.isError
        assert ui.calls == [("snapshot",)]
        assert router.calls == [("action", "Hosts1", "GetInfo", {})]


@pytest.mark.asyncio
async def test_blocking_tool_description_does_not_claim_universal_access_or_immediate_effect():
    server = create_server(router=FakeRouter(), ui=FakeUI())
    tools = await server.list_tools()
    tool = next(item for item in tools if item.name == "set_device_blocking")
    description = tool.description.lower()
    assert "wan access" in description
    assert "filter profiles may still restrict access" in description
    assert "internet allowed" not in description
    assert "immediately" not in description


@pytest.mark.asyncio
async def test_server_lifespan_closes_ui_and_router_sessions():
    class CloseableRouter(FakeRouter):
        def __init__(self):
            super().__init__()
            self.closed = False

        def close(self):
            self.closed = True

    class CloseableUI(FakeUI):
        def __init__(self):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True
            return "closed"

    from mcp.shared.memory import create_connected_server_and_client_session

    router, ui = CloseableRouter(), CloseableUI()
    server = create_server(router=router, ui=ui)
    async with create_connected_server_and_client_session(server):
        assert not router.closed
        assert not ui.closed
    assert router.closed
    assert ui.closed
