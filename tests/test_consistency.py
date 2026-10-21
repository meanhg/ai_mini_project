"""감사에서 발견한 경계 조건을 회귀 검사한다. 실행 DB·외부 모델은 사용하지 않는다."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from agent.deps import SupervisorDeps
from agent.evidence import render_report
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph, plan_node, prepare_evidence_node, research_node, finalize_node
from agent.llm_common import FastModelMixin, render_facts
from agent.mcp_client import MCPClient
from agent.retrieval_policy import system_search_text
from agent.retry import with_retry
from agent.validation import validate_draft
from scripts.package_project import build_archive


def facts():
    return [{'source': 'monitoring', 'data': {'system_name': 'local-host', 'status': 'healthy',
        'cpu_percent': 10, 'memory_percent': 30, 'disk_percent': 5, 'alerts': [], 'data_origin': 'local'}},
        {'source': 'runbook', 'system_name': 'local-host', 'citation_id': 'RB:cpu:1',
         'data': '# CPU\n1. CPU 사용률과 최근 변경을 확인한다.'}]


class ReportConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_allows_no_action_and_hides_empty_sections(self):
        report = render_report({'diagnosis': '현재 관측은 경고 기준 미만입니다.',
            'action_steps': [], 'check_steps': []}, facts())
        self.assertIn('즉시 필요한 조치는 확인되지 않았습니다', report)
        self.assertIn('담당자 별도 확인:', report)
        self.assertNotIn('추가 확인·조회 한계\n\n', report)
        self.assertFalse(validate_draft(report, facts()))
        abnormal = facts()
        abnormal[0]['data']['status'] = 'degraded'
        self.assertIn('하나 이상', render_report({'diagnosis': '추가 확인이 필요합니다.',
            'action_steps': [], 'check_steps': []}, abnormal))

    async def test_insufficient_evidence_is_not_reported_as_failed_validation(self):
        result = await finalize_node({'collected': facts()[:1], 'events': [],
            'evidence_gaps': [{'target': 'local-host', 'reason': '관련 매뉴얼 없음'}]}, None)
        self.assertIn('근거가 부족', result['final'])
        self.assertNotIn('검증 기준을 통과하지 못', result['final'])
        self.assertFalse(result['validation_passed'])

    async def test_recent_samples_reach_model_with_timestamps_and_no_recursive_payload(self):
        collected = facts()
        collected[0]['data']['recent_samples'] = [{'memory_percent': 20,
            'last_checked': '2026-10-20T10:00:00+00:00', 'recent_samples': ['nested']}]
        text = render_facts(collected)
        self.assertIn('2026-10-20T10:00:00+00:00', text)
        self.assertIn('"memory_percent": 20', text)
        self.assertNotIn('nested', text)

    async def test_report_numbering_checks_actual_sequence(self):
        report = '1) local-host CPU 10%, 메모리 30%입니다.\n3) 최근 변경을 확인하세요. [RB:cpu:1]'
        self.assertTrue(any('순서대로' in error for error in validate_draft(report, facts())))


class ScopeAndPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_previous_checkpoint_prefix_does_not_hide_first_target_symptom(self):
        query = 'payments-service, cache-service: payments-service TLS 만료와 cache-service NTP 오차'
        targets = ['payments-service', 'cache-service']
        text = system_search_text(query, {'system_name': targets[0], 'status': 'healthy'}, targets)
        self.assertIn('TLS', text)
        self.assertNotIn('NTP', text)

    async def test_target_queries_are_authoritative_even_when_summary_mentions_both(self):
        state = {'query': 'payments-service TLS와 cache-service NTP 오류',
            'targets': ['payments-service', 'cache-service'], 'events': [], 'tasks': [
                {'kind': 'monitoring', 'target': 'payments-service', 'queries': ['TLS 인증서 만료']},
                {'kind': 'monitoring', 'target': 'cache-service', 'queries': ['NTP 시간 동기화']}],
            'collected': [{'source': 'monitoring', 'data': {'system_name': name,
                          'status': 'healthy'}} for name in ['payments-service', 'cache-service']]}
        with patch.dict(os.environ, {'RAG_ENABLED': 'true'}):
            result = await prepare_evidence_node(state, None)
        searches = [t for t in result['tasks'] if t.get('kind') == 'runbook_search']
        self.assertIn('TLS', searches[0]['target'])
        self.assertNotIn('NTP', searches[0]['target'])
        self.assertIn('NTP', searches[1]['target'])
        self.assertNotIn('TLS', searches[1]['target'])
        self.assertNotIn('replan_rounds', result)

    async def test_model_plan_normalizes_once_at_graph_boundary(self):
        import agent.graph as graph_module
        class Model(FastModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps({'needs_analysis': False, 'tasks': [
                    {'agent': 'research', 'target': 'local-host', 'queries': ['CPU 상태']}]}), {}
        class MCP:
            async def find_prompt(self, name): return 'ops'
            async def get_prompt(self, *args, **kwargs): return '상태와 매뉴얼 확인'
        with patch.object(graph_module, 'normalize_plan', wraps=graph_module.normalize_plan) as normalize:
            result = await plan_node({'query': 'local-host CPU 상태', 'targets': ['local-host'], 'events': []},
                SupervisorDeps(Model(), None, MCP(), None, 'v2'))
        self.assertEqual(normalize.call_count, 1)
        self.assertEqual(result['tasks'][0]['queries'], ['CPU 상태'])

    async def test_initial_search_does_not_spend_extra_research_budget(self):
        class MCP:
            async def find_prompt(self, name): return 'ops'
            async def get_prompt(self, *args, **kwargs): return '상태와 매뉴얼 확인'
            async def call_tool(self, name, arguments):
                if name == 'get_monitoring_status': return facts()[0]['data']
                return {'matches': [{'citation_id': 'RB:cpu:1', 'title': 'CPU',
                    'content': '# CPU\n1. 최근 변경을 확인한다.'}], 'backend': 'local',
                    'query_results': [{'query': q, 'citations': ['RB:cpu:1']}
                                      for q in arguments['queries']]}
        with patch.dict(os.environ, {'RAG_ENABLED': 'true'}):
            result = await build_graph(SupervisorDeps(FastLLM('test', 0), QualityLLM('test', 0),
                MCP(), None, 'v2')).ainvoke({'query': 'local-host CPU 상태 진단', 'events': []})
        self.assertTrue(result['validation_passed'])
        self.assertEqual(result.get('replan_rounds', 0), 0)
        self.assertFalse(any(e['type'] == 'ReplanCheck' and not e['is_sufficient'] for e in result['events']))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_calls_overlap_instead_of_serializing(self):
        entered = 0
        both = asyncio.Event()
        class Session:
            async def call_tool(self, *args):
                nonlocal entered
                entered += 1
                if entered == 2: both.set()
                await asyncio.wait_for(both.wait(), .5)
                return SimpleNamespace(content=[SimpleNamespace(text='{}')])
        client = MCPClient(flaky_first_tool_call=False)
        client.sessions = {'ops': Session()}
        await asyncio.gather(client.call_tool('one', {}), client.call_tool('two', {}))
        self.assertEqual(entered, 2)

    async def test_stalled_call_is_cancelled_and_retried_with_a_bound(self):
        calls = 0
        async def stalled():
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(with_retry(stalled, retries=3, base_delay=0, timeout=.01), .5)
        self.assertEqual(calls, 3)

    async def test_resource_discovery_failure_keeps_other_monitoring_result(self):
        class MCP:
            async def find_resource_template(self, *args): raise ValueError('not available')
            async def call_tool(self, *args): return facts()[0]['data']
        state = {'events': [], 'tasks': [
            {'id': 'a', 'agent': 'research', 'kind': 'monitoring', 'status': 'pending', 'target': 'local-host'},
            {'id': 'b', 'agent': 'research', 'kind': 'runbook', 'status': 'pending', 'target': 'cpu'}]}
        result = await research_node(state, SupervisorDeps(None, None, MCP(), None, 'v2'))
        self.assertEqual([t['status'] for t in result['tasks']], ['done', 'failed'])
        self.assertEqual(result['collected'][0]['data']['data_origin'], 'local')

    async def test_search_and_resource_reads_retry_transient_timeout(self):
        counts = {'search': 0, 'resource': 0}
        class MCP:
            async def find_resource_template(self, *args): return 'ops', 'runbook://{incident_type}'
            async def read_resource(self, *args, **kwargs):
                counts['resource'] += 1
                if counts['resource'] == 1: raise TimeoutError()
                return '# CPU\n1. 변경 확인'
            async def call_tool(self, *args):
                counts['search'] += 1
                if counts['search'] == 1: raise TimeoutError()
                return {'matches': [], 'query_results': []}
        state = {'events': [], 'tasks': [
            {'id': 'a', 'agent': 'research', 'kind': 'runbook_search', 'status': 'pending', 'target': 'CPU'},
            {'id': 'b', 'agent': 'research', 'kind': 'runbook', 'status': 'pending', 'target': 'cpu'}]}
        result = await research_node(state, SupervisorDeps(None, None, MCP(), None, 'v2'))
        self.assertEqual(result['retry_count'], 2)
        self.assertEqual([t['status'] for t in result['tasks']], ['done', 'done'])


class PackagingAndSetupTests(unittest.TestCase):
    def test_actual_archive_excludes_all_runtime_and_secret_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'; root.mkdir()
            files = ['server.py', '.env.example', '.env', '.env.backup', 'session.db',
                'session.db-wal', 'session.db-shm', 'session.db.checkpoints.db',
                'session.db.checkpoints.db-wal', 'data/monitoring_history.sqlite3',
                'data/rag_index.sqlite3-shm', 'data/runbooks/cpu.md', '.venv/bin/python']
            for name in files:
                p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('fixture')
            output = build_archive(root, Path(directory) / 'project.zip')
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {'project/server.py',
                    'project/.env.example', 'project/data/runbooks/cpu.md'})
            self.assertTrue((root / 'session.db').exists())

    def test_ollama_setup_reuses_server_and_matches_current_models(self):
        from scripts import setup_ollama
        installed = {'qwen3.5:2b', 'qwen3:8b', 'qwen3-embedding:0.6b'}
        with patch.dict(os.environ, {}, clear=True), patch.object(setup_ollama, 'load_dotenv'), \
             patch.object(setup_ollama.shutil, 'which', return_value='/usr/local/bin/ollama'), \
             patch.object(setup_ollama, 'tags', return_value=installed), \
             patch.object(setup_ollama.subprocess, 'Popen') as start, \
             patch.object(setup_ollama.subprocess, 'run') as run:
            self.assertEqual(setup_ollama.main(), 0)
        start.assert_not_called()
        run.assert_not_called()


class CheckpointCompatibilityTests(unittest.TestCase):
    def test_resume_previous_state_without_new_graph_fields(self):
        """이전 노드 이름과 상태 형식으로 만든 체크포인트를 새 API로 복구한다."""
        from fastapi.testclient import TestClient
        from langgraph.graph import StateGraph, END
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from agent.graph import AgentState
        from agent.session_store import SqliteSessionStore
        import server
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.db'
            store = SqliteSessionStore(path)
            store.start_run('previous-run', 'previous-thread', 'local-host CPU 상태 진단', 'fake')
            store.status('previous-run', 'failed')
            store.close()
            async def old_checkpoint():
                async def interrupted(state): raise RuntimeError('previous process stopped')
                async with AsyncSqliteSaver.from_conn_string(str(path) + '.checkpoints.db') as saver:
                    old = StateGraph(AgentState)
                    old.add_node('write', interrupted); old.set_entry_point('write'); old.add_edge('write', END)
                    with self.assertRaises(RuntimeError):
                        await old.compile(checkpointer=saver).ainvoke({
                            'query': 'local-host: local-host CPU 상태 진단', 'thread_id': 'previous-thread',
                            'route': 'incident', 'collected': facts(), 'events': [], 'tasks': [],
                            'replan_rounds': 1, 'usage': [], 'cost_usd': 0},
                            {'configurable': {'thread_id': 'previous-run'}})
            asyncio.run(old_checkpoint())
            reopened = SqliteSessionStore(path)
            with patch.dict(os.environ, {'LLM_BACKEND': 'fake', 'SUPERVISOR_VERSION': 'v2'}), \
                 patch.object(server, 'SqliteSessionStore', return_value=reopened), TestClient(server.app) as client:
                response = client.post('/runs/previous-run/resume', params={'thread_id': 'previous-thread'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['metadata']['validation_passed'])
                history = client.get('/sessions/previous-thread/messages').json()
                self.assertEqual(len(history['messages']), 2)
                self.assertEqual(history['pending_runs'], [])
