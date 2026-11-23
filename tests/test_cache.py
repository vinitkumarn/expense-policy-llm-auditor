"""Response cache: same packet once; any key part change -> new call; bad answers never cached."""

import json
from decimal import Decimal as D

import pytest

from conftest import FakeClient, answer, finding
from expaudit import cache, config, fixed
from expaudit.__main__ import main
from expaudit.evaluate import run_eval

S01 = answer(finding("MEAL_OVER_LIMIT", ["4.2", "Appendix B"], "Tier 2 dinner, per person: $70",
                     limit_amount="70"))


def cache_files():
    return list(config.CACHE_DIR.glob("*.json")) if config.CACHE_DIR.exists() else []


def test_cache_key_is_sha256_of_all_four_parts():
    base = cache.cache_key("m", "v1", "h", "packet")
    assert len(base) == 64
    assert len({base, cache.cache_key("m2", "v1", "h", "packet"),
                cache.cache_key("m", "v2", "h", "packet"), cache.cache_key("m", "v1", "h2", "packet"),
                cache.cache_key("m", "v1", "h", "packet2")}) == 5


def test_same_packet_calls_once(con, sandbox):
    client = FakeClient(S01)
    d1, i1 = fixed.audit_fixed(con, "S01", client=client)
    d2, i2 = fixed.audit_fixed(con, "S01", client=client)
    assert len(client.calls) == 1
    assert (i1.cache_hit, i2.cache_hit) == (False, True)
    assert i2.model_calls == 0 and i2.input_tokens == 0 and i2.cost_usd == D("0")
    assert d1.to_dict() == d2.to_dict()
    payload = json.loads(cache_files()[0].read_text(encoding="utf-8"))
    assert payload["queries"] == [] and payload["answer"]["findings"][0]["code"] == "MEAL_OVER_LIMIT"


@pytest.mark.parametrize("change", ["prompt", "model", "policy"])
def test_prompt_model_or_policy_change_makes_a_new_call(con, sandbox, monkeypatch, change):
    client = FakeClient(S01, S01)
    fixed.audit_fixed(con, "S01", client=client)
    kwargs = {}
    if change == "prompt":
        kwargs["prompt_version"] = "fixed-test-other"
    elif change == "model":
        kwargs["model"] = "claude-opus-5-5"
    else:
        monkeypatch.setattr(fixed, "policy_hash", lambda: "0" * 64)
    _, info = fixed.audit_fixed(con, "S01", client=client, **kwargs)
    assert len(client.calls) == 2 and not info.cache_hit
    if change == "model":
        assert client.calls[1]["model"] == "claude-opus-5-5"
    assert len(cache_files()) == 2


def test_no_cache_forces_a_fresh_call_and_writes_nothing(con, sandbox):
    client = FakeClient(S01, S01)
    fixed.audit_fixed(con, "S01", client=client, use_cache=False)
    fixed.audit_fixed(con, "S01", client=client, use_cache=False)
    assert len(client.calls) == 2 and cache_files() == []


@pytest.mark.parametrize("first", [ConnectionError("network down"),
                                   "not json",                                     # invalid twice
                                   answer(finding("MEAL_OVER_LIMIT", ["6.1"], "x"))])  # guard fail
def test_errors_invalid_and_guard_failed_answers_are_not_cached(con, sandbox, first):
    replies = [first, "still not json"] if first == "not json" else [first]
    client = FakeClient(*replies, S01)
    d1, _ = fixed.audit_fixed(con, "S01", client=client)
    assert d1.status == "NEEDS_HUMAN" and cache_files() == []
    d2, info = fixed.audit_fixed(con, "S01", client=client)   # calls again
    assert not info.cache_hit and d2.status == "OK" and len(cache_files()) == 1


def test_guards_run_on_cache_hits(con, sandbox):
    client = FakeClient(S01)
    fixed.audit_fixed(con, "S01", client=client)
    path = cache_files()[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["answer"]["findings"][0]["limit_amount"] = "7"      # tampered: 7 is not in the quote
    path.write_text(json.dumps(payload), encoding="utf-8")
    d, info = fixed.audit_fixed(con, "S01", client=client)
    assert info.cache_hit and len(client.calls) == 1            # no new call
    assert d.status == "NEEDS_HUMAN" and info.guard_problems


def test_invalid_cached_payload_needs_human(con, sandbox):
    client = FakeClient(S01)
    fixed.audit_fixed(con, "S01", client=client)
    path = cache_files()[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["answer"]["approver"] = "MANAGER"
    path.write_text(json.dumps(payload), encoding="utf-8")
    d, info = fixed.audit_fixed(con, "S01", client=client)
    assert info.cache_hit and d.status == "NEEDS_HUMAN" and len(client.calls) == 1


def test_cache_hit_hides_an_unverified_explanation_again(con, sandbox):
    bad = answer(finding("MEAL_OVER_LIMIT", ["4.2", "Appendix B"],
                         "Tier 2 dinner, per person: $70", limit_amount="70"),
                 explanation="$95 is $25 over.")
    client = FakeClient(bad)
    d1, info1 = fixed.audit_fixed(con, "S01", client=client)
    d2, info2 = fixed.audit_fixed(con, "S01", client=client)
    assert info2.cache_hit and len(client.calls) == 1
    assert info1.explanation_hidden and info2.explanation_hidden
    assert d1.to_dict() == d2.to_dict() and d2.status == "OK"
    rows = [json.loads(r) for r in
            (config.LOGS_DIR / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["explanation_hidden"] is True


# --- eval and CLI with the fixed path ---------------------------------------------------------
def test_eval_by_fixed_writes_a_report_with_model_metrics(data_dir, sandbox):
    client = FakeClient(answer(*S01["findings"], explanation="$95 is $25 over."), answer())
    m, path = run_eval("fixed", "main", cases=["S01", "S05"], data_dir=data_dir,
                       reports_dir=sandbox / "reports", echo=lambda *_: None, client=client)
    assert path.name == "eval_fixed.md"
    assert m["lines_fully_correct"] == 2
    assert m["model"]["model_calls"] == 2 and m["model"]["citation_accuracy"] == 1
    text = path.read_text(encoding="utf-8")
    assert "## Model path" in text and "Cache hit rate" in text
    assert m["model"]["explanations_hidden"] == 1
    assert "| Explanations hidden (unverified number; decision kept) | 1 |" in text
    m2, _ = run_eval("fixed", "main", cases=["S01", "S05"], data_dir=data_dir,
                     reports_dir=sandbox / "reports", echo=lambda *_: None, client=client)
    assert m2["model"]["cache_hit_rate"] == 1 and m2["model"]["model_calls"] == 0


def test_cli_fixed_without_key_is_friendly(data_dir, sandbox, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert main(["run", "--line", "S01", "--by", "fixed", "--no-cache",
                 "--data", str(data_dir)]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out
