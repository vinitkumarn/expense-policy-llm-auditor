"""Audit trail and the app log.

audit.jsonl: one JSON line per model-path decision with ids, hashes, clause ids, tokens, cost and
status only. It never holds memo or merchant text (those are untrusted and may be personal).
expaudit.log: guard problems and full API error messages, one line each, tagged with line_id.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from expaudit import config

AUDIT_FILE = "audit.jsonl"
LOG_FILE = "expaudit.log"

_logger = logging.getLogger("expaudit")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_handler_path: Path | None = None


def _ensure_handler() -> None:
    """Point the file handler at config.LOGS_DIR (tests move it to a temp folder)."""
    global _handler_path
    path = Path(config.LOGS_DIR) / LOG_FILE
    if path == _handler_path:
        return
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
        h.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)
    _handler_path = path


def log_problem(line_id: str, message: str, level: int = logging.WARNING) -> None:
    _ensure_handler()
    _logger.log(level, "%s: %s", line_id, message)
    for h in _logger.handlers:
        h.flush()


AUDIT_FIELDS = (
    "line_id", "by", "model", "prompt_version", "policy_hash", "packet_sha256", "cache_key",
    "cache_hit", "retrieved_ids", "cited_ids", "codes", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd", "latency_ms",
    "model_calls", "guard_problem_count", "tool_calls", "step_limit_hit", "tool_result_ids",
    "status",
)


def record(entry: dict) -> Path:
    """Append one audit line. Only the whitelisted fields are written."""
    path = Path(config.LOGS_DIR) / AUDIT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    row.update({k: entry.get(k) for k in AUDIT_FIELDS})
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return path
