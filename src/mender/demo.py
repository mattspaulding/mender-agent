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


def drive_traffic(*, count: int, currency_bias: float = 0.7) -> None:
    """Run finpay-traffic for `count` queries. Inherits env (incl. PHOENIX_API_KEY)."""
    cmd = [
        "uv", "run", "finpay-traffic",
        "--count", str(count),
        "--currency-bias", str(currency_bias),
    ]
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
        drive_traffic(count=p1_count, currency_bias=currency_bias)
        stop_finpay()
        proc = None

        # Regression event — switch to v2
        console.print()
        console.print("[bold red]regression event — bumping to v2[/]")
        proc = start_finpay("v2")

        # Phase 2 — bad prompt
        console.print("[bold red]phase 2 — v2 (regressed)[/]")
        drive_traffic(count=p2_count, currency_bias=currency_bias)
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
