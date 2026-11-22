"""Parse data/policy.md into clauses. Clause ids are the citation keys."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from expaudit import config

# "## 4.2 Dinner" or "## Appendix B City tiers and limits". "# 4 Meals" section headers are skipped.
_HEADING = re.compile(r"^## (\d+\.\d+|Appendix [A-Z])\s+(.+?)\s*$")


@dataclass(frozen=True)
class Clause:
    clause_id: str
    title: str
    text: str


def load_policy(path: Path = config.POLICY_PATH) -> list[Clause]:
    """Return the clauses in file order. Body text runs until the next heading of any level."""
    clauses: list[Clause] = []
    current: tuple[str, str] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            clauses.append(Clause(current[0], current[1], "\n".join(body).strip()))

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            current, body = (match.group(1), match.group(2)), []
        elif line.startswith("#"):
            flush()
            current, body = None, []
        elif current is not None:
            body.append(line)
    flush()

    ids = [c.clause_id for c in clauses]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate clause ids in {path}: {sorted(duplicates)}")
    return clauses


def clauses_by_id(path: Path = config.POLICY_PATH) -> dict[str, Clause]:
    return {c.clause_id: c for c in load_policy(path)}


def policy_hash(path: Path = config.POLICY_PATH) -> str:
    """sha256 of the raw file bytes. Part of the response cache key."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
