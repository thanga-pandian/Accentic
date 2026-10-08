"""The bank's own profile: what it is, what it does, and how much risk it tolerates."""

import json
from datetime import date
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parent.parent / "data" / "bank" / "profile.json"


def load() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def save(profile: dict) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")


def as_of(profile: dict | None = None) -> date:
    """Assessment date. Fixed in the profile so a re-run reproduces the same scores."""
    profile = profile or load()
    raw = profile.get("assessment_date")
    return date.fromisoformat(raw) if raw else date.today()
