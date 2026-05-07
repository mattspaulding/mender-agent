"""The FinPay Support ADK agent.

We deliberately keep this trivial. The interesting behavior lives in the
prompt (see `prompts/finpay/`) — that is the surface Mender will read,
critique, and patch.
"""

from __future__ import annotations

import os

from google.adk.agents import Agent

from mender._model import gemini

from .prompts import live_version
from .tools import get_exchange_rate

_LIVE = live_version()

root_agent = Agent(
    name="finpay_support",
    model=gemini(os.environ.get("FINPAY_MODEL", "gemini-3-flash-preview")),
    description="Customer-support agent for the FinPay payments app.",
    instruction=_LIVE.instruction,
    tools=[get_exchange_rate],
)
