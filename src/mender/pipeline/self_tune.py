"""Mender self-tuning — component C13.

Reads the IntrospectionSummary produced by C12 and returns a CycleParams
record that adjusts this cycle's behavior. Small, stable parameter set:
nothing exotic, just the knobs that meaningfully change cost / coverage.

The orchestrator (C10) calls `tune(introspection)` at the start of each
cycle and uses the returned params to drive eval count, hypothesis
confidence threshold, and other downstream calls.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .self_introspect import IntrospectionSummary


@dataclass
class CycleParams:
    eval_target_count: int  # how many cases C6 should generate
    min_hypothesis_confidence: float  # below this, skip patch generation
    min_lift: float  # below this, dismiss the patch
    cluster_max_failures: int  # cap on failures fed to clusterer

    notes: list[str] | None = None  # human-readable rationale

    def to_dict(self) -> dict:
        return asdict(self)


# Defaults if there's no introspection signal yet.
_DEFAULTS = CycleParams(
    eval_target_count=8,
    min_hypothesis_confidence=0.6,
    min_lift=0.25,
    cluster_max_failures=20,
)


def tune(introspection: IntrospectionSummary) -> CycleParams:
    """Adjust cycle parameters based on past performance.

    Heuristics — deliberately simple. Each axis nudges one parameter.
    Compounded effects across axes produce the visible "Mender gets
    better at the job" signal over time.
    """
    if introspection.n_cycles_seen == 0:
        return CycleParams(**asdict(_DEFAULTS))

    params = CycleParams(**asdict(_DEFAULTS))
    notes: list[str] = []

    # 1. eval_set_quality is low → generate more cases for better coverage.
    if (introspection.avg_eval_set_quality or 1.0) < 0.5:
        params.eval_target_count = 12
        notes.append("eval_set_quality<0.5 → bump eval_target_count to 12")

    # 2. token_efficiency is low → reduce case count next cycle.
    if (introspection.avg_token_efficiency or 1.0) < 0.5:
        params.eval_target_count = max(6, params.eval_target_count - 2)
        notes.append("token_efficiency<0.5 → trim eval_target_count")

    # 3. fix_effectiveness is consistently low → tighten min_lift so we
    #    don't keep proposing weak patches.
    if (introspection.avg_fix_effectiveness or 1.0) < 0.4:
        params.min_lift = 0.35
        notes.append("fix_effectiveness<0.4 → raise min_lift to 0.35")

    # 4. hypothesis_correctness is low → require higher hypothesis
    #    confidence before going through eval/patch generation.
    if (introspection.avg_hypothesis_correctness or 1.0) < 0.5:
        params.min_hypothesis_confidence = 0.75
        notes.append("hypothesis_correctness<0.5 → require confidence>=0.75")

    # 5. Trend signal — if Mender is improving, gently relax cost knobs.
    if introspection.trend == "improving":
        params.eval_target_count = max(6, params.eval_target_count - 1)
        notes.append("trend=improving → trim eval_target_count by 1")

    if introspection.trend == "declining":
        params.eval_target_count = params.eval_target_count + 2
        notes.append("trend=declining → bump eval_target_count by 2 to widen coverage")

    params.notes = notes or ["no adjustments needed; defaults applied"]
    return params
