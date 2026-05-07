"""Phoenix OTEL setup for FinPay (component B1).

Imported once at process start. If `PHOENIX_API_KEY` isn't set we no-op
with a single warning so local development without Phoenix still works.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)
_initialized = False


def init_telemetry(project_name: str = "finpay-support") -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    api_key = os.environ.get("PHOENIX_API_KEY", "").strip()
    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "https://app.phoenix.arize.com").rstrip("/")

    if not api_key:
        _log.warning(
            "PHOENIX_API_KEY not set; FinPay traces will not be exported. "
            "Set it in .env to enable observability."
        )
        return

    # Imports are local so the package still loads if optional deps are missing.
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor
    from phoenix.otel import register

    # phoenix.otel.register() reads PHOENIX_API_KEY + PHOENIX_COLLECTOR_ENDPOINT
    # from env directly and constructs the right auth header. Don't pass
    # `headers=` — duplicating leads to 401 from the collector.
    tracer_provider = register(
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", project_name),
        auto_instrument=False,
        protocol="http/protobuf",
    )
    GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)
    _log.info("Phoenix telemetry initialized (project=%s)", project_name)
