"""조립 오류의 원인이 형식 오류로 덮이지 않고 재작성으로 전달되는지 검사한다."""
import json
import unittest

from agent.evidence import render_report
from agent.llm_common import QualityModelMixin
from agent.validation import validate_draft


FACTS = [
    {'source': 'monitoring', 'data': {'system_name': 'local-host', 'status': 'healthy',
     'cpu_percent': 0.0, 'memory_percent': 75.2, 'data_origin': 'local'}},
    {'source': 'runbook', 'system_name': 'local-host', 'citation_id': 'RB:memory-leak:1',
     'data': '# 메모리 점검\n1. 메모리 사용률 추세를 확인한다.'},
]


class ReportFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_measured_value_repetition_gets_actionable_feedback_and_recovers(self):
        prompts = []

        class Model(QualityModelMixin):
            async def request(self, system, user, **kwargs):
                prompts.append(user)
                report = {'diagnosis': ('메모리 사용률이 75.2%입니다.' if len(prompts) == 1
                                       else '관측된 메모리는 경고 기준 미만입니다.'),
                          'action_steps': [], 'check_steps': []}
                return json.dumps(report, ensure_ascii=False), {'tokens': 1}

        model = Model()
        draft, _ = await model.write('local-host 메모리 상태', FACTS, 1, None)
        errors = validate_draft(draft, FACTS)
        self.assertEqual(errors, ['원인 후보에 정량 수치를 반복하지 마세요. 측정값은 관측 정보에만 표시합니다.'])
        repaired, _ = await model.write('local-host 메모리 상태', FACTS, 2, '\n'.join(errors))
        self.assertIn(errors[0], prompts[-1])
        self.assertEqual(validate_draft(repaired, FACTS), [])
        self.assertIn('75.2%', repaired)  # 실제 수치는 코드가 측정값 표에 그대로 보존한다.

    def test_unknown_step_error_is_preserved(self):
        report = {'diagnosis': '관측 기준으로 추가 확인이 필요합니다.',
                  'action_steps': ['unregistered-step'], 'check_steps': []}
        errors = validate_draft(render_report(report, FACTS), FACTS)
        self.assertEqual(len(errors), 1)
        self.assertIn('제공된 단계 ID만 선택하세요', errors[0])

    def test_numeric_hallucination_in_real_report_is_still_rejected(self):
        draft = '1) 측정값\nlocal-host 메모리 99%\n2) 출처\n[RB:memory-leak:1]'
        self.assertTrue(any('측정값과 일치하지 않습니다' in e
                            for e in validate_draft(draft, FACTS)))
