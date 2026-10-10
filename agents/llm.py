"""
Claude client wrapper.

The pipeline must run on a demo machine with no API key, so every agent has a
deterministic rule-engine path. `available()` tells the UI which mode is live so
results are never passed off as model output when they came from rules.
"""

import hashlib
import json
import os
import re
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-5-5"

# Every Claude answer is kept, keyed by the exact request, so a run without a key
# replays Claude's work instead of dropping to rules. Replayed output goes through
# the same verification as live output; a changed clause is a new key and a miss.
CACHE_PATH = Path(os.environ.get("ACCENTIC_LLM_CACHE")
                  or Path(__file__).resolve().parent.parent / "data" / "llm_cache.json")

_client = None
_cache = None
_lock = threading.Lock()


def _cache_key(system: str, prompt: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{system}\n{prompt}".encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            try:
                _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _cache = {}
        return _cache


def _store(key: str, value) -> None:
    cache = _load_cache()
    with _lock:
        cache[key] = value
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)


def cached_answers() -> int:
    return len(_load_cache())


def available() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and not key.endswith("...")   # the .env.example placeholder


def mode() -> str:
    return "claude" if available() else "rule_engine"


def _get_client():
    global _client
    with _lock:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic()
        return _client


def complete_json(system: str, prompt: str, max_tokens: int = 8000) -> list | dict | None:
    """
    Ask Claude for JSON. Returns parsed JSON (live, or replayed from the cache), or
    None if neither is available so the caller can fall back to its rule engine.
    """
    key = _cache_key(system, prompt)
    hit = _load_cache().get(key)
    if hit is not None:
        return hit
    if not available():
        return None

    try:
        # Server-side fallback: if a safety classifier declines, the API re-runs the
        # request on a fallback model inside the same call. Output is still verified.
        with _get_client().beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
    except Exception:
        return None

    if response.stop_reason == "refusal":
        return None

    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = _parse_json(text)
    if parsed is not None:
        _store(key, parsed)
    return parsed


def _parse_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = min((i for i in (text.find("["), text.find("{")) if i != -1), default=-1)
    if start == -1:
        return None
    for end in range(len(text), start, -1):
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
    return None
