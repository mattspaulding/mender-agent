"""Single-turn driver for the FinPay agent.

Resolves the live prompt version on every request — so when Mender's
promote_to_live writes a new version into Firestore (D3 atomic swap),
FinPay's next /chat call picks it up immediately, no redeploy needed.

ADK Agent + Runner are heavy-ish to construct (they wire the genai
client, the session service, the model wrapper) so we cache one pair
per version we've seen. In practice that's two: the previous prompt
and the patched prompt. Cache is bounded but unbounded eviction isn't
needed at the volume FinPay sees.
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from mender._model import gemini

from .prompts import PromptVersion, live_version
from .tools import get_exchange_rate

_APP_NAME = "finpay"

# A single in-memory session service shared across all per-version
# runners is fine — sessions are ephemeral per /chat call.
_session_service = InMemorySessionService()
_cache: dict[str, tuple[Agent, Runner]] = {}
_cache_lock = threading.Lock()


def _build_for(version: PromptVersion) -> tuple[Agent, Runner]:
    agent = Agent(
        name="finpay_support",
        model=gemini(os.environ.get("FINPAY_MODEL", "gemini-3-flash-preview")),
        description="Customer-support agent for the FinPay payments app.",
        instruction=version.instruction,
        tools=[get_exchange_rate],
    )
    runner = Runner(agent=agent, app_name=_APP_NAME, session_service=_session_service)
    return agent, runner


def _resolve() -> tuple[str, Runner]:
    v = live_version()
    with _cache_lock:
        pair = _cache.get(v.version)
        if pair is None:
            pair = _build_for(v)
            _cache[v.version] = pair
    return v.version, pair[1]


@dataclass
class FinPayReply:
    text: str
    session_id: str
    prompt_version: str


async def ask(message: str, *, user_id: str | None = None) -> FinPayReply:
    """Send one user message to FinPay, return the final reply."""
    version_tag, runner = _resolve()
    user_id = user_id or f"anon-{uuid.uuid4().hex[:8]}"
    session = await _session_service.create_session(app_name=_APP_NAME, user_id=user_id)
    content = types.Content(role="user", parts=[types.Part.from_text(text=message)])

    final_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text = part.text

    return FinPayReply(text=final_text, session_id=session.id, prompt_version=version_tag)
