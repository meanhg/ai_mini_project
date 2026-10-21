"""표시 형식이 바뀌어도 수치·출처 검증과 조회 한계를 유지한다."""
import unittest

from agent.evidence import render_report
from agent.llm_common import render_facts
from agent.monitoring import collection_scope
from agent.validation import validate_draft


def evidence():
    return [{'source': 'monitoring', 'data': {'system_name': 'local-host', 'status': 'healthy',
        'cpu_percent': 10, 'memory_percent': 40, 'data_origin': 'local',
        'last_checked': '2026-10-20T15:00:00+00:00'}},
        {'source': 'monitoring', 'data': {'system_name': 'auth-service', 'status': 'degraded',
        'cpu_percent': 80, 'memory_percent': 70, 'data_origin': 'sample'}},
        {'source': 'runbook', 'citation_id': 'RB:cpu:1', 'system_name': 'auth-service',
         'data': '# CPU\n1. 담당자가 로그를 별도로 확인한다.'}]


class ReadabilityTests(unittest.TestCase):
    def report(self):
        return render_report({'diagnosis': '원인 확인에는 추가 점검이 필요합니다.',
            'action_steps': [], 'check_steps': ['RB:cpu:1:S1@auth-service']}, evidence())

    def test_summary_precedes_table_and_scopes_are_explicit(self):
        reply = self.report()
        self.assertLess(reply.index('판단:'), reply.index('| 대상 |'))
        self.assertIn('| local-host | CPU | 10% |', reply)
        self.assertIn('| local-host | 디스크 | 미조회 |', reply)
        self.assertIn('실제 로컬 측정', reply)
        self.assertIn('샘플 시나리오', reply)
        self.assertIn('10/21 00:00:00 KST', reply)
        self.assertIn('p95 지연시간', reply)
        self.assertIn('담당자 별도 확인:', reply)
        self.assertFalse(validate_draft(reply, evidence()))

    def test_table_cannot_swap_values_between_targets(self):
        draft = self.report().replace('| local-host | CPU | 10% |', '| local-host | CPU | 80% |')
        self.assertTrue(any('local-host' in error and 'CPU' in error
                            for error in validate_draft(draft, evidence())))

    def test_table_cannot_substitute_memory_for_missing_disk(self):
        draft = self.report().replace('| local-host | 디스크 | 미조회 |', '| local-host | 디스크 | 40% |')
        self.assertTrue(any('디스크' in error for error in validate_draft(draft, evidence())))

    def test_report_retains_source_and_allows_checks_without_forced_actions(self):
        reply = self.report()
        self.assertIn('[RB:cpu:1]', reply)
        self.assertIn('조치 조건이 확인되지 않아', reply)
        self.assertNotIn('하나 이상 선택', reply)

    def test_scope_reaches_writer_without_inventing_missing_measurements(self):
        scope = collection_scope(evidence()[0]['data'])
        self.assertEqual(scope['measured'], ['CPU', '시스템 메모리'])
        self.assertIn('collection_scope', render_facts(evidence()))
        self.assertNotIn('disk_percent', render_facts(evidence()))

    def test_runbooks_no_longer_claim_unsupported_automatic_queries(self):
        from agent.settings import runbook_directory
        directory = runbook_directory()
        self.assertIn('현재 도구가 직접 조회하지 않으므로', (directory / 'high-latency.md').read_text())
        self.assertIn('담당자가 로그와 트레이스를 별도 확인', (directory / 'network-timeout.md').read_text())
        self.assertIn('시스템 전체 메모리 사용률을 프로세스 사용률로 대체하지 않는다',
                      (directory / 'memory-leak.md').read_text())
