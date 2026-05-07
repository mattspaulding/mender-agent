"""Versioned prompt registry for FinPay.

Each prompt version is a YAML file in `prompts/finpay/` at the repo root.
This module is the single source of truth for which prompt is live and for
listing prior versions when Mender wants to correlate a regression with a
recent change.

The registry is intentionally tiny — atomic file swap, no DB, no canary —
which is the agreed scope (BUILD_SCOPE.md, item D3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

# Repo root resolution: this file lives at <repo>/src/finpay/prompts.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPTS_DIR = _REPO_ROOT / "prompts" / "finpay"


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version: str
    released_at: datetime
    notes: str
    instruction: str
    path: Path


def _load(path: Path) -> PromptVersion:
    raw = yaml.safe_load(path.read_text())
    return PromptVersion(
        name=raw["name"],
        version=raw["version"],
        released_at=_parse_ts(raw["released_at"]),
        notes=raw.get("notes", "").strip(),
        instruction=raw["instruction"].strip(),
        path=path,
    )


def _parse_ts(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@lru_cache(maxsize=32)
def load_version(version: str) -> PromptVersion:
    """Load a specific version by tag (e.g. 'v1', 'v2')."""
    path = _PROMPTS_DIR / f"{version}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt version {version!r} not found at {path}. "
            f"Available: {[p.stem for p in _PROMPTS_DIR.glob('*.yaml')]}"
        )
    return _load(path)


def list_versions() -> list[PromptVersion]:
    """All known prompt versions, oldest first by released_at."""
    versions = [_load(p) for p in _PROMPTS_DIR.glob("*.yaml")]
    return sorted(versions, key=lambda v: v.released_at)


def live_version() -> PromptVersion:
    """The currently live FinPay prompt — controlled by env var.

    Bumping FINPAY_PROMPT_VERSION simulates a "model upgrade event":
    point Mender at a window that straddles the bump and the regression
    becomes visible in the eval scores.
    """
    return load_version(os.environ.get("FINPAY_PROMPT_VERSION", "v1"))
