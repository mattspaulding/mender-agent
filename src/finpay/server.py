"""HTTP server for FinPay Support.

Two endpoints:
    GET  /health            - liveness probe.
    POST /chat              - body: {"message": "..."}, returns {"reply": "..."}.

That's the contract the traffic generator and Mender's eval runner depend on.
Anything else is overhead.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

load_dotenv()

# Init telemetry BEFORE importing the agent so the instrumentor sees ADK calls.
from ._telemetry import init_telemetry  # noqa: E402

init_telemetry(project_name="finpay-support")

from ._runner import ask  # noqa: E402
from .prompts import live_version  # noqa: E402

app = FastAPI(title="FinPay Support", version="0.1.0")


class ChatRequest(BaseModel):
    message: str
    user_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    prompt_version: str


@app.get("/health")
def health() -> dict:
    """Liveness probe. Note: Cloud Run's L7 frontend reserves the
    exact path `/healthz`, so `/health` is the canonical name."""
    v = live_version()
    return {"status": "ok", "prompt_version": v.version, "released_at": v.released_at.isoformat()}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    result = await ask(req.message, user_id=req.user_id)
    return ChatResponse(
        reply=result.text,
        session_id=result.session_id,
        prompt_version=result.prompt_version,
    )


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT") or os.environ.get("FINPAY_PORT", "8081"))
    host = os.environ.get("FINPAY_HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    uvicorn.run("finpay.server:app", host=host, port=port, reload=False)
