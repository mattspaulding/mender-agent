"""Slack incident formatter + webhook poster (component D1).

Renders an Incident as a Block Kit message with:
  - severity emoji + window + affected trace count
  - cluster pattern + likely cause
  - eval delta (baseline → staged with +/- lift)
  - prompt diff in a code block
  - Approve / Discard interactive buttons (handled by web/app.py D2)

Posts via the webhook URL in SLACK_INCOMING_WEBHOOK. Returns the
Slack response, or raises if the webhook is misconfigured.

Falls back to a dry-run that prints the rendered JSON to stdout when
SLACK_INCOMING_WEBHOOK isn't set, so D1 is testable end-to-end without
Slack credentials.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ..pipeline.incident import Incident

_log = logging.getLogger(__name__)

# Slack imposes a 3000-char limit on individual block text fields. Diffs
# can run over for big patches; we trim with a clear marker.
_DIFF_MAX = 2800


def post_incident(
    incident: Incident,
    *,
    webhook_url: str | None = None,
    web_base_url: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Post an incident card to Slack. Returns the response JSON.

    Args:
        incident: state must be patch_proposed. Anything else raises.
        webhook_url: override SLACK_INCOMING_WEBHOOK env.
        web_base_url: where the Approve button POSTs back to. Defaults
            to MENDER_WEB_PUBLIC_URL or omits the buttons if unset.
        dry_run: if True, render the payload but don't POST. Defaults
            to True when the webhook URL is missing.
    """
    if incident.state != "patch_proposed":
        raise ValueError(
            f"refusing to send Slack card for incident in state {incident.state!r}; "
            "only patch_proposed cards are sent (others would be confusing)"
        )

    webhook_url = webhook_url or os.environ.get("SLACK_INCOMING_WEBHOOK", "").strip()
    web_base_url = web_base_url or os.environ.get("MENDER_WEB_PUBLIC_URL", "").strip()
    dry_run = dry_run if dry_run is not None else not webhook_url

    payload = build_block_kit(incident, web_base_url=web_base_url)

    if dry_run:
        print(json.dumps(payload, indent=2))
        return {"ok": True, "dry_run": True}

    r = httpx.post(webhook_url, json=payload, timeout=15.0)
    r.raise_for_status()
    body = r.text or "ok"
    return {"ok": True, "status": r.status_code, "body": body}


def build_block_kit(
    incident: Incident,
    *,
    web_base_url: str = "",
) -> dict[str, Any]:
    """Build the Block Kit payload — pure function, easy to snapshot-test."""
    base = incident.baseline_eval or {}
    staged = incident.staged_eval or {}
    base_pass = base.get("pass_count", 0)
    base_total = len(base.get("results", [])) or 0
    staged_pass = staged.get("pass_count", 0)
    staged_total = len(staged.get("results", [])) or 0
    lift = staged.get("pass_rate", 0) - base.get("pass_rate", 0)
    lift_emoji = "📈" if lift > 0 else ("📊" if lift == 0 else "📉")

    severity_emoji = "🔴" if base_total and base.get("pass_rate", 0) < 0.5 else "🟠"

    hyp = incident.hypothesis or {}
    patch = incident.patch or {}
    diff = patch.get("unified_diff", "")
    if len(diff) > _DIFF_MAX:
        diff = diff[: _DIFF_MAX - 50] + "\n...\n[diff truncated — see web UI]"

    affected = len(incident.affected_trace_ids)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{severity_emoji} Mender — quality regression in {incident.target_project}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Pattern*\n{incident.cluster_pattern}"},
                {"type": "mrkdwn", "text": f"*Affected traces*\n{affected}"},
                {"type": "mrkdwn", "text": f"*Likely cause*\n{hyp.get('root_cause', '(unknown)')}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Suspected clause*\n`{hyp.get('suspected_prompt_clause') or '(none)'}`",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{lift_emoji} *Verified fix* — eval delta\n"
                    f"`{base_pass}/{base_total}` pass on `{patch.get('base_version', '?')}` "
                    f"→ `{staged_pass}/{staged_total}` pass on `{patch.get('new_version', '?')}` "
                    f"({lift:+.0%})"
                ),
            },
        },
    ]

    if diff:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Proposed patch*\n```\n{diff}\n```"},
        })

    if web_base_url:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"<{web_base_url}/incidents/{incident.id}|View full incident in Mender>",
                }
            ],
        })

    # Action buttons. Slack requires `action_id`s; D2 reads them.
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "✅  Apply patch"},
                "action_id": "approve_patch",
                "value": incident.id,
            },
            {
                "type": "button",
                "style": "danger",
                "text": {"type": "plain_text", "text": "Discard"},
                "action_id": "discard_patch",
                "value": incident.id,
            },
        ],
    })

    return {
        "text": (
            f"Mender: regression in {incident.target_project} "
            f"({base_pass}/{base_total} → {staged_pass}/{staged_total}, {lift:+.0%})"
        ),
        "blocks": blocks,
    }


def post_confirmation(
    incident: Incident,
    *,
    action: str,  # "applied" | "discarded"
    webhook_url: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Post a follow-up confirmation message after the user approves/dismisses."""
    webhook_url = webhook_url or os.environ.get("SLACK_INCOMING_WEBHOOK", "").strip()
    dry_run = dry_run if dry_run is not None else not webhook_url

    from .. import mascot

    if action == "applied":
        text = (
            f"✅ Patch applied to *{incident.target_project}*: "
            f"`{(incident.patch or {}).get('base_version','?')}` → "
            f"`{(incident.patch or {}).get('new_version','?')}`."
        )
    elif action == "discarded":
        text = f"🗑 Patch discarded for *{incident.target_project}*."
    else:
        raise ValueError(f"unknown action {action!r}")

    mascot_block_text = mascot.slack_block_for_action(action)

    payload = {
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": mascot_block_text}},
        ],
    }

    if dry_run:
        print(json.dumps(payload, indent=2))
        return {"ok": True, "dry_run": True}

    r = httpx.post(webhook_url, json=payload, timeout=15.0)
    r.raise_for_status()
    return {"ok": True, "status": r.status_code}


def verify_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str | None = None,
    max_age_seconds: int = 300,
) -> bool:
    """Verify a Slack interactive callback's HMAC signature.

    Returns True iff the signature is valid AND the timestamp is within
    `max_age_seconds` of now (Slack's default replay-protection window).
    """
    import hashlib
    import hmac
    import time

    signing_secret = signing_secret or os.environ.get("SLACK_SIGNING_SECRET", "").strip()
    if not signing_secret:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > max_age_seconds:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = (
        "v0="
        + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature or "")
