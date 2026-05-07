"""Eval-set generation — component C6.

Given a Hypothesis from C5, produce 8–12 structured test cases that
isolate the suspected failure mode. The eval runner (C7) executes these
against the live target agent and against the patched-staging version
to measure whether a candidate fix actually works.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .hypothesize import Hypothesis


@dataclass
class EvalCase:
    id: str  # stable, e.g. "currency-ambiguous-001"
    input: str  # the user message to send to FinPay
    expected_behavior: str  # one-line: what the agent should do
    scoring_rubric: str  # what specifically counts as PASS
    isolates: str  # short tag: which failure mode this case targets
    difficulty: str  # "easy" | "medium" | "hard"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalSet:
    hypothesis_pattern: str  # echoes Hypothesis.pattern_name
    cases: list[EvalCase] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "hypothesis_pattern": self.hypothesis_pattern,
            "cases": [c.to_dict() for c in self.cases],
            "metadata": self.metadata,
        }


_GEN_PROMPT = """\
You are designing an eval set for FinPay Support, a customer-support
agent. The eval set must isolate ONE failure mode (the hypothesis) and
let us measure whether a candidate fix actually resolves it.

Produce 10 test cases. Distribution:
  - 6 cases that DIRECTLY trigger the failure mode (vary surface form
    so the agent can't pattern-match its way out).
  - 3 control cases that should already PASS on both buggy and patched
    versions (so we can detect a regression introduced by the fix).
  - 1 adversarial case that's tangentially related — tests whether the
    fix overgeneralizes.

Each case is a JSON object with keys:
  id                — stable kebab-case id (e.g. "ambiguous-amount-jpy")
  input             — the EXACT user message to send to FinPay
  expected_behavior — one sentence: what the agent should do
  scoring_rubric    — what specifically the judge will check (e.g. "agent
                      asks one clarifying question OR refuses to assume a
                      currency"). Be unambiguous.
  isolates          — one of: "direct" | "control" | "adversarial"
  difficulty        — "easy" | "medium" | "hard"

Reply STRICT JSON, no prose, no fences:
  {"cases": [ ... ]}
"""


def generate_eval_set(
    hypothesis: Hypothesis,
    *,
    sample_failures: list[dict] | None = None,
    target_count: int = 10,
    judge_model: str | None = None,
) -> EvalSet:
    """Build a test set tailored to the hypothesis.

    Args:
        hypothesis: from C5.
        sample_failures: optional rows from the cluster (input/output
            pairs), used as concrete examples for the generator.
        target_count: total number of cases to produce. Default 10 fits
            the demo's "8/12 fail then 11/12 pass" beat (Scene 5).
        judge_model: override the model used.
    """
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )

    samples = sample_failures or []
    samples_block = "\n".join(
        f"  [{i}] input: {s.get('input', '')[:160]!r}  "
        f"output: {s.get('output', '')[:160]!r}"
        for i, s in enumerate(samples[:5])
    ) or "  (no concrete samples provided)"

    prompt = (
        _GEN_PROMPT
        + f"\n\n=== HYPOTHESIS ===\n"
        + f"pattern              : {hypothesis.pattern_name}\n"
        + f"root_cause           : {hypothesis.root_cause}\n"
        + f"suspected_clause     : {hypothesis.suspected_prompt_clause!r}\n"
        + f"recommended_action   : {hypothesis.recommended_action}\n"
        + f"confidence           : {hypothesis.confidence}\n\n"
        + f"=== SAMPLE FAILURES (for grounding, do not copy verbatim) ===\n"
        + samples_block
        + f"\n\n=== TARGET COUNT ===\n{target_count}\n"
    )

    parsed = _gemini_json(prompt, model=judge_model)
    raw_cases = parsed.get("cases", [])

    cases: list[EvalCase] = []
    for c in raw_cases:
        try:
            cases.append(
                EvalCase(
                    id=str(c["id"]).strip(),
                    input=str(c["input"]).strip(),
                    expected_behavior=str(c["expected_behavior"]).strip(),
                    scoring_rubric=str(c["scoring_rubric"]).strip(),
                    isolates=str(c.get("isolates", "direct")).strip().lower(),
                    difficulty=str(c.get("difficulty", "medium")).strip().lower(),
                )
            )
        except (KeyError, ValueError):
            # Skip malformed cases rather than failing the whole set.
            continue

    return EvalSet(
        hypothesis_pattern=hypothesis.pattern_name,
        cases=cases,
        metadata={
            "judge_model": judge_model,
            "target_count": target_count,
            "actual_count": len(cases),
        },
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
