"""후속 질문의 대상은 명시적인 이전 조사 대상에서만 복원한다."""
from .settings import policy
from .llm_common import mentioned_systems


def compact_history(history, budget=None):
    budget = budget or policy().history_chars
    recent = history[-policy().history_messages:]
    chosen, length = [], 0
    for message in reversed(recent):
        if length + len(message['content']) > budget:
            break
        chosen.insert(0, message)
        length += len(message['content'])
    # 특정 이름 표현 대신 초기 사용자 발화도 남는 예산 내에서 보존한다.
    for message in reversed([m for m in history[:-policy().history_messages] if m['role'] == 'user'][:2]):
        if length + len(message['content']) <= budget:
            chosen.insert(0, message)
            length += len(message['content'])
    return chosen


# 이전 테스트 코드용 이름. 실제 모델의 해석에는 사용하지 않는다.
from .fake_llm import resolve_demo_query as resolve_query


def next_context(previous, original, result):
    context = dict(previous)
    if any(e['type'] == 'ClarificationRequired' for e in result.get('events', [])):
        context['pending_query'] = original
    elif result.get('route') == 'incident':
        targets = result.get('targets') or mentioned_systems(result.get('query', original))
        context.update(targets=targets, incident_query=result.get('query', original), pending_query=None)
    return context
