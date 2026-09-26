"""Service clients (acl.yaml ``service_clients``): a headless OAuth client
that acts as a user but may only call an allowlist of tools in an allowlist of
projects. Driven through the lowlevel MCP request handlers, so the tests prove
the restricted methods are what FastMCP actually dispatches to."""

from __future__ import annotations

import asyncio

import pytest
from mcp import types
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from memaix_gateway import server
from memaix_gateway.acl import Acl, ServiceClient
from memaix_gateway.safety.audit import AuditLog

CLIENT = "7f1c2d3e-0000-4000-8000-00000000n8n1"
READ_TOOLS = ["email_search", "email_read", "email_attachments", "email_attachment_get", "email_export_pdf"]


def _acl_cfg(**service_clients) -> dict:
    return {
        "users": {
            "jimmy": {"admin": True, "oauth_subjects": ["jimmy"], "grants": {}},
            "bob": {"oauth_subjects": ["bob"], "grants": {"shared": "reader"}},
        },
        "projects": {"jimlov": {"vault": "/tmp/x"}, "memaix": {"vault": "/tmp/y"}, "shared": {"vault": "/tmp/z"}},
        "service_clients": service_clients,
    }


@pytest.fixture()
def acl(monkeypatch, tmp_path):
    acl = Acl.from_config(_acl_cfg(**{CLIENT: {"acts_as": "jimmy", "tools": READ_TOOLS, "projects": ["jimlov"]}}))
    AuditLog._clear_instances()
    monkeypatch.setattr(server, "_acl", acl)
    monkeypatch.setattr(server, "_audit", AuditLog.for_path(tmp_path / "audit.db"))
    monkeypatch.setenv("MEMAIX_TRANSPORT", "http")
    server._rate_limiter._windows.clear()
    return acl


@pytest.fixture()
def reached(monkeypatch):
    """Stub the tool implementations: record what got past the guard."""
    calls: list[tuple[str, dict]] = []

    async def fake_call_tool(name, arguments, context=None, convert_result=False):
        calls.append((name, arguments))
        return [types.TextContent(type="text", text="ok")]

    monkeypatch.setattr(server.mcp._tool_manager, "call_tool", fake_call_tool)
    return calls


def _as(subject: str):
    tok = AccessToken(token="t", client_id=subject, scopes=[], subject=subject)
    return auth_context_var.set(AuthenticatedUser(tok))


def _run(subject: str, request):
    reset = _as(subject)
    try:
        handler = server.mcp._mcp_server.request_handlers[type(request)]
        return asyncio.run(handler(request)).root
    finally:
        auth_context_var.reset(reset)


def _call(subject: str, name: str, **arguments) -> types.CallToolResult:
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    return _run(subject, req)


def _list_tools(subject: str) -> set[str]:
    return {t.name for t in _run(subject, types.ListToolsRequest(method="tools/list")).tools}


# ------------------------------------------------------------------
# Allowed
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool", READ_TOOLS)
def test_service_client_may_call_read_tools_in_jimlov(acl, reached, tool):
    result = _call(CLIENT, tool, project="jimlov", id="g1")
    assert not result.isError, result.content
    assert reached == [(tool, {"project": "jimlov", "id": "g1"})]


def test_service_client_acts_as_its_user(acl):
    """Real whoami, not stubbed: _user() maps the client to acts_as."""
    acl.service_clients[CLIENT] = ServiceClient(CLIENT, "jimmy", frozenset({"whoami"}), frozenset({"jimlov"}))
    result = _call(CLIENT, "whoami")
    assert not result.isError, result.content
    assert '"user_id": "jimmy"' in result.content[0].text


# ------------------------------------------------------------------
# Denied
# ------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["email_send", "email_create_draft", "files_write", "memory_write"])
def test_service_client_is_denied_tools_outside_allowlist(acl, reached, tool):
    result = _call(CLIENT, tool, project="jimlov", to="x@y.se", path="a", note="n", content="c")
    assert result.isError
    assert f"service client {CLIENT} may not call tool '{tool}'" in result.content[0].text
    assert reached == []


@pytest.mark.parametrize("project", ["memaix", "shared", "nope"])
def test_service_client_is_denied_other_projects(acl, reached, project):
    result = _call(CLIENT, "email_search", project=project, query="kvitto")
    assert result.isError
    assert f"in project '{project}'" in result.content[0].text
    assert reached == []


def test_service_client_without_project_argument_is_denied(acl, reached):
    result = _call(CLIENT, "email_search", query="kvitto")
    assert result.isError
    assert "in project '(none)'" in result.content[0].text
    assert reached == []


def test_denial_is_audit_logged(acl, reached):
    _call(CLIENT, "email_send", project="jimlov")
    rows = server._get_audit().tail(10)
    assert any(r["tool"] == "email_send" and not r["ok"] and CLIENT in (r["detail"] or "") for r in rows), rows


def test_service_client_without_tools_gets_nothing(monkeypatch, acl, reached):
    bare = Acl.from_config(_acl_cfg(**{CLIENT: {"acts_as": "jimmy", "projects": ["jimlov"]}}))
    monkeypatch.setattr(server, "_acl", bare)
    assert _list_tools(CLIENT) == set()
    assert _call(CLIENT, "email_search", project="jimlov", query="x").isError
    assert reached == []


def test_service_client_sees_no_resources_or_prompts(acl):
    assert _run(CLIENT, types.ListResourcesRequest(method="resources/list")).resources == []
    assert _run(CLIENT, types.ListPromptsRequest(method="prompts/list")).prompts == []
    assert _run(CLIENT, types.ListResourceTemplatesRequest(method="resources/templates/list")).resourceTemplates == []
    req = types.ReadResourceRequest(
        method="resources/read", params=types.ReadResourceRequestParams(uri="memaix://capabilities")
    )
    with pytest.raises(server.ServiceClientDenied):
        _run(CLIENT, req)
    prompt = next(iter(server.mcp._prompt_manager._prompts))
    with pytest.raises(server.ServiceClientDenied):
        _run(CLIENT, types.GetPromptRequest(method="prompts/get", params=types.GetPromptRequestParams(name=prompt)))


# ------------------------------------------------------------------
# tools/list
# ------------------------------------------------------------------


def test_tools_list_is_filtered_for_service_client(acl):
    assert _list_tools(CLIENT) == set(READ_TOOLS)


def test_normal_user_is_unaffected(acl, reached):
    everything = server.all_tool_names()
    assert _list_tools("jimmy") == everything
    assert len(everything) > len(READ_TOOLS)
    assert _run("jimmy", types.ListPromptsRequest(method="prompts/list")).prompts
    result = _call("jimmy", "email_send", project="memaix", to="x@y.se")
    assert not result.isError
    assert reached == [("email_send", {"project": "memaix", "to": "x@y.se"})]


def test_user_resolves_service_client_to_acts_as(acl):
    reset = _as(CLIENT)
    try:
        assert server._user() == "jimmy"
        assert server._service_client().client_id == CLIENT
    finally:
        auth_context_var.reset(reset)
    reset = _as("jimmy")
    try:
        assert server._user() == "jimmy"
        assert server._service_client() is None
    finally:
        auth_context_var.reset(reset)


def test_user_by_subject_does_not_resolve_service_clients(acl):
    # Only _user() (behind the dispatch guard) may turn a service client into
    # its acts_as user.
    assert acl.user_by_subject(CLIENT) is None


# ------------------------------------------------------------------
# Config validation
# ------------------------------------------------------------------


def test_overlap_with_user_oauth_subject_is_rejected():
    cfg = _acl_cfg(**{CLIENT: {"acts_as": "jimmy", "tools": READ_TOOLS, "projects": ["jimlov"]}})
    cfg["users"]["bob"]["oauth_subjects"].append(CLIENT)
    with pytest.raises(ValueError, match="either a user login or a service client"):
        Acl.from_config(cfg)


def test_overlap_with_user_oauth_sub_is_rejected():
    cfg = _acl_cfg(**{CLIENT: {"acts_as": "jimmy", "tools": READ_TOOLS}})
    cfg["users"]["bob"]["oauth_sub"] = CLIENT
    with pytest.raises(ValueError, match="either a user login or a service client"):
        Acl.from_config(cfg)


def test_missing_acts_as_is_rejected():
    with pytest.raises(ValueError, match="acts_as is required"):
        Acl.from_config(_acl_cfg(**{CLIENT: {"tools": READ_TOOLS, "projects": ["jimlov"]}}))


def test_unknown_acts_as_user_is_rejected():
    with pytest.raises(ValueError, match="is not a user"):
        Acl.from_config(_acl_cfg(**{CLIENT: {"acts_as": "mallory", "tools": READ_TOOLS}}))


def test_unknown_project_is_rejected():
    with pytest.raises(ValueError, match="unknown projects"):
        Acl.from_config(_acl_cfg(**{CLIENT: {"acts_as": "jimmy", "tools": READ_TOOLS, "projects": ["jimlv"]}}))


@pytest.mark.parametrize("bad", [{"tools": "email_search"}, {"projects": "jimlov"}, {"read_only": True}])
def test_malformed_entries_are_rejected(bad):
    with pytest.raises(ValueError, match=CLIENT):
        Acl.from_config(_acl_cfg(**{CLIENT: {"acts_as": "jimmy", **bad}}))


def test_no_service_clients_section_is_fine():
    cfg = _acl_cfg()
    del cfg["service_clients"]
    assert Acl.from_config(cfg).service_clients == {}
