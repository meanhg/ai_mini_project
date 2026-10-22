import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.settings import Policy, policy
from agent.coverage import evidence_gaps
from agent.evidence import runbook_steps, render_report
from agent.graph import route_node, replan_check_node
from agent.deps import SupervisorDeps
from agent.llm_common import FastModelMixin, normalize_plan
from agent.monitoring import warning_alerts
from agent.rag import RunbookIndex, Embedder


def monitor(target):
    return {'source': 'monitoring', 'data': {'system_name': target, 'cpu_percent': 20,
            'memory_percent': 30, 'status': 'healthy'}}


def document(target, citation='RB:tls:1'):
    return {'source': 'runbook', 'system_name': target, 'citation_id': citation,
            'data': '# 인증서\n만료된 인증서를 교체하기 전에 체인과 유효 기간을 확인한다.'}


class DynamicPolicyTests(unittest.TestCase):
    def test_thresholds_and_limits_reload_without_code_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'policy.json'
            settings = Policy().model_dump()
            settings.update(max_research_rounds=2, max_validation_attempts=2,
                            system_thresholds={'local-host': {'cpu_percent': 95}})
            path.write_text(json.dumps(settings))
            with patch.dict(os.environ, {'AGENT_POLICY_PATH': str(path)}):
                self.assertEqual(policy().max_validation_attempts, 2)
                self.assertFalse(warning_alerts({'cpu_percent': 90}, 'local-host')[0])
                self.assertTrue(warning_alerts({'cpu_percent': 90}, 'other-host')[0])
                settings['system_thresholds']['local-host']['cpu_percent'] = 80
                path.write_text(json.dumps(settings))
                self.assertTrue(warning_alerts({'cpu_percent': 90}, 'local-host')[0])
            settings['warning_thresholds']['cpu_percent'] = 500
            with self.assertRaises(ValueError):
                Policy.model_validate(settings)

    def test_registry_reload_supports_arbitrary_new_target_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'sources.json'
            path.write_text('{}')
            with patch.dict(os.environ, {'MONITORING_SOURCES_PATH': str(path)}):
                from agent.llm_common import mentioned_systems
                self.assertEqual(mentioned_systems('edge-router を確認'), [])
                path.write_text('{"edge-router":{"queries":{}}}')
                self.assertEqual(mentioned_systems('edge-router 상태 확인'), ['edge-router'])

    def test_unnumbered_and_bullet_evidence_and_configurable_layout(self):
        collected = [monitor('auth-service'), document('auth-service'),
                     {'source': 'runbook', 'citation_id': 'RB:bullet:1',
                      'data': '# 절차\n- 실패가 반복될 때만 교체한다.\n  인증 체인은 먼저 확인한다.'}]
        steps = runbook_steps(collected)
        self.assertIn('RB:tls:1:S1@auth-service', steps)
        self.assertIn('인증 체인은 먼저 확인한다.', steps['RB:bullet:1:S1']['text'])
        report = {'diagnosis': '인증서 유효 기간을 추가 확인해야 합니다.',
                  'action_steps': ['RB:tls:1:S1@auth-service'], 'check_steps': []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'policy.json'
            settings = Policy().model_dump()
            settings['report_sections'] = [
                {'title': '측정 결과', 'content': ['observations']},
                {'title': '분석', 'content': ['diagnosis']},
                {'title': '대응', 'content': ['actions']},
                {'title': '문서', 'content': ['sources']}]
            path.write_text(json.dumps(settings))
            with patch.dict(os.environ, {'AGENT_POLICY_PATH': str(path)}):
                reply = render_report(report, collected)
            self.assertIn('1) 측정 결과', reply)
            self.assertIn('4) 문서', reply)
            self.assertIn('체인과 유효 기간을 확인한다.', reply)


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_target_source_does_not_cover_other_target(self):
        state = {'query': 'auth-service와 web-01 점검', 'targets': ['auth-service', 'web-01'], 'collected': [
            monitor('auth-service'), monitor('web-01'), document('auth-service')],
            'tasks': [], 'events': [], 'replan_rounds': 1}
        gaps = evidence_gaps(state)
        self.assertEqual([g['target'] for g in gaps], ['web-01'])
        result = await replan_check_node(state, None)
        self.assertFalse(result['events'][-1]['is_sufficient'])
        self.assertTrue(result['events'][-1]['limit_reached'])

    async def test_each_symptom_needs_a_retained_source(self):
        state = {'targets': ['auth-service'], 'tasks': [{'kind': 'monitoring', 'target': 'auth-service',
                  'queries': ['CPU 과부하', '인증서 만료']}],
                 'collected': [monitor('auth-service'), document('auth-service'),
                    {'source': 'retrieval_status', 'system_name': 'auth-service', 'data': [
                        {'query': 'CPU 과부하', 'citations': []}, {'query': '인증서 만료', 'citations': ['RB:tls:1']}]}]}
        with patch.dict(os.environ, {'RAG_ENABLED': 'true'}):
            gaps = evidence_gaps(state)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['query'], 'CPU 과부하')

    async def test_extra_round_only_schedules_missing_target(self):
        state = {'query': 'auth-service와 web-01 점검', 'targets': ['auth-service', 'web-01'], 'collected': [
            monitor('auth-service'), monitor('web-01'), document('auth-service')],
            'tasks': [], 'events': [], 'replan_rounds': 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'policy.json'
            path.write_text('{"max_research_rounds": 2}')
            with patch.dict(os.environ, {'AGENT_POLICY_PATH': str(path), 'RAG_ENABLED': 'true'}):
                result = await replan_check_node(state, None)
        self.assertEqual(result['replan_rounds'], 2)
        self.assertEqual([t['system_name'] for t in result['tasks']], ['web-01'])


class SemanticRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_model_path_uses_context_without_trigger_words(self):
        class Model(FastModelMixin):
            async def request(self, system, user, **kwargs):
                self.input = json.loads(user)
                return json.dumps({'route': 'incident', 'needs_analysis': False, 'target_resolution': 'resolved', 'targets': ['cache-service'],
                    'unknown_targets': [], 'uses_previous': True, 'resolved_request': 'cache-service 상태 재조회',
                    'clarification': ''}), {'tokens': 10, 'usd': None}
        model = Model()
        state = {'query': '방금 살펴본 곳 한 번 더 살펴봐줄래?', 'history': [], 'events': [],
                 'conversation_context': {'targets': ['cache-service'], 'incident_query': '메모리 증가'}}
        result = await route_node(state, SupervisorDeps(model, None, None, None, 'v2'))
        self.assertEqual(result['targets'], ['cache-service'])
        self.assertIn('cache-service', result['query'])
        self.assertEqual(model.input['context']['incident_query'], '메모리 증가')

    async def test_unregistered_model_target_cannot_reach_tools(self):
        class Model(FastModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps({'route': 'incident', 'needs_analysis': False, 'target_resolution': 'resolved', 'targets': ['unregistered'],
                    'unknown_targets': [], 'uses_previous': False, 'resolved_request': 'unregistered 점검',
                    'clarification': ''}), {'tokens': 1}
        result, _ = await Model().interpret('새 라우터 점검', [], {})
        self.assertEqual(result['targets'], [])
        self.assertEqual(result['unknown_targets'], ['unregistered'])

    async def test_new_symptom_queries_are_not_replaced_by_six_categories(self):
        raw = {'tasks': [{'agent': 'research', 'target': 'gateway-service',
                         'queries': ['TLS 인증서 체인 만료', '시간 동기화 오차']}], 'needs_analysis': False}
        tasks = normalize_plan(raw, 'gateway-service 문제')['tasks']
        self.assertEqual(tasks[0]['queries'], ['TLS 인증서 체인 만료', '시간 동기화 오차'])

    async def test_plain_text_document_is_searchable_and_citable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_MIN_SCORE': '0'}):
            root = Path(directory)
            (root/'tls.txt').write_text('인증서 유효 기간\n\n인증서 체인 만료 전에 갱신 상태를 확인한다.')
            result = await RunbookIndex(root, root/'cache.db', Embedder('local')).search(
                '인증서 체인 만료', 1, queries=['인증서 체인 만료'])
            self.assertEqual(result['matches'][0]['incident_type'], 'tls')
            self.assertEqual(result['query_results'][0]['citations'], ['RB:tls:1'])
            self.assertTrue(runbook_steps([{'source': 'runbook', **result['matches'][0],
                                           'data': result['matches'][0]['content']}]))


class ScopeAndFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_must_distinguish_whole_scope_from_ambiguous_reference(self):
        class Model(FastModelMixin):
            all_previous = False
            async def request(self, *args, **kwargs):
                return json.dumps({'route': 'incident', 'needs_analysis': True,
                    'targets': ['cache-service', 'payments-service'], 'target_resolution': 'resolved',
                    'selects_all_previous': self.all_previous, 'unknown_targets': [],
                    'uses_previous': True, 'resolved_request': '이전 대상 재확인', 'clarification': ''}), {}
        model = Model()
        context = {'targets': ['cache-service', 'payments-service']}
        result, _ = await model.interpret('방금 그곳 상태는?', [], context)
        self.assertTrue(result['clarification'])
        self.assertEqual(result['targets'], [])
        model.all_previous = True
        result, _ = await model.interpret('아까 본 대상 전체 상태는?', [], context)
        self.assertFalse(result['clarification'])
        self.assertEqual(len(result['targets']), 2)

    async def test_new_korean_filename_has_valid_citation(self):
        from agent.validation import validate_draft
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_MIN_SCORE': '0'}):
            root = Path(directory)
            (root/'인증서 갱신.txt').write_text('인증서 만료 전 갱신 상태와 인증 체인을 확인한다.')
            result = await RunbookIndex(root, root/'cache.db', Embedder('local')).search(
                '인증서 갱신', 1, queries=['인증서 갱신'])
            source = {**result['matches'][0], 'source': 'runbook', 'data': result['matches'][0]['content']}
            collected = [monitor('auth-service'), source]
            step_id = next(iter(runbook_steps(collected)))
            report = render_report({'diagnosis': '인증서 상태를 추가 확인해야 합니다.',
                'action_steps': [step_id], 'check_steps': []}, collected)
            self.assertFalse(validate_draft(report, collected))

    async def test_configured_validation_cap_changes_running_graph(self):
        from agent.graph import build_graph
        from agent.fake_llm import FastLLM, QualityLLM
        class MCP:
            async def find_prompt(self, name):
                return 'ops'
            async def get_prompt(self, *args, **kwargs):
                return '현재 상태와 문서를 조회한다.'
            async def find_resource_template(self, name):
                return 'ops', 'runbook://incident-response/{incident_type}'
            async def read_resource(self, *args, **kwargs):
                return '# 절차\n1. 최근 변경을 확인한다.'
            async def call_tool(self, name, arguments):
                return monitor('auth-service')['data']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'policy.json'
            path.write_text('{"max_validation_attempts": 1}')
            with patch.dict(os.environ, {'AGENT_POLICY_PATH': str(path), 'RAG_ENABLED': 'false'}):
                deps = SupervisorDeps(FastLLM('test', 0), QualityLLM('test', 0), MCP(), None, 'v2')
                result = await build_graph(deps).ainvoke({'query': 'auth-service 장애 진단', 'events': []})
            self.assertEqual(result['validation_attempts'], 1)
            self.assertFalse(result['validation_passed'])
            self.assertNotIn('재시작하면 됩니다.', result['final'])
