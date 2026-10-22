"""실제 응답 점검에서 발견한 대상 범위·의미 검수 회귀 사례."""
import json
import unittest
from agent.llm_common import FastModelMixin, QualityModelMixin
from agent.target_scope import restrict_scope

REGISTERED = {'local-host', 'payments-service', 'cache-service'}


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_select_every_system_when_target_is_missing(self):
        class Model(FastModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps({'route': 'incident', 'needs_analysis': False,
                    'targets': sorted(REGISTERED), 'target_resolution': 'resolved',
                    'selects_all_previous': True, 'unknown_targets': [], 'uses_previous': False,
                    'resolved_request': '전체 서버 메모리 상태', 'clarification': ''}), {}
        result, _ = await Model().interpret('메모리 상태를 확인해줘', [], {})
        self.assertEqual(result['targets'], [])
        self.assertTrue(result['clarification'])

    async def test_single_reference_cannot_become_all_previous_even_if_model_says_all(self):
        class Model(FastModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps({'route': 'incident', 'needs_analysis': False,
                    'targets': ['payments-service', 'cache-service'], 'target_resolution': 'resolved',
                    'selects_all_previous': True, 'unknown_targets': [], 'uses_previous': True,
                    'resolved_request': '두 시스템 재확인', 'clarification': ''}), {}
        result, _ = await Model().interpret('그 서버 다시 확인해줘', [],
                    {'targets': ['payments-service', 'cache-service']})
        self.assertEqual(result['targets'], [])
        self.assertIn('어떤 시스템', result['clarification'])

    def test_whole_previous_scope_requires_user_expression(self):
        targets = ['payments-service', 'cache-service']
        selected, question = restrict_scope('아까 본 대상 전부 다시 확인', {'targets': targets}, targets, [], REGISTERED)
        self.assertEqual(selected, targets)
        self.assertEqual(question, '')

    def test_explicit_target_excludes_extra_model_targets(self):
        selected, _ = restrict_scope('payments-service 상태', {}, ['payments-service', 'cache-service'], ['payments-service'], REGISTERED)
        self.assertEqual(selected, ['payments-service'])

    def test_local_alias_and_single_previous_reference_remain_supported(self):
        for query, context in [('내 PC 메모리 상태', {}), ('그 서버 다시 확인', {'targets': ['local-host']})]:
            selected, question = restrict_scope(query, context, ['local-host'], [], REGISTERED)
            self.assertEqual(selected, ['local-host'])
            self.assertEqual(question, '')

    def test_global_scope_can_be_explicitly_requested_in_new_conversation(self):
        selected, question = restrict_scope('등록된 모든 시스템 확인', {}, sorted(REGISTERED), [], REGISTERED)
        self.assertEqual(set(selected), REGISTERED)
        self.assertEqual(question, '')


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def review(self, verdict, draft):
        class Model(QualityModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps(verdict, ensure_ascii=False), {'tokens': 1}
        return (await Model().judge('payments-service CPU 장애', [], draft))[0]

    async def test_invented_rejection_quote_is_not_accepted_as_a_real_issue(self):
        verdict = await self.review({'supported': False, 'issues': [
            {'quote': '서비스 전체가 정상입니다.', 'reason': '전체 정상 근거가 없습니다.'}]},
            '현재 CPU 사용률이 높습니다. 원인은 미확인입니다.')
        self.assertFalse(verdict['supported'])
        self.assertIn('초안에서 확인할 수 없습니다', verdict['issues'][0])

    async def test_real_unsupported_claim_keeps_exact_sentence_and_reason(self):
        verdict = await self.review({'supported': False, 'issues': [
            {'quote': '최근 배포가 원인입니다.', 'reason': '배포 정보를 조회하지 않았습니다.'}]},
            '1) 진단\n최근 배포가 원인입니다.')
        self.assertFalse(verdict['supported'])
        self.assertIn('최근 배포가 원인입니다.', verdict['issues'][0])
        self.assertIn('배포 정보를 조회하지 않았습니다', verdict['issues'][0])

    async def test_supported_report_can_pass_without_forced_cause_or_action(self):
        verdict = await self.review({'supported': True, 'issues': []},
            'CPU 사용률이 높지만 원인은 미확인입니다. 조건 확인 후 조치를 검토합니다.')
        self.assertTrue(verdict['supported'])


class CorrectedSystemNameTests(unittest.IsolatedAsyncioTestCase):
    async def interpret(self, query):
        class Model(FastModelMixin):
            async def request(self, *args, **kwargs):
                return json.dumps({'route': 'incident', 'needs_analysis': False,
                    'targets': ['cache-service'], 'target_resolution': 'missing',
                    'selects_all_previous': False, 'unknown_targets': ['payment-service'],
                    'uses_previous': True, 'resolved_request': 'payment-service와 cache-service 확인',
                    'clarification': 'payment-service은 미등록입니다.'}), {}
        return (await Model().interpret(query, [],
            {'pending_query': 'payment-service와 cache-service 확인해줘'}))[0]

    async def test_current_exact_names_override_previous_typo(self):
        result = await self.interpret('payments-service 와 cache-service 확인해줘')
        self.assertEqual(set(result['targets']), {'payments-service', 'cache-service'})
        self.assertEqual(result['unknown_targets'], [])
        self.assertEqual(result['clarification'], '')
        self.assertNotIn('payment-service', result['resolved_request'])

    async def test_real_user_typo_is_not_silently_redirected(self):
        result = await self.interpret('payment-service와 cache-service 확인해줘')
        self.assertIn('payment-service', result['unknown_targets'])
        self.assertNotIn('payments-service', result['targets'])
        self.assertTrue(result['clarification'])

    def test_unknown_id_before_korean_particle_is_detected(self):
        from agent.llm_common import unknown_systems
        self.assertEqual(unknown_systems('payment-service와 cache-service 확인해줘'), ['payment-service'])
