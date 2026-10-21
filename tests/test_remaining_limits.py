import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from agent.conversation import resolve_query, compact_history
from agent.evidence import render_report
from agent.rag import RunbookIndex, Embedder
from agent.http_llm import HTTPModel
from agent.monitoring import _prometheus, get_status
from agent.retrieval_policy import system_search_text
from agent.validation import validate_draft


class EvidenceTests(unittest.TestCase):
    def test_conditions_are_preserved_and_unknown_steps_fail(self):
        data = [{'source': 'runbook', 'data': '# CPU\n1. CPU가 90%를 넘고 지속되면 부하를 분산한다.',
                 'citation_id': 'RB:cpu:1'}]
        report = {'diagnosis': '부하 증가 가능성을 확인해야 합니다.',
                  'action_steps': ['RB:cpu:1:S1'], 'check_steps': []}
        self.assertIn('CPU가 90%를 넘고 지속되면', render_report(report, data))
        report['action_steps'] = ['RB:invented:1:S1']
        self.assertNotIn('1)', render_report(report, data))

    def test_heap_is_not_system_memory(self):
        collected = [{'source': 'monitoring', 'data': {'system_name': 'cache-service',
                     'memory_percent': 80, 'cpu_percent': 20, 'status': 'degraded'}}]
        self.assertTrue(any('힙 사용률' in error for error in validate_draft(
            '1) cache-service 힙 80% 사용 중입니다. 2) 확인합니다.', collected)))

    def test_followup_does_not_guess_ambiguous_target(self):
        context = {'targets': ['cache-service'], 'incident_query': '메모리 증가'}
        query, error = resolve_query('그 서버 다시 확인해줘', context)
        self.assertIsNone(error)
        self.assertIn('cache-service', query)
        self.assertIn('메모리 증가', query)
        context['targets'].append('payments-service')
        self.assertIsNotNone(resolve_query('그 서버 다시 확인해줘', context)[1])

    def test_clarification_retains_symptom_and_history_budget(self):
        query, _ = resolve_query('cache-service', {'pending_query': 'billing-service 메모리 증가 진단'})
        self.assertIn('메모리 증가', query)
        self.assertNotIn('billing-service', query)
        history = [{'role': 'user', 'content': '제 이름은 민수입니다'}] + [
            {'role': 'assistant', 'content': 'x' * 5000} for _ in range(20)]
        compact = compact_history(history)
        self.assertIn('민수', compact[0]['content'])
        self.assertLess(len(str(compact)), 12500)

    def test_multi_target_queries_do_not_leak_other_symptoms(self):
        query = 'payments-service CPU 급증과 cache-service 메모리 증가'
        local = system_search_text(query, {'system_name': 'cache-service', 'status': 'degraded',
                                          'alerts': ['memory high']}, ['payments-service', 'cache-service'])
        self.assertNotIn('CPU', local)


class IntegrationUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_compound_symptoms_and_query_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cpu.md').write_text('# CPU 급증과 과부하\n1. CPU 프로세서 코어 연산 부하를 확인한다.')
            (root / 'memory.md').write_text('# 메모리 누수와 힙 증가\n1. 메모리 누수 객체 힙 OOM을 점검한다.')
            index = RunbookIndex(root, root / 'index.db', Embedder('local'))
            result = await index.search('CPU 급증과 메모리 누수', 1)
            self.assertGreaterEqual(result['query_count'], 2)
            self.assertEqual({m['incident_type'] for m in result['matches']}, {'cpu', 'memory'})
            with patch.object(index.embedder, 'embed', side_effect=AssertionError('cache miss')):
                await index.search('CPU 급증과 메모리 누수', 1)
            with patch.dict(os.environ, {'RAG_MIN_SCORE': '1.01'}):
                self.assertEqual((await index.search('관련 없는 요청'))['matches'], [])

    async def test_local_metrics_are_real_and_labelled(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'MONITORING_BACKEND': 'local', 'MONITORING_HISTORY_PATH': directory + '/history.db'}):
            data = await get_status('local-host')
            self.assertEqual(data['data_origin'], 'local')
            for key in ['cpu_percent', 'memory_percent', 'disk_percent']:
                self.assertTrue(0 <= data[key] <= 100)
            self.assertEqual(len(data['recent_samples']), 1)
            with self.assertRaises(ValueError):
                await get_status('auth-service')

    async def test_prometheus_requires_single_fresh_finite_series(self):
        factory = httpx.AsyncClient
        config = {'queries': {'up': 'up', 'cpu_percent': 'cpu', 'memory_percent': 'mem'}}
        for values, valid in [([[time.time(), '20']], True), ([], False),
                              ([[time.time()-200, '20']], False), ([[time.time(), 'NaN']], False)]:
            def handle(request):
                samples = [[time.time(), '1']] if request.url.params['query'] == 'up' else values
                return httpx.Response(200, json={'status': 'success', 'data': {'result': [
                    {'value': v} for v in samples]}})
            with patch.dict(os.environ, {'PROMETHEUS_URL': 'http://metrics.test'}), patch(
                'agent.monitoring.httpx.AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handle), **kw)):
                if valid:
                    self.assertEqual((await _prometheus(config))['data_origin'], 'prometheus')
                else:
                    with self.assertRaises(RuntimeError):
                        await _prometheus(config)

    async def test_retry_transient_and_do_not_retry_after_stream_output(self):
        class Model(HTTPModel):
            provider = 'openai'
        factory = httpx.AsyncClient
        count = 0
        async def handle(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={'choices': [{'message': {'content': '정상'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test', 'MODEL_PRICES_JSON': '{}'}), patch(
            'agent.http_llm.httpx.AsyncClient', side_effect=lambda **kw: factory(transport=httpx.MockTransport(handle), **kw)):
            reply, usage = await Model('test', 99).request('system', 'user')
            self.assertEqual(count, 2)
            self.assertIsNone(usage['usd'])
            with patch.dict(os.environ, {'MODEL_PRICES_JSON': '{"test":{"input_per_million":1,"output_per_million":2}}'}):
                _, usage = await Model('test', 99).request('s', 'u')
                self.assertAlmostEqual(usage['usd'], .00002)
        from agent.http_llm import TransientModelError
        class Broken(Model):
            calls = 0
            async def _request(self, system, user, schema, stream, on_token, token_limit):
                self.calls += 1
                on_token('일부')
                raise TransientModelError()
        broken = Broken('test', 0)
        with self.assertRaises(RuntimeError):
            await broken.request('s', 'u', stream=True)
        self.assertEqual(broken.calls, 1)


class PersistenceTests(unittest.TestCase):
    def test_interrupted_graph_resumes_after_restart_without_rerouting(self):
        from fastapi.testclient import TestClient
        import server
        from agent.session_store import SqliteSessionStore
        from agent.fake_llm import FastLLM, QualityLLM
        route_calls = []
        original_route, original_write = FastLLM.route, QualityLLM.write
        def counted_route(model, query):
            route_calls.append(query)
            return original_route(model, query)
        async def broken_write(*args, **kwargs):
            raise RuntimeError('test failure at write')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.db'
            settings = {'LLM_BACKEND': 'fake', 'SUPERVISOR_VERSION': 'v2',
                        'RAG_EMBEDDING_BACKEND': 'local', 'RAG_MIN_SCORE': '0', 'RAG_ENABLED': 'true',
                        'MONITORING_HISTORY_PATH': directory+'/monitoring.db',
                        'RAG_INDEX_PATH': directory+'/rag.db'}
            for phase in ['fail', 'resume']:
                store = SqliteSessionStore(path)
                write = broken_write if phase == 'fail' else original_write
                with patch.dict(os.environ, settings), patch.object(server, 'SqliteSessionStore', return_value=store), \
                     patch.object(FastLLM, 'route', counted_route), patch.object(QualityLLM, 'write', write), \
                     TestClient(server.app) as client:
                    if phase == 'fail':
                        with self.assertRaises(RuntimeError):
                            client.post('/chat', json={'message': 'cache-service 메모리 장애 진단', 'thread_id': 'resume'})
                        pending = client.get('/sessions/resume/messages').json()
                        self.assertEqual(pending['messages'], [])
                        run_id = pending['pending_runs'][0]['id']
                    else:
                        response = client.post(f'/runs/{run_id}/resume', params={'thread_id': 'resume'})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(response.json()['metadata']['validation_passed'])
                        restored = client.get('/sessions/resume/messages').json()
                        self.assertEqual(len(restored['messages']), 2)
                        self.assertTrue(restored['messages'][1]['metadata']['sources'])
                        self.assertEqual(restored['pending_runs'], [])
                        self.assertEqual(client.post(f'/runs/{run_id}/resume', params={'thread_id': 'resume'}).status_code, 409)
                        self.assertEqual(client.post(f'/runs/{run_id}/resume', params={'thread_id': 'other'}).status_code, 404)
                        followup = client.post('/chat', json={'message': '그 서버 다시 확인해줘', 'thread_id': 'resume'}).json()
                        self.assertIn('cache-service', followup['reply'])
            self.assertEqual(len(route_calls), 2)  # 최초 요청 + 후속 질문; 복구는 route를 반복하지 않음


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_rejection_cannot_be_forced_to_pass(self):
        from agent.graph import validate_node
        from agent.deps import SupervisorDeps
        class Judge:
            async def judge(self, *args):
                return {'supported': False, 'issues': ['원인이 확인되지 않았는데 확정했습니다.']}, {'usd': None, 'tokens': 10}
        collected = [{'source': 'monitoring', 'data': {'system_name': 'auth-service',
                     'status': 'degraded', 'cpu_percent': 78, 'memory_percent': 61}},
                     {'source': 'runbook', 'data': '# CPU\n1. 배포를 확인한다.', 'citation_id': 'RB:cpu:1'}]
        state = {'query': 'auth-service 장애', 'draft': '1) auth-service CPU 78%, 메모리 61%입니다.\n2) 배포가 원인입니다. [RB:cpu:1]',
                 'collected': collected, 'events': [], 'validation_attempts': 2}
        with patch.dict(os.environ, {'SEMANTIC_VALIDATION': 'true'}):
            result = await validate_node(state, SupervisorDeps(None, Judge(), None, None, 'v2'))
        self.assertFalse(result['validation_passed'])
        self.assertTrue(result['events'][-1]['exhausted'])
        self.assertEqual(result['events'][-1]['semantic'], 'rejected')


class BoundaryTests(unittest.TestCase):
    def test_long_runbook_keeps_each_step_with_its_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            steps = [f'{i}. 조건을 확인한다.\n\n' + '증거를 수집하고 담당자에게 확인한다. ' * 35 + f'\n조건{i}이 충족될 때만 조치한다.' for i in range(1, 8)]
            (root / 'long.md').write_text('# 긴 매뉴얼\n' + '\n'.join(steps))
            chunks = RunbookIndex(root, root/'cache.db', Embedder('local')).chunks()
            self.assertGreater(len(chunks), 1)
            for step in steps:
                self.assertEqual(sum(step in chunk['content'] for chunk in chunks), 1)

    def test_model_cannot_invent_target_and_disk_values_cannot_be_swapped(self):
        from agent.llm_common import normalize_plan
        result = normalize_plan({'tasks': [{'agent': 'research', 'target': 'auth-service'}]}, '원인을 진단해줘')
        self.assertEqual(result['tasks'], [])
        data = [{'source': 'monitoring', 'data': {'system_name': 'local-host', 'status': 'degraded',
                 'cpu_percent': 10, 'memory_percent': 60, 'disk_percent': 95}},
                {'source': 'runbook', 'data': '# 디스크\n1. 용량을 확인한다.'}]
        self.assertFalse(validate_draft('1) local-host 디스크 95%입니다.\n2) 원인을 추가로 확인하세요.', data))
        self.assertTrue(validate_draft('1) local-host 디스크 60%입니다.\n2) 원인을 추가로 확인하세요.', data))

    def test_legacy_db_migration_and_history_pagination(self):
        import sqlite3
        from agent.session_store import SqliteSessionStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session.db'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE messages (thread_id TEXT,role TEXT,content TEXT,created TEXT)')
                connection.execute("INSERT INTO messages VALUES ('old','user','과거 내용','2026-10-20')")
            store = SqliteSessionStore(path)
            try:
                self.assertEqual(store.load('old')[0]['content'], '과거 내용')
                for i in range(150):
                    store.append('old', 'user', str(i))
                recent = store.history('old')
                earlier = store.history('old', before=recent[0]['id'])
                self.assertEqual(len(recent), 100)
                self.assertEqual(len(earlier), 51)
                self.assertEqual(earlier[0]['content'], '과거 내용')
                self.assertTrue(all(m['id'] < recent[0]['id'] for m in earlier))
            finally:
                store.close()
