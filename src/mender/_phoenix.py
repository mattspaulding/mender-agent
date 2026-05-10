"""Tiny typed Phoenix Cloud REST client.

Used by the eval scorer (B3) and later by the C3 trace-window fetcher.
Wraps just the endpoints we need — projects, spans, span_annotations —
to avoid pulling the full Phoenix SDK for what is essentially three
HTTP calls.

Auth is `Authorization: Bearer <PHOENIX_API_KEY>`. Base URL must include
the space path (e.g. `https://app.phoenix.arize.com/s/<space>`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class Span:
    """The bits of a Phoenix span we actually need."""

    span_id: str  # OTel hex, no 0x — what /span_annotations expects
    trace_id: str
    name: str
    start_time: datetime
    end_time: datetime | None
    input_text: str
    output_text: str
    raw_attributes: dict[str, Any]


class PhoenixClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.environ["PHOENIX_BASE_URL"]).rstrip("/")
        api_key = api_key or os.environ["PHOENIX_API_KEY"]
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PhoenixClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- spans ----

    def list_spans(
        self,
        project: str,
        *,
        limit: int = 100,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        cursor: str | None = None,
    ) -> tuple[list[Span], str | None]:
        params: dict[str, str | int] = {"limit": limit}
        if start_time is not None:
            params["start_time"] = _iso(start_time)
        if end_time is not None:
            params["end_time"] = _iso(end_time)
        if cursor is not None:
            params["cursor"] = cursor

        r = self._client.get(f"/v1/projects/{project}/spans", params=params)
        r.raise_for_status()
        body = r.json()
        return [_to_span(raw) for raw in body.get("data", [])], body.get("next_cursor")

    def list_span_annotations(
        self,
        project: str,
        *,
        span_ids: list[str],
        include_annotation_names: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fetch annotations for the given span IDs. Phoenix requires
        an explicit `span_ids` query param — there's no list-all path."""
        if not span_ids:
            return []
        results: list[dict] = []
        # Phoenix caps query string length; chunk to be safe.
        for i in range(0, len(span_ids), 50):
            chunk = span_ids[i : i + 50]
            params: list[tuple[str, str]] = [("span_ids", sid) for sid in chunk]
            params.append(("limit", str(limit)))
            for name in include_annotation_names or []:
                params.append(("include_annotation_names", name))
            r = self._client.get(
                f"/v1/projects/{project}/span_annotations",
                params=params,
            )
            r.raise_for_status()
            results.extend(r.json().get("data", []))
        return results

    def annotate_spans(
        self,
        annotations: list[dict],
        *,
        sync: bool = True,
        retries: int = 4,
        backoff_sec: float = 0.6,
    ) -> dict:
        """POST one or more SpanAnnotationData records.

        sync=True so Phoenix processes the annotation immediately and
        the trace-list "label" column populates without waiting for
        async queue drain.

        On the inline-scoring path, the span_id may briefly precede
        the span itself reaching Phoenix Cloud (OpenInference exports
        on span end; there's a small flush delay). Phoenix returns
        404 ("Spans with IDs X do not exist") in that window. We
        retry with exponential backoff up to `retries` times to bridge
        that race.
        """
        return self._annotate_with_retry(
            "/v1/span_annotations",
            annotations,
            sync=sync,
            retries=retries,
            backoff_sec=backoff_sec,
            kind="Spans",
        )

    def list_trace_annotations(
        self,
        project: str,
        *,
        trace_ids: list[str],
        include_annotation_names: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """Fetch annotations for the given trace IDs. Mirrors
        list_span_annotations but on the trace endpoint. Used by the
        batch scorer to dedupe against traces that were already scored
        inline."""
        if not trace_ids:
            return []
        results: list[dict] = []
        for i in range(0, len(trace_ids), 50):
            chunk = trace_ids[i : i + 50]
            params: list[tuple[str, str]] = [("trace_ids", tid) for tid in chunk]
            params.append(("limit", str(limit)))
            for name in include_annotation_names or []:
                params.append(("include_annotation_names", name))
            r = self._client.get(
                f"/v1/projects/{project}/trace_annotations",
                params=params,
            )
            r.raise_for_status()
            results.extend(r.json().get("data", []))
        return results

    def annotate_traces(
        self,
        annotations: list[dict],
        *,
        sync: bool = True,
        retries: int = 4,
        backoff_sec: float = 0.6,
    ) -> dict:
        """POST one or more TraceAnnotationData records.

        Same shape as annotate_spans but bound to a `trace_id` (OTel
        hex, no 0x). Phoenix's trace-list view renders these — span
        annotations don't show up there. To make a per-trace eval
        score visible in the trace list, write it both as a span
        annotation (where the metrics chart and span-detail panel
        read from) and as a trace annotation (this endpoint).

        Phoenix upserts on (name, identifier, trace_id) so re-running
        the scorer over the same traces updates rather than duplicates.

        Same retry behavior as annotate_spans for sync=true 404 races.
        """
        return self._annotate_with_retry(
            "/v1/trace_annotations",
            annotations,
            sync=sync,
            retries=retries,
            backoff_sec=backoff_sec,
            kind="Traces",
        )

    def _annotate_with_retry(
        self,
        path: str,
        annotations: list[dict],
        *,
        sync: bool,
        retries: int,
        backoff_sec: float,
        kind: str,
    ) -> dict:
        """Shared retry helper for annotate_spans / annotate_traces.

        On sync=True writes, Phoenix returns 404 with a body like
        "Spans with IDs X do not exist" if the span hasn't been
        ingested yet (inline-scoring race). Retry with exponential
        backoff up to `retries` times to bridge that window.
        Anything else short-circuits to raise_for_status.
        """
        import time

        params = {"sync": "true" if sync else "false"}
        delay = backoff_sec
        for attempt in range(retries + 1):
            r = self._client.post(path, params=params, json={"data": annotations})
            if (
                r.status_code == 404
                and sync
                and attempt < retries
                and kind in (r.text or "")
            ):
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        # exhausted retries — final response wasn't 200; raise.
        r.raise_for_status()
        return r.json()


def _iso(dt: datetime) -> str:
    s = dt.isoformat()
    if dt.tzinfo is None:
        s += "Z"
    return s


def _to_span(raw: dict) -> Span:
    attrs = raw.get("attributes") or {}
    ctx = raw.get("context") or {}
    return Span(
        span_id=ctx.get("span_id", ""),
        trace_id=ctx.get("trace_id", ""),
        name=raw.get("name", ""),
        start_time=_parse_ts(raw.get("start_time")),
        end_time=_parse_ts(raw.get("end_time")) if raw.get("end_time") else None,
        input_text=_extract(attrs, "input"),
        output_text=_extract(attrs, "output"),
        raw_attributes=attrs,
    )


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract(attrs: dict, kind: str) -> str:
    """Phoenix flattens or nests OTel attribute keys depending on age.

    Try both shapes; prefer the actual text content if the value is a
    JSON envelope (FinPay wraps user/agent messages in JSON for ADK).
    """
    val = attrs.get(f"{kind}.value")
    if val is None:
        nested = attrs.get(kind)
        if isinstance(nested, dict):
            val = nested.get("value")
    if not val:
        return ""
    return _unwrap_text(val)


def _unwrap_text(val: str) -> str:
    """If `val` is JSON wrapping a {parts:[{text:...}]} structure (ADK
    Content), pull out the human-readable text. Otherwise return as-is.
    """
    s = str(val).strip()
    if not (s.startswith("{") or s.startswith("[")):
        return s
    try:
        import json

        obj = json.loads(s)
    except json.JSONDecodeError:
        return s
    return _walk_for_text(obj) or s


def _walk_for_text(obj: object) -> str:
    """Best-effort: find the first {text: "..."} or {content: ...} field."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        # ADK new_message.parts[].text shape
        for key in ("text", "value"):
            if key in obj and isinstance(obj[key], str):
                return obj[key]
        # ADK Content shape
        for key in ("content", "new_message", "message", "parts"):
            if key in obj:
                got = _walk_for_text(obj[key])
                if got:
                    return got
        return ""
    if isinstance(obj, list):
        chunks = []
        for item in obj:
            got = _walk_for_text(item)
            if got:
                chunks.append(got)
        return "\n".join(chunks)
    return ""
