"""실제 모델·MCP 답변 점검. 운영 대화와 분리한 임시 DB를 사용한다."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false', RAG_ENABLED='false')
from fastapi.testclient import TestClient
import server
from agent.session_store import SqliteSessionStore

CASES = [
    ('local_status', '실제 로컬 상태', 'local', 'local-host 메모리 상태', ['local-host'], 'diagnosis'),
    ('implicit_followup', '생략된 대상 연결', 'local', '그 서버 다시 확인해줘', ['local-host'], 'diagnosis'),
    ('compare', '실제 이전 표본 비교', 'local', 'local-host 현재 측정값을 이전에 저장된 표본과 비교해줘', ['local-host'], 'compare'),
    ('healthy_symptom', '정상 지표지만 증상 보고', 'symptom', 'web-01에서 응답이 느려요. CPU와 메모리 상태를 확인해줘', ['web-01'], 'symptom'),
    ('cpu', 'CPU 장애', 'cpu', 'payments-service CPU가 급증했어요. 원인 후보와 대응 방법을 알려줘', ['payments-service'], 'diagnosis'),
    ('memory', '메모리 증가', 'memory', 'cache-service 메모리 사용량이 계속 증가합니다. 무엇을 확인해야 해?', ['cache-service'], 'diagnosis'),
    ('multi', '복수 대상·복합 증상', 'multi', 'payments-service CPU 급증과 cache-service 메모리 증가를 함께 진단해줘', ['payments-service', 'cache-service'], 'diagnosis'),
    ('ambiguous', '복수 대상 후 모호한 지칭', 'multi', '그 서버 다시 확인해줘', [], 'clarify'),
    ('unknown', '미등록 대상', 'unknown', 'billing-service 메모리 상태를 확인해줘', [], 'clarify'),
    ('missing', '대상 누락', 'missing', '메모리 상태를 확인해줘', [], 'clarify'),
    ('greeting', '장애 대화 후 일반 대화', 'multi', '안녕, 고마워', [], 'chat'),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['openai', 'ollama'], default='openai')
    parser.add_argument('--cases', nargs='+', choices=[row[0] for row in CASES])
    parser.add_argument('--output', default='response_check.json')
    args = parser.parse_args()
    if Path(args.output).name != args.output or not args.output.endswith('.json'):
        parser.error('--output은 eval 폴더에 저장할 JSON 파일 이름이어야 합니다.')
    rows = []
    with tempfile.TemporaryDirectory(prefix='response-check-') as directory:
        store = SqliteSessionStore(Path(directory) / 'session.db')
        env = {'MONITORING_HISTORY_PATH': directory + '/monitoring.db',
               'RAG_INDEX_PATH': directory + '/unused-index.db'}
        with patch.dict(os.environ, env), patch.object(server, 'SqliteSessionStore', return_value=store), TestClient(server.app) as client:
            for case_id, label, thread, query, targets, kind in CASES:
                if args.cases and case_id not in args.cases:
                    continue
                started = time.perf_counter()
                row = {'id': case_id, 'label': label, 'query': query, 'expected_targets': targets, 'kind': kind}
                try:
                    response = client.post('/chat', json={'message': query, 'thread_id': 'check-' + thread, 'backend': args.backend})
                    response.raise_for_status()
                    data = response.json()
                    meta, events, reply = data['metadata'], data['events'], data['reply']
                    monitors = meta.get('monitoring', [])
                    actual = sorted(m['system_name'] for m in monitors)
                    checks = {'target_scope': actual == sorted(targets)}
                    if kind in ('diagnosis', 'compare', 'symptom'):
                        checks['validated'] = meta.get('validation_passed') is True
                        checks['no_fallback'] = not reply.startswith(('답변이 검증 기준', '진단에 필요한 근거가 부족', '진단 보고서 검증'))
                        checks['has_metrics'] = '| 대상 | 항목 |' in reply
                        healthy = all(m.get('status') == 'healthy' and not m.get('alerts') for m in monitors)
                        if case_id == 'local_status' and healthy:
                            checks['normal_uses_code'] = any(e['type'] == 'Validation' and e.get('semantic') == 'deterministic' for e in events)
                            checks['no_unnecessary_risk'] = not any(word in reply for word in ['누수 가능', 'OOM 위험', '힙 덤프'])
                        if kind == 'symptom':
                            checks['symptom_not_dismissed'] = '보고된 증상은 미확인' in reply
                            checks['semantic_validation'] = any(e['type'] == 'Validation' and e.get('semantic') == 'passed' for e in events)
                        if kind == 'compare':
                            checks['previous_values_shown'] = '변화(%p)' in reply and '이전 표본:' in reply
                        checks['followups_available'] = bool(meta.get('followups'))
                    elif kind == 'clarify':
                        checks['asks_for_target'] = any(e['type'] == 'ClarificationRequired' for e in events)
                        checks['no_diagnosis'] = not monitors and meta.get('validation_attempts') == 0
                        if case_id == 'unknown':
                            checks['unknown_is_explicit'] = 'billing-service' in reply and '등록되지 않은' in reply
                    else:
                        checks['chat_route'] = meta.get('route') == 'chat'
                    row.update(checks=checks, passed=all(checks.values()), reply=reply, metadata=meta, events=events)
                except Exception as error:
                    row.update(passed=False, error=type(error).__name__)
                row['elapsed_seconds'] = round(time.perf_counter() - started, 3)
                rows.append(row)
                print(json.dumps({k: row.get(k) for k in ['id', 'passed', 'elapsed_seconds', 'checks', 'error']}, ensure_ascii=False), flush=True)
    result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'backend': args.backend,
              'scope': '각 사례 1회 실행한 현재 동작 점검이며 개선 전후 비교·운영 정확도 추정이 아닙니다.',
              'total': len(rows), 'passed': sum(row['passed'] for row in rows), 'results': rows}
    (ROOT / 'eval' / args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ['total', 'passed', 'backend']}, ensure_ascii=False), flush=True)
    return 0 if result['passed'] == result['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
