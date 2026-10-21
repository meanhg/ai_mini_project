"""외부 LLM 없이 실제 LangGraph와 검증·저장 동작을 검증한다."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph, replan_check_node, research_node
from agent.session_store import SqliteSessionStore
from agent.validation import validate_draft, verified_fallback


def facts():
    return [
        {"source": "monitoring", "data": {
            "system_name": "auth-service", "status": "degraded", "cpu_percent": 78.2,
            "memory_percent": 61.5, "alerts": ["p95 latency 2.4s"]}},
        {"source": "monitoring", "data": {
            "system_name": "web-01", "status": "healthy", "cpu_percent": 34.1,
            "memory_percent": 48.0, "alerts": []}},
        {"source": "runbook", "data": "# 대응 매뉴얼\n1. CPU가 90%를 넘으면 부하를 확인한다."},
    ]


class ValidationTests(unittest.TestCase):
    def test_correct_values(self):
        self.assertEqual(validate_draft(
            "1) auth-service CPU 78.2%, 메모리 61.5%입니다.\n"
            "2) web-01 CPU 34.1%입니다. 담당자에게 증상을 공유하세요.", facts()), [])

    def test_unsupported_cpu_rejected(self):
        for value in [999, 18.5, -3]:
            with self.subTest(value=value):
                self.assertTrue(validate_draft(
                    f"1) auth-service CPU {value}%입니다. 원인과 최근 변경 사항을 추가 확인하세요.", facts()))

    def test_values_cannot_be_swapped_between_systems(self):
        self.assertTrue(validate_draft(
            "1) auth-service CPU 34.1%입니다. 다른 시스템의 수치를 인용한 잘못된 답변입니다.", facts()))

    def test_memory_and_unknown_system(self):
        for text in ["auth-service 메모리 사용률은 10%", "billing-service CPU 78.2%"]:
            self.assertTrue(validate_draft(f"1) {text}입니다. 관련 알림과 최근 배포를 확인하세요.", facts()))

    def test_runbook_threshold_is_not_current_measurement(self):
        self.assertEqual(validate_draft(
            "1) auth-service CPU 78.2%입니다.\n2) CPU가 90%를 넘으면 부하를 확인하세요.", facts()), [])

    def test_missing_runbook_and_monitoring(self):
        self.assertTrue(validate_draft("1) 장애 대응 절차를 안내합니다. 관련 모니터링과 원인을 확인하세요.", []))

    def test_fallback_does_not_include_bad_draft(self):
        result = verified_fallback(facts())
        self.assertIn("78.2%", result)
        self.assertNotIn("999%", result)
        self.assertIn("확정할 수 없습니다", result)


class StubMCP:
    async def find_prompt(self, name):
        return "ops"

    async def get_prompt(self, *args, **kwargs):
        return "모니터링 조회 후 대응 매뉴얼을 확인하세요."

    async def find_resource_template(self, name):
        return "ops", "runbook://incident-response/{incident_type}"

    async def call_tool(self, name, arguments):
        return dict(facts()[0]["data"])

    async def read_resource(self, *args, **kwargs):
        return facts()[-1]["data"]


class BrokenWriter:
    async def write(self, query, collected, attempt, feedback, on_token=None):
        return ("1) auth-service CPU 999%이므로 근거가 없는 원인을 확정하고 재시작하세요.",
                {"usd": 0.0})


class GraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.environment = patch.dict(os.environ, {'RAG_ENABLED': 'false'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def graph(self, writer):
        deps = SupervisorDeps(FastLLM("test", 0), writer, StubMCP(), None, "v2")
        return build_graph(deps)

    def state(self):
        return {"query": "auth-service 장애 원인 진단", "history": [], "events": [], "cost_usd": 0.0}

    async def test_fake_writer_rewrites_then_passes(self):
        result = await self.graph(QualityLLM("test", 0)).ainvoke(self.state())
        self.assertTrue(result["validation_passed"])
        self.assertEqual(result["validation_attempts"], 2)
        self.assertNotIn("재시작하면 됩니다.", result["final"])

    async def test_repeated_bad_draft_stays_rejected(self):
        result = await self.graph(BrokenWriter()).ainvoke(self.state())
        self.assertFalse(result["validation_passed"])
        self.assertEqual(result["validation_attempts"], 3)
        self.assertNotIn("999%", result["final"])
        self.assertIn("78.2%", result["final"])

    async def test_invalid_drafts_never_stream_to_user(self):
        tokens = []
        final = ""
        async for mode, data in self.graph(BrokenWriter()).astream(
                self.state(), stream_mode=["updates", "custom"]):
            if mode == "custom":
                tokens.append(data)
            else:
                for delta in data.values():
                    final = delta.get("final", final)
        self.assertEqual(tokens, [])
        self.assertNotIn("999%", final)

    async def test_missing_runbook_stays_insufficient_at_limit(self):
        result = await replan_check_node({"collected": [
            {"source": "runbook", "data": "# Runbook Not Found"}],
            "events": [], "tasks": [], "replan_rounds": 1}, None)
        self.assertFalse(result["events"][-1]["is_sufficient"])
        self.assertTrue(result["events"][-1]["limit_reached"])

    async def test_unregistered_system_requests_clarification(self):
        state = self.state()
        state['query'] = 'billing-service 장애 원인 진단'
        result = await self.graph(QualityLLM('test', 0)).ainvoke(state)
        self.assertIn('등록되지 않은 시스템', result['final'])
        self.assertFalse(any(e['type'] == 'PlanCreated' for e in result['events']))

    async def test_partial_tool_failure_keeps_other_result(self):
        class PartialMCP(StubMCP):
            async def call_tool(self, name, arguments):
                if arguments['system_name'] == 'auth-service':
                    raise RuntimeError('test failure')
                return dict(facts()[1]['data'])
        deps = SupervisorDeps(None, None, PartialMCP(), None, 'v2')
        state = {'events': [], 'tasks': [
            {'id': f'task-{i}', 'agent': 'research', 'kind': 'monitoring',
             'target': target, 'status': 'pending'} for i, target in enumerate(['auth-service', 'web-01'])]}
        result = await research_node(state, deps)
        self.assertEqual(result['collected'][0]['data']['system_name'], 'web-01')
        self.assertEqual([t['status'] for t in result['tasks']], ['failed', 'done'])


class SessionAndRouteTests(unittest.TestCase):
    def test_session_survives_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.db"
            store = SqliteSessionStore(path)
            store.append("test", "user", "제 이름은 민수입니다")
            store.close()
            reopened = SqliteSessionStore(path)
            try:
                self.assertEqual(reopened.load("test")[0]["content"], "제 이름은 민수입니다")
            finally:
                reopened.close()

    def test_fake_route_recognizes_existing_regression_queries(self):
        llm = FastLLM("test", 0)
        for query in ["cache-service 메모리 사용률이 계속 올라가요", "log-service 디스크 부족",
                      "payments-service 결제 응답이 느려요", "gateway-service 타임아웃",
                      "payments-service랑 cache-service 둘 다 저하됐는데 공통 원인을 분석해줘"]:
            self.assertEqual(llm.route(query)[0]["route"], "incident")
        self.assertEqual(llm.route("안녕하세요")[0]["route"], "chat")

    def test_scripts_preserve_session_database(self):
        root = Path(__file__).resolve().parents[1]
        for name in ["run_agent_ollma.sh", "run_agent_openai.sh", "run_demo.sh"]:
            self.assertNotIn("rm -f session.db", (root / "sh" / name).read_text())


if __name__ == "__main__":
    unittest.main()
