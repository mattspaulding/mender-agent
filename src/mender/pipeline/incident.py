"""Incident state machine — component C10.

Ties C3-C9 into one orchestrated flow, persists state across cycles so
the same incident doesn't re-fire on every heartbeat, and produces the
final structured Incident record the Slack action layer (D1-D3) and
the Web UI (E3) consume.

State machine:

    detected       — failures clustered, hypothesis pending
        ↓
    hypothesized   — root cause + suspected clause named
        ↓
    evaluating     — eval set generated, baseline pass rate measured
        ↓
    patch_proposed — patch + staged eval pass rate measured;
                     Slack incident card sent (action layer fires here)
        ↓
    patch_applied  — human approved + prod prompt swapped (D3)
        ↓
    resolved       — recovery confirmed by next heartbeat scoring green

Or:

    dismissed      — pipeline ran but the candidate patch did NOT lift
                     pass rate by the required margin; no Slack card.

Persistence is a JSON file at MENDER_INCIDENTS_PATH (defaults to
.mender/incidents.json). Cloud Run mode swaps this for Firestore.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Minimum pass-rate lift (staged - live) required to escalate from
# `evaluating` to `patch_proposed`. Below this, the patch is dismissed
# and we wait for a better hypothesis on the next cycle.
MIN_LIFT = 0.25

# Don't re-run the pipeline for the same pattern more often than this.
DEDUPE_WINDOW = timedelta(hours=2)


@dataclass
class Incident:
    id: str
    target_project: str
    cluster_pattern: str
    affected_trace_ids: list[str]
    hypothesis: dict[str, Any]
    baseline_eval: dict[str, Any] | None
    staged_eval: dict[str, Any] | None
    patch: dict[str, Any] | None
    state: str  # detected | hypothesized | evaluating | patch_proposed | patch_applied | resolved | dismissed
    created_at: str  # ISO timestamp
    updated_at: str
    history: list[dict] = field(default_factory=list)  # state transitions
    introspection: dict[str, Any] | None = None  # C12 summary for this cycle
    cycle_params: dict[str, Any] | None = None  # C13 params used this cycle
    self_eval: dict[str, Any] | None = None  # C11 self-score for this cycle

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> Incident:
        return cls(**raw)

    def transition(self, state: str, *, note: str = "") -> None:
        self.history.append(
            {"at": _now_iso(), "from": self.state, "to": state, "note": note}
        )
        self.state = state
        self.updated_at = _now_iso()


class IncidentStore:
    """Tiny JSON-file persistence. Thread-safe via a process-wide lock.

    Cloud Run autoscale will need Firestore instead — the interface
    here (find_open_for_pattern, upsert, list_*) is small enough that
    a Firestore-backed implementation is a drop-in.
    """

    _lock = threading.Lock()

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("MENDER_INCIDENTS_PATH", ".mender/incidents.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]")

    def _read(self) -> list[dict]:
        with self.path.open("r") as f:
            return json.load(f)

    def _write(self, items: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(items, f, indent=2, default=str)
        tmp.replace(self.path)

    def list_all(self) -> list[Incident]:
        return [Incident.from_dict(d) for d in self._read()]

    def list_open(self) -> list[Incident]:
        return [i for i in self.list_all() if i.state not in {"resolved", "dismissed"}]

    def find_open_for_pattern(self, project: str, pattern: str) -> Incident | None:
        for i in self.list_open():
            if i.target_project == project and i.cluster_pattern == pattern:
                return i
        return None

    def upsert(self, incident: Incident) -> None:
        with self._lock:
            items = self._read()
            for idx, raw in enumerate(items):
                if raw["id"] == incident.id:
                    items[idx] = incident.to_dict()
                    break
            else:
                items.append(incident.to_dict())
            self._write(items)


@dataclass
class PipelineOutcome:
    incident: Incident | None  # None when no failures detected this cycle
    skipped_reason: str | None = None  # set if dedupe / no-failures / etc
    elapsed_seconds: float = 0.0


def run_incident_pipeline(
    *,
    target_project: str = "finpay-support",
    window_minutes: int = 60,
    store: IncidentStore | None = None,
    eval_target_count: int = 8,
    finpay_url: str = "http://127.0.0.1:8081",
) -> PipelineOutcome:
    """Run one full detect→hypothesize→eval→patch→stage→verify cycle.

    Returns the resulting Incident (in any terminal-for-this-cycle
    state) or None when there's nothing to do.
    """
    from datetime import datetime as _dt

    from finpay.prompts import live_version, list_versions

    from .detect import cluster_failures
    from .eval_gen import generate_eval_set
    from .eval_run import http_endpoint, run_eval_set
    from .hypothesize import PromptVersionRef, generate_hypothesis
    from .patch_gen import generate_patch
    from .staging import apply_patch_to_staging, simulated_finpay_endpoint

    from ..tools.traces import get_failed_traces

    t0 = _dt.now(timezone.utc)
    store = store or IncidentStore()

    # 0. Self-introspect (C12) and tune (C13) — read past Mender cycles
    #    and pick this cycle's parameters from the trend.
    from .self_introspect import summarize_past_cycles
    from .self_tune import tune

    introspection = summarize_past_cycles()
    params = tune(introspection)
    eval_target_count = params.eval_target_count
    min_lift = params.min_lift

    # 1. detect + cluster
    failures = get_failed_traces(
        window_minutes=window_minutes,
        project=target_project,
        max_n=params.cluster_max_failures,
    )["rows"]
    if not failures:
        return PipelineOutcome(None, "no failures in window", _elapsed(t0))
    clusters = cluster_failures(failures)
    if not clusters:
        return PipelineOutcome(None, "no clusters formed", _elapsed(t0))
    top = clusters[0]

    # 2. dedupe — bail if we have a recent open incident for this pattern
    existing = store.find_open_for_pattern(target_project, top.pattern_name)
    if existing is not None:
        last = datetime.fromisoformat(existing.updated_at)
        if datetime.now(timezone.utc) - last < DEDUPE_WINDOW:
            return PipelineOutcome(existing, "deduped against open incident", _elapsed(t0))

    # 3. open / refresh the incident record
    incident = existing or Incident(
        id=uuid.uuid4().hex[:12],
        target_project=target_project,
        cluster_pattern=top.pattern_name,
        affected_trace_ids=list(top.trace_ids),
        hypothesis={},
        baseline_eval=None,
        staged_eval=None,
        patch=None,
        state="detected",
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )
    if not existing:
        incident.transition("detected", note=f"{len(top.trace_ids)} affected traces")
    incident.introspection = introspection.to_dict()
    incident.cycle_params = params.to_dict()
    store.upsert(incident)

    # 4. hypothesize. Resolve "live" by asking FinPay's /healthz so the
    #    pipeline doesn't desynchronize when the local env disagrees
    #    with what the target server is actually serving.
    import httpx as _httpx

    try:
        r = _httpx.get(f"{finpay_url.rstrip('/')}/healthz", timeout=5.0)
        r.raise_for_status()
        live_version_tag = r.json().get("prompt_version")
    except _httpx.HTTPError:
        live_version_tag = None
    if live_version_tag:
        from finpay.prompts import load_version as _load_version

        live = _load_version(live_version_tag)
    else:
        live = live_version()
    versions = [
        PromptVersionRef(version=v.version, released_at=v.released_at, instruction=v.instruction, notes=v.notes)
        for v in list_versions()
    ]
    hyp = generate_hypothesis(top, current_prompt=live.instruction, recent_versions=versions)
    incident.hypothesis = hyp.to_dict()
    incident.transition("hypothesized", note=hyp.suspected_prompt_clause[:60])
    store.upsert(incident)

    # 4b. Bail early if hypothesis confidence is below the tuned threshold.
    if hyp.confidence < params.min_hypothesis_confidence:
        incident.transition(
            "dismissed",
            note=(
                f"hypothesis confidence {hyp.confidence:.2f} below "
                f"threshold {params.min_hypothesis_confidence:.2f}"
            ),
        )
        store.upsert(incident)
        return PipelineOutcome(incident, "low-confidence hypothesis", _elapsed(t0))

    # 5. baseline eval against live FinPay
    eval_set = generate_eval_set(hyp, sample_failures=top.sample_failures, target_count=eval_target_count)
    live_run = run_eval_set(eval_set, target=http_endpoint(finpay_url), target_label="live")
    incident.baseline_eval = live_run.to_dict()
    incident.transition("evaluating", note=f"baseline {live_run.pass_count}/{len(live_run.results)} pass")
    store.upsert(incident)

    # 6. patch + stage
    patch = generate_patch(hyp, current_prompt=live.instruction, base_version=live.version)
    incident.patch = patch.to_dict()
    apply_patch_to_staging(patch)

    # 7. eval against staged patch (in-process)
    staged_endpoint = simulated_finpay_endpoint(patch.patched_prompt, label="staged")
    staged_run = run_eval_set(eval_set, target=staged_endpoint, target_label="staged")
    incident.staged_eval = staged_run.to_dict()

    lift = staged_run.pass_rate - live_run.pass_rate
    if lift >= min_lift:
        incident.transition(
            "patch_proposed",
            note=f"+{lift:.0%} lift ({live_run.pass_count}→{staged_run.pass_count}/{len(staged_run.results)})",
        )
    else:
        incident.transition(
            "dismissed",
            note=f"insufficient lift {lift:+.0%} (need {min_lift:+.0%})",
        )
    store.upsert(incident)

    # 8. Self-eval (C11) — score the cycle and store it on the incident.
    #    Phoenix annotation write happens best-effort; if Mender's own
    #    cycle span isn't available in Phoenix yet, we still keep the
    #    score on the incident record for the web UI.
    from .self_eval import score_cycle

    self_eval = score_cycle(incident)
    incident.self_eval = self_eval.to_dict()
    store.upsert(incident)

    # 9. Slack notification (D1). Fires only on patch_proposed; the
    #    poster no-ops with a printout when SLACK_INCOMING_WEBHOOK
    #    isn't set, so this is safe in dry-run / local dev.
    if incident.state == "patch_proposed":
        try:
            from ..integrations.slack import post_incident as _post_incident

            _post_incident(incident)
        except Exception as e:
            # Notification failure shouldn't fail the pipeline; the
            # incident is still in the store and surfaced in the web UI.
            incident.history.append(
                {"at": _now_iso(), "from": incident.state, "to": incident.state,
                 "note": f"slack notify failed: {e.__class__.__name__}: {e}"}
            )
            store.upsert(incident)

    return PipelineOutcome(incident, None, _elapsed(t0))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _elapsed(t0: datetime) -> float:
    return (datetime.now(timezone.utc) - t0).total_seconds()
