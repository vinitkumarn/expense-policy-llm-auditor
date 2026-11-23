"""Score an auditor against the planted answer keys.

Labels come ONLY from the plan files written by the generator (plan.json, redteam_plan.json,
reworded_plan.json), never from rules code. The red-team and reworded sets are reported
separately and are never used to tune prompts or rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from expaudit import config
from expaudit.baselines import BASELINES, audit
from expaudit.db import DB_FILE, connect
from expaudit.decide import ACTION_ORDER, Decision

AUDITORS = ("naive", "rules", "fixed", "agent")
MODEL_PATHS = ("fixed", "agent")

SETS = {
    "main": (config.PLAN_FILE, ""),
    "redteam": (config.REDTEAM_PLAN_FILE, "_redteam"),
    "reworded": (config.REWORDED_PLAN_FILE, "_reworded"),
}


def audit_line(con, line_id: str, by: str, *, client=None, use_cache: bool = True):
    """(Decision, RunInfo or None) for one line and one auditor."""
    if by in BASELINES:
        return audit(con, line_id, by), None
    if by == "fixed":
        from expaudit.fixed import audit_fixed
        return audit_fixed(con, line_id, client=client, use_cache=use_cache)
    if by == "agent":
        from expaudit.agent import audit_agent
        return audit_agent(con, line_id, client=client, use_cache=use_cache)
    raise ValueError(f"unknown auditor {by!r}; choose from {list(AUDITORS)}")


def _money(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _canonical(actions: list[str]) -> list[str]:
    return sorted(actions, key=lambda a: ACTION_ORDER.index(a) if a in ACTION_ORDER else 99)


def load_labels(set_name: str, data_dir: Path | None = None) -> list[dict]:
    """The answer key for one set (main excludes the duplicate 'support_lines')."""
    data_dir = Path(data_dir or config.DATA_DIR)
    plan = json.loads((data_dir / SETS[set_name][0]).read_text(encoding="utf-8"))
    return plan["cases"]


@dataclass
class LineResult:
    label: dict
    decision: Decision
    info: object = None          # fixed.RunInfo on model paths (both)
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    dollars_ok: bool = False
    held_ok: bool = False
    status_ok: bool = False
    approver_ok: bool = False
    actions_ok: bool = False

    @property
    def all_ok(self) -> bool:
        return (not self.missed and not self.extra and self.dollars_ok and self.held_ok
                and self.status_ok and self.approver_ok and self.actions_ok)


def score_line(label: dict, decision: Decision, info=None) -> LineResult:
    expected, got = label["expected_codes"], decision.codes
    return LineResult(
        label=label, decision=decision, info=info,
        found=[c for c in expected if c in got],
        missed=[c for c in expected if c not in got],
        extra=[c for c in got if c not in expected],
        dollars_ok=_money(label["dollars_out_of_policy"]) == decision.dollars_out_of_policy,
        held_ok=_money(label["dollars_held"]) == decision.dollars_held,
        status_ok=label["status"] == decision.status,
        approver_ok=label["approver"] == decision.approver,
        actions_ok=_canonical(label["actions"]) == _canonical(decision.actions),
    )


def metrics(results: list[LineResult]) -> dict:
    n = len(results)
    planted = sum(len(r.label["expected_codes"]) for r in results)
    found = sum(len(r.found) for r in results)
    clean = [r for r in results if not r.label["expected_codes"] and r.label["status"] == "OK"]

    planted_dollars = Decimal("0.00")
    caught_dollars = Decimal("0.00")
    for r in results:
        for code, exp in r.label["dollars_by_code"].items():
            exp = _money(exp)
            if exp is None:
                continue
            planted_dollars += exp
            got = r.decision.dollars_by_code.get(code)
            if got is not None:
                caught_dollars += min(got, exp)

    def total(values):   # totals skip unknown (None) dollars
        return sum((v for v in values if v is not None), Decimal("0.00"))

    return {
        "lines": n,
        "planted_findings": planted,
        "found_findings": found,
        "finding_recall": found / planted if planted else None,
        "clean_lines": len(clean),
        "clean_lines_with_false_findings": sum(1 for r in clean if r.decision.codes),
        "false_findings_all_lines": sum(len(r.extra) for r in results),
        "dollars_exact_match": sum(r.dollars_ok for r in results) / n if n else None,
        "dollars_planted": planted_dollars,
        "dollars_caught": caught_dollars,
        "dollars_out_of_policy_expected_total":
            total(_money(r.label["dollars_out_of_policy"]) for r in results),
        "dollars_out_of_policy_predicted_total":
            total(r.decision.dollars_out_of_policy for r in results),
        "lines_unknown_dollars_expected":
            sum(r.label["dollars_out_of_policy"] is None for r in results),
        "lines_unknown_dollars_predicted":
            sum(r.decision.dollars_out_of_policy is None for r in results),
        "dollars_held_match": sum(r.held_ok for r in results) / n if n else None,
        "status_accuracy": sum(r.status_ok for r in results) / n if n else None,
        "approver_accuracy": sum(r.approver_ok for r in results) / n if n else None,
        "actions_accuracy": sum(r.actions_ok for r in results) / n if n else None,
        "lines_fully_correct": sum(r.all_ok for r in results),
    }


def model_metrics(results: list[LineResult]) -> dict:
    """Model-path extras: calls, cache, guards, tokens, cost, latency, citation accuracy."""
    infos = [r.info for r in results if r.info is not None]
    n = len(infos)
    costs = [i.cost_usd for i in infos]
    cost = None if any(c is None for c in costs) else sum(costs, Decimal("0"))
    tool_calls = [i.tool_calls for i in infos]
    # Citation accuracy: for each expected finding the model path reported, are the expected
    # clause ids all among the cited ids?
    cite_total = cite_ok = 0
    for r in results:
        for code, expected_ids in r.label["expected_cited_clause_ids"].items():
            if code in r.decision.cites:
                cite_total += 1
                cite_ok += set(expected_ids) <= set(r.decision.cites[code])
    return {
        "model_calls": sum(i.model_calls for i in infos),
        "cache_hit_rate": sum(i.cache_hit for i in infos) / n if n else None,
        "lines_with_guard_failures": sum(bool(i.guard_problems) for i in infos),
        "explanations_hidden": sum(i.explanation_hidden for i in infos),
        "model_errors": sum(i.error is not None for i in infos),
        "input_tokens": sum(i.input_tokens for i in infos),
        "output_tokens": sum(i.output_tokens for i in infos),
        "cache_read_input_tokens": sum(i.cache_read_input_tokens for i in infos),
        "cost_usd": cost,
        "cost_per_line_usd": (cost / n).quantize(Decimal("0.000001")) if cost is not None and n
        else None,
        "avg_latency_ms": sum(i.latency_ms for i in infos) / n if n else None,
        "max_latency_ms": max((i.latency_ms for i in infos), default=None),
        "avg_tool_calls": sum(tool_calls) / n if n else None,
        "max_tool_calls": max(tool_calls, default=None),
        "step_limit_hits": sum(i.step_limit_hit for i in infos),
        "citation_accuracy": cite_ok / cite_total if cite_total else None,
        "citations_checked": cite_total,
    }


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt(value) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


def _model_block(mm: dict) -> list[str]:
    cost = "unknown" if mm["cost_usd"] is None else f"${mm['cost_usd']:.4f}"
    per_line = ("unknown" if mm["cost_per_line_usd"] is None
                else f"${mm['cost_per_line_usd']:.4f}")
    latency = "n/a" if mm["avg_latency_ms"] is None else f"{mm['avg_latency_ms']:.0f} ms"
    max_latency = "n/a" if mm["max_latency_ms"] is None else f"{mm['max_latency_ms']} ms"
    tools = ("n/a" if mm["avg_tool_calls"] is None
             else f"{mm['avg_tool_calls']:.2f} / {mm['max_tool_calls']}")
    return [
        "",
        "## Model path",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Model calls (retries included) | {mm['model_calls']} |",
        f"| Cache hit rate | {_pct(mm['cache_hit_rate'])} |",
        f"| Lines with guard failures | {mm['lines_with_guard_failures']} |",
        f"| Explanations hidden (unverified number; decision kept) | "
        f"{mm['explanations_hidden']} |",
        f"| Lines with model call errors | {mm['model_errors']} |",
        f"| Tokens in / out / cache read | {mm['input_tokens']} / {mm['output_tokens']} / "
        f"{mm['cache_read_input_tokens']} |",
        f"| Cost | {cost} |",
        f"| Cost per line | {per_line} |",
        f"| Latency per line, avg / max | {latency} / {max_latency} |",
        f"| Tool calls per line, avg / max | {tools} |",
        f"| Step-limit hits (limit {config.AGENT_MAX_STEPS} searches) | {mm['step_limit_hits']} |",
        f"| Citation accuracy (expected ids within cited ids) | "
        f"{_pct(mm['citation_accuracy'])} of {mm['citations_checked']} findings |",
    ]


def render_report(by: str, set_name: str, results: list[LineResult], m: dict) -> str:
    lines = [
        f"# Eval: {by} on the {set_name} set",
        "",
        "Labels come from the plan files only. "
        + ("This set is reported separately and is never used to tune prompts or rules."
           if set_name != "main" else "Red-team and reworded sets are in separate reports."),
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Lines | {m['lines']} |",
        f"| Finding recall | {_pct(m['finding_recall'])} "
        f"({m['found_findings']} of {m['planted_findings']}) |",
        f"| False findings on clean lines | {m['clean_lines_with_false_findings']} of "
        f"{m['clean_lines']} clean lines |",
        f"| False findings, all lines (extra codes) | {m['false_findings_all_lines']} |",
        f"| Dollars out of policy exact match | {_pct(m['dollars_exact_match'])} |",
        f"| Dollars caught vs planted | {_fmt(m['dollars_caught'])} of "
        f"{_fmt(m['dollars_planted'])} |",
        f"| Dollars out of policy total, expected / predicted (unknown skipped) | "
        f"{_fmt(m['dollars_out_of_policy_expected_total'])} / "
        f"{_fmt(m['dollars_out_of_policy_predicted_total'])} |",
        f"| Lines with unknown dollars, expected / predicted | "
        f"{m['lines_unknown_dollars_expected']} / {m['lines_unknown_dollars_predicted']} |",
        f"| Dollars held match | {_pct(m['dollars_held_match'])} |",
        f"| Status accuracy | {_pct(m['status_accuracy'])} |",
        f"| Approver accuracy | {_pct(m['approver_accuracy'])} |",
        f"| Actions accuracy | {_pct(m['actions_accuracy'])} |",
        f"| Lines fully correct | {m['lines_fully_correct']} of {m['lines']} |",
    ]
    if "model" in m:
        lines += _model_block(m["model"])
    lines += [
        "",
        "## Per line",
        "",
        "| Line | Expected codes | Got codes | Out of policy exp / got | Held exp / got "
        "| Approver exp / got | Actions exp / got | Status exp / got | OK |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lab, d = r.label, r.decision
        lines.append(
            f"| {lab['line_id']} | {', '.join(lab['expected_codes']) or '-'} "
            f"| {', '.join(d.codes) or '-'} "
            f"| {_fmt(_money(lab['dollars_out_of_policy']))} / {_fmt(d.dollars_out_of_policy)} "
            f"| {_fmt(_money(lab['dollars_held']))} / {_fmt(d.dollars_held)} "
            f"| {lab['approver']} / {d.approver} "
            f"| {', '.join(lab['actions']) or '-'} / {', '.join(d.actions) or '-'} "
            f"| {lab['status']} / {d.status} | {'yes' if r.all_ok else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def run_eval(by: str, set_name: str = "main", cases: list[str] | None = None,
             limit: int | None = None, data_dir: Path | None = None,
             reports_dir: Path | None = None, echo=print, client=None,
             use_cache: bool = True) -> tuple[dict, Path]:
    data_dir = Path(data_dir or config.DATA_DIR)
    reports_dir = Path(reports_dir or config.REPORTS_DIR)
    labels = load_labels(set_name, data_dir)
    if cases:
        wanted = set(cases)
        unknown = wanted - {lab["line_id"] for lab in labels}
        if unknown:
            raise KeyError(f"not in the {set_name} set: {sorted(unknown)}")
        labels = [lab for lab in labels if lab["line_id"] in wanted]
    if limit is not None:
        labels = labels[:limit]

    con = connect(data_dir / DB_FILE)
    results = []
    try:
        for i, label in enumerate(labels, 1):
            decision, info = audit_line(con, label["line_id"], by, client=client,
                                        use_cache=use_cache)
            r = score_line(label, decision, info)
            results.append(r)
            echo(f"[{i}/{len(labels)}] {label['line_id']}  expected={label['expected_codes']} "
                 f"got={r.decision.codes}  {'ok' if r.all_ok else 'diff'}")
    finally:
        con.close()

    m = metrics(results)
    if by in MODEL_PATHS:
        m["model"] = model_metrics(results)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"eval_{by}{SETS[set_name][1]}.md"
    path.write_text(render_report(by, set_name, results, m), encoding="utf-8", newline="\n")
    write_json(by, set_name, results, m, reports_dir)
    return m, path


def json_path(by: str, set_name: str, reports_dir: Path) -> Path:
    return Path(reports_dir) / f"eval_{by}{SETS[set_name][1]}.json"


def write_json(by: str, set_name: str, results: list[LineResult], m: dict,
               reports_dir: Path) -> Path:
    """Machine-readable copy of the eval (read by `compare`). Decimals are written as strings."""
    payload = {
        "by": by, "set": set_name, "metrics": m,
        "lines": [{"line_id": r.label["line_id"], "all_ok": r.all_ok, "dollars_ok": r.dollars_ok,
                   "codes": r.decision.codes} for r in results],
    }
    path = json_path(by, set_name, reports_dir)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8", newline="\n")
    return path
