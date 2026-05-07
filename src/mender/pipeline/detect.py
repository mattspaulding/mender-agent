"""Failure clustering — component C4.

`cluster_failures(failures)` is a deterministic step that groups a set
of failed/partial-scored user turns into one or more named clusters
with structured attributes. Used by:

  - the Slack incident formatter (D1) to fill the `pattern` and
    `affected traces` fields of the incident card;
  - the self-introspection step (C12) to compare cluster shapes
    across heartbeats over time.

The agent's heartbeat does *some* of this reasoning informally via the
typed trace tools. This module is for the structured pipeline path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FailureCluster:
    pattern_name: str
    trace_ids: list[str]
    span_ids: list[str]
    common_attributes: dict[str, Any] = field(default_factory=dict)
    sample_failures: list[dict] = field(default_factory=list)
    confidence: float = 0.0  # 0-1, judge's self-rated cohesion

    def to_dict(self) -> dict:
        return asdict(self)


_CLUSTER_PROMPT = """\
You are analyzing failed/partial-scored user turns from FinPay Support, a
customer-support agent. Group them into 1–3 clusters; each cluster should
share a clearly-stated common failure pattern.

For each cluster, return:
  pattern_name        — one short noun phrase (≤ 10 words) like
                        "ambiguous currency silently defaulted to USD"
  common_attributes   — small dict of structured commonalities
                        (e.g. {"input_trait": "ambiguous bare amount",
                               "agent_failure_mode": "default to USD",
                               "currency_pair_handling": "skipped clarifier"})
  member_indices      — list of indices (0-based, into the input list) of
                        the rows in this cluster
  confidence          — 0.0–1.0 self-rated cohesion (how tightly the
                        member rows actually share the named pattern)

Rules:
  - If failures are heterogeneous, return up to 3 clusters.
  - If you see one dominant pattern, return one cluster.
  - Every input row must end up in exactly one cluster.
  - Index must be in range [0, N-1].

Reply STRICT JSON, no prose, no fences:
  {"clusters": [{"pattern_name": "...", "common_attributes": {...},
                 "member_indices": [...], "confidence": ...}, ...]}
"""


def cluster_failures(
    failures: list[dict],
    *,
    min_cluster_size: int = 1,
    judge_model: str | None = None,
) -> list[FailureCluster]:
    """Cluster failed turns by what they have in common.

    Args:
        failures: rows from get_failed_traces() — each must have
            span_id, trace_id, input, output, label, score, explanation.
        min_cluster_size: drop clusters smaller than this (default 1
            keeps singletons, which is right for v1: small windows
            often have only a handful of failures).
        judge_model: override the Gemini model used to name clusters.

    Returns:
        List of FailureCluster ordered by size (largest first).
    """
    if not failures:
        return []

    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )

    rendered = "\n".join(
        f"[{i}] input: {row.get('input', '')[:200]!r}  "
        f"output: {row.get('output', '')[:200]!r}  "
        f"label: {row.get('label', '?')}  "
        f"explanation: {row.get('explanation', '')[:200]!r}"
        for i, row in enumerate(failures)
    )
    prompt = (
        _CLUSTER_PROMPT
        + f"\n\n=== {len(failures)} FAILED TURNS ===\n"
        + rendered
    )

    raw = _gemini_json(prompt, model=judge_model)
    raw_clusters = raw.get("clusters", [])

    out: list[FailureCluster] = []
    seen_indices: set[int] = set()
    for c in raw_clusters:
        indices = [
            i for i in c.get("member_indices", [])
            if isinstance(i, int) and 0 <= i < len(failures) and i not in seen_indices
        ]
        if len(indices) < min_cluster_size:
            continue
        seen_indices.update(indices)
        members = [failures[i] for i in indices]
        out.append(
            FailureCluster(
                pattern_name=str(c.get("pattern_name", "(unnamed)")).strip(),
                trace_ids=[m["trace_id"] for m in members],
                span_ids=[m["span_id"] for m in members],
                common_attributes=dict(c.get("common_attributes", {})),
                sample_failures=members[:3],
                confidence=float(c.get("confidence", 0.0) or 0.0),
            )
        )

    # Backstop: any failures the judge didn't bucket get grouped as
    # "uncategorized" — better than dropping data on the floor.
    leftover = [
        i for i in range(len(failures)) if i not in seen_indices
    ]
    if leftover and len(leftover) >= min_cluster_size:
        members = [failures[i] for i in leftover]
        out.append(
            FailureCluster(
                pattern_name="uncategorized",
                trace_ids=[m["trace_id"] for m in members],
                span_ids=[m["span_id"] for m in members],
                common_attributes={},
                sample_failures=members[:3],
                confidence=0.0,
            )
        )

    out.sort(key=lambda c: -len(c.trace_ids))
    return out


def _gemini_json(prompt: str, *, model: str) -> dict:
    """One Gemini call constrained to JSON output."""
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
