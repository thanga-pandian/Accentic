"""
Claude client wrapper.

The pipeline must run on a demo machine with no API key, so every agent has a
deterministic rule-engine path. `available()` tells the UI which mode is live so
results are never passed off as model output when they came from rules.
"""

import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-opus-5-5"

_client = None


def available() -> bool:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return bool(key) and not key.endswith("...")   # the .env.example placeholder


def mode() -> str:
    return "claude" if available() else "rule_engine"


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def complete_json(system: str, prompt: str, max_tokens: int = 8000) -> list | dict | None:
    """
    Ask Claude for JSON. Returns parsed JSON, or None if unavailable/unparseable
    so the caller can fall back to its rule engine.
    """
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
    return _parse_json(text)


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
