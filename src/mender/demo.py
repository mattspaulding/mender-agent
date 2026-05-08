"""Demo staging — component G1.

Resets local state, drives traffic against FinPay v1, switches to the
regressed v2 prompt at a precise moment, and runs the eval scorer so
Phoenix has annotated traces ready for the recording.

Use: `mender stage-demo --phase1 5m --phase2 3m`. Idempotent — safe to
re-run.

Assumes the local toolchain (uv, the package installed in editable mode,
network access to Phoenix Cloud + Vertex). Does NOT touch Cloud Run
deployments — for prod-shaped staging you'd run a similar script
remotely against the deployed services.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx
from rich.console import Console

console = Console()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INCIDENTS_PATH = _REPO_ROOT / ".mender" / "incidents.json"
_STAGING_DIR = _REPO_ROOT / "prompts" / "finpay" / "staging"
_LIVE_POINTER = _REPO_ROOT / "prompts" / "finpay" / ".live"


def _parse_duration(spec: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([sm])\s*", spec)
    if not m:
        raise ValueError(f"bad duration: {spec!r} (try '30s' or '5m')")
    n = int(m.group(1))
    return n if m.group(2) == "s" else n * 60


def reset_state() -> None:
    """Wipe incidents store, staging prompts, .live pointer."""
    if _INCIDENTS_PATH.exists():
        _INCIDENTS_PATH.unlink()
    if _STAGING_DIR.exists():
        shutil.rmtree(_STAGING_DIR, ignore_errors=True)
    if _LIVE_POINTER.exists():
        _LIVE_POINTER.unlink()
    # Also remove any v3+.yaml that an earlier promote may have created.
    finpay_dir = _REPO_ROOT / "prompts" / "finpay"
    for stray in finpay_dir.glob("v*.yaml"):
        if stray.stem in {"v1", "v2"}:
            continue
        stray.unlink()
    console.print("[dim]reset: incidents, staging, .live, generated v3+.yaml[/]")


def stop_finpay() -> None:
    """Kill any running finpay-serve process."""
    subprocess.run(["pkill", "-9", "-f", "finpay-serve"], check=False)
    subprocess.run(["pkill", "-9", "-f", "finpay.server"], check=False)
    time.sleep(1)


def start_finpay(version: str, *, port: int = 8081, log_path: Path = Path("/tmp/finpay.log")) -> subprocess.Popen:
    """Start finpay-serve with the given prompt version. Returns the Popen handle."""
    env = os.environ.copy()
    env["FINPAY_PROMPT_VERSION"] = version
    env["FINPAY_PORT"] = str(port)
    log_path.parent.mkdir(exist_ok=True)
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        ["uv", "run", "finpay-serve"],
        cwd=_REPO_ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Wait for /health.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
            if r.status_code == 200:
                got = r.json().get("prompt_version")
                console.print(f"  finpay up @ :{port} on prompt [bold]{got}[/]")
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"finpay didn't come up on :{port} within 30s")


def drive_traffic(
    *,
    count: int,
    currency_bias: float = 0.7,
    question_mode: str = "mixed",
    inline_score: bool = False,
) -> None:
    """Run finpay-traffic for `count` queries. Inherits env (incl. PHOENIX_API_KEY)."""
    cmd = [
        "uv", "run", "finpay-traffic",
        "--count", str(count),
        "--currency-bias", str(currency_bias),
        "--question-mode", question_mode,
    ]
    if inline_score:
        cmd.append("--inline-score")
    proc = subprocess.run(cmd, cwd=_REPO_ROOT, check=False)
    if proc.returncode != 0:
        console.print(f"[yellow]traffic returned non-zero: {proc.returncode}[/]")


def score_traffic(window_minutes: int) -> None:
    """Run mender score-finpay over the recent traffic."""
    cmd = ["uv", "run", "mender", "score-finpay", "--window", f"{window_minutes}m"]
    subprocess.run(cmd, cwd=_REPO_ROOT, check=False)


def stage_demo(
    *,
    phase1: str = "5m",
    phase2: str = "3m",
    queries_per_minute: float = 12.0,
    currency_bias: float = 1.0,
    inline_score: bool = True,
) -> None:
    """End-to-end staging.

    Phase 1: FinPay on v1, drive traffic, traces score green.
    "Regression event": flip to v2.
    Phase 2: FinPay on v2, drive traffic, traces score red.
    Then score everything so Phoenix has annotated data.

    Default currency_bias=1.0 — every staged turn is a currency
    question, so every Phoenix trace gets a real pass/fail score
    (no n_a noise in the trace-list view). Override for mixed-corpus
    runs when you want general support questions in the dataset.
    """
    phase1_secs = _parse_duration(phase1)
    phase2_secs = _parse_duration(phase2)

    p1_count = max(4, int(phase1_secs / 60 * queries_per_minute))
    p2_count = max(4, int(phase2_secs / 60 * queries_per_minute))

    console.print(
        f"[bold]stage-demo[/]  phase1=[bold]{phase1}[/] ({p1_count} queries) → "
        f"regression → phase2=[bold]{phase2}[/] ({p2_count} queries)  "
        f"currency_bias=[bold]{currency_bias}[/]"
    )
    console.print()

    reset_state()
    stop_finpay()

    proc = None
    try:
        # Phase 1 — good prompt
        console.print("[bold green]phase 1 — v1 (well-behaved)[/]")
        proc = start_finpay("v1")
        drive_traffic(count=p1_count, currency_bias=currency_bias, inline_score=inline_score)
        stop_finpay()
        proc = None

        # Regression event — switch to v2
        console.print()
        console.print("[bold red]regression event — bumping to v2[/]")
        proc = start_finpay("v2")

        # Phase 2 — bad prompt
        console.print("[bold red]phase 2 — v2 (regressed)[/]")
        drive_traffic(count=p2_count, currency_bias=currency_bias, inline_score=inline_score)
    finally:
        stop_finpay()

    # Score everything
    console.print()
    console.print("[bold]scoring traffic with LLM-as-judge[/]")
    total_window = max(15, (phase1_secs + phase2_secs) // 60 + 5)
    score_traffic(window_minutes=total_window)

    console.print()
    console.print(f"[bold green]✓ demo staged[/]  Phoenix has {p1_count + p2_count} annotated traces")
    console.print()
    console.print("Next: start FinPay on v2 and run an investigate cycle:")
    console.print("  [dim]FINPAY_PROMPT_VERSION=v2 uv run finpay-serve &[/]")
    console.print("  [dim]uv run mender investigate --window 30m[/]")
    console.print("  [dim]uv run mender-web    # open http://127.0.0.1:8082[/]")


# --- 3-act capture flow (Phase A green / Phase B red / Phase C green) ---


_V3_FALLBACK_INSTRUCTION = """\
You are FinPay Support. Be concise and direct. Reply in one or two sentences.

Capabilities:
  - Answer general questions about the app.
  - Convert amounts between currencies using `get_exchange_rate`.

Currency rules:
  - If the user's question is ambiguous about currency (they mention an
    amount but not the source currency, or only mention one side of a
    conversion), ask one brief clarifying question before invoking
    `get_exchange_rate`. Never silently assume USD or any default.
  - If both source and target currencies are stated clearly, perform the
    conversion using `get_exchange_rate`.
"""


def _write_v3_fallback() -> Path:
    """Hand-rolled v3.yaml as a safety net when investigate doesn't auto-promote."""
    import yaml

    v3_path = _REPO_ROOT / "prompts" / "finpay" / "v3.yaml"
    v3_path.parent.mkdir(parents=True, exist_ok=True)
    v3_path.write_text(
        yaml.safe_dump(
            {
                "name": "finpay-support",
                "version": "v3",
                "released_at": "2026-05-08T00:00:00Z",
                "notes": "Hand-rolled fallback. Restores v1's clarification rule, removes v2's USD-default clause.",
                "instruction": _V3_FALLBACK_INSTRUCTION,
            },
            sort_keys=False,
        )
    )
    return v3_path


def _generate_v3_via_investigate(*, finpay_url: str = "http://127.0.0.1:8081") -> tuple[str, dict | None]:
    """Run mender investigate against the live FinPay (currently v2 with
    fresh ambiguous-only fails) and auto-approve the resulting patch.

    Returns:
        ("auto", patch_dict)  if the cycle produced patch_proposed and
                              we promoted it to live.
        ("fallback", None)    if the cycle didn't produce a patch
                              (dismissed, low confidence, no failures).
                              Caller should fall back to _write_v3_fallback.
    """
    from mender.pipeline.incident import run_incident_pipeline
    from mender.pipeline.patch_gen import Patch
    from mender.pipeline.staging import promote_to_live

    console.print("[bold]running mender investigate (auto-approve mode)[/]")
    outcome = run_incident_pipeline(
        target_project="finpay-support",
        window_minutes=15,
        eval_target_count=8,
        finpay_url=finpay_url,
    )
    incident = outcome.incident
    if incident is None:
        console.print(f"[yellow]investigate skipped: {outcome.skipped_reason}[/]")
        return "fallback", None
    console.print(
        f"  state=[bold]{incident.state}[/]  elapsed={outcome.elapsed_seconds:.0f}s"
    )
    if incident.state != "patch_proposed" or not incident.patch:
        return "fallback", None

    patch = Patch(**incident.patch)
    promote_to_live(patch)
    console.print(f"  [green]✓[/] auto-promoted [bold]{patch.base_version}->{patch.new_version}[/]: {patch.summary}")
    return "auto", incident.patch


def stage_full_arc(
    *,
    phase_a: str = "5m",
    phase_b: str = "3m",
    phase_c: str = "3m",
    queries_per_minute: float = 6.0,
    inline_score: bool = True,
) -> None:
    """3-act demo arc: green -> red -> green, dramatizing detect-and-fix.

    Phase A: v1 + mixed currency traffic. Should score 100% pass.
    Phase B: v2 + ambiguous-only traffic. Should score 100% fail (every
        question hits the regression).
    Patch step: run `mender investigate` against the v2 fails, auto-
        promote the resulting patch to v3 (writes Firestore + local
        prompts/finpay/v3.yaml). Falls back to a hand-rolled v3 if
        investigate doesn't materialize a patch.
    Phase C: v3 + same ambiguous-only traffic. Should score 100% pass
        because v3 asks for clarification instead of defaulting.
    """
    a_secs = _parse_duration(phase_a)
    b_secs = _parse_duration(phase_b)
    c_secs = _parse_duration(phase_c)

    a_count = max(8, int(a_secs / 60 * queries_per_minute))
    b_count = max(8, int(b_secs / 60 * queries_per_minute))
    c_count = max(8, int(c_secs / 60 * queries_per_minute))

    console.print(
        f"[bold]stage-demo --full-arc[/]  "
        f"phaseA=[bold green]{phase_a}[/]({a_count}q v1 mixed) -> "
        f"phaseB=[bold red]{phase_b}[/]({b_count}q v2 ambiguous) -> "
        f"patch -> phaseC=[bold green]{phase_c}[/]({c_count}q v3 ambiguous)"
    )
    console.print()

    reset_state()
    stop_finpay()

    proc = None
    try:
        # Phase A — v1, mixed (should be all pass).
        console.print("[bold green]phase A — v1 (well-behaved), mixed traffic[/]")
        proc = start_finpay("v1")
        drive_traffic(count=a_count, currency_bias=1.0, question_mode="mixed", inline_score=inline_score)
        stop_finpay()
        proc = None

        # Phase B — v2, ambiguous-only (every prompt triggers the regression).
        console.print()
        console.print("[bold red]phase B — v2 (regressed), ambiguous-only traffic[/]")
        proc = start_finpay("v2")
        drive_traffic(count=b_count, currency_bias=1.0, question_mode="ambiguous-only", inline_score=inline_score)

        # Score Phase B before investigate so the orchestrator's
        # get_failed_traces() sees real fails.
        console.print()
        console.print("[bold]scoring phase B traces (so investigate has labeled fails)[/]")
        score_traffic(window_minutes=10)

        # Patch step — keep FinPay on v2 running so the eval runner can hit it.
        console.print()
        path, _patch = _generate_v3_via_investigate(finpay_url="http://127.0.0.1:8081")
        if path == "fallback":
            console.print("[yellow]investigate didn't propose a patch; falling back to hand-rolled v3[/]")
            v3_path = _write_v3_fallback()
            console.print(f"  wrote [dim]{v3_path}[/]")

        stop_finpay()
        proc = None

        # Phase C — v3, same ambiguous-only traffic (should now all pass).
        console.print()
        console.print("[bold green]phase C — v3 (patched), ambiguous-only traffic[/]")
        proc = start_finpay("v3")
        drive_traffic(count=c_count, currency_bias=1.0, question_mode="ambiguous-only", inline_score=inline_score)
    finally:
        stop_finpay()

    # Final score over the whole arc.
    console.print()
    console.print("[bold]scoring full arc with LLM-as-judge[/]")
    total_window = max(20, (a_secs + b_secs + c_secs) // 60 + 15)
    score_traffic(window_minutes=total_window)

    console.print()
    console.print(
        f"[bold green]✓ 3-act arc staged[/]  "
        f"Phoenix has ~{a_count + b_count + c_count} annotated traces"
    )
    console.print()
    console.print("Open Phoenix -> finpay-support -> Traces -> Last 1 Hour")
    console.print("Newest at top: green (phase C) -> red (phase B) -> green (phase A) at bottom.")
