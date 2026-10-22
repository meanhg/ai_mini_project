"""채팅 실행·저장·중단 복구의 공통 서비스. HTTP 경로는 server.py가 담당한다."""
import asyncio
import uuid

from fastapi import HTTPException
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from .conversation import compact_history, next_context
from .fake_llm import resolve_demo_query
from .settings import policy
from .checklists import candidates
from .response_policy import followup_suggestions
from .supervisor_v1 import run_v1


def build_metadata(result: dict, run_id: str | None, backend, state) -> dict:
    return {
        "route": result.get("route"),
        "followups": followup_suggestions(result),
        "response_context": result.get("response_context", {}),
        "task_count": len(result.get("tasks", [])),
        "gate_removed": result.get("gate_removed", 0),
        "replan_rounds": result.get("replan_rounds", 0),
        "retry_count": result.get("retry_count", 0),
        "parallel_elapsed": result.get("parallel_elapsed"),
        "validation_attempts": result.get("validation_attempts", 0),
        "validation_passed": result.get("validation_passed"),
        "validation_errors": result.get("validation_errors", []),
        "evidence_sufficient": result.get("evidence_sufficient"),
        "evidence_gaps": result.get("evidence_gaps", []),
        "partial_result": bool(result.get('evidence_gaps')) or result.get('validation_passed') is False
                          or any(e['type'] == 'TaskFailed' for e in result.get('events', [])),
        "cost_usd": (result.get('cost_usd', 0) if result.get('usage')
                     and all(u.get('usd') is not None for u in result['usage']) else None),
        "usage": result.get('usage', []),
        "tokens": sum(u.get('tokens', 0) for u in result.get('usage', [])),
        "monitoring": [c['data'] for c in result.get('collected', []) if c.get('source') == 'monitoring'],
        "llm_backend": backend or state.llm_backend,
        "sources": result.get('sources', []),
        "cost_is_estimate": True,
        "supervisor_version": state.deps.version,
        "instance_id": state.instance_id,
        "langsmith_run_id": run_id,
    }


class ChatService:
    def __init__(self, state, get_bundle):
        self.state = state
        self.get_bundle = get_bundle

    def lock(self, thread_id):
        return self.state.locks.setdefault(thread_id, asyncio.Lock())

    def run_for_thread(self, run_id, thread_id):
        run = self.state.deps.session_store.run(run_id)
        if not run or run['thread_id'] != thread_id:
            raise HTTPException(404, '실행 기록을 찾을 수 없습니다.')
        if run['status'] in ('completed', 'discarded'):
            raise HTTPException(409, '이미 종료한 실행입니다.')
        return run

    async def stream_graph(self, graph, initial, run_id, resume):
        """저장된 단계에서 실행하고 진행 이벤트와 최종 상태를 따로 돌려준다."""
        config = {'configurable': {'thread_id': run_id}, 'recursion_limit':
                  14 + 2 * policy().max_research_rounds + 2 * policy().max_validation_attempts}
        snapshot = await graph.aget_state(config) if resume else None
        resuming = bool(resume and snapshot and snapshot.values)
        result = dict(snapshot.values) if resuming else dict(initial)
        seen = len(result.get('events', [])) if resume else 0
        # 끝난 체크포인트가 대화 저장 전에 중단됐으면 호출 없이 저장만 복구한다.
        if not (resume and snapshot and not snapshot.next and result.get('final')):
            async for mode, payload in graph.astream(None if resuming else initial,
                    config=config, stream_mode=['updates', 'custom']):
                if mode == 'custom':
                    yield 'event', payload
                    continue
                for node, delta in payload.items():
                    if not isinstance(delta, dict):
                        continue
                    for event in delta.get('events', [])[seen:]:
                        yield 'event', {**event, 'node': node}
                    if 'events' in delta:
                        seen = len(delta['events'])
                    result.update(delta)
        yield 'result', result

    @traceable(name='chat_request')
    async def execute(self, message, thread_id, backend=None, resume=None):
        """POST/SSE가 동일한 저장·검증·복구 경로를 사용한다."""
        backend, deps, graph = self.get_bundle(backend)
        store = deps.session_store
        run_id = resume or str(uuid.uuid4())
        async with self.lock(thread_id):
            if resume:
                self.run_for_thread(run_id, thread_id)
                store.status(run_id, 'running')
            else:
                pending = [r for r in store.pending(thread_id) if r['status'] != 'discarded']
                if pending:
                    raise HTTPException(409, '중단된 작업을 이어서 실행하거나 종료해주세요.')
                store.start_run(run_id, thread_id, message, backend)
            try:
                yield {'type': 'RunStarted', 'run_id': run_id}
                context = store.context(thread_id)
                effective, clarification = ((message, None) if deps.version == 'v2' and hasattr(deps.fast_llm, 'interpret')
                                            else resolve_demo_query(message, context))
                initial = {'query': effective, 'original_query': message, 'conversation_context': context, 'thread_id': thread_id,
                           'history': compact_history(store.load(thread_id)), 'events': [],
                           'cost_usd': 0.0, 'usage': []}
                if clarification and not resume:
                    result = {**initial, 'final': clarification, 'route': 'incident',
                              'events': [{'type': 'ClarificationRequired'}]}
                elif deps.version != 'v2':
                    result = await run_v1(deps, effective, thread_id, initial['history'])
                    for event in result.get('events', []):
                        yield event
                else:
                    async for kind, payload in self.stream_graph(graph, initial, run_id, resume):
                        if kind == 'event':
                            yield payload
                        else:
                            result = payload
                trace = get_current_run_tree()
                metadata = build_metadata(result, str(trace.id) if trace else None, backend, self.state)
                metadata['run_id'] = run_id
                metadata['checklist_candidates'] = candidates(result['final'], metadata)
                store.finish(run_id, result['final'], metadata, next_context(context, message, result))
                yield {'type': 'FinalAnswer', 'reply': result['final'], 'sources': result.get('sources', []),
                       'backend': backend, 'metadata': metadata, 'events': result.get('events', []),
                       'prompt': result.get('last_prompt')}
            except BaseException:
                if store.run(run_id)['status'] != 'completed':
                    store.status(run_id, 'failed' if deps.version == 'v2' else 'discarded')
                raise


