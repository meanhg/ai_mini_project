"""정상·장애·정보 부족 답변과 실제 실행 가능한 후속 제안 회귀 검사."""
import copy
import unittest
from agent.evidence import render_report
from agent.validation import validate_draft
from agent.response_policy import concise_status, followup_suggestions, previous_sample


def facts(kind='status', symptoms=False):
    return [
        {'source': 'request_context', 'data': {'request_kind': kind, 'reported_symptoms': symptoms}},
        {'source': 'monitoring', 'data': {'system_name': 'local-host', 'status': 'healthy',
         'cpu_percent': 5.0, 'memory_percent': 40.0, 'disk_percent': 1.0, 'alerts': [],
         'last_checked': '2026-10-22T01:00:00+00:00', 'data_origin': 'local'}},
        {'source': 'runbook', 'system_name': 'local-host', 'citation_id': 'RB:memory-leak:1',
         'data': '# 메모리 점검\n1. 담당자가 힙 덤프를 별도 확인한다.'},
    ]


def report():
    return {'diagnosis': '현재 관측 지표를 확인했습니다.', 'action_steps': [], 'check_steps': []}


class ResponsePolicyTests(unittest.TestCase):
    def test_normal_status_is_short_and_does_not_force_runbook_actions_or_citations(self):
        data = facts()
        text = render_report(report(), data)
        self.assertTrue(concise_status(data))
        self.assertNotIn('힙 덤프', text)
        self.assertNotIn('p95', text)
        self.assertNotIn('권장 조치', text)
        self.assertNotIn('[RB:', text)
        self.assertIn('| local-host | 메모리 | 40.0% |', text)
        self.assertFalse(validate_draft(text, data))

    def test_normal_status_rejects_speculative_leak_even_when_hedged(self):
        bad = {**report(), 'diagnosis': '메모리 누수 가능성을 배제할 수 없습니다.'}
        self.assertIn('가능성을 제기하지', render_report(bad, facts()))

    def test_normal_status_rejects_unnecessary_invasive_check(self):
        bad = {**report(), 'check_steps': ['RB:memory-leak:1:S1@local-host']}
        self.assertIn('조치·점검 배열을 비우세요', render_report(bad, facts()))

    def test_reported_symptom_is_not_dismissed_by_normal_metrics(self):
        data = facts(symptoms=True)
        self.assertFalse(concise_status(data))
        text = render_report(report(), data)
        self.assertIn('보고된 증상은 미확인', text)
        self.assertIn('증상의 원인·해소 여부', text)
        self.assertIn('담당자 별도 확인', text)

    def test_abnormal_metric_or_alert_blocks_concise_normal_response(self):
        for key, value in [('memory_percent', 90), ('alerts', ['user-facing errors']), ('status', 'unknown')]:
            data = facts(); data[1]['data'][key] = value
            self.assertFalse(concise_status(data))

    def test_numeric_validation_is_preserved_in_compact_mode(self):
        data = facts()
        text = render_report(report(), data).replace('| 메모리 | 40.0% |', '| 메모리 | 99% |')
        self.assertTrue(any('측정값과 일치' in e for e in validate_draft(text, data)))

    def test_comparison_uses_only_previous_same_source_samples(self):
        data = facts(); monitor = data[1]['data']
        previous = {**monitor, 'memory_percent': 35, 'last_checked': '2026-10-22T00:59:00+00:00'}
        monitor['recent_samples'] = [previous, {**previous, 'data_origin': 'sample'}, copy.deepcopy(monitor)]
        data[0]['data']['compare_history'] = True
        self.assertEqual(previous_sample(monitor)['memory_percent'], 35)
        text = render_report(report(), data)
        self.assertIn('| 메모리 | 40.0% | 35% | +5.0 |', text)
        self.assertFalse(validate_draft(text, data))
        self.assertIn('정기 수집 추세나 장애 원인을 의미하지 않습니다', text)

    def test_comparison_without_history_says_unavailable(self):
        data = facts(); data[0]['data']['compare_history'] = True
        self.assertIn('비교할 수 있는 이전 표본이 없습니다', render_report(report(), data))

    def test_followups_explicitly_keep_target_and_only_offer_available_history(self):
        result = {'route': 'incident', 'validation_passed': True, 'collected': facts()}
        suggestions = followup_suggestions(result)
        self.assertEqual(len(suggestions), 1)
        self.assertIn('local-host', suggestions[0]['query'])
        monitor = result['collected'][1]['data']
        monitor['recent_samples'] = [{**monitor, 'last_checked': '2026-10-22T00:59:00+00:00'}]
        self.assertEqual(len(followup_suggestions(result)), 2)

    def test_failed_or_unrelated_response_has_no_followups(self):
        self.assertEqual(followup_suggestions({'route': 'chat'}), [])
        self.assertEqual(followup_suggestions({'route': 'incident', 'validation_passed': False, 'collected': facts()}), [])

    def test_ambiguous_target_options_require_explicit_user_choice(self):
        result = {'route': 'incident', 'events': [{'type': 'ClarificationRequired'}],
                  'conversation_context': {'targets': ['local-host', 'payments-service']}}
        self.assertEqual([s['query'] for s in followup_suggestions(result)], ['local-host', 'payments-service'])


class DeterministicStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_status_avoids_generation_and_judge_but_rejects_tampering(self):
        from agent.graph import write_node, validate_node
        from agent.deps import SupervisorDeps
        class UnexpectedModel:
            async def write(self, *args, **kwargs):
                raise AssertionError('정상 안내에 생성 호출이 발생했습니다.')
            async def judge(self, *args, **kwargs):
                raise AssertionError('코드 생성 안내에 LLM 심판 호출이 발생했습니다.')
        model = UnexpectedModel()
        deps = SupervisorDeps(None, model, None, None, 'v2')
        state = {'query': 'local-host 메모리 상태', 'collected': facts(),
                 'response_context': {'request_kind': 'status', 'reported_symptoms': False}, 'events': []}
        state.update(await write_node(state, deps))
        result = await validate_node(state, deps)
        self.assertTrue(result['validation_passed'])
        self.assertEqual(result['events'][-1]['semantic'], 'deterministic')
        state['draft'] += '\n추가로 시스템 전체는 절대 장애가 없습니다.'
        self.assertFalse((await validate_node(state, deps))['validation_passed'])

    async def test_reported_symptoms_still_use_model_and_semantic_validation(self):
        from agent.graph import write_node, validate_node
        from agent.deps import SupervisorDeps
        calls = []
        class Model:
            async def write(self, query, collected, *args, **kwargs):
                calls.append('write')
                return render_report(report(), collected), {'tokens': 1}
            async def judge(self, *args):
                calls.append('judge')
                return {'supported': True, 'issues': []}, {'tokens': 1}
        state = {'query': 'local-host 응답이 느려요', 'collected': facts(symptoms=True),
                 'response_context': {'request_kind': 'diagnostic', 'reported_symptoms': True}, 'events': []}
        deps = SupervisorDeps(None, Model(), None, None, 'v2')
        state.update(await write_node(state, deps))
        self.assertFalse(state['deterministic_report'])
        self.assertTrue((await validate_node(state, deps))['validation_passed'])
        self.assertEqual(calls, ['write', 'judge'])
