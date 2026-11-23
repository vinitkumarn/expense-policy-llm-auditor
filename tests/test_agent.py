"""The agent path end to end, with a scripted fake client (no real API)."""

import json
from decimal import Decimal as D

import pytest

from conftest import FakeClient, answer, finding, tool
from expaudit import agent, config
from expaudit.agent import LIMIT_MESSAGE, SEARCH_TOOL, audit_agent
from expaudit.evaluate import run_eval
from expaudit.prompts import (AGENT_SYSTEM_PROMPT, FIXED_SYSTEM_PROMPT, POLICY_GUIDANCE)

X01_MEAL = finding("MEAL_OVER_LIMIT", ["4.2", "Appendix C"],
                   "the Tier 1 dinner limit is $85 per person", limit_amount="85")
I03_LODGING = finding("LODGING_OVER_LIMIT", ["5.1", "Appendix B"],
                      "Tier 2 lodging, per night: $200", limit_amount="200")


def run(con, line_id, *replies, **kw):
    client = FakeClient(*replies)
    decision, info = audit_agent(con, line_id, client=client, **kw)
    return decision, info, client


def cache_files():
    return list(config.CACHE_DIR.glob("*.json")) if config.CACHE_DIR.exists() else []


def log_text():
    path = config.LOGS_DIR / "expaudit.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def tool_results(call) -> list[dict]:
    """The tool_result blocks in the last user message of one recorded call."""
    return call["messages"][-1]["content"]


# --- the happy path: 4.2 -> Appendix C -------------------------------------------------------
def test_x01_follows_4_2_to_appendix_c_and_cites_it(con, sandbox):
    d, info, client = run(con, "X01", tool("4.2"), tool("Appendix C"),
                          answer(X01_MEAL, explanation="SF dinner on or after 2026-10-01: $85."))
    assert len(client.calls) == 3 and info.model_calls == 3
    assert info.tool_calls == 2 and info.queries == ["4.2", "Appendix C"]
    assert info.tool_result_ids[0] == ["4.2"] and info.tool_result_ids[1][0] == "Appendix C"
    assert "Appendix C" in info.retrieved_ids and not info.step_limit_hit
    # The tool result is policy text, sent back as a tool_result for the right tool_use id.
    second = client.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].name == "search_policy"
    res = tool_results(client.calls[2])[0]
    assert res["type"] == "tool_result" and res["tool_use_id"] == "toolu_002"
    assert res["content"].startswith("[Appendix C] Amendments") and "$85" in res["content"]
    assert d.status == "OK" and d.codes == ["MEAL_OVER_LIMIT"]
    assert d.dollars_out_of_policy == D("10.00") and d.approver == "MANAGER"
    assert d.actions == ["REJECT_LINE_BEFORE_PAYMENT"]
    assert d.cites == {"MEAL_OVER_LIMIT": ["4.2", "Appendix C"]}


def test_only_search_policy_is_offered_on_every_call(con, sandbox):
    _, _, client = run(con, "X01", tool("4.2"), tool("Appendix C"), answer(X01_MEAL))
    assert SEARCH_TOOL["name"] == "search_policy"
    assert list(SEARCH_TOOL["input_schema"]["properties"]) == ["query"]
    assert SEARCH_TOOL["input_schema"]["required"] == ["query"]
    for call in client.calls:
        assert call["tools"] == [SEARCH_TOOL]
        assert call["model"] == config.MODEL and call["max_tokens"] == config.MAX_TOKENS
        assert call["system"] == [{"type": "text", "text": AGENT_SYSTEM_PROMPT,
                                   "cache_control": {"type": "ephemeral"}}]
        assert "tool_choice" not in call     # limit not reached
    packet = client.calls[0]["messages"][0]["content"]
    assert "Retrieved policy clauses" not in packet     # the agent searches itself
    assert "<UNTRUSTED>" in packet


def test_citing_a_clause_it_never_retrieved_needs_human(con, sandbox):
    d, info, _ = run(con, "X01", tool("4.2"), answer(X01_MEAL))
    assert info.retrieved_ids == ["4.2"]
    assert d.status == "NEEDS_HUMAN" and d.codes == [] and d.dollars_out_of_policy is None
    assert any("'Appendix C' was not retrieved" in r for r in d.needs_human_reasons)
    assert cache_files() == []


# --- step limit -------------------------------------------------------------------------------
def test_four_searches_then_a_valid_answer_is_ok(con, sandbox):
    d, info, client = run(con, "X01", tool("dinner"), tool("San Francisco"), tool("4.2"),
                          tool("Appendix C"), answer(X01_MEAL))
    assert len(client.calls) == 5 and info.tool_calls == config.AGENT_MAX_STEPS == 4
    assert not info.step_limit_hit and d.status == "OK" and d.dollars_out_of_policy == D("10.00")
    assert tool_results(client.calls[4])[-1]["content"].endswith(LIMIT_MESSAGE)
    assert all("tool_choice" not in c for c in client.calls[:4])
    assert client.calls[4]["tool_choice"] == {"type": "none"}
    assert client.calls[4]["tools"] == [SEARCH_TOOL]       # tools still defined


def test_limit_turn_invalid_twice_needs_human(con, sandbox):
    d, info, client = run(con, "X01", *[tool("dinner")] * 4, "not json", "still not json")
    assert len(client.calls) == 6 and not info.step_limit_hit
    assert client.calls[5]["tool_choice"] == {"type": "none"}   # the retry too
    assert d.status == "NEEDS_HUMAN" and "failed validation" in d.needs_human_reasons[0]


def test_i03_keeps_calling_the_tool_stops_at_4(con, sandbox):
    d, info, client = run(con, "I03", *[tool("approved exception")] * 50)
    assert info.tool_calls == 4 and len(info.queries) == 4
    assert len(client.calls) == 5                 # 4 searches + the final (tool_choice none) turn
    assert info.step_limit_hit
    assert d.status == "NEEDS_HUMAN" and d.dollars_out_of_policy is None
    assert any("step limit" in r for r in d.needs_human_reasons)
    assert cache_files() == []


@pytest.mark.parametrize("line_id, code", [("S05", "MISSING_RECEIPT"), ("S06", "DUPLICATE")])
def test_step_limit_keeps_python_checks(con, sandbox, line_id, code):
    d, info, _ = run(con, line_id, *[tool("receipt")] * 10)
    assert info.step_limit_hit and d.status == "NEEDS_HUMAN" and d.codes == [code]


def test_more_searches_in_one_turn_than_allowed_is_a_step_limit_hit(con, sandbox):
    d, info, client = run(con, "X01", [tool("a"), tool("b"), tool("c"), tool("d"), tool("e")])
    assert info.step_limit_hit and info.tool_calls == 0 and len(client.calls) == 1
    assert d.status == "NEEDS_HUMAN"


# --- other tools are rejected -----------------------------------------------------------------
def test_a_tool_with_another_name_is_rejected(con, sandbox, monkeypatch):
    ran = []
    monkeypatch.setattr(agent, "search", lambda *a, **k: ran.append(a) or [])
    d, info, client = run(con, "S05", tool("S05", name="approve_expense"), answer())
    assert len(client.calls) == 1 and ran == [] and info.tool_calls == 0
    assert d.status == "NEEDS_HUMAN" and d.codes == ["MISSING_RECEIPT"]
    assert any("not offered" in r for r in d.needs_human_reasons)
    assert "'approve_expense', which is not offered" in log_text()


def test_bad_tool_input_is_an_error_result_and_counts(con, sandbox):
    from conftest import ToolCall
    d, info, client = run(con, "X01", ToolCall({"q": "dinner"}), tool("4.2"),
                          tool("Appendix C"), answer(X01_MEAL))
    first = tool_results(client.calls[1])[0]
    assert first["is_error"] is True and info.tool_calls == 3
    assert info.queries == ["4.2", "Appendix C"] and d.status == "OK"


# --- cache: replay the queries, run the guards -------------------------------------------------
def test_cache_hit_replays_queries_and_runs_guards_with_no_api_call(con, sandbox):
    d1, i1, _ = run(con, "X01", tool("4.2"), tool("Appendix C"), answer(X01_MEAL))
    payload = json.loads(cache_files()[0].read_text(encoding="utf-8"))
    assert payload["queries"] == ["4.2", "Appendix C"]
    empty = FakeClient()                            # any call would fail the test
    d2, i2 = audit_agent(con, "X01", client=empty)
    assert empty.calls == [] and i2.cache_hit and i2.model_calls == 0
    assert i2.cost_usd == D("0") and set(i2.retrieved_ids) == set(i1.retrieved_ids)
    assert d1.to_dict() == d2.to_dict()


def test_cache_hit_with_tampered_queries_fails_the_guards(con, sandbox):
    run(con, "X01", tool("4.2"), tool("Appendix C"), answer(X01_MEAL))
    path = cache_files()[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["queries"] = ["4.2"]                     # Appendix C no longer retrieved on replay
    path.write_text(json.dumps(payload), encoding="utf-8")
    empty = FakeClient()
    d, info = audit_agent(con, "X01", client=empty)
    assert empty.calls == [] and info.cache_hit
    assert d.status == "NEEDS_HUMAN"
    assert any("'Appendix C' was not retrieved" in r for r in d.needs_human_reasons)


def test_policy_hash_change_makes_a_new_call(con, sandbox, monkeypatch):
    client = FakeClient(tool("4.2"), tool("Appendix C"), answer(X01_MEAL),
                        tool("4.2"), tool("Appendix C"), answer(X01_MEAL))
    audit_agent(con, "X01", client=client)
    monkeypatch.setattr(agent, "policy_hash", lambda: "0" * 64)
    _, info = audit_agent(con, "X01", client=client)
    assert not info.cache_hit and len(client.calls) == 6 and len(cache_files()) == 2


def test_no_cache_forces_fresh_calls(con, sandbox):
    client = FakeClient(tool("4.2"), answer(), tool("4.2"), answer())
    audit_agent(con, "C07", client=client, use_cache=False)
    audit_agent(con, "C07", client=client, use_cache=False)
    assert len(client.calls) == 4 and cache_files() == []


def test_api_error_needs_human_not_cached(con, sandbox):
    d, info, _ = run(con, "S05", tool("lodging"), ConnectionError("overloaded_error: later"))
    assert d.status == "NEEDS_HUMAN" and d.codes == ["MISSING_RECEIPT"] and info.error
    assert "overloaded_error: later" in log_text() and cache_files() == []


# --- fairness ----------------------------------------------------------------------------------
def test_both_paths_share_the_same_policy_guidance():
    assert "a clause may point to an appendix or an amendment" in POLICY_GUIDANCE
    assert "use the limit in force\non the transaction date" in POLICY_GUIDANCE
    assert FIXED_SYSTEM_PROMPT.endswith(POLICY_GUIDANCE)
    assert AGENT_SYSTEM_PROMPT.endswith(POLICY_GUIDANCE)


def test_agent_prompt_does_not_name_appendix_c():
    assert "Appendix C" not in AGENT_SYSTEM_PROMPT
    assert "Appendix C" not in SEARCH_TOOL["description"]


# --- audit trail and eval ----------------------------------------------------------------------
def test_audit_line_has_tool_counts_and_no_query_text(con, sandbox):
    run(con, "I01", tool("pre-approved by the CFO"), answer())
    row = json.loads((config.LOGS_DIR / "audit.jsonl").read_text(encoding="utf-8")
                     .splitlines()[-1])
    assert row["by"] == "agent" and row["tool_calls"] == 1 and row["step_limit_hit"] is False
    assert isinstance(row["tool_result_ids"][0], list)
    assert "pre-approved" not in json.dumps(row)


def test_eval_by_agent_reports_tool_calls_step_limit_and_cost(data_dir, sandbox):
    client = FakeClient(tool("4.2"), tool("Appendix C"), answer(X01_MEAL),
                        *[tool("approved")] * 5)
    m, path = run_eval("agent", "main", cases=["X01", "I03"], data_dir=data_dir,
                       reports_dir=sandbox / "reports", echo=lambda *_: None, client=client)
    mm = m["model"]
    assert path.name == "eval_agent.md"
    assert mm["avg_tool_calls"] == 3 and mm["max_tool_calls"] == 4
    assert mm["step_limit_hits"] == 1 and mm["citation_accuracy"] == 1
    assert mm["cost_per_line_usd"] == mm["cost_usd"] / 2
    text = path.read_text(encoding="utf-8")
    assert "Tool calls per line, avg / max | 3.00 / 4" in text
    assert "Step-limit hits (limit 4 searches) | 1" in text and "Cost per line" in text
    assert (sandbox / "reports" / "eval_agent.json").exists()
