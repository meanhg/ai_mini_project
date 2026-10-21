"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | incident 파이프라인)
incident: plan → gate → research(관측) → prepare_evidence → research(매뉴얼)
          → replan_check(부족 시 제한된 추가 조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import inspect
import os
import time
from functools import partial
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry
from .llm_common import unknown_systems, mentioned_systems, normalize_plan
from .retrieval_policy import system_search_text
from .validation import validate_draft, verified_fallback
from .settings import policy, runbook_directory
from .coverage import evidence_gaps
from .documents import citation_name


from .monitoring import known_systems



class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    tasks: list[dict]
    gate_removed: int
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: str
    validation_attempts: int
    validation_feedback: str
    validation_passed: bool
    validation_errors: list[str]
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]
    unknown_targets: list[str]
    sources: list[dict]
    usage: list[dict]
    targets: list[str]
    conversation_context: dict
    original_query: str
    clarification_text: str
    evidence_gaps: list[dict]
    evidence_sufficient: bool
    initial_evidence_prepared: bool


async def _invoke(fn, *args):
    """실제 비동기 모델과 결정론적 동기 fake를 함께 지원한다."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


def _usage(state, usage):
    safe = {k: v for k, v in usage.items() if k != 'prompt'}
    return {'usage': state.get('usage', []) + [safe],
            'cost_usd': round((state.get('cost_usd') or 0) + (usage.get('usd') or 0), 6)}


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    if hasattr(deps.fast_llm, 'interpret'):
        context = state.get('conversation_context', {})
        interpreter = (deps.quality_llm if (context.get('targets') or context.get('pending_query'))
                       and hasattr(deps.quality_llm, 'interpret') else deps.fast_llm)
        interpreted, usage = await interpreter.interpret(state['query'], state.get('history', []), context)
        result = {'route': interpreted['route'], 'needs_analysis': interpreted['needs_analysis']}
        query = interpreted['resolved_request']
        extra = {'query': query, 'targets': interpreted['targets'],
                 'unknown_targets': interpreted['unknown_targets'],
                 'clarification_text': interpreted['clarification']}
    else:
        result, usage = await _invoke(deps.fast_llm.route, state['query'])
        extra = {'unknown_targets': unknown_systems(state['query']),
                 'targets': mentioned_systems(state['query'])}
    event = {'type': 'RouteDecided', 'route': result['route']}
    return {**extra, 'route': result['route'], 'needs_analysis': result['needs_analysis'],
            **_usage(state, usage), 'events': state.get('events', []) + [event]}


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            **_usage(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": ', '.join(mentioned_systems(state['query'])) or '대상 확인 필요',
         "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = await _invoke(deps.fast_llm.plan, state["query"], procedure)
    if hasattr(deps.fast_llm, 'interpret'):
        result = normalize_plan(result, state['query'], targets=state.get('targets', []))
    targets = [t["target"] for t in result["tasks"]
               if t["agent"] == "research" and t.get("target")]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"], "needs_analysis": result["needs_analysis"],
            **_usage(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 권위 게이트. needs_analysis=False인데 섞여 들어온 analysis task를
    프롬프트가 아니라 코드로 강제 제거한다."""
    tasks = state["tasks"]
    if not state.get("needs_analysis", False):
        kept = [t for t in tasks if t["agent"] != "analysis"]
        removed = len(tasks) - len(kept)
    else:
        kept, removed = tasks, 0
    event = {"type": "GateApplied", "removed_analysis_tasks": removed}
    return {"tasks": kept, "gate_removed": removed,
            "events": state["events"] + [event]}


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 15 가드 + Wave 10b 병렬 + Wave 9 재시도 + Wave 2 Tool/Resource 실호출."""
    tasks = state["tasks"]
    research_indices = [i for i, t in enumerate(tasks)
                        if t["agent"] == "research" and t["status"] == "pending"]
    # Wave 15 — 객체가 아니라 '실행할 목록'을 검사한다 (not research_agent 버그의 수정판)
    if not research_indices:
        return {"events": state["events"] +
                [{"type": "ResearchSkipped", "reason": "pending research task 0개"}]}

    retry_total = state.get("retry_count", 0)

    async def run_one(idx: int) -> dict:
        task = tasks[idx]
        settings = policy()
        retry_options = {'retries': settings.tool_max_attempts,
                         'timeout': settings.tool_timeout_seconds}
        if task["kind"] == "monitoring":
            data, retries = await with_retry(
                deps.mcp.call_tool, "get_monitoring_status",
                {"system_name": task["target"]}, **retry_options)
            return {"idx": idx, "source": "monitoring", "data": data, "retries": retries}
        if task['kind'] == 'runbook_search':
            data, retries = await with_retry(deps.mcp.call_tool, 'search_runbooks', {
                'query': task['target'], 'top_k': int(os.getenv('RAG_TOP_K', '1')),
                'queries': task.get('queries')}, retries=settings.tool_max_attempts,
                timeout=settings.retrieval_timeout_seconds)
            usage = None
            if hasattr(deps.fast_llm, 'select_evidence') and data.get('matches'):
                matches, usage = await deps.fast_llm.select_evidence(task['target'], data['matches'])
                data['matches'] = matches
                retained = {d['citation_id'] for d in matches}
                for row in data.get('query_results', []):
                    row['citations'] = [c for c in row.get('citations', []) if c in retained]
            return {'idx': idx, 'source': 'retrieval', 'data': data, 'retries': retries, 'usage': usage}
        # 탐색 실패도 해당 작업만 실패시키고 다른 대상의 성공 결과를 보존한다.
        (runbook_server, runbook_uri_template), discovery_retries = await with_retry(
            deps.mcp.find_resource_template, 'get_incident_runbook', **retry_options)
        uri = runbook_uri_template.format(incident_type=task["target"])
        data, retries = await with_retry(deps.mcp.read_resource, uri, server=runbook_server, **retry_options)
        return {"idx": idx, "source": "runbook", "data": data, "retries": retries + discovery_retries}

    started = time.perf_counter()
    results = await asyncio.gather(*(run_one(i) for i in research_indices), return_exceptions=True)
    elapsed = round(time.perf_counter() - started, 2)

    new_tasks = [dict(t) for t in tasks]
    collected = list(state.get("collected", []))
    events = list(state["events"])
    usages, cost = list(state.get("usage", [])), state.get("cost_usd", 0)
    for idx, r in zip(research_indices, results):
        if isinstance(r, BaseException):
            new_tasks[idx]['status'] = 'failed'
            events.append({'type': 'TaskFailed', 'id': new_tasks[idx]['id'],
                           'target': new_tasks[idx].get('system_name') or new_tasks[idx]['target'],
                           'message': '자료 조회 실패. 성공한 조사 결과만 사용합니다.'})
            continue
        new_tasks[r["idx"]]["status"] = "done"
        retry_total += r['retries']
        if r.get('usage'):
            usages.append({k: v for k, v in r['usage'].items() if k != 'prompt'})
            cost += r['usage'].get('usd') or 0
        if r['source'] == 'retrieval':
            task = new_tasks[r['idx']]
            collected.append({'source': 'retrieval_status', 'system_name': task.get('system_name'),
                              'data': r['data'].get('query_results', [])})
            sources = []
            for doc in r['data'].get('matches', []):
                entry = {**doc, 'source': 'runbook', 'data': doc['content'],
                         'system_name': task.get('system_name'),
                         'embedding_backend': r['data'].get('backend')}
                collected.append(entry)
                sources.append({k: entry.get(k) for k in
                                ['citation_id', 'title', 'path', 'score', 'incident_type', 'system_name']})
            events.append({'type': 'EvidenceRetrieved', 'system_name': task.get('system_name'),
                           'sources': sources, 'backend': r['data'].get('backend'),
                           'model': r['data'].get('model')})
            continue
        entry = {"source": r["source"], "data": r["data"]}
        if r['source'] == 'runbook':
            task = new_tasks[r['idx']]
            entry.update(system_name=task.get('system_name'), citation_id=f"RB:{citation_name(task['target'])}:1",
                         title=task.get('title', task['target']), path=task.get('path'))
        collected.append(entry)
        events.append({"type": "TaskCompleted", "id": new_tasks[r["idx"]]["id"],
                       "source": r["source"], "target": new_tasks[r["idx"]]["target"],
                       "retries": r["retries"]})
    return {"tasks": new_tasks, "collected": collected, "retry_count": retry_total,
            "parallel_elapsed": elapsed, "events": events, "usage": usages, "cost_usd": cost}


def _gap_tasks(state, gaps, prefix):
    """첫 검색과 부족분 재조사에서 대상별 작업 구성을 공유한다."""
    monitors = {c['data']['system_name']: c['data'] for c in state.get('collected', [])
                if c.get('source') == 'monitoring' and isinstance(c.get('data'), dict)}
    targets = state.get('targets') or list(monitors)
    tasks, scheduled = [], set()
    for gap in gaps:
        target = gap['target']
        if target not in known_systems() or (target, gap['kind']) in scheduled:
            continue
        scheduled.add((target, gap['kind']))
        task_id = f'{prefix}-{len(tasks)}'
        if gap['kind'] == 'monitoring':
            tasks.append({'id': task_id, 'agent': 'research', 'kind': 'monitoring',
                          'target': target, 'status': 'pending'})
        elif os.getenv('RAG_ENABLED', 'true').lower() == 'true':
            requested = [q for t in state.get('tasks', []) if t.get('kind') == 'monitoring'
                         and t.get('target') == target for q in t.get('queries', [])]
            query = system_search_text(state['query'], monitors[target], targets, queries=requested)
            # 복합 증상은 실제 plan 모델이 만든 질의를 사용한다.
            tasks.append({'id': task_id, 'agent': 'research', 'kind': 'runbook_search',
                          'system_name': target, 'target': query,
                          'queries': requested or [query], 'status': 'pending'})
        else:
            # RAG를 끈 비교 모드: 고정 유형 선택 대신 현재 문서 목록을 전부 읽는다.
            for path in sorted(runbook_directory().glob('*')):
                if path.is_file() and path.suffix.lower() in ('.md', '.txt'):
                    tasks.append({'id': task_id + '-' + path.name, 'agent': 'research', 'kind': 'runbook',
                                  'target': path.stem, 'system_name': target, 'status': 'pending',
                                  'title': path.stem, 'path': str(path)})
    return tasks


@traceable(name='prepare_evidence_node')
async def prepare_evidence_node(state: AgentState, deps: SupervisorDeps) -> dict:
    # 정상적인 첫 매뉴얼 조회는 추가 조사 상한을 소비하지 않는다.
    gaps = [g for g in evidence_gaps(state) if g['kind'] == 'runbook_search']
    tasks = _gap_tasks(state, gaps, 'task-evidence')
    return {'tasks': state.get('tasks', []) + tasks, 'initial_evidence_prepared': True,
            'events': state['events'] + [{'type': 'EvidenceSearchPlanned', 'task_count': len(tasks)}]}


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    gaps = evidence_gaps(state)
    rounds = state.get('replan_rounds', 0)
    limit = policy().max_research_rounds
    if not gaps or rounds >= limit:
        return {'evidence_gaps': gaps, 'evidence_sufficient': not gaps,
                'events': state['events'] + [{'type': 'ReplanCheck', 'round': rounds,
                    'is_sufficient': not gaps, 'limit_reached': rounds >= limit, 'gaps': gaps}]}
    tasks = _gap_tasks(state, gaps, f'task-gap-{rounds}')
    return {'tasks': state.get('tasks', []) + tasks, 'replan_rounds': rounds + 1,
            'evidence_gaps': gaps, 'evidence_sufficient': False,
            'events': state['events'] + [{'type': 'ReplanCheck', 'round': rounds,
                'is_sufficient': False, 'target': '근거 보완 조사', 'search_count': len(tasks), 'gaps': gaps}]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    summary, usage = await _invoke(deps.fast_llm.analyze, state.get("collected", []))
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            **_usage(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """초안은 검증까지 보류하고, 단계별 진행 이벤트만 사용자에게 전달한다."""
    attempt = state.get("validation_attempts", 0) + 1
    draft, usage = await deps.quality_llm.write(
        state["query"], state.get("collected", []),
        attempt, state.get("validation_feedback"),
        on_token=None)
    return {"draft": draft, "last_prompt": usage.get("prompt"),
            **_usage(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt,
                                          "length": len(draft)}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """형식·측정 수치를 검증한다. 상한 도달은 합격으로 바꾸지 않는다."""
    attempts = state.get("validation_attempts", 0) + 1
    errors = validate_draft(state["draft"], state.get("collected", []))
    semantic = 'not_run'
    usage = None
    if not errors and hasattr(deps.quality_llm, 'judge') and os.getenv('SEMANTIC_VALIDATION', 'true').lower() == 'true':
        verdict, usage = await deps.quality_llm.judge(state['query'], state.get('collected', []), state['draft'])
        semantic = 'passed' if verdict['supported'] else 'rejected'
        if not verdict['supported']:
            errors.extend(verdict.get('issues') or ['보고서의 의미적 근거를 확인할 수 없습니다.'])
    ok = not errors
    exhausted = not ok and attempts >= policy().max_validation_attempts
    verdict = "합격" if ok else "검증 실패(최대 시도 도달)" if exhausted else "불합격"
    feedback = None if ok else "\n".join(errors)
    event = {"type": "Validation", "attempt": attempts, "verdict": verdict,
             "accepted": ok, "exhausted": exhausted, "errors": errors, 'semantic': semantic}
    return {"validation_attempts": attempts, "validation_feedback": feedback,
            "validation_passed": ok, "validation_errors": errors,
            **(_usage(state, usage) if usage else {}),
            "events": state["events"] + [event]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    passed = state.get("validation_passed", False)
    reason = ('insufficient_evidence' if state.get('evidence_gaps') else
              'validation_failed' if state.get('validation_attempts', 0) else 'not_validated')
    final = state["draft"] if passed else verified_fallback(state.get("collected", []), reason=reason)
    if state.get('evidence_gaps'):
        final += '\n\n미확인 사항:\n' + '\n'.join('- ' + g['target'] + ': ' + g['reason'] +
                    (' (' + g['query'] + ')' if g.get('query') else '') for g in state['evidence_gaps'])
    failed = [e['target'] for e in state.get('events', []) if e['type'] == 'TaskFailed']
    if failed:
        final += '\n\n일부 자료를 조회하지 못했습니다: ' + ', '.join(dict.fromkeys(failed)) + '. 위 결과는 확인된 자료에 한정됩니다.'
    sources = list({(c['citation_id'], c.get('system_name')):
        {k: c.get(k) for k in ['citation_id', 'title', 'path', 'score', 'system_name']}
        for c in state.get('collected', []) if c.get('citation_id')}.values())
    return {"final": final, "sources": sources, "validation_passed": passed,
            "events": state["events"] + [{"type": "Finalize", "pipeline": "incident-v2",
                                          "verified": passed}]}


def _after_route(state: AgentState) -> str:
    if state['route'] == 'incident' and (state.get('unknown_targets') or state.get('clarification_text')):
        return 'clarify'
    return "chat_answer" if state["route"] == "chat" else "plan"


async def clarify_node(state: AgentState, deps: SupervisorDeps) -> dict:
    unknown = ', '.join(state.get('unknown_targets', []))
    reason = f'{unknown}은 등록되지 않은 시스템입니다.' if unknown else state.get('clarification_text') or '조사할 시스템을 특정하지 못했습니다.'
    final = reason + ' 확인할 시스템을 알려주세요. 조회 가능: ' + ', '.join(sorted(known_systems()))
    return {'final': final, 'events': state['events'] + [{'type': 'ClarificationRequired'}]}


def _after_plan(state):
    return 'gate' if any(t['agent'] == 'research' for t in state.get('tasks', [])) else 'clarify'


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    if last.get('is_sufficient'):
        return 'analysis'
    return 'finalize' if last.get('limit_reached') else 'research' 


def _after_validate(state: AgentState) -> str:
    done = (state.get("validation_passed", False)
            or state.get("validation_attempts", 0) >= policy().max_validation_attempts)
    return "finalize" if done else "write"


def _after_research(state):
    return 'replan_check' if state.get('initial_evidence_prepared') else 'prepare_evidence'


def _after_prepare(state):
    return 'research' if any(t.get('agent') == 'research' and t.get('status') == 'pending'
                             for t in state.get('tasks', [])) else 'replan_check'


def build_graph(deps: SupervisorDeps, checkpointer=None):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node('clarify', node(clarify_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node('prepare_evidence', node(prepare_evidence_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan", 'clarify': 'clarify'})
    g.add_edge("chat_answer", END)
    g.add_edge('clarify', END)
    g.add_conditional_edges('plan', _after_plan, {'gate': 'gate', 'clarify': 'clarify'})
    g.add_edge("gate", "research")
    g.add_conditional_edges('research', _after_research,
        {'prepare_evidence': 'prepare_evidence', 'replan_check': 'replan_check'})
    g.add_conditional_edges('prepare_evidence', _after_prepare,
        {'research': 'research', 'replan_check': 'replan_check'})
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "research": "research", "finalize": "finalize"})
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
