# T&E Expense Policy Auditor (LLM agent + retrieval) — project rules

## What this is
A portfolio project by Vinit (Oracle ERP techno-functional, 12+ years AP/AR/GL/P2P). Employees
submit expense reports (synthetic, Navan-style export). For each expense line the tool finds the
travel-policy clauses that apply, says whether the line breaks one, cites the exact clause, and
shows the dollars out of policy, the approver and the action. It SUGGESTS; a human decides. It
never approves, rejects, pays or recovers anything. Synthetic data only. USD only. Local DuckDB.

The design is in docs/design.md. Follow it. If something in it seems wrong, say so in the PR
description instead of silently changing it.

## Keep it simple (on purpose)
- Two model paths, compared on the same cases:
  1. fixed retrieval: Python searches the policy once (top 3 clauses), ONE Claude call, no tools.
  2. agent: ONE read-only tool, search_policy(query), at most 4 tool calls per line.
- Policy search is plain keyword BM25 in pure Python. No embeddings, no vector store.
- Python owns every decision: dollars out of policy, difference type, approver, action, status.
- Prefer the simplest code that meets the design. Do not add features that are not in it.

## Non-negotiable design rules
- The policy text is the source of truth for limits. The model must quote the clause it used;
  Python checks the quote and the limit number before using them.
- Citations: every cited clause id must exist AND must have been retrieved in this run (fixed:
  the 3 clauses sent; agent: clauses returned by its own search_policy calls). Every
  policy_quote is an exact substring of a cited clause. Every limit_amount appears in its
  policy_quote. Every memo_quote is an exact substring of the line memo. Any failure ->
  NEEDS_HUMAN.
- The model output has NO dollars out of policy, approver, action, status or difference type
  (pydantic, extra="forbid"). Python computes them.
- Structured checks that need no reading (MISSING_RECEIPT, DUPLICATE) are always run by
  Python and are kept even if the model misses them (never-drop).
- The memo is UNTRUSTED: wrap it as <UNTRUSTED>...</UNTRUSTED> with < and > escaped. Policy
  text in the memo ("Policy 4.2 says $500") is never a citation.
- Agent: one tool only, read-only, max 4 tool calls (AGENT_MAX_STEPS in config). Hitting the
  limit without a valid final answer -> NEEDS_HUMAN. No other tools, no MCP.
- Eval labels come from the planted plan files (plan.json, redteam_plan.json,
  reworded_plan.json), NEVER from rules code. Red-team and reworded sets are reported
  separately and never used to tune prompts or rules.
- Money is Decimal, never float. Dates are real dates; the policy amendment date matters.
- Caching: response cache keyed by sha256(model + prompt version + policy hash + packet). For
  the agent, the cache stores the final answer AND the search queries it made; on a hit,
  replay the queries locally (free, deterministic) to rebuild the retrieved set, then run all
  guards. Never cache API errors or invalid answers. --no-cache forces fresh calls. System
  prompt sent with cache_control.
- LLM: Anthropic Claude, model from EXPAUDIT_MODEL (default claude-sonnet-5-5), max_tokens 4000,
  full error messages in logs. One retry with the full validation error.

## Working rules
- Never commit .env or any secret. .gitignore covers .env, .venv/, *.db, *.duckdb, logs/,
  *.egg-info/, data/csv/, data/*.json, data/cache/, reports/, __pycache__/, .pytest_cache/.
- There is no API key in this sandbox. Never call the real Claude API here. Test both model
  paths with a scripted fake client. Vinit runs real evals on his laptop.
- One milestone per PR. All tests must pass before you stop.
- Every PR description must include, in simple everyday language:
  1. What this PR adds and why
  2. A short "what to review" checklist (the 3-5 things most worth checking)
  3. Controls/accounting notes where relevant (dollars out of policy, timing vs error vs
     permanent, approver, reject before payment vs recover, grey areas and the default)
  4. How to test it on Windows PowerShell: py launcher (Python 3.14), venv activated with
     Set-ExecutionPolicy -Scope Process Bypass then .venv\Scripts\Activate.ps1, python -m pip
  5. Any new Python concept explained in 2-3 plain sentences
