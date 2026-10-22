"""진단 출처 기반 체크리스트, 수동 기록 저장, API 회귀 검사."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import server
from agent.checklists import ChecklistStore, candidates
from agent.session_store import SqliteSessionStore

REPLY = '''1) 진단 요약
현재 CPU 지표가 높습니다. 원인은 미확인입니다.
2) 권장 조치
- payments-service: CPU가 높고 지속될 때만 부하를 분산한다. [RB:cpu:1]
3) 추가 확인
- payments-service: 담당자가 최근 배포 이력을 별도 확인한다. [RB:cpu:1]
'''
META = {'run_id': 'run-a', 'validation_passed': True,
        'monitoring': [{'system_name': 'payments-service', 'status': 'degraded'}],
        'sources': [{'citation_id': 'RB:cpu:1', 'system_name': 'payments-service',
                     'title': 'CPU 대응', 'path': 'data/runbooks/cpu.md'}]}


def seed(store, run_id='run-a', thread_id='thread-a', reply=REPLY, passed=True):
    store.start_run(run_id, thread_id, 'payments-service CPU 진단', 'fake')
    store.finish(run_id, reply, {**META, 'run_id': run_id, 'validation_passed': passed}, {})


class CandidateTests(unittest.TestCase):
    def test_only_verified_target_source_lines_are_importable(self):
        items = candidates(REPLY, META)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['text'], 'CPU가 높고 지속될 때만 부하를 분산한다.')
        self.assertEqual(items[0]['citation_id'], 'RB:cpu:1')
        extra = REPLY + '- cache-service: 다른 대상 작업. [RB:cpu:1]\n- payments-service: 미조회 출처. [RB:other:1]'
        self.assertEqual(len(candidates(extra, META)), 2)
        self.assertEqual(candidates(REPLY, {**META, 'validation_passed': False}), [])

    def test_normal_response_has_no_tasks_and_duplicates_are_removed(self):
        self.assertEqual(candidates('현재 정상 범위입니다. 참고한 매뉴얼: [RB:cpu:1]', META), [])
        self.assertEqual(len(candidates(REPLY + REPLY, META)), 2)


class ChecklistStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'sessions.db'
        self.store = SqliteSessionStore(self.path)
        self.checklists = ChecklistStore(self.store)
        seed(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_create_is_idempotent_and_retains_original_provenance(self):
        first = self.checklists.create('run-a', 'thread-a')
        second = self.checklists.create('run-a', 'thread-a')
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['already_exists'])
        groups = self.checklists.list()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]['items']), 2)
        self.assertEqual(groups[0]['query'], 'payments-service CPU 진단')
        self.assertEqual(groups[0]['items'][0]['status'], 'pending')
        self.assertEqual(groups[0]['items'][0]['source_title'], 'CPU 대응')

    def test_status_and_notes_persist_without_changing_original_ai_reply(self):
        group_id = self.checklists.create('run-a', 'thread-a')['id']
        item = self.checklists.list()[0]['items'][0]
        note = '담당자 직접 확인: 배포 이력 이상 없음. <script>실행되지 않는 메모</script>'
        self.checklists.update(group_id, item['id'], 'done', note)
        self.assertEqual(self.store.history('thread-a')[-1]['content'], REPLY)
        self.store.close()
        self.store = SqliteSessionStore(self.path)
        saved = ChecklistStore(self.store).list()[0]['items'][0]
        self.assertEqual(saved['status'], 'done')
        self.assertEqual(saved['note'], note)

    def test_unfinished_failed_and_wrong_thread_diagnoses_cannot_be_imported(self):
        with self.assertRaises(LookupError): self.checklists.create('run-a', 'another-thread')
        self.store.start_run('running', 'thread-a', 'query', 'fake')
        with self.assertRaises(ValueError): self.checklists.create('running', 'thread-a')
        seed(self.store, 'rejected', passed=False)
        with self.assertRaises(ValueError): self.checklists.create('rejected', 'thread-a')

    def test_items_cannot_be_updated_under_another_checklist(self):
        group_id = self.checklists.create('run-a', 'thread-a')['id']
        item_id = self.checklists.list()[0]['items'][0]['id']
        with self.assertRaises(LookupError): self.checklists.update('wrong-group', item_id, 'done', '')
        with self.assertRaises(ValueError): self.checklists.update(group_id, item_id, 'invalid', '')
        with self.assertRaises(ValueError): self.checklists.update(group_id, item_id, 'done', 'a'*2001)


class ChecklistAPITests(unittest.TestCase):
    def test_api_creation_edit_and_existing_history_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / 'session.db')
            seed(store)
            with patch.object(server, 'SqliteSessionStore', return_value=store), \
                 TestClient(server.app) as client:
                history = client.get('/sessions/thread-a/messages').json()['messages']
                self.assertEqual(len(history[-1]['metadata']['checklist_candidates']), 2)
                self.assertEqual(client.post('/checklists', json={'run_id': 'run-a', 'thread_id': 'other'}).status_code, 404)
                response = client.post('/checklists', json={'run_id': 'run-a', 'thread_id': 'thread-a'})
                self.assertEqual(response.status_code, 200)
                group = client.get('/checklists').json()['checklists'][0]
                item = group['items'][0]
                url = f"/checklists/{group['id']}/items/{item['id']}"
                self.assertEqual(client.patch(url, json={'status': 'invalid', 'note': ''}).status_code, 422)
                self.assertEqual(client.patch(url, json={'status': 'done', 'note': '', 'text': '원문 변경'}).status_code, 422)
                self.assertEqual(client.patch(url, json={'status': 'deferred', 'note': '담당자 확인 대기'}).status_code, 200)
                saved = client.get('/checklists').json()['checklists'][0]['items'][0]
                self.assertEqual(saved['status'], 'deferred')
                self.assertEqual(saved['note'], '담당자 확인 대기')
                self.assertTrue(client.post('/checklists', json={'run_id': 'run-a', 'thread_id': 'thread-a'}).json()['already_exists'])
