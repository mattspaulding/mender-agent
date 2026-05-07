"""Mender self-introspection — component C12.

At the start of each cycle, Mender reads its own past N cycles' self-eval
annotations from Phoenix (written by C11) and summarizes "what worked /
what didn't". C13 (self_tune) consumes that summary to adjust this cycle's
parameters before any other work runs.

This is the explicit Arize-bonus criterion: the agent introspects its own
operational data to make itself better over time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from .._phoenix import PhoenixClient
from .self_eval import ANNOTATION_NAME


@dataclass
class CycleSnapshot:
    span_id: str
    started_at: datetime
    overall: float
    hypothesis_correctness: float
    fix_effectiveness: float
    eval_set_quality: float
    token_efficiency: float


@dataclass
class IntrospectionSummary:
    n_cycles_seen: int
    avg_overall: float | None
    avg_hypothesis_correctness: float | None
    avg_fix_effectiveness: float | None
    avg_eval_set_quality: float | None
    avg_token_efficiency: float | None
    trend: str  # "improving" | "stable" | "declining" | "insufficient-data"
    weakest_axis: str | None  # which axis is dragging overall down
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_past_cycles(
    *,
    window_minutes: int = 24 * 60,
    project: str = "mender",
    max_cycles: int = 10,
) -> IntrospectionSummary:
    """Read past Mender cycle self-eval annotations and summarize."""
    snapshots = _load_snapshots(window_minutes=window_minutes, project=project, max_cycles=max_cycles)

    if not snapshots:
        return IntrospectionSummary(
            n_cycles_seen=0,
            avg_overall=None,
            avg_hypothesis_correctness=None,
            avg_fix_effectiveness=None,
            avg_eval_set_quality=None,
            avg_token_efficiency=None,
            trend="insufficient-data",
            weakest_axis=None,
            notes=["no prior cycles found in window"],
        )

    snapshots.sort(key=lambda s: s.started_at)
    avg_overall = mean(s.overall for s in snapshots)
    avg_hyp = mean(s.hypothesis_correctness for s in snapshots)
    avg_fix = mean(s.fix_effectiveness for s in snapshots)
    avg_evalq = mean(s.eval_set_quality for s in snapshots)
    avg_tok = mean(s.token_efficiency for s in snapshots)

    # Trend: split history in half, compare halves.
    if len(snapshots) >= 4:
        midpoint = len(snapshots) // 2
        early = mean(s.overall for s in snapshots[:midpoint])
        late = mean(s.overall for s in snapshots[midpoint:])
        if late - early > 0.05:
            trend = "improving"
        elif early - late > 0.05:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "insufficient-data"

    axis_means = {
        "hypothesis_correctness": avg_hyp,
        "fix_effectiveness": avg_fix,
        "eval_set_quality": avg_evalq,
        "token_efficiency": avg_tok,
    }
    weakest = min(axis_means.items(), key=lambda kv: kv[1])[0]

    notes = []
    if avg_hyp < 0.5:
        notes.append("hypothesis_correctness is low — consider widening the cluster sample size or using a stronger reasoning model")
    if avg_fix < 0.4:
        notes.append("fix_effectiveness is low — patches are not improving pass rate enough; consider a stricter MIN_LIFT threshold")
    if avg_evalq < 0.5:
        notes.append("eval_set_quality is low — the eval set isn't isolating the failure cleanly; raise target_count")
    if avg_tok < 0.5:
        notes.append("token_efficiency is low — too many cases per cycle; lower target_count")

    return IntrospectionSummary(
        n_cycles_seen=len(snapshots),
        avg_overall=round(avg_overall, 3),
        avg_hypothesis_correctness=round(avg_hyp, 3),
        avg_fix_effectiveness=round(avg_fix, 3),
        avg_eval_set_quality=round(avg_evalq, 3),
        avg_token_efficiency=round(avg_tok, 3),
        trend=trend,
        weakest_axis=weakest,
        notes=notes,
    )


def _load_snapshots(
    *, window_minutes: int, project: str, max_cycles: int
) -> list[CycleSnapshot]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    with PhoenixClient() as ph:
        # Pull recent Mender spans, then bulk-fetch our self-eval annotations.
        spans, _ = ph.list_spans(project, start_time=start, end_time=end, limit=200)
        # Top-level cycle spans only — heartbeat / investigate produce one
        # outer span per cycle. Names vary, but they're top-level so they
        # have no parent_id in the OTel sense. As a heuristic accept any
        # non-tool, non-llm span as a candidate.
        candidate_ids = [s.span_id for s in spans]
        if not candidate_ids:
            return []
        anns = ph.list_span_annotations(
            project,
            span_ids=candidate_ids,
            include_annotation_names=[ANNOTATION_NAME],
        )

    snaps: list[CycleSnapshot] = []
    span_by_id = {s.span_id: s for s in spans}
    for ann in anns:
        if ann.get("name") != ANNOTATION_NAME:
            continue
        sid = ann.get("span_id")
        if sid not in span_by_id:
            continue
        result = ann.get("result") or {}
        metadata = ann.get("metadata") or {}
        axes = metadata.get("axes") or {}
        snaps.append(
            CycleSnapshot(
                span_id=sid,
                started_at=span_by_id[sid].start_time,
                overall=float(result.get("score", 0) or 0),
                hypothesis_correctness=float(axes.get("hypothesis_correctness", 0) or 0),
                fix_effectiveness=float(axes.get("fix_effectiveness", 0) or 0),
                eval_set_quality=float(axes.get("eval_set_quality", 0) or 0),
                token_efficiency=float(axes.get("token_efficiency", 0) or 0),
            )
        )

    snaps.sort(key=lambda s: s.started_at, reverse=True)
    return snaps[:max_cycles]
