"""Phoenix OTEL setup for Mender (component B2).

Mender's own LLM and tool calls trace into a separate Phoenix project so
the self-introspection step (C12) can read them later. Same shape as
FinPay's `_telemetry.py`; deduped only by project name.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)
_initialized = False


def init_telemetry(project_name: str = "mender") -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    api_key = os.environ.get("PHOENIX_API_KEY", "").strip()
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "https://app.phoenix.arize.com").rstrip("/")

    if not api_key:
        _log.warning(
            "PHOENIX_API_KEY not set; Mender's own traces will not be exported."
        )
        return

    from openinference.instrumentation.google_adk import GoogleADKInstrumentor
    from phoenix.otel import register

    tracer_provider = register(
        project_name=project_name,
        auto_instrument=False,
        protocol="http/protobuf",
    )
    GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)
    _log.info("Mender telemetry initialized (project=%s)", project_name)
