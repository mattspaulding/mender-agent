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
from opentelemetry import trace as _ot

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
    trace_id: str = ""  # OTel hex (32 char), empty if no active tracer
    span_id: str = ""  # OTel hex (16 char) of the wrapping handle_chat span


_TRACER = _ot.get_tracer("finpay.runner")


async def ask(message: str, *, user_id: str | None = None) -> FinPayReply:
    """Send one user message to FinPay, return the final reply.

    Wrapped in an explicit OTel span ("finpay.handle_chat") so the
    HTTP layer can return the span_id + trace_id in the response.
    The traffic driver reads those IDs to score the trace inline
    (without having to round-trip Phoenix to find the span). The
    span name deliberately does NOT include the substring
    "invocation" so the batch scorer's name-filter doesn't pick it
    up — only the OpenInference auto-instrumented "invocation
    [finpay]" span gets batch-scored, avoiding double-counting.
    """
    with _TRACER.start_as_current_span("finpay.handle_chat") as span:
        # Annotate with OpenInference semantic attributes so Phoenix's
        # trace-list view shows kind/input/output for this trace
        # (otherwise it shows "unknown" / blank because Phoenix reads
        # from the root span and our wrapper would be the root).
        span.set_attribute("openinference.span.kind", "AGENT")
        span.set_attribute("input.mime_type", "text/plain")
        span.set_attribute("input.value", message)

        ctx = span.get_span_context()
        trace_id_hex = format(ctx.trace_id, "032x") if ctx.trace_id else ""
        span_id_hex = format(ctx.span_id, "016x") if ctx.span_id else ""

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

        span.set_attribute("output.mime_type", "text/plain")
        span.set_attribute("output.value", final_text)

        return FinPayReply(
            text=final_text,
            session_id=session.id,
            prompt_version=version_tag,
            trace_id=trace_id_hex,
            span_id=span_id_hex,
        )
