"""Traffic generator for FinPay Support.

Drives realistic-ish support traffic against a running FinPay HTTP server
so Phoenix has traces to look at. Mix of harmless general questions and
currency-conversion questions. The currency questions are where v2's
"Always assume USD if not specified." regression shows up.

Run it as:
    uv run finpay-traffic --rate 30 --duration 5m
or:
    uv run finpay-traffic --count 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import sys
import time
from dataclasses import dataclass

import httpx
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()


@dataclass
class Query:
    text: str
    kind: str  # "general" | "currency"


# A small but varied corpus. The currency questions deliberately span:
#   - explicit ISO codes ("EUR")
#   - symbols ("£")
#   - ambiguous ("dollars")
# so v1 can answer all of them and v2 gets the ambiguous + non-USD ones wrong.
_GENERAL: tuple[str, ...] = (
    "How do I reset my FinPay password?",
    "What's the daily payout limit on a Standard account?",
    "Can I have two FinPay accounts on the same email?",
    "How long does KYC verification usually take?",
    "Are there fees for receiving payments from another FinPay user?",
    "How do I update my linked bank account?",
    "What countries does FinPay support for payouts?",
    "Why was my last transfer flagged for review?",
    "Is there a business tier with higher limits?",
    "Do you have an API for automating invoices?",
)

# Currency questions split into two pools:
#   EXPLICIT  — both source AND target currency stated unambiguously.
#               v1, v2, and v3 should all handle these correctly.
#   AMBIGUOUS — at least one side missing or under-specified ("$50",
#               "Convert 50 to JPY", "send 100 to Mexico"). v1 and v3
#               ask for clarification; v2 silently defaults to USD.
_CURRENCY_EXPLICIT: tuple[str, ...] = (
    "Convert 100 EUR to JPY please.",
    "How much is £250 in USD right now?",
    "Can you tell me 9000 INR in GBP?",
    "If I receive 200 CAD, how much is that in CHF?",
    "How many AUD is 700 SGD?",
    "I want to pay 3000 BRL — show me the USD equivalent.",
    "What's €1,200 in GBP?",
    "Convert 88 KRW to USD.",
    "How much is 450 CHF in MXN?",
    "Convert ¥85000 JPY to AUD please.",
    "What's 5000 ZAR in EUR?",
    "Show me 60 GBP in CNY.",
    "If I have 1200 NZD, how much is that in USD?",
    "Convert 25 EUR to TRY.",
    "What's 2000 SEK in HKD?",
    "How many DKK is 175 USD?",
    "Convert 320 NOK to JPY.",
    "What's $40 USD in INR?",
)

_CURRENCY_AMBIGUOUS: tuple[str, ...] = (
    "I need to send 1500 to a vendor in Mexico — what's that in MXN?",
    "What's 500 dollars in euros?",
    "Convert 50 to JPY.",
    "75 in CNY, what's that in EUR?",
    "I have 200 — how much is that in CHF?",
    "Convert 1000 to AUD please.",
    "How much is 4000 in pounds?",
    "I'd like to send 350 to my brother in Japan, can you convert?",
    "Convert 80 to BRL.",
    "What's 600 in INR these days?",
    "I want to pay 2500 — what would that be in CAD?",
    "Show me 90 in NOK.",
    "Send 800 to my contractor in Mumbai — what's that in INR?",
    "I'm tipping 30 in Singapore, that's how much SGD?",
    "How much is 120 in francs?",
    "Convert 6500 to KRW.",
    "I want to wire 12000 to a supplier in Shanghai — CNY please.",
    "What's 280 in dollars worth in pesos?",
    "Send 95 to my friend in Bangkok — how many baht?",
    "How much is 1750 in zloty?",
    "Convert 4200 to TRY.",
    "What's 18 in shekels?",
    "I owe 540 to a vendor in Mexico City, MXN equivalent?",
    "Show me 65 in CAD.",
)

_CURRENCY: tuple[str, ...] = _CURRENCY_EXPLICIT + _CURRENCY_AMBIGUOUS


_QUERIES: tuple[Query, ...] = tuple(
    [Query(t, "general") for t in _GENERAL] + [Query(t, "currency") for t in _CURRENCY]
)


# Without-replacement queues, keyed by (kind, mode). Refilled+reshuffled
# when exhausted so a long traffic run cycles through the full pool
# before any prompt repeats. Avoids the "same input over and over" look
# in Phoenix's trace list during demo capture.
_QUEUES: dict[tuple[str, str], list[str]] = {}


def _next_from(kind: str, mode: str, pool: tuple[str, ...]) -> str:
    key = (kind, mode)
    q = _QUEUES.get(key)
    if not q:
        q = list(pool)
        random.shuffle(q)
        _QUEUES[key] = q
    return q.pop()


def _pick_query(currency_bias: float, question_mode: str = "mixed") -> Query:
    """Sample one query from the pool, without replacement within a
    sweep through the pool.

    Args:
        currency_bias: probability of picking a currency question (vs general).
        question_mode: which currency sub-pool to draw from when picking
            a currency question. "mixed" samples from the combined
            currency pool (default). "ambiguous-only" forces the bug-
            triggering subset; "explicit-only" forces the always-passing
            subset. The general pool is unaffected.
    """
    if random.random() < currency_bias:
        if question_mode == "ambiguous-only":
            return Query(_next_from("currency", "ambiguous-only", _CURRENCY_AMBIGUOUS), "currency")
        if question_mode == "explicit-only":
            return Query(_next_from("currency", "explicit-only", _CURRENCY_EXPLICIT), "currency")
        return Query(_next_from("currency", "mixed", _CURRENCY), "currency")
    return Query(_next_from("general", "any", _GENERAL), "general")


def _parse_duration(spec: str) -> float:
    """'30s' -> 30.0, '5m' -> 300.0, '2h' -> 7200.0."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh])\s*", spec)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {spec!r} (try '30s', '5m', '2h')")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


async def _send(client: httpx.AsyncClient, url: str, query: Query) -> tuple[Query, int, dict | None, str]:
    """Send one query. Returns (query, status, full_response_dict, error_or_reply)."""
    try:
        r = await client.post(url, json={"message": query.text}, timeout=60.0)
        r.raise_for_status()
        body = r.json()
        return query, r.status_code, body, body.get("reply", "")[:120]
    except httpx.HTTPError as e:
        return query, getattr(e, "response", None) and e.response.status_code or 0, None, str(e)[:120]


def _inline_score(
    *, trace_id: str, span_id: str, user_input: str, agent_output: str
) -> tuple[str, float | None] | None:
    """Score one trace inline. Lazy-imports so traffic without
    --inline-score doesn't pay for the mender package.

    Returns (label, score) or None on any failure (which we log and
    continue — never block traffic on judge errors).
    """
    try:
        from mender.pipeline.scorer import score_inline as _score
    except ImportError as e:
        console.print(f"  [red]inline-score import failed:[/] {e}")
        return None
    try:
        result = _score(
            trace_id=trace_id,
            span_id=span_id,
            user_input=user_input,
            agent_output=agent_output,
        )
        return result.label, result.score
    except Exception as e:
        console.print(f"  [yellow]inline-score error:[/] {e}")
        return None


async def run_async(args: argparse.Namespace) -> None:
    base_url = args.target.rstrip("/")
    chat_url = f"{base_url}/chat"

    console.print(
        f"[bold cyan]traffic[/]  target={chat_url}  "
        f"currency_bias={args.currency_bias}  mode={args.question_mode}  "
        f"inline_score={args.inline_score}  "
        f"{'count='+str(args.count) if args.count else 'rate='+str(args.rate)+'/min duration='+args.duration}"
    )

    deadline = None if args.count else time.monotonic() + _parse_duration(args.duration)
    interval = 60.0 / args.rate if args.rate else 0.0
    sent = 0

    async with httpx.AsyncClient() as client:
        while True:
            if args.count and sent >= args.count:
                break
            if deadline and time.monotonic() >= deadline:
                break

            query = _pick_query(args.currency_bias, args.question_mode)
            t0 = time.monotonic()
            q, status, body, reply = await _send(client, chat_url, query)
            dt = time.monotonic() - t0
            sent += 1

            tag = "[green]ok[/]" if status == 200 else "[red]err[/]"
            console.print(
                f"  {tag} {q.kind:8} {dt*1000:5.0f}ms  "
                f"[dim]{q.text[:60]}[/]  -> [dim]{reply}[/]"
            )

            # Inline scoring: judge the just-completed turn so the chip
            # lands in Phoenix within ~2-3s of the trace itself.
            if args.inline_score and status == 200 and body:
                trace_id = body.get("trace_id", "")
                span_id = body.get("span_id", "")
                if trace_id and span_id:
                    score_t0 = time.monotonic()
                    res = _inline_score(
                        trace_id=trace_id,
                        span_id=span_id,
                        user_input=q.text,
                        agent_output=body.get("reply", ""),
                    )
                    score_dt = time.monotonic() - score_t0
                    if res is not None:
                        label, score = res
                        chip = {
                            "pass": "[bold green]pass[/]",
                            "fail": "[bold red]fail[/]",
                            "partial": "[yellow]part[/]",
                            "n_a": "[dim] n/a[/]",
                        }.get(label, label)
                        score_str = f"μ {score:.2f}" if score is not None else "μ  -"
                        console.print(
                            f"     [dim]→ chip:[/] {chip} {score_str}  "
                            f"[dim]({score_dt*1000:.0f}ms)[/]"
                        )
                else:
                    console.print(
                        "     [yellow]→ chip skipped:[/] /chat response missing trace_id/span_id"
                    )

            if interval and (not args.count or sent < args.count):
                await asyncio.sleep(max(0, interval - dt))

    console.print(f"[bold]done[/] — sent {sent} queries")


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive FinPay Support with mixed traffic.")
    parser.add_argument(
        "--target",
        default=f"http://{os.environ.get('FINPAY_HOST', '127.0.0.1')}:{os.environ.get('FINPAY_PORT', '8081')}",
        help="FinPay base URL",
    )
    parser.add_argument("--rate", type=float, default=30.0, help="queries per minute")
    parser.add_argument("--duration", default="2m", help="how long to run (e.g. 30s, 5m, 1h)")
    parser.add_argument("--count", type=int, default=None, help="exact number of queries (overrides duration)")
    parser.add_argument(
        "--currency-bias",
        type=float,
        default=0.6,
        help="fraction of queries that should be currency-conversion (0-1)",
    )
    parser.add_argument(
        "--question-mode",
        choices=["mixed", "ambiguous-only", "explicit-only"],
        default="mixed",
        help=(
            "how to sample currency questions: 'mixed' uses the full pool "
            "(default); 'ambiguous-only' restricts to bug-triggering prompts "
            "(no source currency stated); 'explicit-only' restricts to "
            "always-passing prompts (both currencies stated)."
        ),
    )
    parser.add_argument(
        "--inline-score",
        action="store_true",
        help=(
            "after each /chat call, immediately score the trace via "
            "Gemini-as-judge and write the annotation to Phoenix. Adds "
            "1–3s per turn but makes the trace-list chip populate live. "
            "Default off; stage-demo flips it on."
        ),
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_async(args))
    except KeyboardInterrupt:
        sys.exit(130)
