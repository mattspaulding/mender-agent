"""Eval runner — component C7.

Executes an EvalSet against a callable agent endpoint and judges each
response against its case-specific rubric. Used twice in a cycle:
once against the live (buggy) agent to measure the baseline pass rate,
then against a patched-staging agent to verify the fix lifts that
rate.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import httpx

from .eval_gen import EvalCase, EvalSet


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    score: float  # 0-1, mostly 0/1 but the judge may allow partial
    explanation: str
    response: str  # the agent's actual reply (truncated)
    latency_ms: int
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvalRun:
    target_label: str  # "live" | "patched-staging" | etc
    pass_count: int
    fail_count: int
    error_count: int
    pass_rate: float
    duration_seconds: float
    results: list[EvalResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target_label": self.target_label,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "error_count": self.error_count,
            "pass_rate": self.pass_rate,
            "duration_seconds": self.duration_seconds,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class AgentResponse:
    """Reply text plus the OTel ids of the underlying trace, when available.

    The eval runner uses trace_id/span_id (when populated) to write a
    trace annotation immediately after judging the case. If the
    endpoint can't surface those (e.g. an in-process callable that
    isn't OTel-instrumented), they remain empty and we fall back to
    just storing the result on the EvalRun.
    """

    reply: str
    trace_id: str = ""
    span_id: str = ""


# An agent endpoint takes a string input and returns AgentResponse.
# The default is an HTTP caller that hits FinPay's /chat endpoint;
# in-process callables build their own AgentResponse.
AgentCallable = Callable[[str], AgentResponse]


def http_endpoint(base_url: str, *, timeout: float = 120.0) -> AgentCallable:
    """Build a callable that POSTs to <base_url>/chat and returns
    AgentResponse, including the trace/span IDs FinPay exposes."""
    base = base_url.rstrip("/")

    def _call(message: str) -> AgentResponse:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{base}/chat", json={"message": message})
            r.raise_for_status()
            body = r.json() or {}
            return AgentResponse(
                reply=body.get("reply", ""),
                trace_id=body.get("trace_id", ""),
                span_id=body.get("span_id", ""),
            )

    return _call


_JUDGE_PROMPT = """\
You are scoring an eval case for FinPay Support. You see the case spec
and the actual agent reply. Decide PASS or FAIL by the case's rubric.

Reply STRICT JSON, no prose, no fences:
  {"passed": true | false,
   "score": 0.0–1.0,
   "explanation": "<one sentence under 25 words>"}

Rules:
  - score 1.0 for unambiguous pass, 0.0 for unambiguous fail. Use 0.5
    for borderline (e.g. partially-correct).
  - "passed" is true iff score >= 0.5.
  - Be strict on the rubric. If the rubric requires asking for
    clarification and the agent silently assumes a currency, that's
    FAIL even if the math is internally consistent.
"""


def _judge_case(case: EvalCase, response: str, *, model: str) -> tuple[bool, float, str]:
    prompt = (
        _JUDGE_PROMPT
        + f"\n\n=== CASE ===\n"
        + f"id                : {case.id}\n"
        + f"input             : {case.input}\n"
        + f"expected_behavior : {case.expected_behavior}\n"
        + f"scoring_rubric    : {case.scoring_rubric}\n"
        + f"isolates          : {case.isolates}\n\n"
        + f"=== AGENT REPLY ===\n{response}\n"
    )
    parsed = _gemini_json(prompt, model=model)
    return (
        bool(parsed.get("passed", False)),
        float(parsed.get("score", 0.0) or 0.0),
        str(parsed.get("explanation", "")).strip(),
    )


def run_eval_set(
    eval_set: EvalSet,
    *,
    target: AgentCallable,
    target_label: str = "live",
    judge_model: str | None = None,
    on_progress: Callable[[EvalResult], None] | None = None,
) -> EvalRun:
    """Run every case in the set against `target`. Returns aggregate run."""
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )

    # Lazy-import the Phoenix annotation writer so that local-only
    # callers (no Phoenix creds in the env) don't pay for it.
    annotate_target_project = os.environ.get(
        "MENDER_EVAL_ANNOTATION_PROJECT", "finpay-support"
    )
    _phoenix_writer = _build_phoenix_writer(annotate_target_project)

    t0 = time.monotonic()
    results: list[EvalResult] = []
    for case in eval_set.cases:
        case_t0 = time.monotonic()
        try:
            response = target(case.input)
        except Exception as e:
            result = EvalResult(
                case_id=case.id,
                passed=False,
                score=0.0,
                explanation="",
                response="",
                latency_ms=int((time.monotonic() - case_t0) * 1000),
                error=f"{e.__class__.__name__}: {e}"[:200],
            )
            results.append(result)
            if on_progress:
                on_progress(result)
            continue

        # Normalize: tolerate legacy callables that still return a bare
        # string (older tests etc.). New canonical shape is AgentResponse.
        if isinstance(response, AgentResponse):
            reply = response.reply
            trace_id = response.trace_id
            span_id = response.span_id
        else:
            reply = response
            trace_id = ""
            span_id = ""

        try:
            passed, score, explanation = _judge_case(case, reply, model=judge_model)
        except Exception as e:
            result = EvalResult(
                case_id=case.id,
                passed=False,
                score=0.0,
                explanation="",
                response=reply[:300],
                latency_ms=int((time.monotonic() - case_t0) * 1000),
                error=f"judge error: {e.__class__.__name__}: {e}"[:200],
            )
            results.append(result)
            if on_progress:
                on_progress(result)
            continue

        result = EvalResult(
            case_id=case.id,
            passed=passed,
            score=score,
            explanation=explanation,
            response=reply[:300],
            latency_ms=int((time.monotonic() - case_t0) * 1000),
        )
        results.append(result)
        if on_progress:
            on_progress(result)

        # Phoenix annotation — same `currency_conversion` name so the
        # trace-list label column populates for eval-runner traffic
        # too. Without this, the baseline-eval traces (which hit live
        # FinPay's /chat) appear in the trace list with a blank chip.
        if _phoenix_writer is not None and (trace_id or span_id):
            label = "pass" if passed else "fail"
            _phoenix_writer(
                trace_id=trace_id,
                span_id=span_id,
                name="currency_conversion",
                label=label,
                score=score,
                explanation=f"[eval:{case.id}] {explanation}"[:300],
                target_label=target_label,
            )

    duration = time.monotonic() - t0
    passes = sum(1 for r in results if r.passed and not r.error)
    fails = sum(1 for r in results if not r.passed and not r.error)
    errs = sum(1 for r in results if r.error)
    pass_rate = passes / len(results) if results else 0.0

    return EvalRun(
        target_label=target_label,
        pass_count=passes,
        fail_count=fails,
        error_count=errs,
        pass_rate=pass_rate,
        duration_seconds=duration,
        results=results,
    )


def _build_phoenix_writer(project: str):
    """Build a closure that writes one trace/span annotation per eval case.

    Returns None if the Phoenix client can't initialize (no creds, etc.).
    Each invocation is best-effort and logs but doesn't raise on failure
    so a Phoenix outage never fails an eval cycle.
    """
    try:
        from .._phoenix import PhoenixClient  # noqa: F401
    except Exception:
        return None

    def _write(
        *,
        trace_id: str,
        span_id: str,
        name: str,
        label: str,
        score: float,
        explanation: str,
        target_label: str,
    ) -> None:
        try:
            from .._phoenix import PhoenixClient as _PC

            payload_result = {
                "label": label,
                "score": score,
                "explanation": explanation,
            }
            metadata = {"target_label": target_label, "source": "eval_runner"}
            with _PC() as ph:
                if span_id:
                    ph.annotate_spans(
                        [
                            {
                                "name": name,
                                "annotator_kind": "LLM",
                                "span_id": span_id,
                                "identifier": "v1",
                                "result": payload_result,
                                "metadata": metadata,
                            }
                        ]
                    )
                if trace_id:
                    ph.annotate_traces(
                        [
                            {
                                "name": name,
                                "annotator_kind": "LLM",
                                "trace_id": trace_id,
                                "identifier": "v1",
                                "result": payload_result,
                                "metadata": metadata,
                            }
                        ]
                    )
        except Exception:  # noqa: BLE001 — best-effort
            pass

    return _write


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
