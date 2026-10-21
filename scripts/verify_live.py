"""명시적으로 실행할 때만 실제 모델을 호출한다. 키와 API 오류 원문은 출력하지 않는다."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / '.env')
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
from agent.rag import RunbookIndex


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['openai', 'ollama', 'all'], default='all')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--local-check', action='store_true')
    parser.add_argument('--retrieval-eval', action='store_true')
    parser.add_argument('--extended-retrieval-eval', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--dynamic-checks', action='store_true')
    args = parser.parse_args()
    if args.dynamic_checks:
        import tempfile
        from agent.rag import Embedder
        rows = []
        providers = ['openai', 'ollama'] if args.backend == 'all' else [args.backend]
        for backend in providers:
            cls = OpenAIFastLLM if backend == 'openai' else OllamaFastLLM
            default = 'gpt-4o-mini' if backend == 'openai' else 'qwen3.5:2b'
            model = cls(os.getenv(backend.upper() + '_FAST_MODEL', default))
            quality_class = OpenAIQualityLLM if backend == 'openai' else OllamaQualityLLM
            quality_default = 'gpt-4o' if backend == 'openai' else 'qwen3:8b'
            quality_model = quality_class(os.getenv(backend.upper() + '_QUALITY_MODEL', quality_default))
            cases = [
                ('natural_followup', '방금 살펴본 곳 한 번 더 살펴봐줄래?',
                 {'targets': ['cache-service'], 'incident_query': 'cache-service 메모리 증가'}),
                ('ambiguous_followup', '거기는 이제 괜찮아?',
                 {'targets': ['cache-service', 'payments-service'], 'incident_query': '두 시스템을 함께 조사'}),
                ('all_previous', '아까 본 대상 전부 다시 확인해줘',
                 {'targets': ['cache-service', 'payments-service'], 'incident_query': '두 시스템을 함께 조사'}),
                ('exclude_target', 'cache-service는 제외하고 payments-service만 점검해줘', {}),
                ('unrelated_chat', '안녕 오늘 기분이 좋아', {'targets': ['cache-service']})]
            for role, query, context in cases:
                try:
                    interpreter = quality_model if context else model
                    result, _ = await interpreter.interpret(query, [], context)
                    ok = (result['targets'] == ['cache-service'] and result['route'] == 'incident' and not result['clarification']
                          if role == 'natural_followup' else bool(result['clarification'])
                          if role == 'ambiguous_followup' else set(result['targets']) == {'cache-service', 'payments-service'} and not result['clarification']
                          if role == 'all_previous' else result['targets'] == ['payments-service']
                          if role == 'exclude_target' else result['route'] == 'chat' and not result['targets'])
                    row = {'component': 'dynamic_behavior', 'backend': backend, 'role': role, 'ok': ok,
                           'targets': result['targets'], 'clarification': result['clarification'], 'route': result['route'],
                           'uses_previous': result.get('uses_previous'), 'target_resolution': result.get('target_resolution'), 'model': interpreter.name}
                except Exception as error:
                    row = {'component': 'dynamic_behavior', 'backend': backend, 'role': role, 'ok': False,
                           'error': type(error).__name__}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                (path/'tls-renewal.txt').write_text('TLS 인증서 만료 대응\n\n인증서 유효 기간과 체인을 확인한다. 만료된 인증서는 갱신하고 HTTPS 연결을 재확인한다.')
                (path/'clock-sync.md').write_text('# NTP 시간 동기화\n- NTP 서버 연결과 시스템 시각 오차를 확인한다.\n- 기준 시간과의 차이가 계속되면 시간 동기화 서비스 상태를 점검한다.')
                (path/'cpu-spike.md').write_text('# CPU 급증\n1. CPU 연산 부하와 프로세스별 사용률을 확인한다.')
                try:
                    query = 'gateway-service TLS 인증서가 만료됐고 NTP 시간 동기화 오차도 발생했어요'
                    queries, _ = await model.search_queries(query)
                    index = RunbookIndex(path, path/'vectors.db', Embedder())
                    result = await index.search(query, 1, queries=queries)
                    predicted = sorted(set(d['incident_type'] for d in result['matches']))
                    row = {'component': 'dynamic_behavior', 'backend': backend, 'role': 'new_documents',
                           'ok': predicted == ['clock-sync', 'tls-renewal'], 'queries': queries, 'documents': predicted}
                except Exception as error:
                    row = {'component': 'dynamic_behavior', 'backend': backend, 'role': 'new_documents',
                           'ok': False, 'error': type(error).__name__}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        (ROOT/'eval/dynamic_verification.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')
        return all(row['ok'] for row in rows)
    if args.status:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434') + '/api/ps')
            print(json.dumps([{'model': m['name'], 'size_vram': m.get('size_vram'),
                               'context_length': m.get('context_length')}
                              for m in response.json().get('models', [])], ensure_ascii=False))
        return True
    if args.extended_retrieval_eval:
        index = RunbookIndex()
        rows = []
        for case in json.loads((ROOT / 'eval/extended_retrieval_cases.json').read_text()):
            result = await index.search(case['query'], 1)
            predicted = sorted(set(d['incident_type'] for d in result['matches']))
            rows.append({**case, 'predicted': predicted, 'exact_match': set(predicted) == set(case['expected']),
                         'scores': [d['score'] for d in result['matches']], 'query_count': result.get('query_count')})
        summary = {'cases': len(rows), 'exact_matches': sum(r['exact_match'] for r in rows),
                   'backend': index.embedder.backend, 'min_score': result.get('min_score'),
                   'cases_detail': rows,
                   'limitation': '추가 수동 회귀 사례이며 독립 성능 평가가 아닙니다. 무관한 주제와 런북 미등록 증상을 포함합니다.'}
        (ROOT / 'eval/extended_retrieval_results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return True
    if args.retrieval_eval:
        from eval.compare_retrieval import main as evaluate
        await evaluate()
        return True
    results = []
    providers = ['openai', 'ollama'] if args.backend == 'all' else [args.backend]
    for backend in providers:
        classes = [OpenAIFastLLM, OpenAIQualityLLM] if backend == 'openai' else [OllamaFastLLM, OllamaQualityLLM]
        defaults = ['gpt-4o-mini', 'gpt-4o'] if backend == 'openai' else ['qwen3.5:2b', 'qwen3:8b']
        for cls, role, default in zip(classes, ['FAST', 'QUALITY'], defaults):
            model = cls(os.getenv(f'{backend.upper()}_{role}_MODEL', default), 0)
            start = time.perf_counter()
            try:
                pieces = []
                answer, usage = await model.request('한국어로 짧게 답하라.', '연결 확인: 정상이라고만 답하세요.',
                                                     stream=True, on_token=pieces.append)
                row = {'backend': backend, 'role': role, 'model': model.model,
                       'ok': bool(answer), 'tokens': usage['tokens'], 'stream_chunks': len(pieces)}
            except Exception as error:
                row = {'backend': backend, 'role': role, 'model': model.model,
                       'ok': False, 'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
            row['elapsed_seconds'] = round(time.perf_counter() - start, 2)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    try:
        index = RunbookIndex()
        matches = await index.search('캐시 프로세스의 힙이 계속 커지고 OOM이 걱정됩니다', 2)
        row = {'component': 'rag', 'backend': index.embedder.backend, 'model': index.embedder.model,
               'ok': bool(matches['matches']), 'matches': [d['incident_type'] for d in matches['matches']]}
    except Exception as error:
        row = {'component': 'rag', 'ok': False,
               'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
    results.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.full or args.local_check:
        # 별도 포트 없이 실제 FastAPI + MCP + 모델 전체를 실행한다.
        import tempfile
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        import server
        from agent.session_store import SqliteSessionStore
        for backend in providers:
            def request():
                with tempfile.TemporaryDirectory() as directory:
                    store = SqliteSessionStore(Path(directory) / 'sessions.db')
                    with patch.dict(os.environ, {'MONITORING_HISTORY_PATH': directory + '/monitoring.db'}), \
                         patch.object(server, 'SqliteSessionStore', return_value=store), TestClient(server.app) as client:
                        return client.post('/chat', json={
                            'message': ('local-host CPU와 메모리 상태를 확인하고 이상 여부를 진단해줘'
                                if args.local_check else 'payments-service CPU 급증과 cache-service 메모리 증가를 함께 진단해줘'),
                            'backend': backend, 'thread_id': f'verify-{backend}'}).json()
            start = time.perf_counter()
            try:
                data = await asyncio.to_thread(request)
                metadata = data['metadata']
                local_ok = not args.local_check or (bool(metadata.get('monitoring')) and all(
                    m.get('data_origin') == 'local' for m in metadata['monitoring']))
                row = {'component': 'local_pipeline' if args.local_check else 'full_pipeline',
                       'backend': backend, 'ok': bool(metadata['validation_passed']) and local_ok,
                       'validation_attempts': metadata['validation_attempts'],
                       'replan_rounds': metadata.get('replan_rounds'),
                       'source_count': len(metadata['sources']), 'reply': data['reply'],
                       'events': data['events'], 'elapsed_seconds': round(time.perf_counter() - start, 2)}
            except Exception as error:
                row = {'component': 'local_pipeline' if args.local_check else 'full_pipeline', 'backend': backend, 'ok': False,
                       'error': str(error) if isinstance(error, RuntimeError) else type(error).__name__}
            results.append(row)
            print(json.dumps({k: v for k, v in row.items() if k not in ['reply', 'events']}, ensure_ascii=False), flush=True)
    output = ROOT / 'eval/live_verification.json'
    previous = json.loads(output.read_text()) if output.exists() else []
    def identity(row):
        return row.get('component', 'model'), row.get('backend'), row.get('role')
    merged = {identity(row): row for row in previous}
    for row in results:
        row['checked_at'] = datetime.now(timezone.utc).isoformat()
        merged[identity(row)] = row
    output.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2) + '\n')
    return all(row['ok'] for row in results)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
