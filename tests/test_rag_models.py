import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from agent.rag import RunbookIndex, Embedder
from agent.llm_common import normalize_plan, ROUTE_SCHEMA
from agent.openai_llm import OpenAIFastLLM
from agent.ollama_llm import OllamaFastLLM
from agent.validation import validate_draft
from agent.llm_common import QualityModelMixin, render_facts


class RAGTests(unittest.IsolatedAsyncioTestCase):
    async def test_index_refreshes_when_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / 'cpu.md'
            doc.write_text('# CPU 급증\nCPU 사용률이 높고 배치 작업이 반복되면 확인합니다.')
            embedder = Embedder('local')
            index = RunbookIndex(root, root / 'index.db', embedder)
            first = await index.search('CPU 급증 배치 작업', 1)
            self.assertEqual(first['matches'][0]['incident_type'], 'cpu')
            self.assertTrue(first['matches'][0]['citation_id'].startswith('RB:cpu:'))
            doc.write_text('# 변경된 절차\n최근 배포 변경 사항을 우선 확인합니다.')
            second = await index.search('최근 배포 변경', 1)
            self.assertEqual(second['matches'][0]['title'], '변경된 절차')
            self.assertNotIn('CPU 사용률', second['matches'][0]['content'])

    async def test_empty_collection_does_not_invent_source(self):
        with tempfile.TemporaryDirectory() as directory:
            index = RunbookIndex(directory, Path(directory) / 'index.db', Embedder('local'))
            self.assertEqual((await index.search('장애'))['matches'], [])

    async def test_cached_documents_do_not_reembed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'a.md').write_text('# 매뉴얼\nCPU 확인 절차')
            class RecordingEmbedder(Embedder):
                def __init__(self):
                    super().__init__('local')
                    self.calls = []
                async def embed(self, texts):
                    self.calls.append(texts)
                    return await super().embed(texts)
            embedder = RecordingEmbedder()
            index = RunbookIndex(root, root / 'cache.db', embedder)
            await index.search('CPU')
            await index.search('메모리')
            self.assertEqual(len(embedder.calls), 3)  # 문서 1회 + 질의 2회


class PlanningAndCitationTests(unittest.TestCase):
    def test_partial_model_plan_restores_all_requested_targets(self):
        result = normalize_plan({'needs_analysis': True, 'tasks': [
            {'agent': 'research', 'target': 'payments-service'},
            {'agent': 'research', 'target': 'web-01'}]},
            'payments-service와 cache-service를 비교해줘')
        self.assertEqual([t['target'] for t in result['tasks'] if t['agent'] == 'research'],
                         ['payments-service', 'cache-service'])

    def test_unknown_target_is_not_silently_replaced(self):
        result = normalize_plan({'tasks': []}, 'billing-service 장애')
        self.assertEqual(result['tasks'], [])

    def test_unknown_or_missing_citations_are_rejected(self):
        collected = [{'source': 'monitoring', 'data': {'system_name': 'auth-service',
                     'status': 'degraded', 'cpu_percent': 78, 'memory_percent': 61}},
                     {'source': 'runbook', 'data': '# 대응 절차\n1. 배포를 확인합니다.',
                      'citation_id': 'RB:cpu-spike:1'}]
        base = '1) auth-service: CPU 78%, 메모리 61%입니다.\n2) 최근 배포를 확인하세요.'
        self.assertTrue(validate_draft(base, collected))
        self.assertTrue(validate_draft(base + ' [RB:fake-doc:1]', collected))
        self.assertFalse(validate_draft(base + ' [RB:cpu-spike:1]', collected))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_uses_observed_numbers_and_model_citations(self):
        class ReportModel(QualityModelMixin):
            async def request(self, system, user, **kwargs):
                self.prompt = user
                return json.dumps({'diagnosis': '최근 배포 영향이 의심됩니다.',
                    'action_steps': ['RB:cpu-spike:1:S1'],
                    'check_steps': []}), {'usd': 0}
        model = ReportModel()
        collected = [{'source': 'monitoring', 'data': {'system_name': 'payments-service',
            'status': 'degraded', 'cpu_percent': 96.2, 'memory_percent': 55.3,
            'alerts': ['CPU 91% threshold 80%']}},
            {'source': 'runbook', 'data': '# CPU 대응\n1. 배포를 확인하세요.',
             'citation_id': 'RB:cpu-spike:1'}]
        reply, _ = await model.write('결제 CPU 장애', collected, 1, None)
        self.assertIn('| payments-service | CPU | 96.2% |', reply)
        self.assertNotIn('91%', model.prompt)
        self.assertIn('[RB:cpu-spike:1]', reply)
        self.assertFalse(validate_draft(reply, collected))

    async def test_openai_sse_usage_only_chunk_and_multiple_tokens(self):
        async def handler(request):
            body = json.loads(request.content)
            self.assertTrue(body['stream_options']['include_usage'])
            events = [{'choices': [{'delta': {'content': '안녕'}}]},
                      {'choices': [{'delta': {'content': '하세요'}}]},
                      {'choices': [], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3}}]
            text = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=text)
        factory = httpx.AsyncClient
        pieces = []
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('agent.http_llm.httpx.AsyncClient',
                side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)):
            text, usage = await OpenAIFastLLM('test', 0).request('system', 'user', stream=True, on_token=pieces.append)
        self.assertEqual(text, '안녕하세요')
        self.assertEqual(pieces, ['안녕', '하세요'])
        self.assertEqual(usage['tokens'], 5)

    async def test_ollama_structured_reply_is_async(self):
        async def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body['format'], ROUTE_SCHEMA)
            return httpx.Response(200, json={'message': {'content': '{"route":"incident","needs_analysis":false}'},
                                            'prompt_eval_count': 4, 'eval_count': 5})
        factory = httpx.AsyncClient
        with patch('agent.http_llm.httpx.AsyncClient',
                side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs)):
            result, usage = await OllamaFastLLM('test', 0).route('장애 진단')
        self.assertEqual(result['route'], 'incident')
        self.assertEqual(usage['tokens'], 9)

    async def test_provider_errors_do_not_leak_credentials(self):
        factory = httpx.AsyncClient
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-secret'}), patch('agent.http_llm.httpx.AsyncClient',
                side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(
                    lambda request: httpx.Response(401, text='test-secret')), **kwargs)):
            with self.assertRaises(RuntimeError) as captured:
                await OpenAIFastLLM('test', 0).request('system', 'user')
        self.assertNotIn('test-secret', str(captured.exception))
