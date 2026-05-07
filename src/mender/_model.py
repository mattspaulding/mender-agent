"""Shared Gemini model factory.

The Vertex genai client builds its host as `{location}-aiplatform.googleapis.com`
from the configured location. Some models (e.g. gemini-3.1-pro-preview) are
currently only reachable via the un-prefixed `aiplatform.googleapis.com`
host, even when the request still scopes to `us-central1` in the URL path.

ADK's `Gemini` wrapper lets us override `base_url` per-model. This helper
centralizes that so both Mender and FinPay agents stay in sync.

Drop the override the moment our project gains access to the regional
host or to a model that doesn't need it (e.g. gemini-3.1-flash once
enabled).
"""

from __future__ import annotations

import os

from google.adk.models.google_llm import Gemini

_VERTEX_HOST = os.environ.get("VERTEX_API_HOST", "https://aiplatform.googleapis.com")


def gemini(name: str) -> Gemini:
    """Build an ADK Gemini model bound to the global Vertex host."""
    return Gemini(model=name, base_url=_VERTEX_HOST)
