"""고정 장애 유형 없이 검색 질의를 구성한다. 실서비스의 질의 분해는 모델이 담당한다."""
import re
from .settings import policy


def expand_queries(query):
    """외부 모델 없는 데모용 문장 분리. 장애 종류나 문서 ID를 매핑하지 않는다."""
    pieces = re.split(r'\s*(?:[,;\n]|(?:그리고|동시에|및)\s+|[과와]\s+)\s*', query)
    return list(dict.fromkeys(p.strip() for p in pieces if p.strip()))[:policy().max_search_queries] or [query]


def system_search_text(query, monitor, targets, queries=None):
    # 실모델이 확정한 대상별 질의가 있으면 문자열 위치로 다시 추측하지 않는다.
    if queries:
        local = '\n'.join(queries)
        return (f"시스템 {monitor['system_name']}, 상태 {monitor['status']}. "
                + ' '.join(monitor.get('alerts', [])) + '\n해당 시스템 요청: ' + local)
    # 예전 체크포인트에 저장된 '대상 목록: 원래 질문'도 처리한다.
    header, separator, body = query.partition(':')
    if separator and set(p.strip() for p in header.split(',')) <= set(targets):
        query = body.strip()
    local = query
    if len(targets) > 1:
        positions = sorted((query.lower().find(name.lower()), name) for name in targets
                           if name.lower() in query.lower())
        local = ''
        for i, (start, name) in enumerate(positions):
            if name == monitor['system_name']:
                end = positions[i+1][0] if i+1 < len(positions) else len(query)
                local = query[start:end]
                break
    return (f"시스템 {monitor['system_name']}, 상태 {monitor['status']}. "
            + ' '.join(monitor.get('alerts', [])) + '\n해당 시스템 요청: ' + local)
