"""Hypothesis generation — component C5.

Given a FailureCluster (from C4) plus the live target prompt and recent
prompt-version history, name the likely root cause AND the specific
prompt clause that explains the cluster. This is the structured payload
the Slack incident card and the patch generator (C8) consume.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .detect import FailureCluster


@dataclass
class PromptVersionRef:
    """Subset of finpay.prompts.PromptVersion needed for correlation."""

    version: str
    released_at: datetime
    instruction: str
    notes: str = ""


@dataclass
class Hypothesis:
    pattern_name: str  # echoed from cluster
    root_cause: str  # one-sentence causal explanation
    suspected_prompt_clause: str  # exact substring lifted from current prompt
    correlation_evidence: str  # one-line: what supports this (timing, diff)
    confidence: float  # 0-1
    recommended_action: str  # one-line: what to change

    def to_dict(self) -> dict:
        return asdict(self)


_HYP_PROMPT = """\
You are diagnosing a quality regression in FinPay Support, a customer-
service agent. You see ONE failure cluster (a group of user turns that
share a failure pattern), the current FinPay system prompt, and the
recent prompt version history. Identify the root cause.

Return STRICT JSON, no prose, no fences:

  {"root_cause": "<one sentence — the causal mechanism. Reference the
                  exact prompt clause if applicable.>",
   "suspected_prompt_clause": "<exact substring from the CURRENT prompt
                               that produces this failure. If no clause
                               fits, return empty string.>",
   "correlation_evidence": "<one line — timing or version-diff evidence,
                            e.g. 'clause was added in v2 (released
                            2026-05-05T12:47Z), failures all post-12:47'>",
   "confidence": 0.0-1.0,
   "recommended_action": "<one sentence — what to change. Be specific:
                          'Remove the X clause' or 'Replace X with Y'.>"}

Rules:
  - suspected_prompt_clause MUST be an exact substring of the current
    prompt verbatim — no paraphrase. If no clause fits, return "".
  - confidence ≤ 0.5 if you cannot pin a specific clause OR the prompt
    diff doesn't temporally correlate with the failures.
  - Don't invent prompt versions or timestamps not present in the input.
"""


def generate_hypothesis(
    cluster: FailureCluster,
    *,
    current_prompt: str,
    recent_versions: list[PromptVersionRef] | None = None,
    judge_model: str | None = None,
) -> Hypothesis:
    """Name the root cause + suspected clause for a failure cluster.

    Args:
        cluster: a FailureCluster from cluster_failures().
        current_prompt: the live target-agent system prompt verbatim.
        recent_versions: prompt history, oldest first. Used so the LLM
            can correlate the cluster's timeframe with a deploy.
        judge_model: override the model used for reasoning.

    Returns:
        Hypothesis with structured fields for the Slack card / patch gen.
    """
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )

    versions = recent_versions or []
    version_block = _render_versions(versions)
    failures_block = "\n".join(
        f"  [{i}] input: {f.get('input', '')[:160]!r}  "
        f"output: {f.get('output', '')[:160]!r}  "
        f"explanation: {f.get('explanation', '')[:160]!r}"
        for i, f in enumerate(cluster.sample_failures)
    )
    prompt = (
        _HYP_PROMPT
        + f"\n\n=== CLUSTER ===\n"
        + f"pattern: {cluster.pattern_name}\n"
        + f"common attrs: {json.dumps(cluster.common_attributes)}\n"
        + f"member count: {len(cluster.trace_ids)}\n"
        + f"sample failures (input → output → explanation):\n{failures_block}\n\n"
        + f"=== CURRENT FINPAY PROMPT ===\n{current_prompt}\n\n"
        + f"=== RECENT VERSION HISTORY ===\n{version_block}\n"
    )

    parsed = _gemini_json(prompt, model=judge_model)

    clause = str(parsed.get("suspected_prompt_clause", "")).strip()
    confidence = float(parsed.get("confidence", 0.0) or 0.0)
    if clause and clause not in current_prompt:
        # The model paraphrased instead of quoting verbatim. Penalize
        # confidence and clear the clause so callers don't trust it.
        clause = ""
        confidence = min(confidence, 0.5)

    return Hypothesis(
        pattern_name=cluster.pattern_name,
        root_cause=str(parsed.get("root_cause", "")).strip(),
        suspected_prompt_clause=clause,
        correlation_evidence=str(parsed.get("correlation_evidence", "")).strip(),
        confidence=confidence,
        recommended_action=str(parsed.get("recommended_action", "")).strip(),
    )


def _render_versions(versions: list[PromptVersionRef]) -> str:
    if not versions:
        return "(no version history available)"
    lines = []
    for v in versions:
        lines.append(
            f"--- version {v.version} (released {v.released_at.isoformat()}) ---"
        )
        if v.notes:
            lines.append(f"notes: {v.notes}")
        lines.append("instruction:")
        lines.append(v.instruction)
        lines.append("")
    return "\n".join(lines)


def _gemini_json(prompt: str, *, model: str) -> dict[str, Any]:
    from google import genai
    from google.genai.types import HttpOptions

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        http_options=HttpOptions(
            base_url=os.environ.get("VERTEX_API_HOST", "https://aiplatform.googleapis.com"),
        ),
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    return json.loads(text)
