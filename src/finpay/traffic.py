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

_CURRENCY: tuple[str, ...] = (
    "Convert 100 EUR to JPY please.",
    "How much is £250 in USD right now?",
    "I need to send 1500 to a vendor in Mexico — what's that in MXN?",
    "What's 500 dollars in euros?",  # ambiguous "dollars"
    "Can you tell me 9000 INR in GBP?",
    "If I receive 200 CAD, how much is that in CHF?",
    "Convert 50 to JPY.",  # ambiguous bare amount
    "How many AUD is 700 SGD?",
    "75 in CNY, what's that in EUR?",  # ambiguous bare amount
    "I want to pay 3000 BRL — show me the USD equivalent.",
    "What's €1,200 in GBP?",
    "Convert 88 KRW to USD.",
)

_QUERIES: tuple[Query, ...] = tuple(
    [Query(t, "general") for t in _GENERAL] + [Query(t, "currency") for t in _CURRENCY]
)


def _pick_query(currency_bias: float) -> Query:
    if random.random() < currency_bias:
        return random.choice([q for q in _QUERIES if q.kind == "currency"])
    return random.choice([q for q in _QUERIES if q.kind == "general"])


def _parse_duration(spec: str) -> float:
    """'30s' -> 30.0, '5m' -> 300.0, '2h' -> 7200.0."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh])\s*", spec)
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration: {spec!r} (try '30s', '5m', '2h')")
    value, unit = float(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


async def _send(client: httpx.AsyncClient, url: str, query: Query) -> tuple[Query, int, str]:
    try:
        r = await client.post(url, json={"message": query.text}, timeout=60.0)
        r.raise_for_status()
        return query, r.status_code, r.json().get("reply", "")[:120]
    except httpx.HTTPError as e:
        return query, getattr(e, "response", None) and e.response.status_code or 0, str(e)[:120]


async def run_async(args: argparse.Namespace) -> None:
    base_url = args.target.rstrip("/")
    chat_url = f"{base_url}/chat"

    console.print(
        f"[bold cyan]traffic[/]  target={chat_url}  "
        f"currency_bias={args.currency_bias}  "
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

            query = _pick_query(args.currency_bias)
            t0 = time.monotonic()
            q, status, reply = await _send(client, chat_url, query)
            dt = time.monotonic() - t0
            sent += 1

            tag = "[green]ok[/]" if status == 200 else "[red]err[/]"
            console.print(
                f"  {tag} {q.kind:8} {dt*1000:5.0f}ms  "
                f"[dim]{q.text[:60]}[/]  -> [dim]{reply}[/]"
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
    args = parser.parse_args()
    try:
        asyncio.run(run_async(args))
    except KeyboardInterrupt:
        sys.exit(130)
