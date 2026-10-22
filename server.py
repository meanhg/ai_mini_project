"""all_total 통합 실습 — FastAPI 백엔드 (Wave 1·3·12·14 축 + 전체 조립).

- Wave 1  : lifespan에서 Agent 번들(MCP 연결·LLM·그래프)을 프로세스당 정확히 1회 생성,
            app.state로 모든 요청이 공유 (creation_count/instance_id로 증명)
- Wave 2  : mcp_server.py를 subprocess(stdio)로 기동해 연결 유지
- Wave 3  : GET /chat/stream — 그래프 실행 과정을 SSE 이벤트로 스트리밍
- Wave 12 : 대화 이력을 SQLite(session.db)에 저장 — 재시작해도 유지
- Wave 14 : SUPERVISOR_VERSION(v1/v2) 환경변수로 legacy/신형 파이프라인 즉시 전환
- LangSmith: 요청당 chat_request 루트 run 아래로 노드·LLM·MCP 호출이 계층 기록됨
"""
import json
import os
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from agent.chat_service import ChatService, build_metadata
from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.checklists import ChecklistStore, candidates
from agent.checklist_api import router as checklist_router

CREATION_COUNT = 0   # Wave 1 — 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


class ChatRequest(BaseModel):   # Wave 1 — Pydantic 요청 검증
    message: str = Field(min_length=1, max_length=6000)
    thread_id: str = "default"
    backend: Literal['fake', 'openai', 'ollama'] | None = None


def _build_llms(backend=None):
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬 실모델) | openai(실제 OpenAI API)."""
    backend = backend or os.environ.get("LLM_BACKEND", "fake")
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3.5:2b"))
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"))
        return backend, fast, quality
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"))
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"))
        return backend, fast, quality
    if backend != 'fake':
        raise ValueError('지원하지 않는 모델 백엔드입니다.')
    fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015)
    quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005)
    return backend, fast, quality


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    CREATION_COUNT += 1
    async with AsyncExitStack() as stack:
        mcp = MCPClient(flaky_first_tool_call=os.getenv('SIMULATE_TOOL_TIMEOUT', 'false').lower() == 'true')
        await mcp.start()
        stack.push_async_callback(mcp.stop)
        backend, fast_llm, quality_llm = _build_llms()
        app.state.llm_backend = backend
        store = SqliteSessionStore()
        app.state.checklists = ChecklistStore(store)
        stack.callback(store.close)
        deps = SupervisorDeps(fast_llm, quality_llm, mcp, store,
                              os.environ.get("SUPERVISOR_VERSION", "v2"))
        checkpoint = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(
            str(store.db_path) + '.checkpoints.db'))
        checkpoint.serde = JsonPlusSerializer(allowed_msgpack_modules=[])
        app.state.checkpointer = checkpoint
        app.state.locks = {}
        app.state.deps = deps
        app.state.graph = build_graph(deps, checkpoint) if deps.version == "v2" else None
        app.state.bundles = {backend: (deps, app.state.graph)}
        app.state.instance_id = id(deps)
        app.state.chat_service = ChatService(app.state, _get_bundle)
        try:
            yield
        finally:
            try:
                from langchain_core.tracers.langchain import wait_for_all_tracers
                wait_for_all_tracers()
            except Exception:
                pass


app = FastAPI(lifespan=lifespan)
app.include_router(checklist_router)


def _get_bundle(backend=None):
    backend = backend or app.state.llm_backend
    if backend not in app.state.bundles:
        _, fast, quality = _build_llms(backend)
        base = app.state.deps
        deps = SupervisorDeps(fast, quality, base.mcp, base.session_store, base.version)
        app.state.bundles[backend] = (deps, build_graph(deps, app.state.checkpointer) if deps.version == 'v2' else None)
    return backend, *app.state.bundles[backend]


@app.get("/health")
async def health():
    return {"status": "ok", "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT,
            "supervisor_version": app.state.deps.version,
            "llm_backend": app.state.llm_backend,
            "fast_llm": app.state.deps.fast_llm.name,
            "quality_llm": app.state.deps.quality_llm.name,
            "rag_enabled": os.getenv('RAG_ENABLED', 'false').lower() == 'true',
            "embedding_backend": (os.getenv('RAG_EMBEDDING_BACKEND', 'ollama')
                if os.getenv('RAG_ENABLED', 'false').lower() == 'true' else None),
            "runbook_mode": ('rag' if os.getenv('RAG_ENABLED', 'false').lower() == 'true' else 'direct'),
            "backends": {name: {'fast': _get_bundle(name)[1].fast_llm.name,
                                 'quality': _get_bundle(name)[1].quality_llm.name}
                         for name in ['openai', 'ollama', 'fake']}}


@app.get("/version")
async def version():
    return {"supervisor_version": app.state.deps.version}


def _metadata(result: dict, run_id: str | None, backend=None) -> dict:
    return build_metadata(result, run_id, backend, app.state)


def _lock(thread_id):
    return app.state.chat_service.lock(thread_id)


def _run_for_thread(run_id, thread_id):
    return app.state.chat_service.run_for_thread(run_id, thread_id)


@app.get('/sessions/{thread_id}/messages')
async def messages(thread_id: str, before: int | None = None):
    store = app.state.deps.session_store
    items = store.history(thread_id, before=before)
    for item in items:
        if item['role'] == 'assistant':
            item['metadata']['checklist_candidates'] = candidates(item['content'], item['metadata'])
    return {'messages': items, 'next_before': items[0]['id'] if len(items) == 100 else None,
            'pending_runs': [r for r in store.pending(thread_id) if r['status'] != 'discarded']}


def _execute(message, thread_id, backend=None, resume=None):
    return app.state.chat_service.execute(message, thread_id, backend, resume)


@app.post('/chat')
async def chat(req: ChatRequest):
    final = None
    async for event in _execute(req.message, req.thread_id, req.backend):
        if event['type'] == 'FinalAnswer':
            final = event
    return {'reply': final['reply'], 'events': final['events'], 'metadata': final['metadata']}


@app.post('/runs/{run_id}/resume')
async def resume_run(run_id: str, thread_id: str):
    run = _run_for_thread(run_id, thread_id)
    if app.state.deps.version != 'v2':
        raise HTTPException(409, '단계 복구는 v2에서 지원합니다.')
    final = None
    async for event in _execute(run['query'], thread_id, run['backend'], resume=run_id):
        if event['type'] == 'FinalAnswer':
            final = event
    return final


@app.post('/runs/{run_id}/discard')
async def discard_run(run_id: str, thread_id: str):
    async with _lock(thread_id):
        _run_for_thread(run_id, thread_id)
        app.state.deps.session_store.status(run_id, 'discarded')
    return {'status': 'discarded'}


@app.get('/chat/stream')
async def chat_stream(message: str, thread_id: str = 'default',
                      backend: Literal['fake', 'openai', 'ollama'] | None = None):
    if not message.strip() or len(message) > 6000:
        raise HTTPException(422, '질문은 1~6000자로 입력해주세요.')
    async def guarded():
        try:
            async for event in _execute(message, thread_id, backend):
                yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
        except Exception as exc:
            error_message = exc.detail if isinstance(exc, HTTPException) else '모델 또는 자료 조회에 실패했습니다. 중단된 작업을 이어서 실행할 수 있습니다.'
            yield 'data: ' + json.dumps({'type': 'Error', 'message': error_message}, ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(guarded(), media_type='text/event-stream')


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")
