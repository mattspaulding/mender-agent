"""LLM-as-judge eval scorer for FinPay traces (component B3).

For every recent FinPay span that doesn't already carry a
`currency_conversion` annotation, this:
  1. Pulls input + output text from the span.
  2. Asks Gemini to judge: was currency handled correctly?
  3. POSTs the result back to Phoenix as a span annotation
     with score (0-1), label (pass/fail/partial/n_a), and a one-line
     explanation.

Mender's detection chain (C3-C5) reads these annotations to spot
eval-score regressions.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rich.console import Console

from .._phoenix import PhoenixClient, Span

DEFAULT_PROJECT = "finpay-support"

_log = logging.getLogger(__name__)
_console = Console()

ANNOTATION_NAME = "currency_conversion"
ANNOTATION_IDENTIFIER = "v1"  # bumping this re-scores everything

_JUDGE_INSTRUCTION = """\
You are a strict eval judge for FinPay Support, a customer-service agent
for a payments app. You see ONE user turn (user input + agent reply) and
must score how well the agent handled currency.

Scoring rubric:

  PASS (1.0)   Currency handling is fully correct:
               - For currency-conversion queries: the agent invoked the
                 exchange-rate tool with the user's stated currencies
                 (or asked one clarifying question when truly ambiguous)
                 AND produced a numerically plausible answer.
               - For non-currency queries: the agent correctly answered
                 the question without inventing currency context.

  PARTIAL (0.5) The agent handled currency adequately but with a flaw —
                e.g. used the right tool but rounded oddly, or included
                a small hallucination, or asked an unnecessary clarifier.

  FAIL (0.0)   The agent silently defaulted to USD on an ambiguous
               amount, used the wrong currency pair, ignored a clearly
               specified currency, or fabricated a rate.

  N_A (null)   The query is unrelated to currency (e.g. KYC, password
               reset, account limits). Score should be null and the
               trace excluded from regression metrics.

Reply STRICTLY as JSON, no prose, no code fences:
  {"label": "pass" | "partial" | "fail" | "n_a",
   "score": 1.0 | 0.5 | 0.0 | null,
   "explanation": "<one sentence, under 25 words>"}
"""


@dataclass
class ScoreResult:
    label: str
    score: float | None
    explanation: str


@dataclass
class WindowStats:
    scanned: int
    scored: int
    skipped_already_scored: int
    skipped_non_currency: int
    failures: int
    elapsed_seconds: float


def _parse_window(spec: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([mh])\s*", spec)
    if not m:
        raise ValueError(f"bad window: {spec!r} (try '60m', '6h')")
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 60


def _build_judge_prompt(span: Span) -> str:
    return (
        _JUDGE_INSTRUCTION
        + "\n\n=== USER INPUT ===\n"
        + (span.input_text.strip() or "(empty)")
        + "\n\n=== AGENT REPLY ===\n"
        + (span.output_text.strip() or "(empty)")
    )


def _parse_judge_response(text: str) -> ScoreResult:
    """Tolerant parse: judge sometimes wraps JSON in code fences or
    in a single-element array."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"judge returned non-JSON: {raw[:120]}") from e
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if not isinstance(obj, dict):
        raise ValueError(f"judge returned non-object: {raw[:120]}")
    label = str(obj.get("label", "")).lower().strip()
    if label not in {"pass", "partial", "fail", "n_a", "n/a"}:
        raise ValueError(f"unknown label: {label!r}")
    if label == "n/a":
        label = "n_a"
    score = obj.get("score")
    if score is not None:
        score = float(score)
    explanation = str(obj.get("explanation", "")).strip()[:300]
    return ScoreResult(label=label, score=score, explanation=explanation)


def _resolve_judge_model(judge_model: str | None) -> str:
    return judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )


def _annotation_payloads(
    *,
    span_id: str,
    trace_id: str,
    result: ScoreResult,
    judge_model: str,
) -> tuple[list[dict], list[dict]]:
    """Build (span_annotations, trace_annotations) for one scored turn."""
    payload = {
        "label": result.label,
        "score": result.score,
        "explanation": result.explanation,
    }
    metadata = {"judge_model": judge_model}
    span_anns = [
        {
            "name": ANNOTATION_NAME,
            "annotator_kind": "LLM",
            "span_id": span_id,
            "identifier": ANNOTATION_IDENTIFIER,
            "result": payload,
            "metadata": metadata,
        }
    ]
    trace_anns = []
    if trace_id:
        trace_anns.append(
            {
                "name": ANNOTATION_NAME,
                "annotator_kind": "LLM",
                "trace_id": trace_id,
                "identifier": ANNOTATION_IDENTIFIER,
                "result": payload,
                "metadata": metadata,
            }
        )
    return span_anns, trace_anns


def score_inline(
    *,
    trace_id: str,
    span_id: str,
    user_input: str,
    agent_output: str,
    project: str = DEFAULT_PROJECT,
    judge_model: str | None = None,
) -> ScoreResult:
    """Score one finished turn immediately and write annotations.

    Used by the traffic driver's --inline-score path: the FinPay /chat
    response carries the trace_id + span_id, plus the input/output we
    already have. Skip the Phoenix span fetch — go straight from text
    to judge call to annotation write.

    Idempotent: writes upsert on (name, identifier, trace_id) and
    (name, identifier, span_id), so retrying doesn't duplicate.
    """
    judge_model = _resolve_judge_model(judge_model)
    pseudo_span = Span(
        span_id=span_id,
        trace_id=trace_id,
        name="finpay.handle_chat",
        start_time=datetime.now(timezone.utc),
        end_time=None,
        input_text=user_input,
        output_text=agent_output,
        raw_attributes={},
    )
    result = _score_with_gemini(_build_judge_prompt(pseudo_span), model=judge_model)
    span_anns, trace_anns = _annotation_payloads(
        span_id=span_id,
        trace_id=trace_id,
        result=result,
        judge_model=judge_model,
    )
    with PhoenixClient() as ph:
        if span_anns:
            ph.annotate_spans(span_anns)
        if trace_anns:
            ph.annotate_traces(trace_anns)
    return result


def _score_with_gemini(prompt: str, *, model: str) -> ScoreResult:
    """One judge round-trip via the genai client (raw text in/out).

    Hard 30s timeout — the inline-scoring path used to hang
    indefinitely when Vertex stalled, blocking the entire traffic
    loop. With this, a stuck judge call surfaces as a TimeoutError
    that the caller can log and skip past.
    """
    from google import genai
    from google.genai.types import HttpOptions

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        http_options=HttpOptions(
            base_url=os.environ.get("VERTEX_API_HOST", "https://aiplatform.googleapis.com"),
            timeout=30_000,  # ms — Vertex returns in 1-10s normally; 30s is generous
        ),
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    text = (response.text or "")
    return _parse_judge_response(text)


def score_window(
    *,
    project: str,
    window_minutes: int,
    judge_model: str | None = None,
    rescore: bool = False,
) -> WindowStats:
    """Score every FinPay span in the window. Idempotent unless rescore."""
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL", os.environ.get("MENDER_MODEL", "gemini-3-flash-preview")
    )
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    t0 = datetime.now(timezone.utc)

    with PhoenixClient() as ph:
        # Fetch spans in window
        spans, _ = ph.list_spans(project, start_time=start, end_time=end, limit=200)
        # One canonical span per trace (the user turn). Prefer
        # "finpay.handle_chat" — our explicit wrapper where inline-
        # scoring writes annotations. Fall back to OpenInference's
        # auto "invocation [finpay]" for older traces from before
        # the wrapper.
        by_trace: dict[str, Span] = {}
        for s in spans:
            if not s.trace_id:
                continue
            if s.name == "finpay.handle_chat":
                by_trace[s.trace_id] = s
            elif "invocation" in s.name.lower() and s.trace_id not in by_trace:
                by_trace[s.trace_id] = s
        spans = list(by_trace.values())

        # Existing annotations to dedupe against. Dedupe on trace_id
        # (not span_id): the inline-scoring path attaches its
        # span_annotation to the "finpay.handle_chat" span (a sibling
        # of "invocation [finpay]" in the same trace), so a span_id
        # check would miss those and the batch would rescore. The
        # trace_annotation is unique per trace_id and present on both
        # the inline and batch paths.
        already: set[str] = set()
        if not rescore and spans:
            trace_ids = list({s.trace_id for s in spans if s.trace_id})
            anns = ph.list_trace_annotations(
                project,
                trace_ids=trace_ids,
                include_annotation_names=[ANNOTATION_NAME],
            )
            already = {ann.get("trace_id") for ann in anns if ann.get("name") == ANNOTATION_NAME}

        stats = WindowStats(
            scanned=len(spans),
            scored=0,
            skipped_already_scored=0,
            skipped_non_currency=0,
            failures=0,
            elapsed_seconds=0.0,
        )

        _console.print(
            f"[bold cyan]scorer[/]  project=[bold]{project}[/]  "
            f"window={window_minutes}m  judge={judge_model}  "
            f"spans={len(spans)}"
        )

        span_annotations: list[dict] = []
        trace_annotations: list[dict] = []
        # Same trace can have multiple "invocation" spans only in pathological
        # cases — but be defensive and only attach one trace annotation per
        # trace_id (Phoenix would upsert anyway, but it saves an HTTP call).
        seen_traces: set[str] = set()

        for span in spans:
            if span.trace_id and span.trace_id in already:
                stats.skipped_already_scored += 1
                continue
            try:
                result = _score_with_gemini(_build_judge_prompt(span), model=judge_model)
            except Exception as e:
                stats.failures += 1
                _console.print(f"  [red]err[/] {span.span_id[:12]}  {e}")
                continue

            tag = {
                "pass": "[green]pass[/]",
                "partial": "[yellow]part[/]",
                "fail": "[red]fail[/]",
                "n_a": "[dim] n/a[/]",
            }.get(result.label, result.label)
            score_str = f"{result.score:.1f}" if result.score is not None else "  -"
            _console.print(f"  {tag} {score_str}  [dim]{result.explanation[:80]}[/]")

            if result.label == "n_a":
                stats.skipped_non_currency += 1
                # Still annotate with n/a so we don't re-score next cycle.
            stats.scored += 1

            payload = {
                "label": result.label,
                "score": result.score,
                "explanation": result.explanation,
            }
            metadata = {"judge_model": judge_model}

            span_annotations.append(
                {
                    "name": ANNOTATION_NAME,
                    "annotator_kind": "LLM",
                    "span_id": span.span_id,
                    "identifier": ANNOTATION_IDENTIFIER,
                    "result": payload,
                    "metadata": metadata,
                }
            )

            # Trace-level annotation — what Phoenix's trace-list view
            # renders. Span annotations don't show there.
            if span.trace_id and span.trace_id not in seen_traces:
                seen_traces.add(span.trace_id)
                trace_annotations.append(
                    {
                        "name": ANNOTATION_NAME,
                        "annotator_kind": "LLM",
                        "trace_id": span.trace_id,
                        "identifier": ANNOTATION_IDENTIFIER,
                        "result": payload,
                        "metadata": metadata,
                    }
                )

        if span_annotations:
            ph.annotate_spans(span_annotations)
        if trace_annotations:
            ph.annotate_traces(trace_annotations)

        stats.elapsed_seconds = (datetime.now(timezone.utc) - t0).total_seconds()
        return stats
