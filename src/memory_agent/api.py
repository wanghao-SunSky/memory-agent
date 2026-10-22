"""FastAPI：POST /episodes、POST /recall、GET /memories、DELETE /memories/{memory_id}、GET /（你记得我什么）。"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import config
from .db import get_engine
from .deletion import delete_memory
from .jobs import get_job
from .recall import recall
from .store import get_space_id, ingest_episode, list_memories, owner_of_fact

SpaceName = Literal["work", "life"]


class EpisodeIn(BaseModel):
    space: SpaceName = "work"
    session_id: str = "default"
    content: str = Field(min_length=1, max_length=8000)
    occurred_at: datetime
    role: Literal["user", "assistant", "tool"] = "user"
    source_type: Literal["user_utterance", "assistant", "tool_result", "web", "document"] = "user_utterance"
    turn_idx: int | None = None


class RecallIn(BaseModel):
    space: SpaceName = "work"
    question: str = Field(min_length=1, max_length=2000)
    as_of: datetime | None = None


def create_app(instrument: bool = True) -> FastAPI:
    app = FastAPI(title="记得住你 memory-agent", version="0.1.0")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/episodes")
    def post_episode(body: EpisodeIn, x_owner_id: str = Header(default="me")):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, x_owner_id, body.space)
            return ingest_episode(conn, space_id=sid, session_id=body.session_id, content=body.content, occurred_at=body.occurred_at,
                                  role=body.role, source_type=body.source_type, turn_idx=body.turn_idx)

    @app.get("/jobs/{job_id}")
    def job_status(job_id: int):
        with get_engine().connect() as conn:
            j = get_job(conn, job_id)
        if not j:
            raise HTTPException(404, "job 不存在")
        return j

    @app.post("/recall")
    def post_recall(body: RecallIn, x_owner_id: str = Header(default="me")):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, x_owner_id, body.space)
            out = recall(conn, sid, body.question, body.as_of)
        if out["trace_id"]:
            out["trace_url"] = f"{config.jaeger_ui()}/trace/{out['trace_id']}"
        return out

    @app.get("/memories")
    def get_memories(space: SpaceName = "work", include_history: bool = True, x_owner_id: str = Header(default="me")):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, x_owner_id, space)
            return {"space": space, "memories": list_memories(conn, sid, include_history)}

    @app.delete("/memories/{memory_id}")
    def del_memory(memory_id: int, x_owner_id: str = Header(default="me")):
        with get_engine().connect() as conn:
            own = owner_of_fact(conn, memory_id)
        if own is None or own[1] != x_owner_id:  # 越权与不存在同样返回 404
            raise HTTPException(404, f"memory_id={memory_id} 不存在；请先用 GET /memories 查看可删除的 memory_id")
        return delete_memory(get_engine(), own[0], memory_id)

    @app.get("/", response_class=HTMLResponse)
    def page(space: SpaceName = Query("work"), owner: str = Query("me")):
        with get_engine().begin() as conn:
            sid = get_space_id(conn, owner, space)
            items = list_memories(conn, sid, True)
        return render_page(items, space, owner)

    if instrument:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    return app


def render_page(items: list[dict], space: str, owner: str) -> str:
    e = html.escape
    tabs = " | ".join(
        f'<a href="/?space={s}&owner={e(owner)}"{" style=font-weight:bold" if s == space else ""}>{n}</a>' for s, n in (("work", "工作"), ("life", "生活"))
    )
    cards = []
    for it in items:
        cur = it["current"]
        rows = []
        for v in it["versions"]:
            src = "；".join(f"会话 {e(s['session_id'])} 第 {s['turn_idx']} 轮（{e(s['occurred_at'][:16])}）" for s in v["sources"]) or "来源已删除"
            style = "" if v["status"] == "active" else "color:#888;text-decoration:line-through"
            label = {"active": "当前", "superseded": "已作废"}.get(v["status"], v["status"])
            rows.append(
                f'<li style="{style}">[{label}] #{v["memory_id"]} {e(v["fact_text"])}'
                f'<br><small>生效 {e((v["valid_from"] or "")[:10])} → {e((v["valid_to"] or "至今")[:10])}；出处：{src}</small></li>'
            )
        cards.append(
            f'<div class="card"><b>{e(cur["subject"])} · {e(cur["predicate"])}</b>'
            f'<button onclick="del({cur["memory_id"]})">删除（含全部版本）</button><ul>{"".join(rows)}</ul></div>'
        )
    body = "".join(cards) or "<p>这个空间里没有记忆。</p>"
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>你记得我什么</title>
<style>body{{font-family:sans-serif;max-width:860px;margin:2em auto}}.card{{border:1px solid #ddd;border-radius:6px;padding:.6em 1em;margin:.6em 0}}
button{{float:right;color:#b00}}li{{margin:.3em 0}}</style></head><body>
<h1>你记得我什么</h1><p>空间：{tabs}</p>{body}
<script>
async function del(id){{
  if(!confirm('确定删除这条记忆的全部版本？来源对话会被排除并删除正文，不可恢复。')) return;
  const r = await fetch('/memories/'+id, {{method:'DELETE', headers:{{'X-Owner-Id': {json.dumps(owner)}}}}});
  const j = await r.json(); alert('删除结果：' + (j.result || JSON.stringify(j))); location.reload();
}}
</script></body></html>"""


def build() -> FastAPI:
    from .tracing import setup_tracing

    setup_tracing("memory-agent-api")
    return create_app()


app = None  # uvicorn 用 --factory memory_agent.api:build
