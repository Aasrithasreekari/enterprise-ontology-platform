"""MCP server tests through the official MCP client (in-process transport)."""

import pytest
from mcp.client import Client

from conftest import operator
from ontology_platform.mcp_server.server import build_mcp_server

EXPECTED_TOOLS = {
    "search_entities", "get_entity_details", "get_related_entities", "find_entity_relationships", "query_knowledge_graph",
    "search_documentation", "trace_application_dependencies", "get_deployment_information", "get_ontology_schema",
}


@pytest.fixture
def server(seeded):
    return build_mcp_server(seeded, default_principal=operator("northwind", roles=("viewer",)))


async def test_tools_listed_with_schemas(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert EXPECTED_TOOLS == set(tools)
    q = tools["query_knowledge_graph"]
    assert q.description and q.input_schema["properties"]["question"]["maxLength"] == 1000
    assert "ctx" not in q.input_schema["properties"]
    assert tools["get_related_entities"].input_schema["properties"]["depth"]["maximum"] == 6
    assert tools["search_entities"].annotations.read_only_hint is True


async def test_query_tool_structured_output(server):
    async with Client(server) as c:
        res = await c.call_tool("query_knowledge_graph", {"question": "Which Kubernetes clusters host Customer B's applications?"})
    assert not res.is_error
    data = res.structured_content
    assert data["intent"] == "clusters_hosting"
    assert {r["entity"]["name"] for r in data["results"]} == {"nw-prod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-eks-use1"}


async def test_graph_tools(server):
    async with Client(server) as c:
        dep = (await c.call_tool("get_deployment_information", {"application": "claims-portal application", "environment": "UAT"}))
        assert dep.is_error  # free text is not an entity reference
        dep = (await c.call_tool("get_deployment_information", {"application": "application:claims-portal", "environment": "UAT"})).structured_content
        assert {d["customer"] for d in dep["deployments"]} == {"globex", "initech"}
        trace = (await c.call_tool("trace_application_dependencies", {"entity": "service:notification-service", "direction": "upstream"})).structured_content
        assert {a["name"] for a in trace["impact"]["affected_applications"]} == {"claims-portal", "document-intake", "identity-platform"}
        rel = (await c.call_tool("find_entity_relationships", {"source": "customer:acme", "target": "api:payments-api"})).structured_content
        assert rel["connected"] and rel["paths"][0][0]["evidence"]
        docs = (await c.call_tool("search_documentation", {"query": "promote read replica"})).structured_content
        assert docs["results"][0]["title"] == "Payments Runbook"
        details = (await c.call_tool("get_entity_details", {"entity": "api:auth-api"})).structured_content
        assert "/oauth2/token" in " ".join(details["entity"]["properties"]["endpoints"])
        related = (await c.call_tool("get_related_entities", {"entity": "product:payhub", "relation_types": ["contains"], "direction": "out"})).structured_content
        assert {r["entity"]["name"] for r in related["related"]} == {"payments-platform", "ledger"}
        found = (await c.call_tool("search_entities", {"query": "fraud", "types": ["service"]})).structured_content
        assert found["results"][0]["name"] == "fraud-detector"
        schema = (await c.call_tool("get_ontology_schema", {})).structured_content
        assert len(schema["relationship_types"]) == 14


async def test_input_validation_and_errors(server):
    async with Client(server) as c:
        bad = await c.call_tool("get_related_entities", {"entity": "product:payhub", "depth": 50})
        assert bad.is_error
        bad_type = await c.call_tool("search_entities", {"query": "x", "types": ["spaceship"]})
        assert bad_type.is_error
        missing = await c.call_tool("get_entity_details", {"entity": "nothing-like-this"})
        assert missing.is_error and "not_found" in missing.content[0].text


async def test_no_credentials_without_default_principal(seeded):
    server = build_mcp_server(seeded)  # network transport mode: no fallback identity
    async with Client(server) as c:
        res = await c.call_tool("search_entities", {"query": "ledger"})
    assert res.is_error and "unauthenticated" in res.content[0].text


async def test_customer_scoped_mcp_principal(seeded):
    server = build_mcp_server(seeded, default_principal=operator("northwind", roles=("viewer",), customers=["initech"]))
    async with Client(server) as c:
        res = (await c.call_tool("get_deployment_information", {"application": "application:claims-portal"})).structured_content
    assert {d["customer"] for d in res["deployments"]} == {"initech"}
