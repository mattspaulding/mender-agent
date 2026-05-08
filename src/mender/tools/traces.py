"""Typed trace-introspection tools (component C3).

These are ADK function tools that wrap the Phoenix REST API into three
opinionated calls Mender's heartbeat actually needs:

  fetch_recent_traces  - one row per user turn, with eval score
  summarize_eval_trend - bucketed timeseries: window-by-window scores
  get_failed_traces    - drill-down: full input/output for failures

Without these, Mender resorts to the Phoenix MCP tools and improvises
its own joins between spans and annotations — which it does poorly,
takes 20+ tool calls, and easily misses the actual signal. With them,
each cycle is 2-3 tool calls and the data is shaped for the next
pipeline step.

The Phoenix MCP toolset stays registered alongside these for ad-hoc
exploration the typed tools don't cover.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .._phoenix import PhoenixClient, Span

EVAL_NAME = "currency_conversion"  # must match scorer.ANNOTATION_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _join_spans_with_scores(
    ph: PhoenixClient, project: str, spans: list[Span]
) -> list[dict[str, Any]]:
    """Attach eval annotations to spans. Returns plain dicts (LLM-friendly)."""
    if not spans:
        return []
    span_ids = [s.span_id for s in spans]
    anns = ph.list_span_annotations(
        project, span_ids=span_ids, include_annotation_names=[EVAL_NAME]
    )
    by_span: dict[str, dict] = {}
    for ann in anns:
        if ann.get("name") != EVAL_NAME:
            continue
        result = ann.get("result") or {}
        by_span[ann.get("span_id")] = {
            "score": result.get("score"),
            "label": result.get("label"),
            "explanation": result.get("explanation", ""),
        }

    rows: list[dict[str, Any]] = []
    for s in spans:
        ann = by_span.get(s.span_id)
        rows.append(
            {
                "span_id": s.span_id,
                "trace_id": s.trace_id,
                "start_time": s.start_time.isoformat(),
                "input": s.input_text[:500],
                "output": s.output_text[:500],
                "score": ann["score"] if ann else None,
                "label": ann["label"] if ann else "unscored",
                "explanation": ann["explanation"] if ann else "",
            }
        )
    return rows


def _list_invocation_spans(
    ph: PhoenixClient, project: str, window_minutes: int
) -> list[Span]:
    end = _now()
    start = end - timedelta(minutes=window_minutes)
    spans, _ = ph.list_spans(project, start_time=start, end_time=end, limit=500)
    # One canonical span per trace (the user turn). Prefer
    # "finpay.handle_chat" — that's our explicit wrapper where
    # inline-scoring writes span_annotations. Fall back to
    # OpenInference's auto "invocation [finpay]" for older batch-
    # scored data that pre-dates the wrapper.
    by_trace: dict[str, Span] = {}
    for s in spans:
        if not s.trace_id:
            continue
        if s.name == "finpay.handle_chat":
            by_trace[s.trace_id] = s  # always wins
        elif "invocation" in s.name.lower() and s.trace_id not in by_trace:
            by_trace[s.trace_id] = s  # fallback
    return list(by_trace.values())


def fetch_recent_traces(
    window_minutes: int = 60,
    project: str = "finpay-support",
) -> dict:
    """Return recent FinPay user turns with their eval scores.

    Use this as your primary handle on the target system's behavior.
    Each row is one user turn: input text, agent reply, and the
    `currency_conversion` eval annotation if one exists yet.

    Args:
        window_minutes: How far back to scan, in minutes.
        project: Phoenix project name. Defaults to "finpay-support".

    Returns:
        A dict with:
          window_minutes: int
          total: int — number of user turns in the window
          scored: int — number with an eval annotation
          unscored: int — turns the scorer hasn't reached yet
          rows: list of {span_id, trace_id, start_time, input, output,
                         score, label, explanation}
    """
    with PhoenixClient() as ph:
        spans = _list_invocation_spans(ph, project, window_minutes)
        rows = _join_spans_with_scores(ph, project, spans)
    scored = sum(1 for r in rows if r["score"] is not None)
    return {
        "window_minutes": window_minutes,
        "total": len(rows),
        "scored": scored,
        "unscored": len(rows) - scored,
        "rows": rows,
    }


def summarize_eval_trend(
    window_minutes: int = 60,
    project: str = "finpay-support",
    bucket_minutes: int = 5,
) -> dict:
    """Bucketed eval-score timeseries — the chart Scene 4 displays.

    Splits the window into equal-size buckets (default 5 min) and
    reports per-bucket trace count, average score, and pass/fail/n_a
    counts. Use this to spot when scores started dropping; combine with
    the bucket boundary plus prompt-version history (list_prompt_versions)
    to correlate with a deploy event.

    Args:
        window_minutes: How far back to scan.
        project: Phoenix project name.
        bucket_minutes: Bucket width.

    Returns:
        A dict with:
          window_minutes: int
          bucket_minutes: int
          buckets: list of {bucket_start, count, scored_count, avg_score,
                            pass_count, partial_count, fail_count, na_count}
          summary: {trend, earliest_score, latest_score, regression_detected}
    """
    with PhoenixClient() as ph:
        spans = _list_invocation_spans(ph, project, window_minutes)
        rows = _join_spans_with_scores(ph, project, spans)

    end = _now()
    start = end - timedelta(minutes=window_minutes)
    bucket_count = max(1, window_minutes // bucket_minutes)
    bucket_starts = [
        start + timedelta(minutes=i * bucket_minutes) for i in range(bucket_count)
    ]
    by_bucket: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        ts = datetime.fromisoformat(row["start_time"])
        offset = (ts - start).total_seconds() / 60
        idx = int(offset // bucket_minutes)
        idx = max(0, min(bucket_count - 1, idx))
        by_bucket[idx].append(row)

    buckets = []
    for i, bstart in enumerate(bucket_starts):
        items = by_bucket.get(i, [])
        scored = [r for r in items if r["score"] is not None]
        avg = sum(r["score"] for r in scored) / len(scored) if scored else None
        labels = [r["label"] for r in items]
        buckets.append(
            {
                "bucket_start": bstart.isoformat(),
                "count": len(items),
                "scored_count": len(scored),
                "avg_score": round(avg, 3) if avg is not None else None,
                "pass_count": labels.count("pass"),
                "partial_count": labels.count("partial"),
                "fail_count": labels.count("fail"),
                "na_count": labels.count("n_a"),
            }
        )

    # Summary: compare first and last buckets that have scored data.
    scored_buckets = [b for b in buckets if b["avg_score"] is not None]
    earliest = scored_buckets[0]["avg_score"] if scored_buckets else None
    latest = scored_buckets[-1]["avg_score"] if scored_buckets else None
    regression = (
        earliest is not None
        and latest is not None
        and earliest - latest >= 0.1  # 10pt drop = regression worth flagging
    )
    trend = "stable"
    if earliest is not None and latest is not None:
        if latest < earliest - 0.05:
            trend = "declining"
        elif latest > earliest + 0.05:
            trend = "improving"

    return {
        "window_minutes": window_minutes,
        "bucket_minutes": bucket_minutes,
        "buckets": buckets,
        "summary": {
            "trend": trend,
            "earliest_score": earliest,
            "latest_score": latest,
            "regression_detected": regression,
        },
    }


def get_failed_traces(
    window_minutes: int = 60,
    project: str = "finpay-support",
    max_n: int = 20,
) -> dict:
    """Drill-down: every trace that scored fail or partial in the window.

    Use this AFTER summarize_eval_trend flags a regression. Returns full
    input + output text + judge explanation so you can cluster failures
    by what they have in common (the C4 step in the pipeline).

    Args:
        window_minutes: How far back to scan.
        project: Phoenix project name.
        max_n: Cap on returned rows (newest first).

    Returns:
        A dict with:
          total: int — count of fail+partial in window
          rows: list of {span_id, trace_id, start_time, input, output,
                         score, label, explanation}
    """
    with PhoenixClient() as ph:
        spans = _list_invocation_spans(ph, project, window_minutes)
        rows = _join_spans_with_scores(ph, project, spans)
    failures = [r for r in rows if r["label"] in {"fail", "partial"}]
    failures.sort(key=lambda r: r["start_time"], reverse=True)
    return {
        "window_minutes": window_minutes,
        "total": len(failures),
        "rows": failures[:max_n],
    }
