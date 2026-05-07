"""Staging mechanism — component C9.

Two responsibilities:

  apply_patch_to_staging(patch) — write the patched prompt to a new
    versioned YAML in prompts/finpay/ (stages it under a new version
    tag without affecting which version the live finpay-serve serves).

  simulated_finpay_endpoint(instruction) — return an AgentCallable
    (string in, string out) that runs FinPay's ADK agent in-process
    with the given system instruction. Lets the eval runner (C7) score
    a staged prompt without spawning a second HTTP server.

The atomic prod swap (changing which version is live, the D3 step) is
intentionally NOT in this module — that's the action layer, gated on
human approval from Slack.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml  # noqa: F401  (used by promote_to_live below)

from finpay.tools import get_exchange_rate

from .eval_run import AgentCallable
from .patch_gen import Patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts" / "finpay"
_STAGING_DIR = _PROMPTS_DIR / "staging"
_LIVE_POINTER = _PROMPTS_DIR / ".live"  # records which version is live


def apply_patch_to_staging(patch: Patch) -> Path:
    """Write the patched prompt to prompts/finpay/staging/<new_version>.yaml.

    Returns the file path. The action layer's `promote_to_live` step
    moves the file from `staging/` up to `prompts/finpay/<new_version>.yaml`
    after human approval; until then it's a candidate, not live.
    """
    _STAGING_DIR.mkdir(parents=True, exist_ok=True)
    path = _STAGING_DIR / f"{patch.new_version}.yaml"
    document = {
        "name": patch.target_name,
        "version": patch.new_version,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "notes": (
            f"Staged patch from {patch.base_version}. "
            f"{patch.summary} — {patch.rationale}"
        ),
        "instruction": patch.patched_prompt,
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def promote_to_live(patch: Patch, *, finpay_url: str | None = None) -> Path | None:
    """D3: atomic prompt swap.

    Source of truth is Firestore — FinPay's per-request `live_version()`
    reads from there, so the flip is visible to live traffic
    immediately with no redeploy. The full instruction body ships in
    the Firestore doc so FinPay doesn't need the patched YAML on its
    container's filesystem.

    The patch body comes from `patch.patched_prompt` (already in the
    incident record) — we don't depend on the local staging file
    because Cloud Run instances are stateless and the staging write
    may have happened on a different instance/revision.

    Returns the local YAML path when a local file move actually
    occurred (useful for local dev), otherwise None.
    """
    # Step 1: Firestore — production swap visibility.
    from .._state import set_live_prompt_version

    set_live_prompt_version(
        target=patch.target_name,
        version=patch.new_version,
        instruction=patch.patched_prompt,
        actor="mender",
        metadata={
            "base_version": patch.base_version,
            "summary": patch.summary,
            "rationale": patch.rationale,
        },
    )

    # Step 2: local filesystem — best effort. Only meaningful when the
    # staging file is on this instance's disk (i.e. local dev).
    staging_path = _STAGING_DIR / f"{patch.new_version}.yaml"
    if staging_path.exists():
        live_path = _PROMPTS_DIR / f"{patch.new_version}.yaml"
        staging_path.replace(live_path)
        try:
            _LIVE_POINTER.write_text(patch.new_version)
        except OSError:
            pass
        return live_path

    # No local staging file — write a freshly-materialized YAML out so
    # `prompts/finpay/<v>.yaml` exists on this instance for parity, but
    # don't fail if the filesystem is read-only or the dir doesn't exist.
    try:
        _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        live_path = _PROMPTS_DIR / f"{patch.new_version}.yaml"
        document = {
            "name": patch.target_name,
            "version": patch.new_version,
            "released_at": datetime.now(timezone.utc).isoformat(),
            "notes": (
                f"Promoted from {patch.base_version}. "
                f"{patch.summary} — {patch.rationale}"
            ),
            "instruction": patch.patched_prompt,
        }
        live_path.write_text(yaml.safe_dump(document, sort_keys=False))
        try:
            _LIVE_POINTER.write_text(patch.new_version)
        except OSError:
            pass
        return live_path
    except OSError:
        return None


def current_live_version() -> str | None:
    """Return the live version. Firestore first (if enabled), else `.live`."""
    try:
        from .._state import get_live_prompt_version

        v = get_live_prompt_version("finpay-support", env_fallback="__none__")
        if v and v != "v1":
            # `v1` is the env_fallback default; bool-check whether
            # Firestore actually answered by passing a sentinel env name.
            return v
    except Exception:
        pass
    if not _LIVE_POINTER.exists():
        return None
    return _LIVE_POINTER.read_text().strip() or None


def simulated_finpay_endpoint(
    instruction: str,
    *,
    model: str | None = None,
    label: str | None = None,
) -> AgentCallable:
    """Build a string-in/string-out callable that runs FinPay in-process.

    Constructs a fresh ADK agent with the given instruction and the
    real get_exchange_rate tool, plus a private session service so each
    call is isolated. Returns a sync callable — internally it spins a
    dedicated event loop on a worker thread so it works regardless of
    how the caller (the eval runner is sync) is structured.

    Args:
        instruction: the system prompt to test.
        model: optional model override (defaults to MENDER_MODEL or
            gemini-3-flash-preview).
        label: shown in the agent's name; useful when debugging through
            Phoenix to tell live vs staged traces apart.
    """
    import os

    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .._model import gemini

    label = label or f"staging-{uuid.uuid4().hex[:6]}"
    model_name = model or os.environ.get("FINPAY_MODEL", "gemini-3-flash-preview")
    agent = Agent(
        name=f"finpay_{label}".replace("-", "_"),
        model=gemini(model_name),
        description=f"FinPay Support, staged variant ({label})",
        instruction=instruction,
        tools=[get_exchange_rate],
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name=f"finpay-{label}",
        session_service=session_service,
    )

    # Each AgentCallable owns a long-lived background event loop on a
    # worker thread; calling submit() schedules an async coroutine and
    # blocks for its result. This avoids `asyncio.run()` constructing
    # a new loop per call (which leaks ADK's internal aiohttp clients)
    # while keeping the call-site interface fully sync.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def _ask(message: str) -> str:
        user_id = f"eval-{uuid.uuid4().hex[:8]}"
        session = await session_service.create_session(
            app_name=f"finpay-{label}", user_id=user_id
        )
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
        text_out = ""
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=content,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        text_out = part.text
        return text_out

    def _call(message: str) -> str:
        future = asyncio.run_coroutine_threadsafe(_ask(message), loop)
        return future.result(timeout=120.0)

    return _call
