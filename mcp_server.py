"""all_total 통합 실습 — MCP Server (Wave 2 축).

Tool / Resource / Prompt 3대 capability를 모두 노출하는 실제 fastmcp 서버.
FastAPI 백엔드(server.py)가 lifespan에서 이 파일을 subprocess(stdio)로 1회 기동해
프로세스가 살아 있는 동안 재사용한다 (Wave 1 싱글턴 + Wave 2 표준화의 결합).

- Tool    get_monitoring_status : 호출마다 값이 달라지는 동적 조회 → Tool이 맞다
- Resource runbook://incident-response/{incident_type} : URI로 식별되는 정적 문서
- Prompt  incident_analysis_prompt : 여러 Tool을 조합하는 표준 진단 절차를 강제
"""
import json

from fastmcp import FastMCP
from agent.rag import RunbookIndex
from agent.settings import runbook_directory

mcp = FastMCP(name="capstone-all-total-ops")

_rag_index = None


@mcp.tool()
async def search_runbooks(query: str, top_k: int = 1, queries: list[str] | None = None) -> dict:
    """장애 증상·알림과 의미가 가까운 런북을 벡터 검색합니다. 본문·출처·점수를 반환합니다."""
    global _rag_index
    if _rag_index is None:
        _rag_index = RunbookIndex()
    return await _rag_index.search(query, top_k, queries=queries)


@mcp.tool()
async def get_monitoring_status(system_name: str) -> dict:
    """등록된 시스템의 상태와 출처(sample/local/prometheus), 최근 측정값을 반환합니다."""
    from agent.monitoring import get_status
    return await get_status(system_name)


@mcp.resource("runbook://incident-response/{incident_type}")
def get_incident_runbook(incident_type: str) -> str:
    """특정 incident 유형의 runbook(정적 문서)을 반환합니다."""
    runbook_path = next((p for p in runbook_directory().iterdir()
        if p.is_file() and p.suffix.lower() in ('.md', '.txt') and p.stem == incident_type), None)
    if runbook_path is None:
        return f"# Runbook Not Found\n\nNo runbook for incident type: {incident_type}"
    return runbook_path.read_text()


@mcp.prompt()
def incident_analysis_prompt(system_name: str, symptom: str) -> str:
    """장애 진단 표준 절차 프롬프트 — planning 노드가 계획을 세울 때 이 절차를 따른다."""
    titles = list(dict.fromkeys(c['title'] for c in RunbookIndex().chunks()))
    return f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 현재 조회 가능한 매뉴얼 제목 (폴더에서 동적으로 읽음)
{json.dumps(titles, ensure_ascii=False)}

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""


if __name__ == "__main__":
    try:
        mcp.run(show_banner=False)
    except TypeError:   # 구버전 fastmcp 호환
        mcp.run()
