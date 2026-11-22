"""Response cache: one JSON file per key under data/cache/.

key = sha256(model + prompt version + policy hash + packet). Only answers that passed the
schema AND every guard are written; API errors, invalid answers and guard failures never are.
Guards run again on every hit. The payload also has room for the agent's search queries (PR 4).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from expaudit import config


def cache_key(model: str, prompt_version: str, policy_hash: str, packet: str) -> str:
    joined = "\n".join([model, prompt_version, policy_hash, packet])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _path(key: str, cache_dir: Path | None) -> Path:
    return Path(cache_dir or config.CACHE_DIR) / f"{key}.json"


def get(key: str, cache_dir: Path | None = None) -> dict | None:
    path = _path(key, cache_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def put(key: str, payload: dict, cache_dir: Path | None = None) -> Path:
    path = _path(key, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8", newline="\n")
    return path
