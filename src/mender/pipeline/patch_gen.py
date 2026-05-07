"""Patch generation — component C8.

Given a Hypothesis and the current target prompt, ask Gemini to produce
the smallest plausible patched prompt that resolves the hypothesized
failure mode. Then compute a unified diff for Slack's code block and
return everything as a structured Patch record.

The atomic-swap version-bump (writing the patched prompt to a new
version YAML) lives in `staging.py` (C9). This module is pure: prompt
in, prompt + diff out.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import asdict, dataclass

from .hypothesize import Hypothesis


@dataclass
class Patch:
    target_name: str  # e.g. "finpay-support"
    base_version: str  # e.g. "v2"
    new_version: str  # e.g. "v3" (suggested; staging may rename)
    original_prompt: str
    patched_prompt: str
    unified_diff: str  # standard unified diff, ready to render in Slack
    summary: str  # one-line: what this patch does
    rationale: str  # one-line: why it should fix the hypothesized failure

    def to_dict(self) -> dict:
        return asdict(self)


_PATCH_PROMPT = """\
You are editing the system instruction for FinPay Support to fix one
specific failure mode. Apply the smallest possible change that
DEMONSTRABLY RESOLVES the hypothesis without introducing other
behavioral changes.

Rules:
  - Edit ONLY what's needed to fix the hypothesized failure. Don't
    rephrase unrelated lines, don't tighten unrelated rules.
  - When removing an imperative rule (anything that tells the model
    to DO X), you MUST replace it with the correct positive rule
    that tells the model what to do instead. Never delete-only —
    the absence of a rule leaves model behavior under-specified
    and unreliable. Example: if removing "Always assume USD if not
    specified.", the patched prompt must add a clear rule like
    "If the user does not specify a currency, ask them to specify
    one before performing the conversion. Never assume a default
    currency." The replacement should be at least as imperative as
    the rule it removes.
  - Preserve the existing voice, capabilities list, and structure.
  - The output must be a complete, runnable system instruction — not
    a diff and not a description of changes.

Reply STRICT JSON, no prose, no fences:
  {"patched_instruction": "<the full new system instruction>",
   "summary": "<one short line: what changed>",
   "rationale": "<one short line: why this resolves the hypothesis>"}
"""


def generate_patch(
    hypothesis: Hypothesis,
    *,
    current_prompt: str,
    target_name: str = "finpay-support",
    base_version: str = "v2",
    new_version: str | None = None,
    judge_model: str | None = None,
) -> Patch:
    """Produce a patched prompt + unified diff against the original.

    Args:
        hypothesis: from C5. Drives what to fix.
        current_prompt: the live target system instruction verbatim.
        target_name: agent name (used in the diff header).
        base_version: the version we're patching from.
        new_version: bump tag for the patched version. Defaults to
            base_version with the trailing integer + 1 (v2 → v3).
        judge_model: override the model.
    """
    judge_model = judge_model or os.environ.get(
        "MENDER_JUDGE_MODEL",
        os.environ.get("MENDER_MODEL", "gemini-3-flash-preview"),
    )
    new_version = new_version or _bump_version(base_version)

    prompt = (
        _PATCH_PROMPT
        + f"\n\n=== HYPOTHESIS ===\n"
        + f"pattern              : {hypothesis.pattern_name}\n"
        + f"root_cause           : {hypothesis.root_cause}\n"
        + f"suspected_clause     : {hypothesis.suspected_prompt_clause!r}\n"
        + f"recommended_action   : {hypothesis.recommended_action}\n"
        + f"confidence           : {hypothesis.confidence}\n\n"
        + f"=== CURRENT FINPAY SYSTEM INSTRUCTION ({base_version}) ===\n"
        + current_prompt
        + "\n"
    )

    parsed = _gemini_json(prompt, model=judge_model)
    patched = str(parsed.get("patched_instruction", "")).strip()
    if not patched:
        raise ValueError("patch generator returned empty patched_instruction")

    diff = "".join(
        difflib.unified_diff(
            current_prompt.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"{target_name}/{base_version}",
            tofile=f"{target_name}/{new_version}",
            n=2,  # 2 lines of context — keeps Slack code blocks compact
        )
    )

    return Patch(
        target_name=target_name,
        base_version=base_version,
        new_version=new_version,
        original_prompt=current_prompt,
        patched_prompt=patched,
        unified_diff=diff,
        summary=str(parsed.get("summary", "")).strip(),
        rationale=str(parsed.get("rationale", "")).strip(),
    )


def _bump_version(version: str) -> str:
    m = re.fullmatch(r"v(\d+)", version)
    if not m:
        return f"{version}-patched"
    return f"v{int(m.group(1)) + 1}"


def _gemini_json(prompt: str, *, model: str) -> dict:
    from google import genai
    from google.genai.types import HttpOptions

    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        http_options=HttpOptions(
            base_url=os.environ.get("VERTEX_API_HOST", "https://aiplatform.googleapis.com"),
        ),
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
