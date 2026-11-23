from datetime import date
from decimal import Decimal

import pytest

from expaudit import db
from expaudit.db import Line
from expaudit.generator import build


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    """Planted data + DuckDB built once into a temp folder (never touches data/)."""
    out = tmp_path_factory.mktemp("data")
    build(out)
    db.build_db(out / "csv", out / db.DB_FILE).close()
    return out


@pytest.fixture()
def con(data_dir):
    connection = db.connect(data_dir / db.DB_FILE)
    yield connection
    connection.close()


def make_line(**overrides) -> Line:
    """A clean SUBMITTED Tier 2 dinner for one STAFF employee; override any field."""
    fields = dict(
        line_id="T01", report_id="RPT-T01", employee_id="E104", grade="STAFF",
        report_status="SUBMITTED", submitted_date=date(2026, 10, 20),
        transaction_date=date(2026, 10, 15), merchant="Test Kitchen", category="Meals",
        amount=Decimal("50.00"), city="Denver", nights=None, attendees_count=1,
        receipt_attached=True, memo="Dinner.",
    )
    fields.update(overrides)
    return Line(**fields)


# --- scripted fake Claude client (no real API is ever called in tests) -----------------------
import json
from dataclasses import dataclass
from types import SimpleNamespace

from expaudit import config


@dataclass
class ToolCall:
    input: dict
    name: str = "search_policy"


def tool(query, name="search_policy"):
    """A scripted tool_use request (search_policy by default)."""
    return ToolCall({"query": query}, name)


class FakeClient:
    """Stands in for anthropic.Anthropic(). Each create() pops the next scripted reply.

    A reply is a str (the model's text), a dict (dumped as JSON), an Exception (raised), or a
    ToolCall / list of ToolCalls (the model asks to use tools; stop_reason "tool_use").
    Every call's keyword arguments are kept in .calls for the tests to inspect.
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.tool_ids = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.replies:
            raise AssertionError("FakeClient: no scripted reply left")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200,
                                cache_read_input_tokens=0, cache_creation_input_tokens=800)
        if isinstance(reply, ToolCall):
            reply = [reply]
        if isinstance(reply, list):
            blocks = []
            for t in reply:
                self.tool_ids += 1
                blocks.append(SimpleNamespace(type="tool_use", id=f"toolu_{self.tool_ids:03d}",
                                              name=t.name, input=t.input))
            return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=usage)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=800),
        )


def answer(*findings, explanation="See the cited clause."):
    return {"findings": list(findings), "explanation": explanation}


def finding(code, cites, quote, **extra):
    return {"code": code, "cited_clause_ids": list(cites), "policy_quote": quote, **extra}


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Logs and the response cache go to a temp folder for each test."""
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    return tmp_path
