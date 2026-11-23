"""Model path 1: fixed retrieval.

Python searches the policy once (BM25, top 3 clauses), then makes ONE Claude call with no tools.
The answer is schema-checked (one retry with the full validation error), every claim is checked
by the guards, and only then do the model findings go to decide.py next to the Python checks.
Any failure -> NEEDS_HUMAN with the Python checks kept (never-drop).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal

from expaudit import audit, cache, config
from expaudit.checks import run_checks
from expaudit.db import Line, load_line
from expaudit.decide import Decision, Finding, decide
from expaudit.guards import run_guards
from expaudit.packet import build_packet
from expaudit.policy import clauses_by_id, policy_hash
from expaudit.prompts import FIXED_SYSTEM_PROMPT
from expaudit.schema import ModelAnswer, parse_answer
from expaudit.search import search, tokenize

PROMPT_VERSION = "fixed-v4"
SYSTEM_PROMPT = FIXED_SYSTEM_PROMPT


@dataclass
class RunInfo:
    """What happened on one model-path line (for the audit trail and the eval report)."""
    cache_hit: bool = False
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: Decimal | None = Decimal("0")
    latency_ms: int = 0
    retrieved_ids: list[str] = field(default_factory=list)
    cited_ids: list[str] = field(default_factory=list)
    guard_problems: list[str] = field(default_factory=list)
    explanation_hidden: bool = False
    explanation: str | None = None          # what may be shown (hidden text if unverified)
    error: str | None = None
    # Agent path only (stay 0 / empty on the fixed path).
    tool_calls: int = 0
    step_limit_hit: bool = False
    queries: list[str] = field(default_factory=list)
    tool_result_ids: list[list[str]] = field(default_factory=list)


def make_client():
    """The real Anthropic client. Built only on a cache miss."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example) "
                           "or use --by naive|rules.")
    import anthropic
    return anthropic.Anthropic()


def query_for(line: Line) -> str:
    """category + city + memo words (the memo is untrusted, but only steers the search)."""
    return " ".join([line.category, line.city, *tokenize(line.memo)])


def _system(prompt: str = SYSTEM_PROMPT) -> list[dict]:
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def _text(response) -> str:
    return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")


def _add_usage(info: RunInfo, usage) -> None:
    for name in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens"):
        setattr(info, name, getattr(info, name) + (getattr(usage, name, 0) or 0))


def cost_usd(model: str, info: RunInfo) -> Decimal | None:
    price = config.PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    total = (info.input_tokens * price["input"] + info.output_tokens * price["output"]
             + info.cache_creation_input_tokens * price["cache_write"]
             + info.cache_read_input_tokens * price["cache_read"])
    return (total / Decimal(1_000_000)).quantize(Decimal("0.000001"))


def ask_model(client, packet: str, info: RunInfo, model: str) -> ModelAnswer:
    """ONE messages.create (no tools); one retry with the full validation error.

    Raises ValueError if the answer is still invalid after the retry. API errors propagate.
    """
    messages = [{"role": "user", "content": packet}]
    last_error = None
    for _attempt in range(2):
        response = client.messages.create(model=model, max_tokens=config.MAX_TOKENS,
                                          system=_system(), messages=messages)
        info.model_calls += 1
        _add_usage(info, response.usage)
        try:
            return parse_answer(_text(response))
        except ValueError as exc:
            last_error = f"stop_reason={getattr(response, 'stop_reason', None)}: {exc}"
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Your answer failed validation:\n" + str(exc)
                 + "\nReply with the corrected JSON object only."},
            ]
    raise ValueError(f"invalid answer after one retry: {last_error}")


def to_findings(answer: ModelAnswer) -> list[Finding]:
    return [Finding(f.code, tuple(f.cited_clause_ids), limit_amount=f.limit_amount,
                    memo_amount=f.memo_amount, attendees_count=f.attendees_count,
                    flight_hours=f.flight_hours) for f in answer.findings]


def audit_fixed(con, line_id: str, client=None, use_cache: bool = True,
                cache_dir=None, model: str | None = None,
                prompt_version: str = PROMPT_VERSION) -> tuple[Decision, RunInfo]:
    model = model or config.MODEL
    started = time.perf_counter()
    line = load_line(con, line_id)
    python_findings = run_checks(con, line)          # always kept (never-drop)

    clauses = search(query_for(line), config.RETRIEVAL_TOP_K)
    retrieved = {c["clause_id"]: c for c in clauses}
    packet = build_packet(line, clauses)
    p_hash = policy_hash()
    key = cache.cache_key(model, prompt_version, p_hash, packet)
    info = RunInfo(retrieved_ids=list(retrieved))

    answer: ModelAnswer | None = None
    reasons: list[str] = []
    hit = cache.get(key, cache_dir) if use_cache else None
    if hit is not None:
        info.cache_hit = True
        try:
            answer = ModelAnswer.model_validate(hit["answer"])
        except ValueError as exc:
            reasons.append(f"cached answer is invalid: {exc}")
            audit.log_problem(line.line_id, reasons[-1])
    else:
        try:
            answer = ask_model(client or make_client(), packet, info, model)
        except ValueError as exc:
            reasons.append("model answer failed validation")
            audit.log_problem(line.line_id, str(exc))
        except RuntimeError:
            raise
        except Exception as exc:   # API / network errors: full message to the log, never cached
            info.error = f"{type(exc).__name__}: {exc}"
            reasons.append("model call failed")
            audit.log_problem(line.line_id, f"model call failed: {info.error}")

    decision = finish(line, python_findings, answer, reasons, retrieved, info, by="fixed",
                      model=model, prompt_version=prompt_version, p_hash=p_hash, packet=packet,
                      key=key, use_cache=use_cache, cache_dir=cache_dir, started=started)
    return decision, info


def finish(line: Line, python_findings: list[Finding], answer: ModelAnswer | None,
           reasons: list[str], retrieved: dict[str, dict], info: RunInfo, *, by: str,
           model: str, prompt_version: str, p_hash: str, packet: str, key: str,
           use_cache: bool, cache_dir, started: float) -> Decision:
    """Shared by both model paths: guards -> findings -> cache -> decide -> audit trail.

    Model findings are used only if every guard passes; Python findings are always kept.
    The cache is written only for a fresh answer that passed every guard (with the agent's
    search queries, so a hit can replay them).
    """
    findings = list(python_findings)
    client_meal = names_quoted = False
    if answer is not None:
        info.cited_ids = sorted({c for f in answer.findings for c in f.cited_clause_ids})
        info.guard_problems, shown, info.explanation_hidden = run_guards(
            answer, line, retrieved, set(clauses_by_id()))
        if info.guard_problems:
            reasons += [f"guard: {p}" for p in info.guard_problems]
        else:
            info.explanation = shown.explanation
            findings += to_findings(answer)
            client_meal = answer.client_meal is True
            names_quoted = bool(answer.attendee_names_quote and answer.attendee_names_quote.strip())
            # The cache keeps the model's own answer; on a hit the guards run again.
            if use_cache and not info.cache_hit:
                cache.put(key, {"answer": json.loads(answer.model_dump_json()),
                                "queries": list(info.queries)}, cache_dir)

    decision = decide(line, findings, reasons, client_meal=client_meal,
                      attendee_names_quoted=names_quoted, check_attendees=True)
    info.cost_usd = cost_usd(model, info)
    info.latency_ms = int((time.perf_counter() - started) * 1000)
    audit.record({
        "line_id": line.line_id, "by": by, "model": model, "prompt_version": prompt_version,
        "policy_hash": p_hash, "packet_sha256": hashlib.sha256(packet.encode()).hexdigest(),
        "cache_key": key, "cache_hit": info.cache_hit, "retrieved_ids": info.retrieved_ids,
        "cited_ids": info.cited_ids, "codes": decision.codes,
        "input_tokens": info.input_tokens, "output_tokens": info.output_tokens,
        "cache_read_input_tokens": info.cache_read_input_tokens,
        "cache_creation_input_tokens": info.cache_creation_input_tokens,
        "cost_usd": info.cost_usd, "latency_ms": info.latency_ms,
        "model_calls": info.model_calls, "guard_problem_count": len(info.guard_problems),
        "explanation_hidden": info.explanation_hidden,
        "tool_calls": info.tool_calls, "step_limit_hit": info.step_limit_hit,
        "tool_result_ids": info.tool_result_ids, "status": decision.status,
    })
    return decision


def appendix_c_in_top3(con, line_ids: list[str]) -> list[tuple[str, list[str], bool]]:
    """For each line: the 3 clauses the fixed path would send, and whether Appendix C is one."""
    out = []
    for lid in line_ids:
        ids = [c["clause_id"] for c in search(query_for(load_line(con, lid)),
                                               config.RETRIEVAL_TOP_K)]
        out.append((lid, ids, "Appendix C" in ids))
    return out
