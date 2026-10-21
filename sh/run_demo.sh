#!/usr/bin/env bash
# 실제 MCP·그래프를 사용하며 외부 모델은 호출하지 않는 데모.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    echo "먼저 bash sh/setup_venv.sh 로 실행 환경을 준비해주세요."
    exit 1
fi
export LLM_BACKEND=fake SUPERVISOR_VERSION=v2
export RAG_EMBEDDING_BACKEND=local RAG_MIN_SCORE=0
export LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false
exec .venv/bin/python scripts/run_service.py --backend fake "$@"
