"""변경 가능한 운영 정책. JSON 수정은 다음 요청부터 적용한다."""
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator

ROOT = Path(__file__).resolve().parents[1]


class Section(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1)
    content: list[Literal['observations', 'diagnosis', 'actions', 'checks', 'limitations', 'sources']] = Field(min_length=1)


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_research_rounds: int = Field(default=1, ge=1, le=5)
    max_validation_attempts: int = Field(default=3, ge=1, le=5)
    tool_timeout_seconds: float = Field(default=15, gt=0, le=300)
    retrieval_timeout_seconds: float = Field(default=180, gt=0, le=600)
    tool_max_attempts: int = Field(default=3, ge=1, le=5)
    max_search_queries: int = Field(default=4, ge=1, le=12)
    max_search_results: int = Field(default=5, ge=1, le=20)
    max_results_per_query: int = Field(default=3, ge=1, le=10)
    max_selected_steps: int = Field(default=6, ge=1, le=20)
    chunk_chars: int = Field(default=1600, ge=200, le=12000)
    query_cache_size: int = Field(default=64, ge=0, le=1000)
    history_messages: int = Field(default=12, ge=1, le=100)
    history_chars: int = Field(default=12000, ge=1000, le=100000)
    warning_thresholds: dict[str, float] = Field(default_factory=lambda: {
        'cpu_percent': 85, 'memory_percent': 85, 'disk_percent': 85})
    system_thresholds: dict[str, dict[str, float]] = Field(default_factory=dict)
    report_sections: list[Section] = Field(default_factory=lambda: [
        Section(title='진단 요약', content=['diagnosis']),
        Section(title='측정값', content=['observations']),
        Section(title='권장 조치', content=['actions']),
        Section(title='추가 확인·조회 한계', content=['checks', 'limitations'])])

    @model_validator(mode='after')
    def validate_policy(self):
        for thresholds in [self.warning_thresholds, *self.system_thresholds.values()]:
            if any(not 0 <= value <= 100 for value in thresholds.values()):
                raise ValueError('경고 임계값은 0~100이어야 합니다.')
        fields = [part for section in self.report_sections for part in section.content]
        if len(fields) != len(set(fields)) or not {'observations', 'diagnosis', 'actions'} <= set(fields):
            raise ValueError('보고서에는 관측·원인 후보·조치 영역이 중복 없이 필요합니다.')
        return self


def policy():
    path = Path(os.getenv('AGENT_POLICY_PATH', str(ROOT / 'data/agent_policy.json')))
    if not path.exists():
        if os.getenv('AGENT_POLICY_PATH'):
            raise ValueError('AGENT_POLICY_PATH 파일이 없습니다.')
        return Policy()
    return Policy.model_validate(json.loads(path.read_text(encoding='utf-8')))


def runbook_directory():
    return Path(os.getenv('RUNBOOK_DIRECTORY', str(ROOT / 'data/runbooks')))
