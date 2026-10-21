#!/usr/bin/env bash
# 기존 백엔드에 연결할 화면 서버만 실행. 다른 프로세스는 종료하지 않는다.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/run_service.py --frontend-only "$@"
