"""The multi-agent workflow tool through the official MCP client (in-process transport)."""

from mcp.client import Client

from conftest import operator
from ontology_platform.mcp_server.server import build_mcp_server

TOOL = "run_enterprise_intelligence_workflow"


async def test_workflow_tool_listed_with_schema_and_guidance(seeded):
    server = build_mcp_server(seeded, default_principal=operator("northwind", roles=("viewer",)))
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    tool = tools[TOOL]
    props = tool.input_schema["properties"]
    assert set(props) == {"question"} and props["question"]["minLength"] == 3 and props["question"]["maxLength"] == 1000
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert "Example" in tool.description and "confirmed" in tool.description
    for t in tools.values():  # every tool explains when to use it and gives an example or return shape
        assert t.description and len(t.description) > 120, t.name


async def test_workflow_tool_runs_end_to_end(seeded):
    server = build_mcp_server(seeded, default_principal=operator("northwind", roles=("viewer",)))
    async with Client(server) as c:
        res = await c.call_tool(TOOL, {"question": "Which Kubernetes clusters host Customer B's applications?"})
    assert not res.is_error
    run = res.structured_content
    assert run["status"] == "completed" and run["validation"]["status"] == "validated"
    assert "nw-prod-eks-use1" in run["answer"]["summary"]
    assert run["plan"] and run["events"] and run["answer"]["sources"]
    assert seeded.get_workflow(operator("northwind", roles=("viewer",)), run["id"])["id"] == run["id"]


async def test_workflow_tool_validation_and_auth(seeded):
    server = build_mcp_server(seeded, default_principal=operator("northwind", roles=("viewer",)))
    async with Client(server) as c:
        assert (await c.call_tool(TOOL, {"question": "x"})).is_error
    unauthenticated = build_mcp_server(seeded)
    async with Client(unauthenticated) as c:
        res = await c.call_tool(TOOL, {"question": "Which APIs are exposed by the claims-portal application?"})
    assert res.is_error and "unauthenticated" in res.content[0].text
