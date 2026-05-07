"""Mender command-line entry point.

Subcommands:
    heartbeat   - run one full cycle (the thing Cloud Scheduler triggers).
    doctor      - check the local environment (creds, MCP, Phoenix).

The heartbeat output is also the demo's Scene 3. Format intentionally:
green for cycle/state changes, dim gray for tool calls and intermediate
reasoning, white for the final report.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

console = Console()


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M:%S")


def _parse_window(spec: str) -> int:
    """'60m' -> 60, '6h' -> 360, '24h' -> 1440 (returns minutes)."""
    m = re.fullmatch(r"\s*(\d+)\s*([mh])\s*", spec)
    if not m:
        raise argparse.ArgumentTypeError(f"bad window: {spec!r} (try '60m', '6h')")
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 60


async def _run_heartbeat(window_minutes: int, target_project: str) -> int:
    # Init telemetry first so even the agent-construction call gets traced.
    from ._telemetry import init_telemetry
    init_telemetry()

    # Importing agent.py constructs the ADK Agent + Phoenix MCP toolset.
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .agent import root_agent

    cycle_id = uuid.uuid4().hex[:8]
    started = datetime.now(timezone.utc)

    console.print(
        f"[bold green][heartbeat][/] {_fmt_time(started)}  cycle [bold]{cycle_id}[/] "
        f"started — scanning last [bold]{window_minutes}m[/] of [bold]{target_project}[/] traces"
    )
    console.print(
        f"  [dim]Cloud Run · {os.environ.get('MENDER_MODEL', 'gemini-3-flash-preview')} · Phoenix MCP[/]"
    )

    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name="mender", session_service=session_service)
    session = await session_service.create_session(app_name="mender", user_id=f"cycle-{cycle_id}")

    prompt = (
        f"This is heartbeat cycle {cycle_id}.\n"
        f"Target Phoenix project: {target_project!r}.\n"
        f"Window: the last {window_minutes} minutes (now is "
        f"{started.isoformat(timespec='seconds')}).\n\n"
        "Use Phoenix MCP to:\n"
        "  1) find traces in the window for this project\n"
        "  2) summarize their eval-score distribution\n"
        "  3) flag any cluster of low-scoring traces and what they share\n\n"
        "Reply in this exact shape:\n"
        "  [scan]    one line: trace count + eval score summary\n"
        "  [cluster] one line per cluster, or 'none'\n"
        "  [status]  one line: ok | watching | regression\n"
    )
    content = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])

    final_text = ""
    tool_calls = 0
    async for event in runner.run_async(
        user_id=f"cycle-{cycle_id}",
        session_id=session.id,
        new_message=content,
    ):
        # Surface tool activity so the demo capture has the streaming feel.
        if event.content and event.content.parts:
            for part in event.content.parts:
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_calls += 1
                    args_preview = ", ".join(f"{k}={v!r}" for k, v in (fc.args or {}).items())
                    console.print(f"  [dim]→ mcp.{fc.name}({args_preview[:80]})[/]")
                fr = getattr(part, "function_response", None)
                if fr is not None:
                    console.print(f"  [dim]← {fr.name} ok[/]")
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text = part.text

    console.print()
    console.print(final_text.strip() or "[dim](no final response)[/]")
    finished = datetime.now(timezone.utc)
    console.print(
        f"\n[bold green][heartbeat][/] {_fmt_time(finished)}  cycle [bold]{cycle_id}[/] "
        f"complete — {tool_calls} tool call{'s' if tool_calls != 1 else ''}, "
        f"{(finished - started).total_seconds():.1f}s elapsed"
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    window = _parse_window(args.window)
    return asyncio.run(_run_heartbeat(window, args.target_project))


def _check_vertex_model() -> tuple[bool, str]:
    """Probe Vertex with the configured Mender model to confirm project access."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    model = os.environ.get("MENDER_MODEL", "gemini-3-flash-preview").strip()
    if not project:
        return False, "GOOGLE_CLOUD_PROJECT not set"
    try:
        import subprocess
        token = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            check=True, capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception as e:
        return False, f"could not obtain ADC token ({e.__class__.__name__})"

    import httpx
    host = os.environ.get("VERTEX_API_HOST", "https://aiplatform.googleapis.com").rstrip("/")
    url = (
        f"{host}/v1/projects/{project}/locations/{location}/"
        f"publishers/google/models/{model}:generateContent"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 4},
    }
    try:
        r = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=15)
    except httpx.HTTPError as e:
        return False, f"network error: {e}"
    if r.status_code == 200:
        return True, f"{model} responding in {location}"
    if r.status_code == 404:
        return False, (
            f"{model} not accessible in this project. Open Vertex AI in the "
            f"Cloud Console once for project {project!r} to activate model "
            f"access, or set MENDER_MODEL to an accessible alias."
        )
    return False, f"HTTP {r.status_code}: {r.text[:120]}"


def _cmd_doctor(_: argparse.Namespace) -> int:
    issues = []
    ok = []

    def check(label: str, predicate: bool, hint: str = "", detail: str = "") -> None:
        if predicate:
            ok.append((label, detail))
        else:
            issues.append((label, hint or detail))

    check("PHOENIX_API_KEY", bool(os.environ.get("PHOENIX_API_KEY", "").strip()), "set in .env")
    check(
        "PHOENIX_COLLECTOR_ENDPOINT",
        bool(os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "").strip()),
        "set in .env (default: https://app.phoenix.arize.com)",
    )
    check("GOOGLE_CLOUD_PROJECT", bool(os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()), "set in .env")
    check(
        "Vertex AI mode",
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true",
        "GOOGLE_GENAI_USE_VERTEXAI=true",
    )

    import shutil
    check("npx on PATH", shutil.which("npx") is not None, "install Node 18+")
    check("uv on PATH", shutil.which("uv") is not None, "install uv")

    vertex_ok, vertex_msg = _check_vertex_model()
    check("Vertex Gemini model access", vertex_ok, hint=vertex_msg, detail=vertex_msg if vertex_ok else "")

    for label, detail in ok:
        suffix = f"  [dim]{detail}[/]" if detail else ""
        console.print(f"  [green]✓[/] {label}{suffix}")
    for label, hint in issues:
        console.print(f"  [red]✗[/] {label}  [dim]{hint}[/]")

    if issues:
        console.print(f"\n[red]{len(issues)} issue{'s' if len(issues) != 1 else ''}[/]")
        return 1
    console.print("\n[green]all checks passed[/]")
    return 0


def _cmd_investigate(args: argparse.Namespace) -> int:
    """Run the full incident pipeline (C3-C9) end-to-end."""
    from .pipeline.incident import run_incident_pipeline
    from .pipeline.scorer import _parse_window  # type: ignore[attr-defined]

    window = _parse_window(args.window)
    outcome = run_incident_pipeline(
        target_project=args.project,
        window_minutes=window,
        eval_target_count=args.eval_count,
        finpay_url=args.finpay_url,
    )
    if outcome.incident is None:
        console.print(f"[dim](no incident — {outcome.skipped_reason})[/]")
        return 0

    inc = outcome.incident
    base = inc.baseline_eval or {}
    staged = inc.staged_eval or {}
    base_pass = base.get("pass_count", 0)
    base_total = len(base.get("results", []))
    staged_pass = staged.get("pass_count", 0)
    staged_total = len(staged.get("results", []))

    state_color = {
        "patch_proposed": "green",
        "patch_applied": "green",
        "resolved": "green",
        "dismissed": "yellow",
    }.get(inc.state, "cyan")
    console.print()
    console.print(f"[bold]incident {inc.id}[/]  state=[bold {state_color}]{inc.state}[/]")
    console.print(f"  pattern  : {inc.cluster_pattern}")
    console.print(f"  affected : {len(inc.affected_trace_ids)} traces")
    if base_total:
        console.print(f"  baseline : {base_pass}/{base_total} pass ({100*base_pass/base_total:.0f}%)")
    if staged_total:
        console.print(f"  staged   : {staged_pass}/{staged_total} pass ({100*staged_pass/staged_total:.0f}%)")
    console.print(f"  elapsed  : {outcome.elapsed_seconds:.0f}s")
    if outcome.skipped_reason:
        console.print(f"  [dim]note: {outcome.skipped_reason}[/]")
    return 0


def _cmd_notify(args: argparse.Namespace) -> int:
    """Send (or dry-run) the Slack incident card for a stored incident."""
    from .integrations.slack import post_incident
    from .pipeline.incident import IncidentStore

    store = IncidentStore()
    incident = next((i for i in store.list_all() if i.id == args.incident_id), None)
    if incident is None:
        console.print(f"[red]incident not found: {args.incident_id}[/]")
        return 1
    if incident.state != "patch_proposed":
        console.print(
            f"[yellow]warning: incident state is {incident.state!r}, "
            f"not patch_proposed[/]"
        )
        if not args.force:
            console.print("[dim]use --force to send anyway[/]")
            return 1
        incident.state = "patch_proposed"  # local override; not persisted
    result = post_incident(incident, dry_run=args.dry_run)
    if result.get("dry_run"):
        console.print("[dim](dry run — printed payload above; set SLACK_INCOMING_WEBHOOK to send)[/]")
    else:
        console.print(f"[green]sent[/] http {result.get('status')}")
    return 0


def _cmd_score_finpay(args: argparse.Namespace) -> int:
    from .pipeline.scorer import score_window, _parse_window

    window = _parse_window(args.window)
    stats = score_window(
        project=args.project,
        window_minutes=window,
        judge_model=args.model,
        rescore=args.rescore,
    )
    console.print(
        f"\n[bold]done[/] — scanned {stats.scanned}, scored {stats.scored}, "
        f"already-scored {stats.skipped_already_scored}, "
        f"non-currency {stats.skipped_non_currency}, "
        f"errors {stats.failures} ({stats.elapsed_seconds:.1f}s)"
    )
    return 0 if stats.failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mender", description="Mender — catches the cracks. Mends them.")
    sub = p.add_subparsers(dest="cmd", required=True)

    hb = sub.add_parser("heartbeat", help="run one Mender cycle")
    hb.add_argument("--window", default=os.environ.get("MENDER_WINDOW", "60m"), help="time window (e.g. 60m, 6h)")
    hb.add_argument(
        "--target-project",
        default=os.environ.get("PHOENIX_TARGET_PROJECT", "finpay-support"),
        help="Phoenix project name to scan",
    )
    hb.set_defaults(func=_cmd_heartbeat)

    doc = sub.add_parser("doctor", help="check local environment")
    doc.set_defaults(func=_cmd_doctor)

    sc = sub.add_parser(
        "score-finpay",
        help="score FinPay traces with LLM-as-judge, write annotations to Phoenix",
    )
    sc.add_argument("--window", default=os.environ.get("MENDER_WINDOW", "60m"))
    sc.add_argument("--project", default=os.environ.get("PHOENIX_TARGET_PROJECT", "finpay-support"))
    sc.add_argument("--model", default=None, help="override judge model (defaults to MENDER_JUDGE_MODEL)")
    sc.add_argument("--rescore", action="store_true", help="re-score spans even if annotated")
    sc.set_defaults(func=_cmd_score_finpay)

    iv = sub.add_parser(
        "investigate",
        help="run the full pipeline: detect → hypothesize → eval → patch → stage → verify",
    )
    iv.add_argument("--window", default=os.environ.get("MENDER_WINDOW", "60m"))
    iv.add_argument("--project", default=os.environ.get("PHOENIX_TARGET_PROJECT", "finpay-support"))
    iv.add_argument("--eval-count", type=int, default=8, help="number of eval cases to generate")
    iv.add_argument(
        "--finpay-url",
        default=os.environ.get("FINPAY_URL", "http://127.0.0.1:8081"),
        help="FinPay HTTP endpoint for the baseline eval run",
    )
    iv.set_defaults(func=_cmd_investigate)

    nf = sub.add_parser("notify", help="post an incident's Slack card (uses webhook env, or dry-run)")
    nf.add_argument("incident_id")
    nf.add_argument("--dry-run", action="store_true", help="print payload, don't post")
    nf.add_argument("--force", action="store_true", help="send even if state isn't patch_proposed")
    nf.set_defaults(func=_cmd_notify)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
