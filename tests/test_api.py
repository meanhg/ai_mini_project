"""실제 MCP subprocess를 연결해 HTTP/SSE와 재시작 후 대화를 확인한다."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LLM_BACKEND"] = "fake"

from fastapi.testclient import TestClient
import server
from agent.session_store import SqliteSessionStore


def stream_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


class APITests(unittest.TestCase):
    def test_real_mcp_chat_and_sse(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteSessionStore(Path(directory) / "session.db")
            with patch.dict(os.environ, {"SUPERVISOR_VERSION": "v2", 'RAG_EMBEDDING_BACKEND': 'local', 'RAG_MIN_SCORE': '0',
                    'RAG_ENABLED': 'true', 'RAG_INDEX_PATH': str(Path(directory) / 'index.db')}), \
                 patch.object(server, "SqliteSessionStore", return_value=store), \
                 TestClient(server.app) as client:
                response = client.post("/chat", json={
                    "message": "payments-service CPU 급증 원인 진단", "thread_id": "api-test"})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertTrue(data["metadata"]["validation_passed"])
                self.assertEqual(data["metadata"]["validation_attempts"], 2)
                self.assertIn("payments-service", data["reply"])
                self.assertNotIn("재시작하면 됩니다.", data["reply"])
                response = client.get("/chat/stream", params={
                    "message": "cache-service 메모리 장애 진단", "thread_id": "api-sse"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("data: [DONE]", response.text)
                events = stream_events(response)
                self.assertFalse(any(e["type"] == "AnswerToken" for e in events))
                self.assertTrue(any(e["type"] == "Validation" and e["accepted"] for e in events))
                self.assertEqual(events[-1]["type"], "FinalAnswer")
                self.assertEqual(len(store.load("api-sse")), 2)

    def test_v1_sse_history_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.db"
            for phase in ["write", "read"]:
                store = SqliteSessionStore(path)
                with patch.dict(os.environ, {"SUPERVISOR_VERSION": "v1"}), \
                     patch.object(server, "SqliteSessionStore", return_value=store), \
                     TestClient(server.app) as client:
                    message = "제 이름은 민수입니다" if phase == "write" else "제 이름이 뭐였죠?"
                    response = client.get("/chat/stream", params={"message": message, "thread_id": "remember"})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("민수", stream_events(response)[-1]["reply"])
                    self.assertEqual(len(store.load("remember")), 2 if phase == "write" else 4)
