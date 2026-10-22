"""결정론적 테스트용 모델. 토큰은 문자 수 기반 모의 추정이며 금액은 계산하지 않는다."""
import re

from langsmith import traceable


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": None, "pricing": "simulated", "tokens_are_estimated": True}


class FastLLM(FakeLLM):
    """분류·계획 등 짧고 정형적인 단계 전담 (저비용)."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        """Wave 5 — 쿼리 라우팅. 장애 관련이면 incident 파이프라인, 아니면 chat 직행."""
        incident = bool(re.search(
            r"장애|진단|다운|지연|저하|급증|느려|치솟|메모리|디스크|타임아웃|"
            r"CPU|모니터링|표본|측정|상태|incident|티켓|latency|timeout|memory|disk", query, re.I))
        result = {"route": "incident" if incident else "chat", "needs_analysis": False}
        # 외부 모델 없는 데모에서만 사용하는 간단한 요청 범위 분류.
        symptoms = bool(re.search(r'느려|지연|오류|장애|급증|계속 증가|누수|OOM|타임아웃', query, re.I))
        result['response_context'] = {'request_kind': 'status' if not symptoms and re.search(r'상태|측정|표본|사용률', query) else 'diagnostic',
                                      'reported_symptoms': symptoms,
                                      'compare_history': bool(re.search(r'이전|표본|기록', query) and re.search(r'비교|차이|변화', query))}
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        """Wave 4 — Plan/Task 분해. MCP Prompt가 준 표준 절차를 따라 계획을 세운다.

        Wave 6 재현: needs_analysis=False라고 스스로 판단해 놓고도 analysis task를
        끼워 넣는 '모델의 자기모순'을 결정론적으로 재현한다 → gate 노드가 걸러낸다.
        """
        from .llm_common import mentioned_systems
        systems = mentioned_systems(query)
        needs_analysis = len(set(systems)) > 1 and bool(re.search(r"공통|비교|분석|패턴", query))
        tasks = [
            {"id": f"task-{i}", "agent": "research", "kind": "monitoring",
             "target": s, "status": "pending"}
            for i, s in enumerate(dict.fromkeys(systems))
        ]
        tasks.append({"id": "task-analysis", "agent": "analysis",
                      "kind": "pattern-analysis", "status": "pending"})   # 자기모순 task
        result = {"needs_analysis": needs_analysis, "tasks": tasks}
        return result, self._usage(query + procedure_prompt, str(result))

    @traceable(run_type="llm", name="fast_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """Wave 6 보완 — 여러 시스템의 monitoring 결과 사이에 공통 패턴이 있는지 비교."""
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        degraded = [d["system_name"] for d in mon if d.get("status") != "healthy"]
        if len(degraded) >= 2:
            summary = (f"{', '.join(degraded)} 복수 시스템이 동시에 저하됨 — "
                       f"공유 의존성 등 공통 원인이 의심됨")
        elif degraded:
            summary = f"{degraded[0]}만 단독 저하 — 연관 시스템은 정상, 국소 이슈로 추정"
        else:
            summary = "모니터링된 시스템 모두 healthy — 패턴상 이상 징후 없음"
        return summary, self._usage(str(collected), summary)

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """Wave 5의 direct 경로 + Wave 12 증명용 — 히스토리에서 이름을 기억해낸다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                if on_token is not None:
                    on_token(reply)
                return reply, self._usage(query, reply)
        m = re.search(r"제 이름은 (\S+?)입니다", query)
        if m:
            reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
        else:
            reply = "안녕하세요! 무엇을 도와드릴까요?"
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


class QualityLLM(FakeLLM):
    """최종 답변 작성 전담 (고품질·고비용)."""

    @traceable(run_type="llm", name="quality_llm.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
              feedback: str | None, on_token=None) -> tuple[str, dict]:
        """Wave 8 재현: 1차 초안은 의도적으로 저품질(너무 짧음) → validator가 반려하면
        피드백을 반영한 구조화된 2차 초안을 내놓는다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        if attempt == 1:
            draft = "재시작하면 됩니다."
            if on_token is not None:
                on_token(draft)
            return draft, self._usage(query, draft)

        # 실제 모델과 같은 원문 단계·보고서 조립기를 사용해 형식이 어긋나지 않게 한다.
        from .evidence import runbook_steps, render_report
        from .settings import policy
        steps = list(runbook_steps(collected))
        abnormal = any(c.get('source') == 'monitoring' and c['data'].get('status') != 'healthy'
                       for c in collected)
        limit = policy().max_selected_steps
        diagnosis = '현재 관측과 조회한 매뉴얼을 확인했습니다. 원인 확정에는 추가 확인이 필요합니다.'
        comparisons = [c['data'] for c in collected if c.get('source') == 'analysis']
        if comparisons:
            diagnosis += ' ' + ' '.join(comparisons)
        report = {'diagnosis': diagnosis,
                  'action_steps': steps[:limit] if abnormal else [],
                  'check_steps': []}
        draft = render_report(report, collected)
        if on_token is not None:
            on_token(draft)
        return draft, self._usage(query + str(feedback), draft)


def resolve_demo_query(query, context):
    from .llm_common import mentioned_systems, unknown_systems
    """외부 모델 없는 fake/v1 전용. 실제 모델 경로는 interpret가 의미로 해석한다."""
    explicit = mentioned_systems(query)
    remainder = query.lower()
    for target in explicit:
        remainder = remainder.replace(target, '')
    target_answer = bool(explicit) and bool(re.fullmatch(r'[\s,와과랑로을를요야이입니다.!?]*', remainder))
    if target_answer and context.get('pending_query'):
        pending = context['pending_query']
        for unknown in unknown_systems(pending):
            pending = pending.replace(unknown, '')
        return query + '\n이전 요청의 대상 지정: ' + pending, None
    followup = bool(re.search(r'그\s*(?:서버|서비스|시스템)|다시\s*(?:확인|진단|조사)|아까.*(?:서버|장애)|이어서', query))
    if not explicit and followup:
        targets = context.get('targets', [])
        if len(targets) == 1:
            return f"{targets[0]}: {query}\n이전 장애 맥락: {context.get('incident_query', '')[:500]}", None
        return query, '이전 조사 대상이 여러 개이거나 없습니다. 확인할 시스템 이름을 지정해주세요.'
    return query, None

