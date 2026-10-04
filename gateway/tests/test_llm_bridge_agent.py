# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fas 2 — verktygsbryggan + agentloopen (FEATURE-LLM-ENGINE).

Acceptanskriterierna ur specen, som tester:
- en reader kan inte NÅ skriv-verktyg via chatten (varken schema eller anrop)
- ett scriptat testsamtal ("vad har jag i kalendern?") kör calendar_list som
  RÄTT användare — identiteten sätts och återställs runt varje anrop
- anropen går genom SAMMA funktionsobjekt som MCP (ingen parallell väg —
  det är den arkitektoniska garantin för att outbox/ACL/limits ärvs intakta)
"""

from __future__ import annotations

import pytest

from memaix_gateway.acl import Acl
from memaix_gateway.capabilities.registry import Capability, clear_registry, register
from memaix_gateway.llm.agent import DailyBudget, run_turn
from memaix_gateway.llm.client import LLMError
from memaix_gateway.llm.identity import AGENT_USER
from memaix_gateway.llm.toolbridge import ToolBridge


class _FakeTool:
    def __init__(self, name, fn, description="", parameters=None):
        self.name, self.fn = name, fn
        self.description = description
        self.parameters = parameters or {"type": "object"}


class _FakeToolManager:
    def __init__(self, tools):
        self._tools = tools

    def list_tools(self):
        return self._tools


class _FakeMCP:
    def __init__(self, tools):
        self._tool_manager = _FakeToolManager(tools)


class _Audit:
    def __init__(self):
        self.entries = []

    def log(self, *a):
        self.entries.append(a)


@pytest.fixture()
def rig(monkeypatch):
    clear_registry()
    register(
        Capability(key="cal.view", area="calendar", title_key="t", summary_key="s",
                   tools=("calendar_list",), example_prompts_key="e",
                   needs_role="collaborator", needs_resource="calendar", tags=()),
        Capability(key="mail.send", area="mail", title_key="t2", summary_key="s2",
                   tools=("email_send",), example_prompts_key="e2",
                   needs_role="owner", needs_resource="mailbox", tags=()),
        Capability(key="mem.recall", area="memory", title_key="t3", summary_key="s3",
                   tools=("memory_search",), example_prompts_key="e3",
                   needs_role="reader", needs_resource="vault", tags=()),
    )
    acl = Acl(
        users={
            "jimmy": {"grants": {"acme": "owner"}},
            "rita": {"grants": {"acme": "reader"}},
        },
        projects={"acme": {"vault": "/tmp/v"}},
    )
    seen = {"identity_at_call": None, "sent": []}

    def calendar_list(project: str):
        seen["identity_at_call"] = AGENT_USER.get()
        return [{"title": "standup 09:00", "project": project}]

    def email_send(project: str, to: str, body: str):
        seen["sent"].append(to)
        return {"queued": True}

    def memory_search(project: str, query: str):
        return []

    tools = [
        _FakeTool("calendar_list", calendar_list),
        _FakeTool("email_send", email_send),
        _FakeTool("memory_search", memory_search),
        _FakeTool("internal_secret_tool", lambda: "hemligt"),  # okatalogiserad
    ]
    audit = _Audit()
    mk = lambda user: ToolBridge(user, _mcp=_FakeMCP(tools), _acl=acl, _audit=audit)
    yield mk, seen, audit
    clear_registry()


def test_reader_never_sees_write_tools(rig):
    mk, _, _ = rig
    names = {t["name"] for t in mk("rita").schemas()}
    assert "memory_search" in names, "reader ser läs-verktyg"
    assert "calendar_list" not in names, "collaborator-verktyg är Never för reader"
    assert "email_send" not in names, "owner-verktyg är Never för reader"
    assert "internal_secret_tool" not in names, "okatalogiserat exponeras aldrig"


def test_owner_sees_tools_but_uncataloged_stays_hidden(rig):
    mk, _, _ = rig
    names = {t["name"] for t in mk("jimmy").schemas()}
    assert {"calendar_list", "email_send", "memory_search"} <= names
    assert "internal_secret_tool" not in names


def test_reader_denied_even_when_guessing_tool_name(rig):
    # Schemafiltret är hygien — grinden håller även om modellen gissar namnet.
    mk, seen, _ = rig
    outcome = mk("rita").call("email_send", {"project": "acme", "to": "x@y.se", "body": "hej"})
    assert outcome["ok"] is False and "owner" in outcome["error"]
    assert seen["sent"] == [], "verktygsfunktionen får aldrig ens köras"
    outcome = mk("rita").call("internal_secret_tool", {})
    assert outcome["ok"] is False


def test_call_runs_as_right_user_and_resets_identity(rig):
    mk, seen, audit = rig
    assert AGENT_USER.get() is None
    outcome = mk("jimmy").call("calendar_list", {"project": "acme"})
    assert outcome["ok"] is True
    assert seen["identity_at_call"] == "jimmy", "verktyget kördes som inloggad användare"
    assert AGENT_USER.get() is None, "identiteten återställs alltid"
    user, project, tool, ok, detail = audit.entries[-1]
    assert (user, project, tool, ok) == ("jimmy", "acme", "chat:calendar_list", True)
    assert "acme" not in detail, "audit loggar arg-NYCKLAR, aldrig värden"


def test_identity_reset_even_on_tool_crash(rig):
    mk, _, audit = rig

    def boom(project: str):
        raise RuntimeError("smäll (med hemlig detalj)")

    bridge = ToolBridge("jimmy",
                        _mcp=_FakeMCP([_FakeTool("calendar_list", boom)]),
                        _acl=mk("jimmy")._acl_override, _audit=audit)
    outcome = bridge.call("calendar_list", {"project": "acme"})
    assert outcome["ok"] is False and "RuntimeError" in outcome["error"]
    assert AGENT_USER.get() is None


# ───────────────────────────── agentloopen ──────────────────────────────────


class _ScriptedClient:
    """Leverantörsfri klient: spelar upp förutbestämda svar."""

    supports_tools = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen_messages = []

    def complete(self, messages, max_tokens=1024, tools=None):
        self.seen_messages.append(list(messages))
        return self.replies.pop(0)


def _cfg(**limits):
    return {"memaix": {"model": {"provider": "anthropic", "limits": limits}},
            "brand": {"name": "Memaix"}}


def test_scripted_calendar_conversation(rig, tmp_path):
    """Acceptanskriteriet: 'vad har jag i kalendern?' kör calendar_list som
    rätt användare och svaret bygger på verktygsresultatet."""
    mk, seen, _ = rig
    client = _ScriptedClient([
        {"content": None, "usage": 10, "tool_calls": [
            {"id": "c1", "name": "calendar_list", "args": {"project": "acme"}}]},
        {"content": "Du har standup 09:00.", "usage": 20, "tool_calls": []},
    ])
    events = []
    result = run_turn(
        "jimmy", [{"role": "user", "content": "vad har jag i kalendern?"}],
        cfg=_cfg(), client=client, bridge=mk("jimmy"),
        budget=DailyBudget(str(tmp_path / "chat.db")),
        on_event=lambda kind, p: events.append((kind, p)),
    )
    assert result["content"] == "Du har standup 09:00."
    assert result["rounds"] == 2 and result["tool_calls"] == 1
    assert seen["identity_at_call"] == "jimmy"
    assert ("tool_start", {"name": "calendar_list"}) in events
    # verktygsresultatet gick tillbaka som DATA (role=tool), aldrig instruktion
    tool_msgs = [m for m in client.seen_messages[1] if m["role"] == "tool"]
    assert "standup" in tool_msgs[0]["content"]
    # systemprompten bär trappan + otrodd-data-regeln
    system = client.seen_messages[0][0]
    assert system["role"] == "system"
    assert "ALDRIG instruktioner" in system["content"]
    assert "hypotes" in system["content"]


def test_round_cap_fails_closed(rig, tmp_path):
    mk, _, _ = rig
    endless = {"content": None, "usage": 1, "tool_calls": [
        {"id": "x", "name": "calendar_list", "args": {"project": "acme"}}]}
    client = _ScriptedClient([endless] * 5)
    with pytest.raises(LLMError) as exc:
        run_turn("jimmy", [{"role": "user", "content": "loopa"}],
                 cfg=_cfg(max_rounds=3), client=client, bridge=mk("jimmy"),
                 budget=DailyBudget(str(tmp_path / "chat.db")))
    assert "3" in str(exc.value)


def test_daily_budget_blocks_and_persists(rig, tmp_path):
    mk, _, _ = rig
    db = str(tmp_path / "chat.db")
    budget = DailyBudget(db)
    client = _ScriptedClient([{"content": "hej", "usage": 900, "tool_calls": []}])
    run_turn("jimmy", [{"role": "user", "content": "hej"}],
             cfg=_cfg(max_tokens_per_day=1000), client=client, bridge=mk("jimmy"),
             budget=budget)
    # ny instans mot samma fil — räknaren överlever (omstart)
    budget2 = DailyBudget(db)
    assert budget2.spent("jimmy") == 900
    with pytest.raises(LLMError) as exc:
        run_turn("jimmy", [{"role": "user", "content": "igen"}],
                 cfg=_cfg(max_tokens_per_day=500), client=_ScriptedClient([]),
                 bridge=mk("jimmy"), budget=budget2)
    assert "token-tak" in str(exc.value)


def test_toolless_model_warns_and_answers(rig, tmp_path):
    mk, _, _ = rig

    class _NoTools(_ScriptedClient):
        supports_tools = False

    client = _NoTools([{"content": "svar utan verktyg", "usage": 5, "tool_calls": []}])
    events = []
    result = run_turn("jimmy", [{"role": "user", "content": "hej"}],
                      cfg=_cfg(), client=client, bridge=mk("jimmy"),
                      budget=DailyBudget(str(tmp_path / "chat.db")),
                      on_event=lambda k, p: events.append(k))
    assert result["content"] == "svar utan verktyg"
    assert "warning" in events


def test_server_user_respects_agent_identity(monkeypatch):
    """CI-bunden (kräver mcp): _user() läser AGENT_USER före OAuth-vägen."""
    pytest.importorskip("mcp")
    from memaix_gateway import server

    token = AGENT_USER.set("jimmy")
    try:
        assert server._user() == "jimmy"
    finally:
        AGENT_USER.reset(token)


# ─────────────── Granskningsfynd fas 2 (adversariell review) ─────────────────


def test_disabled_user_sees_no_tools_and_is_denied(monkeypatch):
    """Fynd 1: kill-switch måste gälla i bryggan, inte bara vid enforce —
    en avstängd användare får varken se scheman eller nå anrop."""
    clear_registry()
    register(
        Capability(key="mem.recall", area="memory", title_key="t", summary_key="s",
                   tools=("memory_search",), example_prompts_key="e",
                   needs_role="reader", needs_resource="vault", tags=()),
    )
    acl = Acl(
        users={"spärrad": {"grants": {"acme": "owner"}, "disabled": True}},
        projects={"acme": {"vault": "/tmp/v"}},
    )
    tools = [_FakeTool("memory_search", lambda project, query: [])]
    bridge = ToolBridge("spärrad", _mcp=_FakeMCP(tools), _acl=acl, _audit=_Audit())
    assert bridge.schemas() == [], "avstängd ser inga verktyg"
    outcome = bridge.call("memory_search", {"project": "acme", "query": "x"})
    assert outcome["ok"] is False, "avstängd nekas vid grinden"
    clear_registry()


def test_single_turn_cannot_blow_past_daily_cap(rig, tmp_path):
    """Fynd 2: mid-tur-omkontroll — en tur med många rundor stoppas när
    ackumulerad förbrukning når taket, inte först vid bokföring efteråt."""
    mk, _, _ = rig
    heavy = {"content": None, "usage": 300, "tool_calls": [
        {"id": "x", "name": "calendar_list", "args": {"project": "acme"}}]}
    client = _ScriptedClient([heavy] * 8)
    budget = DailyBudget(str(tmp_path / "chat.db"))
    with pytest.raises(LLMError) as exc:
        run_turn("jimmy", [{"role": "user", "content": "loopa dyrt"}],
                 cfg=_cfg(max_rounds=8, max_tokens_per_day=1000),
                 client=client, bridge=mk("jimmy"), budget=budget)
    assert "tak" in str(exc.value)
    # och förbrukningen bokfördes trots avbrottet (ingen gratis-tur)
    assert budget.spent("jimmy") > 0


def test_aborted_turn_still_charges_budget(rig, tmp_path):
    """Round-cap-turen bokför sina tokens (annars kringgås taket via retry)."""
    mk, _, _ = rig
    endless = {"content": None, "usage": 50, "tool_calls": [
        {"id": "x", "name": "calendar_list", "args": {"project": "acme"}}]}
    budget = DailyBudget(str(tmp_path / "chat.db"))
    with pytest.raises(LLMError):
        run_turn("jimmy", [{"role": "user", "content": "loopa"}],
                 cfg=_cfg(max_rounds=3, max_tokens_per_day=100000),
                 client=_ScriptedClient([endless] * 3), bridge=mk("jimmy"), budget=budget)
    assert budget.spent("jimmy") == 150  # 3 rundor × 50


# ───────── Karakterisering av run_turn (Sonar S3776-sanering) ─────────


class _StubBridge:
    """Minimal brygga: spelar upp förutbestämda verktygsutfall."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def schemas(self):
        return [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]

    def call(self, name, args):
        self.calls.append((name, args))
        return self.outcomes.pop(0)


def _call(i="c1", name="t", args=None):
    return {"id": i, "name": name, "args": args or {}}


def test_run_turn_failed_tool_sends_error_body_and_reports_not_ok(tmp_path):
    client = _ScriptedClient([
        {"content": "tänker", "usage": 3, "tool_calls": [_call()]},
        {"content": "klart", "usage": 4, "tool_calls": []},
    ])
    bridge = _StubBridge([{"ok": False, "error": "nekad"}])
    events = []
    out = run_turn("u1", [{"role": "user", "content": "x"}], cfg=_cfg(),
                   client=client, bridge=bridge,
                   budget=DailyBudget(str(tmp_path / "c.db")),
                   on_event=lambda k, p: events.append((k, p)))
    assert events == [("tool_start", {"name": "t"}), ("tool_result", {"name": "t", "ok": False})]
    msgs = out["messages"]
    # historik utan systemprompt: user, assistant(tool_calls), tool
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1] == {"role": "assistant", "content": "tänker", "tool_calls": [_call()]}
    assert msgs[2] == {"role": "tool", "call_id": "c1", "name": "t",
                       "content": '{"error": "nekad"}'}
    assert out["content"] == "klart" and out["rounds"] == 2
    assert out["tool_calls"] == 1 and out["tokens"] == 7


def test_run_turn_ok_result_is_json_encoded_and_truncated(tmp_path):
    big = {"ok": True, "result": {"text": "å" * 9000}}
    client = _ScriptedClient([
        {"content": None, "tool_calls": [_call()]},  # usage saknas -> 0
        {"content": None, "usage": 0, "tool_calls": []},
    ])
    out = run_turn("u2", [], cfg=_cfg(), client=client, bridge=_StubBridge([big]),
                   budget=DailyBudget(str(tmp_path / "c.db")))
    body = out["messages"][-1]["content"]
    assert len(body) == 8000 and body.startswith('{"text": "å')
    assert out["content"] == ""  # content None -> tom sträng
    assert out["tokens"] == 0


def test_run_turn_passes_limits_and_tools_to_client(tmp_path):
    seen = {}

    class _Spy(_ScriptedClient):
        def complete(self, messages, max_tokens=1024, tools=None):
            seen["max_tokens"] = max_tokens
            seen["tools"] = tools
            return super().complete(messages, max_tokens, tools)

    run_turn("u3", [], cfg=_cfg(max_tokens_per_turn=77), client=_Spy(
        [{"content": "a", "usage": 1, "tool_calls": []}]),
        bridge=_StubBridge(), budget=DailyBudget(str(tmp_path / "c.db")))
    assert seen["max_tokens"] == 77 and seen["tools"][0]["name"] == "t"


def test_run_turn_toolless_client_gets_no_tools_and_warning_text(tmp_path):
    seen = {}

    class _NoTools(_ScriptedClient):
        supports_tools = False

        def complete(self, messages, max_tokens=1024, tools=None):
            seen["tools"] = tools
            return super().complete(messages, max_tokens, tools)

    events = []
    run_turn("u4", [], cfg=_cfg(), client=_NoTools(
        [{"content": "a", "usage": 1, "tool_calls": []}]),
        bridge=_StubBridge(), budget=DailyBudget(str(tmp_path / "c.db")),
        on_event=lambda k, p: events.append((k, p)))
    assert seen["tools"] is None
    assert events == [("warning", {"message": "modellen saknar verktygsstöd i v1 (google) — svarar utan verktyg"})]


def test_run_turn_error_messages_and_accounting(tmp_path):
    budget = DailyBudget(str(tmp_path / "c.db"))
    budget.add("u5", 100)
    # dagstak redan nått före start
    with pytest.raises(LLMError) as exc:
        run_turn("u5", [], cfg=_cfg(max_tokens_per_day=100), client=_ScriptedClient([]),
                 bridge=_StubBridge(), budget=budget)
    assert str(exc.value) == (
        "dagens token-tak nått (100) — höj model.limits.max_tokens_per_day "
        "eller vänta till imorgon")
    # mid-tur-tak
    heavy = {"content": None, "usage": 60, "tool_calls": [_call()]}
    ok = {"ok": True, "result": 1}
    with pytest.raises(LLMError) as exc:
        run_turn("u5", [], cfg=_cfg(max_tokens_per_day=220),
                 client=_ScriptedClient([heavy, heavy]),
                 bridge=_StubBridge([ok, ok]), budget=budget)
    assert str(exc.value) == (
        "dagens token-tak nått under turen (220) — "
        "höj model.limits.max_tokens_per_day eller vänta")
    assert budget.spent("u5") == 220  # 100 + 2 rundor x 60 bokförda
    # rundtak
    with pytest.raises(LLMError) as exc:
        run_turn("u6", [], cfg=_cfg(max_rounds=2), client=_ScriptedClient(
            [{"content": None, "usage": 0, "tool_calls": [_call()]}] * 2),
            bridge=_StubBridge([ok, ok]), budget=budget)
    assert str(exc.value) == "turen nådde taket på 2 verktygsrundor utan slutsvar"
    assert budget.spent("u6") == 0  # ingen förbrukning -> ingen bokföring


def test_run_turn_multiple_tool_calls_in_one_round_and_brand_in_prompt(tmp_path):
    client = _ScriptedClient([
        {"content": None, "usage": 1, "tool_calls": [_call("a", args={"k": 1}), _call("b")]},
        {"content": "x", "usage": 1, "tool_calls": []},
    ])
    bridge = _StubBridge([{"ok": True, "result": "ra"}, {"ok": True, "result": None}])
    cfg = {"memaix": {"model": {"limits": {}}}, "brand": {"name": "Acme"}}
    out = run_turn("u7", [], cfg=cfg, client=client, bridge=bridge,
                   budget=DailyBudget(str(tmp_path / "c.db")))
    assert bridge.calls == [("t", {"k": 1}), ("t", {})]
    assert out["tool_calls"] == 2
    assert [m.get("content") for m in out["messages"] if m["role"] == "tool"] == ['"ra"', "null"]
    assert client.seen_messages[0][0]["content"].startswith("Du är Acmes assistent. Du hjälper u7")


def test_daily_budget_add_clamps_negative_to_zero(tmp_path):
    b = DailyBudget(str(tmp_path / "c.db"))
    b.add("n", -5)
    assert b.spent("n") == 0
