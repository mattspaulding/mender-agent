"""Mender web UI (components E1-E4) and Slack callback (D2 hook).

Routes:
    GET  /                   - incident list (landing).
    GET  /incidents/<id>     - full incident detail with diff + eval table.
    GET  /charts/eval-trend  - eval-score timeseries page (Scene 4).
    GET  /api/eval-trend     - JSON for the chart, used by external tools.
    GET  /about              - architecture + component map (Scene 7 mirror).
    GET  /healthz            - liveness probe for Cloud Run.
    POST /api/approve-patch  - Slack interactive callback (D2; stubbed
                               until D1 lands).

Renders templates from `src/mender/web/templates/`. Reads incidents
from the IncidentStore JSON file (or Firestore in prod). Reads eval
trend data from Phoenix via the typed tools.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

# Init telemetry so the web app's own activity is traced too.
from .._telemetry import init_telemetry  # noqa: E402

init_telemetry(project_name="mender-web")

from ..pipeline.incident import IncidentStore  # noqa: E402
from ..tools.traces import summarize_eval_trend  # noqa: E402

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# ---------- template filters ----------

def _short_ts(value: str | datetime) -> str:
    """ISO-string or datetime → 'May 7 09:55:38' (local time)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone().strftime("%b %-d %H:%M:%S")


def _diff_to_html(diff: str) -> str:
    """Render unified diff with class-tagged add/del/header lines."""
    out = []
    for line in diff.splitlines():
        escaped = html.escape(line)
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            out.append(f'<span class="hdr">{escaped}</span>')
        elif line.startswith("+"):
            out.append(f'<span class="add">{escaped}</span>')
        elif line.startswith("-"):
            out.append(f'<span class="del">{escaped}</span>')
        else:
            out.append(escaped)
    return "\n".join(out)


templates.env.filters["short_ts"] = _short_ts


# ---------- app ----------

app = FastAPI(title="Mender", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


def _store() -> IncidentStore:
    return IncidentStore()


def _common_ctx(request: Request, **extra) -> dict:
    return {
        "request": request,
        "tagline": "Catches the cracks. Mends them.",
        **extra,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> Response:
    incidents = _store().list_all()
    incidents.sort(key=lambda i: i.updated_at, reverse=True)
    # Convert to dict so jinja can do ['key'] AND .key uniformly.
    return templates.TemplateResponse(
        "index.html",
        _common_ctx(request, active="home", incidents=[_inc_view(i.to_dict()) for i in incidents]),
    )


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_detail(incident_id: str, request: Request) -> Response:
    for inc in _store().list_all():
        if inc.id == incident_id:
            return templates.TemplateResponse(
                "incident.html",
                _common_ctx(request, active="home", inc=_inc_view(inc.to_dict())),
            )
    raise HTTPException(status_code=404, detail="incident not found")


@app.get("/charts/eval-trend", response_class=HTMLResponse)
def eval_trend_page(
    request: Request,
    project: str = "finpay-support",
    window: str = "60m",
    bucket: str = "10m",
) -> Response:
    window_min = _parse_window(window)
    bucket_min = _parse_window(bucket)
    trend = summarize_eval_trend(
        window_minutes=window_min, project=project, bucket_minutes=bucket_min
    )
    return templates.TemplateResponse(
        "trend.html",
        _common_ctx(
            request,
            active="trend",
            project=project,
            window_minutes=window_min,
            trend=trend,
        ),
    )


@app.get("/api/eval-trend")
def eval_trend_api(
    project: str = "finpay-support",
    window: str = "60m",
    bucket: str = "10m",
) -> JSONResponse:
    return JSONResponse(
        summarize_eval_trend(
            window_minutes=_parse_window(window),
            project=project,
            bucket_minutes=_parse_window(bucket),
        )
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> Response:
    return templates.TemplateResponse(
        "about.html",
        _common_ctx(request, active="about"),
    )


@app.post("/api/approve-patch")
async def approve_patch(request: Request) -> JSONResponse:
    """Slack interactive callback (component D2).

    Slack POSTs application/x-www-form-urlencoded with a `payload` field
    containing the action JSON. We verify the HMAC signature, look up
    the incident, and either promote_to_live() (D3) or dismiss.
    """
    import json as _json

    from ..integrations.slack import (
        post_confirmation,
        verify_signature,
    )
    from ..pipeline.incident import IncidentStore
    from ..pipeline.staging import promote_to_live

    # Read body up-front so we can verify the signature.
    body = await request.body()
    sig = request.headers.get("x-slack-signature", "")
    ts = request.headers.get("x-slack-request-timestamp", "")
    if not verify_signature(body=body, timestamp=ts, signature=sig):
        return JSONResponse({"ok": False, "error": "bad signature"}, status_code=401)

    # Slack form-encodes the JSON in a `payload` field.
    form = await request.form()
    raw = form.get("payload")
    if not raw:
        return JSONResponse({"ok": False, "error": "missing payload"}, status_code=400)
    try:
        payload = _json.loads(raw if isinstance(raw, str) else raw.decode())
    except _json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)

    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({"ok": False, "error": "no actions"}, status_code=400)
    action = actions[0]
    action_id = action.get("action_id", "")
    incident_id = action.get("value", "")

    store = IncidentStore()
    incident = next((i for i in store.list_all() if i.id == incident_id), None)
    if incident is None:
        return JSONResponse({"ok": False, "error": "incident not found"}, status_code=404)

    if action_id == "approve_patch":
        if incident.state != "patch_proposed" or not incident.patch:
            return JSONResponse({"ok": False, "error": f"incident in state {incident.state}"}, status_code=409)
        from ..pipeline.patch_gen import Patch
        patch = Patch(**incident.patch)
        promote_to_live(patch)
        incident.transition("patch_applied", note=f"promoted {patch.base_version}->{patch.new_version}")
        store.upsert(incident)
        post_confirmation(incident, action="applied")
        return JSONResponse({"ok": True, "state": "patch_applied"})

    if action_id == "discard_patch":
        incident.transition("dismissed", note="discarded by user")
        store.upsert(incident)
        post_confirmation(incident, action="discarded")
        return JSONResponse({"ok": True, "state": "dismissed"})

    return JSONResponse({"ok": False, "error": f"unknown action {action_id!r}"}, status_code=400)


# ---------- helpers ----------

def _parse_window(spec: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([mh])\s*", spec)
    if not m:
        return 60
    n = int(m.group(1))
    return n if m.group(2) == "m" else n * 60


def _inc_view(d: dict) -> dict:
    """Render-time enrichments for the incident dict (e.g. diff HTML)."""
    if d.get("patch") and d["patch"].get("unified_diff"):
        d["patch"]["unified_diff_html"] = _diff_to_html(d["patch"]["unified_diff"])
    return d


def main() -> None:
    import uvicorn

    uvicorn.run(
        "mender.web.app:app",
        host=os.environ.get("MENDER_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MENDER_WEB_PORT", "8082")),
        reload=False,
    )
