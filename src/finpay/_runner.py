"""Single-turn driver for the FinPay agent.

ADK's `Runner` is multi-turn, session-based, async. For our purposes
(HTTP request -> agent reply, one shot) we wrap that into a small helper
that creates a session, runs one turn, and returns the final text plus
metadata useful for tracing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agent import root_agent
from .prompts import live_version

_APP_NAME = "finpay"

# A single in-memory session service is fine for v1: each request creates
# its own session and discards it. Cloud Run instances are stateless so
# this is also correct under autoscaling.
_session_service = InMemorySessionService()
_runner = Runner(
    agent=root_agent,
    app_name=_APP_NAME,
    session_service=_session_service,
)


@dataclass
class FinPayReply:
    text: str
    session_id: str
    prompt_version: str


async def ask(message: str, *, user_id: str | None = None) -> FinPayReply:
    """Send one user message to FinPay, return the final reply."""
    user_id = user_id or f"anon-{uuid.uuid4().hex[:8]}"
    session = await _session_service.create_session(app_name=_APP_NAME, user_id=user_id)
    content = types.Content(role="user", parts=[types.Part.from_text(text=message)])

    final_text = ""
    async for event in _runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text = part.text

    return FinPayReply(
        text=final_text,
        session_id=session.id,
        prompt_version=live_version().version,
    )
