"""Model path 2: agentic retrieval.

Claude gets the expense line (no clauses) and ONE read-only tool, search_policy(query). It
decides what to look up next from what it just read (4.2 -> Appendix B -> an amendment). At
most AGENT_MAX_STEPS searches per line. After the last allowed search Python tells the model to
answer and makes the next call with tool_choice "none". If the model still asks for another
search, the run stops and the line goes to NEEDS_HUMAN with the Python checks kept.

A tool with any other name is never run. The first time in a run, the model gets an error
tool_result telling it only search_policy exists and to answer in plain JSON text (this does
not count as a search). A second unknown tool in the same run stops it -> NEEDS_HUMAN.

The retrieved set is the union of the clauses returned by the agent's own searches; the same
guards, decide.py and audit trail as the fixed path are used on the final answer.
"""

from __future__ import annotations

import time

from expaudit import audit, cache, config
from expaudit.checks import run_checks
from expaudit.db import load_line
from expaudit.decide import Decision
from expaudit.fixed import RunInfo, _add_usage, _system, _text, finish, make_client
from expaudit.packet import build_packet
from expaudit.policy import policy_hash
from expaudit.prompts import AGENT_SYSTEM_PROMPT
from expaudit.schema import ModelAnswer, parse_answer
from expaudit.search import search

PROMPT_VERSION = "agent-v4"
SYSTEM_PROMPT = AGENT_SYSTEM_PROMPT
TOOL_NAME = "search_policy"
SEARCH_TOOL = {
    "name": TOOL_NAME,
    "description": ("Read-only keyword search of the company travel policy. Returns the top 3 "
                    "matching clauses with their ids, titles and text. A query that is exactly "
                    "a clause id returns that clause first."),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "a short search query"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}
LIMIT_MESSAGE = "Search limit reached. Give your final answer now."
UNKNOWN_TOOL_MESSAGE = ("That tool does not exist. Only search_policy exists. "
                        "Give your final answer as plain JSON text.")
MAX_UNKNOWN_TOOLS = 1     # the first unknown tool gets an error reply; the second stops the run
NO_TOOLS = {"type": "none"}


class ToolRejected(Exception):
    """The model asked for a tool that was not offered."""


class StepLimit(Exception):
    """The model asked for more than AGENT_MAX_STEPS searches."""


def format_results(clauses: list[dict]) -> str:
    """Policy text (trusted), in the same [id] title / text layout as the fixed packet."""
    if not clauses:
        return "No matching clauses."
    return "\n\n".join(f"[{c['clause_id']}] {c['title']}\n{c['text']}" for c in clauses)


def _tool_uses(response) -> list:
    return [b for b in response.content if getattr(b, "type", "") == "tool_use"]


def _run_search(block, info: RunInfo, retrieved: dict[str, dict]) -> dict:
    """Run one search_policy call and return its tool_result block."""
    info.tool_calls += 1
    query = block.input.get("query") if isinstance(block.input, dict) else None
    if not isinstance(query, str) or not query.strip():
        info.tool_result_ids.append([])
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": 'search_policy needs one string field "query".'}
    clauses = search(query, config.RETRIEVAL_TOP_K)
    info.queries.append(query)
    info.tool_result_ids.append([c["clause_id"] for c in clauses])
    for c in clauses:
        retrieved.setdefault(c["clause_id"], c)
    return {"type": "tool_result", "tool_use_id": block.id, "content": format_results(clauses)}


def run_agent(client, packet: str, info: RunInfo, model: str,
              retrieved: dict[str, dict], line_id: str = "") -> ModelAnswer:
    """The tool loop. Fills `retrieved` and `info`; returns a schema-valid answer.

    Raises ToolRejected, StepLimit, or ValueError (invalid after one retry). API errors propagate.
    """
    messages: list[dict] = [{"role": "user", "content": packet}]
    retried = False
    last_error = None
    unknown_tools = 0
    while True:
        kwargs = dict(model=model, max_tokens=config.MAX_TOKENS, system=_system(SYSTEM_PROMPT),
                      tools=[SEARCH_TOOL], messages=messages)
        if info.tool_calls >= config.AGENT_MAX_STEPS:
            kwargs["tool_choice"] = NO_TOOLS
        response = client.messages.create(**kwargs)
        info.model_calls += 1
        _add_usage(info, response.usage)

        uses = _tool_uses(response)
        if uses:
            searches = [b for b in uses if b.name == TOOL_NAME]
            for block in uses:
                if block.name != TOOL_NAME:            # never executed
                    unknown_tools += 1
                    message = f"model asked for tool {block.name!r}, which is not offered"
                    if unknown_tools > MAX_UNKNOWN_TOOLS:
                        raise ToolRejected(message)
                    audit.log_problem(line_id, f"{message}; replied with an error, run continues")
            if info.tool_calls + len(searches) > config.AGENT_MAX_STEPS:
                info.step_limit_hit = True
                raise StepLimit(f"model asked for more than {config.AGENT_MAX_STEPS} searches")
            results = [_run_search(b, info, retrieved) if b.name == TOOL_NAME else
                       {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                        "content": UNKNOWN_TOOL_MESSAGE}
                       for b in uses]
            if searches and info.tool_calls >= config.AGENT_MAX_STEPS:
                last = [r for r, b in zip(results, uses) if b.name == TOOL_NAME][-1]
                last["content"] = f"{last['content']}\n\n{LIMIT_MESSAGE}"
            messages = messages + [{"role": "assistant", "content": response.content},
                                   {"role": "user", "content": results}]
            continue

        try:
            return parse_answer(_text(response))
        except ValueError as exc:
            last_error = f"stop_reason={getattr(response, 'stop_reason', None)}: {exc}"
            if retried:
                raise ValueError(f"invalid answer after one retry: {last_error}") from exc
            retried = True
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Your answer failed validation:\n" + str(exc)
                 + "\nReply with the corrected JSON object only."},
            ]


def replay(queries: list[str]) -> dict[str, dict]:
    """Rebuild the retrieved set from cached queries (local search, free, deterministic)."""
    retrieved: dict[str, dict] = {}
    for q in queries:
        for c in search(q, config.RETRIEVAL_TOP_K):
            retrieved.setdefault(c["clause_id"], c)
    return retrieved


def audit_agent(con, line_id: str, client=None, use_cache: bool = True,
                cache_dir=None, model: str | None = None,
                prompt_version: str = PROMPT_VERSION) -> tuple[Decision, RunInfo]:
    model = model or config.MODEL
    started = time.perf_counter()
    line = load_line(con, line_id)
    python_findings = run_checks(con, line)          # always kept (never-drop)

    packet = build_packet(line)                      # no clauses: the agent searches itself
    p_hash = policy_hash()
    key = cache.cache_key(model, prompt_version, p_hash, packet)
    info = RunInfo()
    retrieved: dict[str, dict] = {}

    answer: ModelAnswer | None = None
    reasons: list[str] = []
    hit = cache.get(key, cache_dir) if use_cache else None
    if hit is not None:
        info.cache_hit = True
        try:
            answer = ModelAnswer.model_validate(hit["answer"])
            info.queries = [str(q) for q in hit.get("queries", [])]
            retrieved = replay(info.queries)
            info.tool_calls = len(info.queries)
        except (ValueError, TypeError, KeyError) as exc:
            answer = None
            reasons.append(f"cached answer is invalid: {exc}")
            audit.log_problem(line.line_id, reasons[-1])
    else:
        try:
            answer = run_agent(client or make_client(), packet, info, model, retrieved,
                               line_id=line.line_id)
        except ToolRejected as exc:
            reasons.append("agent asked for a tool that is not offered (rejected)")
            audit.log_problem(line.line_id, str(exc))
        except StepLimit as exc:
            reasons.append(f"agent step limit hit ({config.AGENT_MAX_STEPS} searches)")
            audit.log_problem(line.line_id, str(exc))
        except ValueError as exc:
            reasons.append("model answer failed validation")
            audit.log_problem(line.line_id, str(exc))
        except RuntimeError:
            raise
        except Exception as exc:   # API / network errors: full message to the log, never cached
            info.error = f"{type(exc).__name__}: {exc}"
            reasons.append("model call failed")
            audit.log_problem(line.line_id, f"model call failed: {info.error}")

    info.retrieved_ids = list(retrieved)
    decision = finish(line, python_findings, answer, reasons, retrieved, info, by="agent",
                      model=model, prompt_version=prompt_version, p_hash=p_hash, packet=packet,
                      key=key, use_cache=use_cache, cache_dir=cache_dir, started=started)
    return decision, info
