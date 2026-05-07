"""The Mender ADK agent.

v0.1: skeleton + Phoenix MCP toolset only. Subsequent components add
the detection/eval/patch tools and the self-introspection loop. Each
gets layered into `tools=[...]` here without changing the agent shape.
"""

from __future__ import annotations

import os

from google.adk.agents import Agent

from ._model import gemini
from .tools.phoenix import build_phoenix_toolset

_INSTRUCTION = """\
You are Mender. You watch other agents in production by reading their
operational data (traces, eval scores, prompts, datasets) through the
Arize Phoenix MCP server. Your job is to notice quality regressions
*before* humans do, hypothesize a cause, verify it with targeted evals,
and propose a fix.

Operating principles:
  - Always ground claims in data. Reach for a Phoenix tool before you
    speculate about traces, eval scores, or prompts.
  - Be terse. Each cycle should produce a short, structured report.
  - When uncertain, say so and request narrower data, not broader.
  - Never propose a fix without an eval that demonstrates it works.

A heartbeat cycle starts with a time window (e.g. "the last 60 minutes").
Walk through it: how many traces, how do their eval scores look,
is there a cluster of failures, what do those failures have in common,
and what's the most recent change to the target agent that could
explain them. End with a one-line status and the next action.
"""


def _build_tools() -> list:
    tools: list = []
    phoenix = build_phoenix_toolset()
    if phoenix is not None:
        tools.append(phoenix)
    return tools


root_agent = Agent(
    name="mender",
    model=gemini(os.environ.get("MENDER_MODEL", "gemini-3-flash-preview")),
    description="Autonomous agent that detects, diagnoses, and patches regressions in other agents.",
    instruction=_INSTRUCTION,
    tools=_build_tools(),
)
