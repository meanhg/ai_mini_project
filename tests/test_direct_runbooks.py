"""기본 진단은 임베딩 없이 내부 매뉴얼 원문을 MCP로 조회한다."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import server
from agent.session_store import SqliteSessionStore


class DirectRunbookTests(unittest.TestCase):
    def test_direct_pipeline_without_embedding_service(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / 'unused-vectors.db'
            settings = {'LLM_BACKEND': 'fake', 'SUPERVISOR_VERSION': 'v2',
                        'RAG_ENABLED': 'false', 'OLLAMA_BASE_URL': 'http://127.0.0.1:1',
                        'RAG_INDEX_PATH': str(index), 'OPENAI_API_KEY': '',
                        'LANGCHAIN_TRACING_V2': 'false', 'LANGSMITH_TRACING': 'false',
                        'MONITORING_HISTORY_PATH': directory + '/monitoring.db'}
            store = SqliteSessionStore(Path(directory) / 'session.db')
            with patch.dict(os.environ, settings), \
                 patch.object(server, 'SqliteSessionStore', return_value=store), \
                 TestClient(server.app) as client:
                health = client.get('/health').json()
                self.assertFalse(health['rag_enabled'])
                self.assertIsNone(health['embedding_backend'])
                self.assertEqual(health['runbook_mode'], 'direct')
                for query in ['payments-service CPU 급증 원인 진단',
                              'local-host CPU와 메모리 상태를 확인하고 이상 여부를 진단해줘']:
                    response = client.post('/chat', json={
                        'message': query, 'thread_id': query, 'backend': 'fake'})
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertTrue(data['metadata']['validation_passed'])
                    self.assertEqual(len(data['metadata']['sources']), 6)
                    self.assertTrue(all(s['citation_id'].startswith('RB:')
                                        for s in data['metadata']['sources']))
                    self.assertFalse(any(e['type'] == 'EvidenceRetrieved' for e in data['events']))
                    reads = [e for e in data['events'] if e['type'] == 'TaskCompleted'
                             and e['source'] == 'runbook']
                    self.assertEqual(len(reads), 6)
                followup = client.post('/chat', json={
                    'message': 'local-host 현재 측정값을 이전에 저장된 표본과 비교해줘',
                    'thread_id': 'followup-test', 'backend': 'fake'}).json()
                self.assertTrue(followup['metadata']['validation_passed'])
                self.assertIn('이전 표본', followup['reply'])
                self.assertEqual(len(followup['metadata']['followups']), 2)
                history = client.get('/sessions/followup-test/messages').json()['messages']
                self.assertEqual(history[-1]['metadata']['followups'], followup['metadata']['followups'])
                self.assertFalse(index.exists())
