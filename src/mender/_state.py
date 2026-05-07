"""Firestore-backed shared state for cross-service coordination.

Both Mender and FinPay run as separate Cloud Run services with their
own filesystems. To make the prompt swap visible to FinPay's live
traffic, the live-version pointer has to live in shared storage.

This module exposes two helpers used at the boundary:

  get_live_prompt_version(target)
      FinPay reads this on each request to decide which prompt to serve.
      Falls back to FINPAY_PROMPT_VERSION env if Firestore is disabled
      or unreachable, so local dev still works without a Firestore.

  set_live_prompt_version(target, version, ...)
      Mender's promote_to_live writes here after the user clicks Approve.

A single Firestore document per target — `mender-state/<target>-live` —
holds the canonical answer. All operations are best-effort with quiet
fallback to env so a Firestore outage degrades to "stale prompt" rather
than "FinPay won't serve."
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

_log = logging.getLogger(__name__)

# Set MENDER_USE_FIRESTORE_STATE=true (Cloud Run deploy does this) to
# enable. Local dev defaults to env-var only, no GCP creds required.
def _enabled() -> bool:
    return os.environ.get("MENDER_USE_FIRESTORE_STATE", "").strip().lower() in {"1", "true", "yes"}


def _doc_id(target: str) -> str:
    return f"{target}-live"


@lru_cache(maxsize=1)
def _client():
    from google.cloud import firestore

    return firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))


def get_live_prompt_version(
    target: str,
    *,
    env_fallback: str | None = None,
) -> str:
    """Read the live prompt version. Firestore first, then env fallback.

    Args:
        target: e.g. "finpay-support".
        env_fallback: env var name to fall back to. Defaults to
            FINPAY_PROMPT_VERSION for the canonical FinPay target.
    """
    fallback_env = env_fallback or "FINPAY_PROMPT_VERSION"
    fallback = os.environ.get(fallback_env, "v1")

    if not _enabled():
        return fallback

    try:
        doc = _client().collection("mender-state").document(_doc_id(target)).get()
        if doc.exists:
            data = doc.to_dict() or {}
            v = data.get("version")
            if v:
                return str(v)
    except Exception as e:
        _log.warning("Firestore read for %s failed: %s; using env fallback", target, e)

    return fallback


def get_live_prompt(target: str) -> dict[str, Any] | None:
    """Read the full live prompt record (version + instruction body).

    Returns None if Firestore is disabled or no doc exists. The
    `instruction` field carries the full system prompt — needed for
    promoted versions (v3+) whose YAML doesn't ship in FinPay's image.
    """
    if not _enabled():
        return None
    try:
        doc = _client().collection("mender-state").document(_doc_id(target)).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        _log.warning("Firestore read for %s failed: %s", target, e)
    return None


def set_live_prompt_version(
    target: str,
    version: str,
    *,
    instruction: str | None = None,
    actor: str = "mender",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record the new live version + (optional) full instruction body.

    Pass `instruction` whenever the promoted version isn't a baseline
    YAML that ships with FinPay's image. Without it, FinPay would only
    know the tag and would fall back to its bundled YAMLs.

    No-op if Firestore is disabled.
    """
    if not _enabled():
        _log.info("Firestore state disabled; not recording %s -> %s", target, version)
        return

    payload: dict[str, Any] = {
        "version": version,
        "target": target,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
    }
    if instruction is not None:
        payload["instruction"] = instruction
    if metadata:
        payload["metadata"] = metadata
    try:
        _client().collection("mender-state").document(_doc_id(target)).set(payload)
        _log.info("Firestore: %s live version -> %s", target, version)
    except Exception as e:
        _log.error("Firestore write for %s -> %s failed: %s", target, version, e)
        raise
