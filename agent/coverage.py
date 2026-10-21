"""요청한 모든 대상과 검색 질의가 실제 수집 근거로 채워졌는지 검사한다."""
import os
from .validation import valid_runbook


def evidence_gaps(state):
    collected = state.get('collected', [])
    monitors = {c['data']['system_name']: c['data'] for c in collected
                if c.get('source') == 'monitoring' and isinstance(c.get('data'), dict)
                and c['data'].get('status') != 'unknown'}
    targets = state.get('targets') or list(dict.fromkeys(t['target'] for t in state.get('tasks', [])
        if t.get('kind') == 'monitoring')) or list(monitors)
    gaps = []
    for target in targets:
        if target not in monitors:
            gaps.append({'target': target, 'kind': 'monitoring', 'reason': '모니터링 조회 결과가 없습니다.'})
            continue
        documents = [c for c in collected if c.get('source') == 'runbook' and valid_runbook(c.get('data'))
                     and (c.get('system_name') == target or len(targets) == 1 and not c.get('system_name'))]
        if not documents:
            gaps.append({'target': target, 'kind': 'runbook_search', 'reason': '관련 매뉴얼 근거가 없습니다.'})
            continue
        citations = {d.get('citation_id') for d in documents}
        queries = {q for t in state.get('tasks', []) if t.get('kind') == 'monitoring' and t.get('target') == target
                   for q in t.get('queries', [])}
        results = {}
        for item in collected:
            if item.get('source') == 'retrieval_status' and item.get('system_name') == target:
                for row in item.get('data', []):
                    results.setdefault(row['query'], set()).update(row.get('citations', []))
        if os.getenv('RAG_ENABLED', 'true').lower() != 'true':
            continue  # 비검색 모드는 현재 문서를 전부 읽고 작성·의미 검증 단계에서 선택한다.
        for query in queries | set(results):
            if not results.get(query, set()) & citations:
                gaps.append({'target': target, 'kind': 'runbook_search', 'query': query,
                             'reason': '요청 증상에 대한 검색 근거를 확보하지 못했습니다.'})
    if not targets:
        gaps.append({'target': '대상 미확인', 'kind': 'monitoring', 'reason': '확인된 조사 대상이 없습니다.'})
    return gaps
