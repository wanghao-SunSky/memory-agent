"""HTTP API, identity boundary, readiness and the memory workspace."""
from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import text

from . import auth, config
from .db import get_engine
from .deletion import delete_memory
from .jobs import get_job
from .recall import recall
from .store import get_space_id, ingest_episode, list_memories, owner_of_fact

SpaceName = Literal["work", "life"]


class EpisodeIn(BaseModel):
    space: SpaceName = "work"
    session_id: str = Field(default="default", min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=8000)
    occurred_at: AwareDatetime
    role: Literal["user", "assistant", "tool"] = "user"
    source_type: Literal["user_utterance", "assistant", "tool_result", "web", "document"] = "user_utterance"
    turn_idx: int | None = Field(default=None, ge=0)

class RecallIn(BaseModel):
    space: SpaceName = "work"
    question: str = Field(min_length=1, max_length=2000)
    as_of: AwareDatetime | None = None


def model_configuration() -> tuple[str, bool]:
    config.load_secrets()
    mode = os.environ.get("MEMORY_MODEL_MODE", "demo")
    return mode, mode == "demo" or (mode == "live" and bool(os.environ.get("DEEPSEEK_API_KEY")))


def create_app(instrument: bool = True, *, allow_test_auth: bool = False) -> FastAPI:
    app = FastAPI(title="Memory Agent", version="0.2.0")

    def owner(request: Request) -> str:
        return auth.identity(request, allow_test_auth=allow_test_auth)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Pydantic errors normally include the original input. Keep private text out.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]})

    @app.exception_handler(Exception)
    async def internal_error(_request, _exc):
        return JSONResponse(status_code=503, content={"detail": "处理暂未完成，请检查数据库、模型配置或稍后重试"})

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            # Avoid the server's default traceback containing provider request excerpts.
            logging.getLogger("memory_agent.api").warning("request failed: %s", type(exc).__name__)
            response = JSONResponse(status_code=503, content={"detail": "处理暂未完成，请检查数据库、模型配置或稍后重试"})
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/livez")
    def livez():
        return {"ok": True}

    @app.get("/readyz")
    @app.get("/healthz")
    def readyz():
        mode, model_ok = model_configuration()
        checks = {"database": False, "worker": False, "model": model_ok,
                  "auth": auth.auth_configured(allow_test_auth=allow_test_auth)}
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
                checks["database"] = True
                checks["worker"] = bool(conn.execute(text("""SELECT EXISTS(SELECT 1 FROM worker_heartbeats
                    WHERE seen_at > now() - interval '30 seconds' AND model_mode=:mode)"""), {"mode": mode}).scalar_one())
        except Exception:
            pass
        ok = all(checks.values())
        return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "checks": checks})

    @app.get("/info")
    def info():
        mode, configured = model_configuration()
        return {"model_mode": mode, "model_configured": configured, "auth_mode": auth.auth_mode()}

    @app.get("/me")
    def me(owner_id: str = Depends(owner)):
        return {"owner": owner_id}

    @app.post("/episodes")
    def post_episode(body: EpisodeIn, owner_id: str = Depends(owner)):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, owner_id, body.space)
            return ingest_episode(conn, space_id=sid, session_id=body.session_id, content=body.content,
                                  occurred_at=body.occurred_at, role=body.role, source_type=body.source_type, turn_idx=body.turn_idx)

    @app.get("/jobs/{job_id}")
    def job_status(job_id: int, owner_id: str = Depends(owner)):
        with get_engine().connect() as conn:
            job = get_job(conn, job_id, owner_id=owner_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        # Raw provider errors may contain request excerpts even after regex redaction.
        if job["last_error"]:
            job["last_error"] = "任务执行失败；请检查模型配置，重试用尽后可重新排队"
        return job

    @app.post("/jobs/{job_id}/retry")
    def retry_job(job_id: int, owner_id: str = Depends(owner)):
        from .jobs import requeue_dead
        with get_engine().begin() as conn:
            job = get_job(conn, job_id, owner_id=owner_id)
            if not job:
                raise HTTPException(404, "任务不存在")
            if job["status"] != "dead":
                raise HTTPException(409, "只有重试次数已用尽的任务可以重新排队")
            requeue_dead(conn, job_id)
        return {"job_id": job_id, "status": "queued"}

    def with_trace(out):
        if out.get("trace_id") and config.otlp_endpoint():
            out["trace_url"] = f"{config.jaeger_ui()}/trace/{out['trace_id']}"
        return out

    @app.post("/recall")
    def post_recall(body: RecallIn, owner_id: str = Depends(owner)):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, owner_id, body.space)
            return with_trace(recall(conn, sid, body.question, body.as_of))

    @app.post("/investigate")
    def post_investigate(body: RecallIn, owner_id: str = Depends(owner)):
        from .investigation import investigate
        with get_engine().begin() as conn:
            sid = get_space_id(conn, owner_id, body.space)
            return with_trace(investigate(conn, sid, body.question, body.as_of))

    @app.get("/memories")
    def get_memories(space: SpaceName = "work", include_history: bool = True, owner_id: str = Depends(owner)):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, owner_id, space)
            return {"space": space, "memories": list_memories(conn, sid, include_history)}

    @app.delete("/memories/{memory_id}")
    def del_memory(memory_id: int, owner_id: str = Depends(owner)):
        with get_engine().connect() as conn:
            own = owner_of_fact(conn, memory_id)
        if own is None or own[1] != owner_id:
            raise HTTPException(404, "记忆不存在")
        return delete_memory(get_engine(), own[0], memory_id)

    @app.get("/", response_class=HTMLResponse)
    def page():
        # No private data is embedded before authentication, nor query text inserted in HTML.
        from .ui import PAGE
        return PAGE

    if instrument:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,livez,readyz")
    return app


def build() -> FastAPI:
    from .tracing import setup_tracing
    config.load_secrets()
    if auth.auth_mode() == "test":
        raise RuntimeError("test 身份模式仅供显式测试应用使用")
    setup_tracing("memory-agent-api")
    return create_app()


app = None
