"""Mender self-eval — component C11.

After each completed cycle, score Mender's own work on four axes and
write the scores back to Phoenix as annotations on Mender's traces.
The self-introspection step (C12) reads these annotations on the next
cycle to compute trends and adjust cycle parameters (C13).

Axes (all 0.0–1.0):
  hypothesis_correctness — did the suspected_prompt_clause actually
    point at the real cause? (Computed from baseline-vs-staged lift +
    a brief LLM check.)
  fix_effectiveness — how much pass-rate the patch added.
  eval_set_quality — did the eval set actually isolate the failure?
    (high if direct-cases failed at baseline AND control cases passed
    at baseline; low otherwise.)
  token_efficiency — bounded inverse of LLM-call count vs. an ideal
    of ~6 calls per cycle.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

from .._phoenix import PhoenixClient
from .incident import Incident

ANNOTATION_NAME = "mender_self_eval"
ANNOTATION_IDENTIFIER = "v1"


@dataclass
class SelfEvalResult:
    hypothesis_correctness: float
    fix_effectiveness: float
    eval_set_quality: float
    token_efficiency: float
    overall: float
    explanation: str

    def to_dict(self) -> dict:
        return asdict(self)


def score_cycle(incident: Incident, *, judge_model: str | None = None) -> SelfEvalResult:
    """Score a completed cycle. Idempotent — pure function over incident data."""
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )

    base = incident.baseline_eval or {}
    staged = incident.staged_eval or {}
    base_total = len(base.get("results", [])) or 1
    base_pass = base.get("pass_count", 0)
    staged_total = len(staged.get("results", [])) or 1
    staged_pass = staged.get("pass_count", 0)

    # 1. fix_effectiveness — straight pass-rate lift, normalized to [0,1].
    base_rate = base_pass / base_total
    staged_rate = staged_pass / staged_total
    lift = staged_rate - base_rate
    fix_effectiveness = max(0.0, min(1.0, lift / 0.6))  # 60pt lift = full credit

    # 2. eval_set_quality — direct cases should fail at baseline, controls
    #    should pass. (We don't store the original case isolates kind on
    #    the result rows, so we approximate: a *good* eval set has a
    #    bimodal baseline result — many fails AND some passes — and the
    #    staged run pushes the fails to passes.)
    base_results = base.get("results", [])
    base_fail_share = sum(1 for r in base_results if not r.get("passed")) / max(1, len(base_results))
    base_pass_share = 1 - base_fail_share
    # Want both > 0 but base_fail_share dominant. Score peaks when
    # base_fail_share is around 0.6-0.8 (a real failure mode AND
    # control coverage).
    if base_fail_share == 0:
        eval_set_quality = 0.2  # patch had nothing to fix; eval set may be miscalibrated
    elif base_pass_share == 0:
        eval_set_quality = 0.5  # all-fail eval set offers no false-positive safety
    else:
        # Triangle peak around 0.7 fail share
        eval_set_quality = max(0.0, 1 - abs(base_fail_share - 0.7) / 0.7)

    # 3. token_efficiency — number of cases is a rough proxy for token
    #    spend. Ideal cycle uses ~6-10 cases. Penalize >12 (wasteful) or
    #    <4 (under-tested).
    n_cases = max(base_total, staged_total)
    if n_cases <= 4:
        token_efficiency = 0.5
    elif n_cases <= 10:
        token_efficiency = 1.0
    elif n_cases <= 14:
        token_efficiency = 0.8
    else:
        token_efficiency = max(0.3, 1 - (n_cases - 14) * 0.1)

    # 4. hypothesis_correctness — uses the LLM to judge whether the
    #    cluster, suspected clause, and the eval-pass-rate lift are
    #    coherent. Falls back to deterministic if the call fails.
    hypothesis_correctness = _hypothesis_correctness_score(
        incident, lift=lift, model=judge_model
    )

    # Overall: weighted average (fix-effectiveness weighted highest).
    overall = (
        0.45 * fix_effectiveness
        + 0.30 * hypothesis_correctness
        + 0.15 * eval_set_quality
        + 0.10 * token_efficiency
    )

    explanation = (
        f"lift={lift:+.0%}; "
        f"hyp={hypothesis_correctness:.2f}; "
        f"evalq={eval_set_quality:.2f}; "
        f"tok={token_efficiency:.2f}"
    )

    return SelfEvalResult(
        hypothesis_correctness=round(hypothesis_correctness, 3),
        fix_effectiveness=round(fix_effectiveness, 3),
        eval_set_quality=round(eval_set_quality, 3),
        token_efficiency=round(token_efficiency, 3),
        overall=round(overall, 3),
        explanation=explanation,
    )


_HYP_PROMPT = """\
You are auditing one Mender debugging cycle. Decide whether the named
hypothesis really explains the cluster of failures.

Score 0.0–1.0:
  1.0 — hypothesis names a specific prompt clause that's clearly
        responsible AND the staged patch lifted pass rate substantially.
  0.5 — hypothesis is plausible but only partially supported.
  0.0 — hypothesis points at the wrong thing (e.g. the patch didn't
        improve, or the suspected clause is unrelated to the cluster).

Reply STRICT JSON, no prose:
  {"score": 0.0-1.0, "explanation": "<one sentence under 25 words>"}
"""


def _hypothesis_correctness_score(
    incident: Incident, *, lift: float, model: str
) -> float:
    hyp = incident.hypothesis or {}
    if not hyp:
        return 0.0

    body = (
        _HYP_PROMPT
        + f"\n\n=== CLUSTER ===\n"
        + f"pattern: {incident.cluster_pattern}\n"
        + f"\n=== HYPOTHESIS ===\n"
        + f"root_cause          : {hyp.get('root_cause', '')}\n"
        + f"suspected_clause    : {hyp.get('suspected_prompt_clause', '')!r}\n"
        + f"recommended_action  : {hyp.get('recommended_action', '')}\n"
        + f"hypothesis_confidence: {hyp.get('confidence', 0)}\n"
        + f"\n=== EVAL OUTCOME ===\n"
        + f"baseline_pass_rate  : {(incident.baseline_eval or {}).get('pass_rate', 0):.2f}\n"
        + f"staged_pass_rate    : {(incident.staged_eval or {}).get('pass_rate', 0):.2f}\n"
        + f"lift                : {lift:+.2f}\n"
    )

    try:
        parsed = _gemini_json(body, model=model)
        return float(parsed.get("score", 0.0) or 0.0)
    except Exception:
        # Best-effort fallback if the LLM judge fails.
        return max(0.0, min(1.0, 0.5 + lift))


def write_to_phoenix(
    cycle_span_id: str,
    result: SelfEvalResult,
    *,
    project: str = "mender",
) -> dict:
    """Attach the self-eval scores as a span annotation on Mender's cycle span.

    The cycle_span_id is the span_id of Mender's own outermost span for
    this cycle (heartbeat / investigate run). C12 reads these back next
    cycle to compute trends.
    """
    with PhoenixClient() as ph:
        return ph.annotate_spans(
            [
                {
                    "name": ANNOTATION_NAME,
                    "annotator_kind": "CODE",
                    "span_id": cycle_span_id,
                    "identifier": ANNOTATION_IDENTIFIER,
                    "result": {
                        "score": result.overall,
                        "label": "self_eval",
                        "explanation": result.explanation,
                    },
                    "metadata": {
                        "axes": {
                            "hypothesis_correctness": result.hypothesis_correctness,
                            "fix_effectiveness": result.fix_effectiveness,
                            "eval_set_quality": result.eval_set_quality,
                            "token_efficiency": result.token_efficiency,
                        },
                    },
                }
            ]
        )


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
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
